"""Contract tests for the off-site backup job script (ADR-0023).

The job is opt-in by configuration: with no destination it must report itself
disabled (never fail), and with a partial or unsafe configuration it must
refuse to run. These run the checked-in shell script directly with a minimal
environment; the end-to-end dump/upload/restore path needs PostgreSQL and two
S3 services and is recorded in docs/backup-and-recovery.md (tested run D).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "deploy" / "backup" / "offsite-backup.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is required to run the job script")

SECRET = "s3cr3t-value-never-logged"
DESTINATION = {
    "BACKUP_S3_ENDPOINT": "https://backup.example.com",
    "BACKUP_S3_BUCKET": "client-backups",
    "BACKUP_S3_ACCESS_KEY_ID": "backup-key",
    "BACKUP_S3_SECRET_ACCESS_KEY": SECRET,
}
SOURCES = {
    "BACKUP_ENCRYPTION_PASSWORD": SECRET,
    "DATABASE_OPERATOR_URL": f"postgresql+asyncpg://app_operator:{SECRET}@db.example.com:25060/app",
    "STORAGE_ENDPOINT_URL": "https://fra1.digitaloceanspaces.com",
    "STORAGE_BUCKET": "app-files",
    "STORAGE_ACCESS_KEY_ID": "storage-key",
    "STORAGE_SECRET_ACCESS_KEY": SECRET,
}


def _run(arguments: list[str], environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(
        [BASH, str(SCRIPT), *arguments],
        env={"PATH": "/usr/bin:/bin", **environment},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _source(snippet: str, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(
        [BASH, "-c", f'source "{SCRIPT}"; {snippet}'],
        env={"PATH": "/usr/bin:/bin", **environment},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_no_destination_means_disabled_not_failing() -> None:
    result = _run(["check-config"], {})
    assert result.returncode == 0
    assert result.stdout.strip() == "disabled"


def test_disabled_job_is_healthy() -> None:
    assert _run(["health"], {}).returncode == 0


def test_full_configuration_is_enabled() -> None:
    result = _run(["check-config"], DESTINATION | SOURCES)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "enabled"


@pytest.mark.parametrize("missing", sorted(DESTINATION))
def test_partial_destination_is_refused(missing: str) -> None:
    environment = {key: value for key, value in DESTINATION.items() if key != missing} | SOURCES
    result = _run(["check-config"], environment)
    assert result.returncode == 1
    assert "partial_destination" in result.stderr
    assert missing in result.stderr
    assert SECRET not in result.stderr + result.stdout


@pytest.mark.parametrize("missing", ["BACKUP_ENCRYPTION_PASSWORD", "DATABASE_OPERATOR_URL"])
def test_encryption_and_database_credential_are_required(missing: str) -> None:
    environment = DESTINATION | {key: value for key, value in SOURCES.items() if key != missing}
    result = _run(["check-config"], environment)
    assert result.returncode == 1
    assert missing in result.stderr


def test_storage_source_is_required_only_when_files_are_included() -> None:
    database_only = {
        "BACKUP_ENCRYPTION_PASSWORD": SECRET,
        "DATABASE_OPERATOR_URL": SOURCES["DATABASE_OPERATOR_URL"],
    }
    refused = _run(["check-config"], DESTINATION | database_only)
    assert refused.returncode == 1
    assert "STORAGE_BUCKET" in refused.stderr

    allowed = _run(
        ["check-config"], DESTINATION | database_only | {"BACKUP_INCLUDE_FILES": "false"}
    )
    assert allowed.returncode == 0, allowed.stderr
    assert allowed.stdout.strip() == "enabled"


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("BACKUP_INCLUDE_FILES", "yes"),
        ("BACKUP_HOUR_UTC", "24"),
        ("BACKUP_HOUR_UTC", "2am"),
    ],
)
def test_invalid_settings_are_refused(variable: str, value: str) -> None:
    result = _run(["check-config"], DESTINATION | SOURCES | {variable: value})
    assert result.returncode == 1
    assert variable in result.stderr


def test_enabled_job_without_a_run_is_unhealthy() -> None:
    # No scheduler has started in this environment, so there is neither a
    # successful run nor a start marker: an enabled job must not report healthy.
    result = _run(["health"], DESTINATION | SOURCES | {"BACKUP_STATE_DIR": "/nonexistent"})
    assert result.returncode == 1


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "postgresql+asyncpg://app_operator:p%40ss%2Fw%3Ard@db.example.com:25060/app_db",
            "app_operator|p@ss/w:rd|db.example.com|25060|app_db|",
        ),
        (
            "postgresql://app_operator:pw@db.example.com/app_db?ssl=require",
            "app_operator|pw|db.example.com|5432|app_db|require",
        ),
        (
            "postgresql+asyncpg://app_operator:pw@db.example.com:5432/app_db?sslmode=verify-full",
            "app_operator|pw|db.example.com|5432|app_db|verify-full",
        ),
    ],
)
def test_operator_url_becomes_libpq_environment(url: str, expected: str) -> None:
    result = _source(
        'export_pg_environment "$URL" && '
        'printf "%s|%s|%s|%s|%s|%s" "$PGUSER" "$PGPASSWORD" "$PGHOST" "$PGPORT" '
        '"$PGDATABASE" "${PGSSLMODE:-}"',
        {"URL": url},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected


@pytest.mark.parametrize(
    "url",
    [
        f"mysql://app:{SECRET}@db.example.com/app",
        f"postgresql://app:{SECRET}@db.example.com/app?target_session_attrs=any",
        "not a url",
    ],
)
def test_unsupported_operator_url_is_refused_without_echoing_it(url: str) -> None:
    result = _source('export_pg_environment "$URL"', {"URL": url})
    assert result.returncode == 1
    assert "DATABASE_OPERATOR_URL" in result.stderr
    assert SECRET not in result.stderr + result.stdout


@pytest.mark.parametrize("hour", ["0", "2", "23"])
def test_next_run_is_within_a_day(hour: str) -> None:
    result = _source("seconds_until_next_run", {"BACKUP_HOUR_UTC": hour})
    assert result.returncode == 0, result.stderr
    assert 0 < int(result.stdout) <= 86400
