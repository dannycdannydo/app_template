"""Job-foundation tests: model shape and the bounded retry policy (Scope §6.4).

The real-database lifecycle and the real-broker round trip live in
``test_jobs_db.py`` and ``test_jobs_broker.py``. This module proves the two
things that need neither PostgreSQL nor Redis:

- the ``jobs`` table shape (blueprint §18: no ``updated_at``, lifecycle timing
  in ``started_at``/``completed_at``, org FK, range constraints);
- the retry policy contract: permanent errors (``JobPermanentError``, declared
  in ``throws``) are never retried, transient errors are retried up to
  ``MAX_ATTEMPTS``, and the ``on_retry_exhausted`` actor name matches the
  handler the tasks module declares.

The retry mechanics run on an in-process StubBroker with a real Worker, so
the Retries middleware itself is exercised. The test actors override only the
backoff (from 1000 ms to ~10 ms) for speed; ``max_retries``, ``throws`` and
``on_retry_exhausted`` come from the real :func:`retry_policy`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from typing import cast as typing_cast

import dramatiq
import pytest
from dramatiq.brokers.stub import StubBroker
from dramatiq.worker import Worker
from sqlalchemy import Table

from app.db.base import Base
from app.modules.jobs import service as jobs_service
from app.modules.jobs import tasks as jobs_tasks
from app.modules.jobs.models import Job, JobStatus

_QUEUE = "test-jobs"


def _test_policy_options(*, min_backoff: int = 10) -> dict[str, Any]:
    """The real retry policy, minus the exhausted-handler, with a fast backoff.

    ``on_retry_exhausted`` is dropped so these tests never message the real
    (database-bound) handler; the exhaustion path is proven end to end in
    ``test_jobs_broker.py``. The backoff is shrunk so the retries complete in
    milliseconds instead of seconds.
    """
    options = dict(jobs_service.retry_policy())
    options.pop("on_retry_exhausted")
    options["min_backoff"] = min_backoff
    return options


@pytest.fixture
def broker_and_worker() -> Iterator[tuple[StubBroker, Worker]]:
    """A StubBroker + in-process Worker for the retry-mechanics tests."""
    broker = StubBroker()
    dramatiq.set_broker(broker)
    worker = Worker(broker, worker_timeout=100, worker_threads=2)
    worker.start()
    yield broker, worker
    worker.stop()
    broker.flush_all()


# --- Model metadata (BP §7, §10, §18) ---


def test_jobs_table_registered_on_base_metadata() -> None:
    assert "jobs" in Base.metadata.tables


def test_job_has_org_and_creator_foreign_keys_and_composite_index() -> None:
    table = typing_cast(Table, Job.__table__)
    fk_names = {constraint.name for constraint in table.foreign_key_constraints}
    assert fk_names == {
        "fk_jobs_organisation_id_organisations",
        "fk_jobs_created_by_user_id_users",
    }
    index_names = {index.name for index in table.indexes}
    assert index_names == {
        "ix_jobs_organisation_id",
        "ix_jobs_organisation_id_created_at",
        "ix_jobs_created_by_user_id",
        # Durable delivery (plan P1): ownership settlement by dispatch id and
        # queued-job reconciliation by status.
        "ix_jobs_dispatch_id",
        "ix_jobs_status_created_at",
    }
    composite = next(
        index for index in table.indexes if index.name == "ix_jobs_organisation_id_created_at"
    )
    assert [column.name for column in composite.columns] == [
        "organisation_id",
        "created_at",
    ]


def test_job_table_shape_matches_blueprint_18() -> None:
    """BP §18 shape: no updated_at; lifecycle timing in started/completed."""
    table = typing_cast(Table, Job.__table__)
    columns = {column.name for column in table.columns}
    assert columns == {
        "id",
        "organisation_id",
        "job_type",
        "status",
        "progress",
        "input_reference",
        "result_reference",
        "error_code",
        "error_message",
        "attempt_count",
        "created_by_user_id",
        "created_at",
        "started_at",
        "completed_at",
        # Durable delivery (plan P1) adds internal ownership fields; they are
        # not part of the blueprint §18 shape and not exposed by API schemas.
        "dispatch_id",
        "execution_lease_expires_at",
    }
    assert "updated_at" not in columns


def test_job_status_enum_matches_blueprint_18() -> None:
    assert [status.value for status in JobStatus] == [
        "queued",
        "running",
        "succeeded",
        "failed",
        "cancelled",
    ]


# --- Retry policy contract (Scope §6.4) ---


def test_retry_policy_contract() -> None:
    """The policy encodes MAX_ATTEMPTS, permanent-never-retried, exhaustion."""
    policy = jobs_service.retry_policy()
    assert policy["max_retries"] == jobs_service.MAX_ATTEMPTS - 1
    assert jobs_service.JobPermanentError in policy["throws"]
    # The exhausted-handler name in the policy and the declared actor are the
    # same constant, so the middleware and the handler cannot drift apart.
    assert policy["on_retry_exhausted"] == jobs_service.MARK_FAILED_AFTER_RETRIES_ACTOR
    assert jobs_tasks.mark_job_failed_after_retries_actor.actor_name == (
        jobs_service.MARK_FAILED_AFTER_RETRIES_ACTOR
    )
    assert jobs_service.MAX_ATTEMPTS >= 2


def test_permanent_errors_are_never_retried(broker_and_worker: tuple[StubBroker, Worker]) -> None:
    """BP §18: permanent validation errors do not retry (throws wins)."""
    broker, _worker = broker_and_worker
    attempts = {"count": 0}

    @dramatiq.actor(queue_name=_QUEUE, **_test_policy_options())
    def _permanent_failure() -> None:
        attempts["count"] += 1
        raise jobs_service.JobPermanentError("the input can never be processed")

    _permanent_failure.send()
    # join() re-raises the dead-lettered exception (fail_fast), which is what
    # proves the message was not retried and was failed instead.
    with pytest.raises(jobs_service.JobPermanentError):
        broker.join(_QUEUE, timeout=10000)
    assert attempts["count"] == 1
    assert len(broker.dead_letters_by_queue[_QUEUE]) == 1


def test_transient_errors_retry_up_to_max_attempts(
    broker_and_worker: tuple[StubBroker, Worker],
) -> None:
    """BP §18: transient errors retry, bounded by MAX_ATTEMPTS."""
    broker, _worker = broker_and_worker
    attempts = {"count": 0}

    @dramatiq.actor(queue_name=_QUEUE, **_test_policy_options(min_backoff=10))
    def _transient_failure() -> None:
        attempts["count"] += 1
        raise RuntimeError("storage temporarily unreachable")

    _transient_failure.send()
    # The transient failures retry until MAX_ATTEMPTS is exhausted, then the
    # message is dead-lettered and join() re-raises the last exception.
    with pytest.raises(RuntimeError):
        broker.join(_QUEUE, timeout=10000)
    assert attempts["count"] == jobs_service.MAX_ATTEMPTS
    assert len(broker.dead_letters_by_queue[_QUEUE]) == 1


def test_job_id_from_message_extracts_enqueued_id() -> None:
    """The exhausted-handler reads the durable job id from the message kwargs."""
    job_id = uuid.uuid4()
    message_dict = {"kwargs": {"job_id": str(job_id)}, "args": ()}
    assert jobs_tasks.job_id_from_message(message_dict) == job_id
