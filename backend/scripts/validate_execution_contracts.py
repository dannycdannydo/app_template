"""Validate checkpointed standalone plans used by prompts 01-03.

Only exact ``Status: Draft|Active|Complete`` lines opt a Markdown file into
this contract. Historical/design-source plans with descriptive status text are
therefore unaffected. At most one plan may be Active.
"""

from __future__ import annotations

import re
from pathlib import Path

STATUS_RE = re.compile(r"^Status: (Draft|Active|Complete)$", re.MULTILINE)
CHECKPOINT_RE = re.compile(r"^### (P\d+)\s+[—-]\s+.+$", re.MULTILINE)
UNCHECKED_RE = re.compile(r"^- \[ \] ", re.MULTILINE)
CHECKED_RE = re.compile(r"^- \[x\] ", re.MULTILINE | re.IGNORECASE)
REQUIRED_HEADINGS = (
    "## Goal",
    "## Agreed scope",
    "## Out of scope",
    "## Decisions and assumptions",
    "## Commands that must work",
    "## Acceptance criteria",
    "## Implementation checkpoints",
    "## Reference map",
    "## API, data and security impact",
    "## Validation plan",
    "## Review and delivery",
)


def validate_plan(path: Path, text: str) -> list[str]:
    """Return actionable structural errors for one opted-in plan."""

    errors: list[str] = []
    match = STATUS_RE.search(text)
    if match is None:
        return errors
    status = match.group(1)

    for heading in REQUIRED_HEADINGS:
        if heading not in text:
            errors.append(f"{path}: missing required heading {heading!r}")

    checkpoints = list(CHECKPOINT_RE.finditer(text))
    if not checkpoints:
        errors.append(f"{path}: no '### Pn — name' implementation checkpoint")
    for index, checkpoint in enumerate(checkpoints):
        expected = f"P{index + 1}"
        name = checkpoint.group(1)
        if name != expected:
            errors.append(f"{path}: checkpoints must be consecutive; expected {expected}, found {name}")
        end = checkpoints[index + 1].start() if index + 1 < len(checkpoints) else len(text)
        body = text[checkpoint.end() : end]
        if "Dependencies:" not in body:
            errors.append(f"{path}: {name} is missing an explicit Dependencies line")
        if not (UNCHECKED_RE.search(body) or CHECKED_RE.search(body)):
            errors.append(f"{path}: {name} has no checklist items")
        if "Human review required before application:" not in body:
            errors.append(f"{path}: {name} is missing its human-review gate")
        reference_map = text[text.find("## Reference map") :]
        if not re.search(rf"^\|\s*{re.escape(name)}\s*\|", reference_map, re.MULTILINE):
            errors.append(f"{path}: {name} is missing from the reference map")

    has_unchecked = UNCHECKED_RE.search(text) is not None
    if status == "Active" and not has_unchecked:
        errors.append(f"{path}: Active plan has no unchecked implementation item")
    if status == "Complete" and has_unchecked:
        errors.append(f"{path}: Complete plan still has unchecked implementation items")
    if status == "Active" and re.search(r"\b(TODO|TBD|if needed)\b", text, re.IGNORECASE):
        errors.append(f"{path}: Active plan contains an unresolved TODO/TBD/'if needed'")
    return errors


def validate_plans(plans_dir: Path) -> list[str]:
    """Validate all opted-in plans and the unique-active invariant."""

    errors: list[str] = []
    active: list[Path] = []
    for path in sorted(plans_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        match = STATUS_RE.search(text)
        if match is not None and match.group(1) == "Active":
            active.append(path)
        errors.extend(validate_plan(path, text))
    if len(active) > 1:
        errors.append("multiple Active plans: " + ", ".join(str(path) for path in active))
    return errors


def main() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    errors = validate_plans(repository_root / "plans")
    if errors:
        raise SystemExit("Invalid execution contracts:\n- " + "\n- ".join(errors))
    print("Execution contracts valid")


if __name__ == "__main__":
    main()
