"""Provider-neutral PDF inspection for non-inline AI transfers.

The verified source is inspected exactly once, in the generic transfer
pipeline, before any provider upload, cloud staging, signed-URL minting or
dispatch.  Provider/model contracts may declare a page ceiling; adapters only
receive an already-authorised, already-inspected source and never parse PDFs.

``pypdf`` is used instead of a partial in-house parser so ordinary PDF 1.5+
features are supported, including incremental updates, cross-reference streams
and compressed object streams.  The reader operates on the existing bounded
temporary-file handle (passing a path to ``PdfReader`` would copy the whole
file into a ``BytesIO``).  Page-tree traversal has explicit depth/node/page
bounds and never extracts page content.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pypdf import PdfReader
from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject

from app.ai.errors import AIInputValidationError

_MAX_PAGE_TREE_DEPTH = 64
_MAX_PAGE_TREE_NODES = 10_000


def _inspection_error() -> AIInputValidationError:
    """Return the stable, content-free error exposed for unreadable PDFs."""
    return AIInputValidationError("the PDF could not be inspected safely")


def _resolved_dictionary(value: Any) -> DictionaryObject:
    try:
        resolved = value.get_object() if hasattr(value, "get_object") else value
    except Exception as exc:
        raise _inspection_error() from exc
    if not isinstance(resolved, DictionaryObject):
        raise _inspection_error()
    return resolved


def _reference_identity(value: Any) -> tuple[int, int] | None:
    if isinstance(value, IndirectObject):
        return (value.idnum, value.generation)
    reference = getattr(value, "indirect_reference", None)
    if isinstance(reference, IndirectObject):
        return (reference.idnum, reference.generation)
    return None


def _count_page_tree(reader: PdfReader, *, cap: int) -> int:
    """Walk the effective page tree and stop after proving ``cap + 1`` pages."""
    try:
        pages_root = reader.root_object.get("/Pages")
    except Exception as exc:
        raise _inspection_error() from exc
    if pages_root is None:
        raise _inspection_error()

    # Each stack item carries the node and its depth.  The visited reference
    # set prevents cycles; the node budget bounds hostile trees even when they
    # contain many intermediate /Pages nodes but few leaves.
    stack: list[tuple[Any, int]] = [(pages_root, 1)]
    visited: set[tuple[int, int]] = set()
    resolved_nodes = 0
    page_count = 0

    while stack:
        node, depth = stack.pop()
        if depth > _MAX_PAGE_TREE_DEPTH:
            raise _inspection_error()
        identity = _reference_identity(node)
        if identity is not None:
            if identity in visited:
                raise _inspection_error()
            visited.add(identity)
        resolved_nodes += 1
        if resolved_nodes > _MAX_PAGE_TREE_NODES:
            raise _inspection_error()

        resolved = _resolved_dictionary(node)
        node_type = str(resolved.get("/Type") or "")
        if node_type == "/Page":
            page_count += 1
            if page_count > cap:
                return cap + 1
            continue
        if node_type != "/Pages":
            raise _inspection_error()

        try:
            kids_raw = resolved.get("/Kids")
            if kids_raw is None:
                raise _inspection_error()
            kids = kids_raw.get_object() if hasattr(kids_raw, "get_object") else kids_raw
        except Exception as exc:
            raise _inspection_error() from exc
        if not isinstance(kids, ArrayObject) or not kids:
            raise _inspection_error()
        stack.extend((kid, depth + 1) for kid in reversed(kids))

    if page_count < 1:
        raise _inspection_error()
    return page_count


def count_pdf_pages(source_path: Path, *, cap: int = _MAX_PAGE_TREE_NODES) -> int:
    """Count pages from a verified file, returning ``cap + 1`` once exceeded.

    The source has already passed the generic byte/MIME/digest checks.  This
    function adds structural PDF validation and an actual page-tree count; it
    does not extract text, images, annotations or form data.
    """
    if cap < 1:
        raise AIInputValidationError("the PDF page ceiling must be positive")
    try:
        with source_path.open("rb") as source:
            reader = PdfReader(source, strict=False, root_object_recovery_limit=1_000)
            if reader.is_encrypted:
                raise _inspection_error()
            return _count_page_tree(reader, cap=cap)
    except AIInputValidationError:
        raise
    except Exception as exc:
        raise _inspection_error() from exc


def validate_pdf_page_limit(source_path: Path, *, max_pages: int) -> int:
    """Inspect once and reject a PDF above the effective provider/model limit."""
    page_count = count_pdf_pages(source_path, cap=max_pages)
    if page_count > max_pages:
        raise AIInputValidationError(
            f"the PDF exceeds the reviewed {max_pages}-page ceiling for this model"
        )
    return page_count


__all__ = ["count_pdf_pages", "validate_pdf_page_limit"]
