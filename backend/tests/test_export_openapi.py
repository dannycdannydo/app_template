"""Tests for the OpenAPI export used by the generated client pipeline (blueprint §15)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.export_openapi import export_openapi, main


def test_openapi_spec_exposes_health_and_ready() -> None:
    spec = export_openapi()

    assert spec["openapi"].startswith("3.")
    assert "/health" in spec["paths"]
    assert "/ready" in spec["paths"]
    assert "HealthResponse" in spec["components"]["schemas"]


def test_export_script_writes_spec_to_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "nested" / "openapi.json"

    monkeypatch.setattr("sys.argv", ["export_openapi", "--output", str(output)])
    main()

    assert output.is_file()
    spec = json.loads(output.read_text(encoding="utf-8"))
    assert spec["info"]["title"]
    assert spec["paths"]["/health"]["get"]["operationId"] == "health_health_get"
