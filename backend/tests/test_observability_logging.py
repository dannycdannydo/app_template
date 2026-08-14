"""Logging-context tests (Scope §6.1, blueprint §28).

Blueprint §28 defines the logging-context field set — ``request_id``,
``user_id``, ``organisation_id``, ``route``, ``job_id``, ``resource_id``,
``event`` — and the never-log list (passwords, tokens, authorisation headers,
signed URLs, full connection strings). The request-id middleware already binds
``request_id`` and logs ``path`` on ``request_finished`` (the ``route`` field);
the authentication dependencies now bind ``user_id``/``organisation_id`` and
the worker tasks bind ``job_id``/``resource_id``. These tests assert the
fields in captured log lines and prove the never-log list is enforced.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Generator
from contextlib import contextmanager

import pytest
import structlog
from httpx import Response
from structlog.typing import EventDict
from tests.auth_helpers import make_token
from tests.context_helpers import (
    build_context_app_fixture,
    context_client,
    make_membership,
    make_user,
)

from app.core.logging import (
    REDACTED,
    bind_identity_context,
    bind_worker_context,
    redact_sensitive_data,
)

logger = structlog.get_logger()


@contextmanager
def _capture_logs() -> Generator[list[EventDict]]:
    """Enter structlog's capture with contextvars merged into every entry."""
    with structlog.testing.capture_logs(
        processors=[structlog.contextvars.merge_contextvars]
    ) as logs:
        yield logs


async def test_authenticated_request_binds_full_logging_context() -> None:
    app, state, private_key = build_context_app_fixture()
    user = make_user()
    state.users[user.workos_user_id] = user
    organisation_id = uuid.uuid4()
    state.lookup_queue = [user, make_membership(user, organisation_id)]

    with _capture_logs() as logs:
        async with context_client(app) as client:
            response: Response = await client.get(
                "/_test/context",
                headers={
                    "Authorization": f"Bearer {make_token(private_key)}",
                    "X-Org-Id": str(organisation_id),
                },
            )

    assert response.status_code == 200
    assert logs
    # BP §28: every log line carries the event name and the request id.
    for entry in logs:
        assert "event" in entry
        assert entry["request_id"]
    # The request_finished line carries the route (path) and the caller's
    # identity bound by the auth dependencies.
    finished = [entry for entry in logs if entry["event"] == "request_finished"]
    assert finished
    assert finished[0]["path"] == "/_test/context"
    assert finished[0]["user_id"] == str(user.id)
    assert finished[0]["organisation_id"] == str(organisation_id)


async def test_public_request_never_binds_identity_context() -> None:
    """Anonymous /health requests log request_id but no user/organisation."""
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    app = create_app()
    with _capture_logs() as logs:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            response: Response = await client.get("/health")
    assert response.status_code == 200
    finished = [entry for entry in logs if entry["event"] == "request_finished"]
    assert finished
    assert "user_id" not in finished[0]
    assert "organisation_id" not in finished[0]


def test_bind_identity_context_binds_user_then_organisation() -> None:
    with _capture_logs() as logs:
        bind_identity_context(user_id="user-1")
        logger.info("authentication_done")
        bind_identity_context(user_id="user-1", organisation_id="org-1")
        logger.info("membership_resolved")

    assert logs[0]["user_id"] == "user-1"
    assert "organisation_id" not in logs[0]
    assert logs[1]["user_id"] == "user-1"
    assert logs[1]["organisation_id"] == "org-1"


def test_bind_worker_context_binds_job_and_resource_ids() -> None:
    with _capture_logs() as logs:
        bind_worker_context(job_id="job-1")
        logger.info("task_started")
        bind_worker_context(job_id="job-1", resource_id="file-42")
        logger.info("resource_loaded")

    assert [entry["event"] for entry in logs] == ["task_started", "resource_loaded"]
    assert logs[0]["job_id"] == "job-1"
    assert "resource_id" not in logs[0]
    assert logs[1]["job_id"] == "job-1"
    assert logs[1]["resource_id"] == "file-42"


