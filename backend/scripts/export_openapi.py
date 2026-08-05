"""Export the FastAPI OpenAPI schema for the generated client pipeline (blueprint §15).

FastAPI is the single source of truth for the API surface. This script dumps
``app.openapi()`` to a JSON file that ``openapi-typescript`` consumes to
regenerate ``frontend/src/api/generated/openapi.d.ts``. The exported JSON is a
transient build artifact (gitignored); the committed artifact is the generated
TypeScript client.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any


def export_openapi() -> dict[str, Any]:
    """Return the application's OpenAPI schema as a plain dict."""
    # Building the schema never touches the database, so the export must work
    # on a fresh clone with no .env file. Provide safe defaults before the
    # application module is imported; a developer's own environment still
    # takes precedence because these only set missing values. The import is
    # deferred so the defaults are applied first.
    os.environ.setdefault("APP_ENV", "development")
    os.environ.setdefault(
        "DATABASE_URL", "postgresql+asyncpg://app:app@localhost:5432/app_template"
    )

    from app.main import app

    return app.openapi()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export the FastAPI OpenAPI schema to a JSON file."
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Destination path for the exported openapi.json.",
    )
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(export_openapi(), indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