async def test_worker_tasks_emit_context_bound_log_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scope §6.1: task log lines carry job_id and resource_id.

    The worker tasks bind ``job_id``/``resource_id`` but originally emitted no
    log line, so there was nothing to observe under ``make dev``. The task
    handlers now log start/result events; this test proves the captured lines
    carry the context fields, keeping the "verify under make dev" item
    automatable in the suite. The handlers run against stubbed sessions so
    the assertions need no database or broker.
    """
    from app.modules.files import tasks as files_tasks
    from app.modules.jobs import service as jobs_service
    from app.modules.jobs import tasks as jobs_tasks
    from app.modules.jobs.models import JobStatus

    JOB_ID = "00000000-0000-7000-8000-000000000001"

    class _FakeSession:
        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    def _fake_factory() -> _FakeSession:
        return _FakeSession()

    # --- files task: a terminal job makes the attempt a logged no-op, so the
    # start line (job_id only) and the skip line (job_id + resource_id) are
    # both emitted without touching a database. ---
    class _TerminalJob:
        input_reference = "file-42"
        status = JobStatus.SUCCEEDED

    async def _get_terminal_job(session: object, *, job_id: object) -> _TerminalJob:
        return _TerminalJob()

    def _is_terminal(status: object) -> bool:
        return True

    monkeypatch.setattr(files_tasks, "async_session_factory", _fake_factory)
    monkeypatch.setattr(jobs_service, "get_job_for_task", _get_terminal_job)
    monkeypatch.setattr(jobs_service, "is_terminal", _is_terminal)

    with _capture_logs() as logs:
        await files_tasks.process_file(JOB_ID)

    started = [entry for entry in logs if entry["event"] == "file.processing.started"]
    skipped = [entry for entry in logs if entry["event"] == "file.processing.skipped"]
    assert started and started[0]["job_id"] == JOB_ID
    assert "resource_id" not in started[0]
    assert skipped and skipped[0]["job_id"] == JOB_ID
    assert skipped[0]["resource_id"] == "file-42"

    # --- jobs task: the retries-exhausted finalizer logs with job_id only. ---
    recorded: list[str] = []

    async def _fail(
        session: object, *, job_id: object, error_code: str, error_message: str
    ) -> None:
        recorded.append(error_code)

    monkeypatch.setattr(jobs_service, "fail", _fail)

    message_dict: dict[str, object] = {"kwargs": {"job_id": JOB_ID}}
    with _capture_logs() as logs:
        await jobs_tasks.mark_job_failed_after_retries(message_dict, {})

    started = [entry for entry in logs if entry["event"] == "job.retries_exhausted.started"]
    recorded_lines = [entry for entry in logs if entry["event"] == "job.retries_exhausted.recorded"]
    assert started and started[0]["job_id"] == JOB_ID
    assert recorded_lines and recorded_lines[0]["job_id"] == JOB_ID
    assert recorded == [jobs_service.ERROR_CODE_RETRIES_EXHAUSTED]

    # A stale message whose durable row is still absent after bounded retries
    # is acknowledged with one structured warning instead of failing the
    # finalizer and creating a second dead letter.
    from app.core.exceptions import NotFoundError

    async def _missing(
        session: object, *, job_id: object, error_code: str, error_message: str
    ) -> None:
        raise NotFoundError(code="job_not_found", message="The job could not be found.")

    monkeypatch.setattr(jobs_service, "fail", _missing)
    with _capture_logs() as logs:
        await jobs_tasks.mark_job_failed_after_retries(message_dict, {})

    skipped = [entry for entry in logs if entry["event"] == "job.retries_exhausted.skipped"]
    assert skipped and skipped[0]["job_id"] == JOB_ID
    assert skipped[0]["reason"] == "job_not_found"


async def test_ai_execute_started_log_binds_deterministic_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.7 Scope §6.7: every AI-job log line binds ``ai_request_id``.

    The deterministic request id (``job_id.hex``) is derived and bound to the
    worker context before ``ai.execute.started`` is emitted, so even the first
    AI-job log line carries the id (BP §28).
    """
    from app.ai import execution as ai_execution
    from app.modules.jobs import service as jobs_service
    from app.modules.jobs.models import JobStatus

    class _FakeSession:
        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    def _fake_factory() -> _FakeSession:
        return _FakeSession()

    class _TerminalJob:
        status = JobStatus.SUCCEEDED

    async def _get_terminal_job(session: object, *, job_id: object) -> _TerminalJob:
        return _TerminalJob()

    def _is_terminal(status: object) -> bool:
        return True

    monkeypatch.setattr(ai_execution, "async_session_factory", _fake_factory)
    monkeypatch.setattr(jobs_service, "get_job_for_task", _get_terminal_job)
    monkeypatch.setattr(jobs_service, "is_terminal", _is_terminal)

    JOB_ID = "00000000-0000-7000-8000-000000000001"
    expected_request_id = ai_execution.request_id_for_job(uuid.UUID(JOB_ID))
    with _capture_logs() as logs:
        await ai_execution.execute_ai_task(JOB_ID)

    started = [entry for entry in logs if entry["event"] == "ai.execute.started"]
    skipped = [entry for entry in logs if entry["event"] == "ai.execute.skipped"]
    assert started and skipped
    assert started[0]["ai_request_id"] == expected_request_id
    assert skipped[0]["ai_request_id"] == expected_request_id
    assert skipped[0]["job_id"] == JOB_ID


async def test_untrusted_request_id_is_replaced_before_logging() -> None:
    """A client cannot inject arbitrary or oversized content into log context."""
    app, state, private_key = build_context_app_fixture()
    user = make_user()
    state.users[user.workos_user_id] = user
    organisation_id = uuid.uuid4()
    state.lookup_queue = [user, make_membership(user, organisation_id)]

    injected_request_id = "Bearer super-secret-bearer-token-7f3a"

    with _capture_logs() as logs:
        async with context_client(app) as client:
            response: Response = await client.get(
                "/_test/context",
                headers={
                    "Authorization": f"Bearer {make_token(private_key)}",
                    "X-Org-Id": str(organisation_id),
                    "X-Request-ID": injected_request_id,
                },
            )

    assert response.status_code == 200
    serialised = json.dumps(logs)
    assert injected_request_id not in serialised
    finished = [entry for entry in logs if entry["event"] == "request_finished"]
    assert finished
    assert finished[0]["request_id"] != injected_request_id
    assert len(str(finished[0]["request_id"])) == 32


def test_never_log_list_is_actually_redacted() -> None:
    """Sensitive keys and values are removed from the serialized event."""
    candidates = {
        "authorization": "Bearer super-secret-bearer-token-7f3a",
        "password": "super-secret-password-9c21",
        "nested": {
            "database_url": "postgresql+asyncpg://app:db-secret@db.example/db",
            "error": "upload failed for https://s3.example/file?X-Amz-Signature=abc123secret",
        },
    }

    redacted = redact_sensitive_data(candidates)
    serialised = json.dumps(redacted)

    assert redacted["authorization"] == REDACTED
    assert redacted["password"] == REDACTED
    assert redacted["nested"]["database_url"] == REDACTED
    for candidate in (
        "super-secret-bearer-token-7f3a",
        "super-secret-password-9c21",
        "db-secret",
        "abc123secret",
    ):
        assert candidate not in serialised


def test_production_json_line_keeps_core_processors(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A real production JSON line still carries the full context fields.

    Regression guard for the ``configure_logging`` refactor: the production
    chain is rebuilt on every configure and must keep the core processors, or
    ``request_id``/``level``/``timestamp`` silently vanish from shipped log
    lines. ``structlog.testing.capture_logs`` masks such a regression because
    it installs its own processor list, so this test asserts against the real
    stdout output via ``PrintLoggerFactory`` instead.
    """
    from app.core.logging import configure_logging

    configure_logging(log_level="INFO", json_logs=True)
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id="req-prod-1")
    structlog.get_logger().info("production_probe")
    out, _ = capsys.readouterr()
    line = json.loads(out.strip().splitlines()[-1])

    assert line["event"] == "production_probe"
    assert line["request_id"] == "req-prod-1"
    assert line["level"] == "info"
    assert "timestamp" in line


def test_production_exception_line_redacts_secret_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The real processor runs after traceback formatting and scrubs its text."""
    from app.core.logging import configure_logging

    configure_logging(log_level="INFO", json_logs=True)
    try:
        raise RuntimeError(
            "password=exception-secret "
            "Bearer exception-token "
            "postgresql://app:database-secret@db.example/app"
        )
    except RuntimeError:
        structlog.get_logger().exception("production_failure_probe")

    out, _ = capsys.readouterr()
    line = out.strip().splitlines()[-1]
    assert "exception-secret" not in line
    assert "exception-token" not in line
    assert "database-secret" not in line
    assert REDACTED in line
