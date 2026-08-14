"""Shared durable-actor execution wrapper (durable delivery plan P2).

Every durable job task runs its domain work through :func:`run_claimed`, which
owns the delivery-ownership concerns every actor shares:

- **claim**: :func:`run_claimed` atomically claims the next attempt via
  ``jobs_service.claim_dispatch``. A legacy row with no dispatch identity
  receives one on first claim, so old one-argument broker messages keep
  working unchanged.
- **defer**: a duplicate that finds a live lease is deferred, never executed
  concurrently with the owning attempt. Short remaining leases are waited out
  in-process (bounded); longer leases raise :class:`DispatchDeferredError`
  (a transient error the Retries middleware retries), and the retries-
  exhausted finalizer refuses to fail a still-leased dispatch.
- **release**: a transient failure releases the owned attempt back to
  ``queued`` (owner-checked) before the exception propagates, so the retry of
  the same dispatch can re-claim it.
- **stale settlement**: :class:`StaleDispatchError` from an owner-checked
  mutation means the attempt was superseded; the wrapper acknowledges the
  message as a no-op instead of retrying or failing over a newer owner.
- **permanent failure**: :class:`JobPermanentError` passes through untouched
  — the handler has already marked the durable row ``failed`` itself (the
  retry policy declares it in ``throws``, so the message is never retried).

The handler receives the session, the claimed job, the captured dispatch id
and the rotated owner token. It must never call ``jobs_service`` mutation
helpers without the captured ``owner_token``, or the ownership checks in the
service will reject the mutation.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from dramatiq.middleware import CurrentMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory
from app.modules.jobs import service as jobs_service
from app.modules.jobs.models import Job

logger = structlog.get_logger()

# A deferred duplicate waits out a short remaining lease in-process (so a
# takeover or terminal completion resolves the race deterministically) rather
# than bouncing through the broker. A lease longer than this bound, or a
# deferral that exceeds the total budget, raises a transient
# :class:`DispatchDeferredError` for the Retries middleware to retry.
MAX_DEFER_WAIT_SECONDS = 30.0
DEFER_TOTAL_BUDGET_SECONDS = 120.0
# Small margin so the re-claim happens strictly after the lease bound.
_DEFER_MARGIN_SECONDS = 0.1


@dataclass(frozen=True)
class DurableJobContext:
    """Everything the domain handler needs to run one owned attempt."""

    job_id: uuid.UUID
    job: Job
    dispatch_id: uuid.UUID
    owner_token: uuid.UUID


Handler = Callable[[DurableJobContext, AsyncSession], Awaitable[None]]


def _stamp_current_message(dispatch_id: uuid.UUID, owner_token: uuid.UUID) -> None:
    """Stamp the current broker message with the attempt just claimed.

    The stamp travels with the message (in ``options``, which the Retries
    middleware forwards verbatim to the retries-exhausted handler), so the
    exhausted finalizer can tell which attempt the message actually claimed.
    Both the dispatch id and the rotated owner token are stamped: a retry
    re-claim or an expired-lease takeover keeps the dispatch identity while
    rotating the token, so the finalizer must correlate by attempt, not by
    dispatch alone, to refuse settling a newer owner of the same dispatch.
    When the handler runs outside a broker message (direct test calls) there
    is nothing to stamp and the finalizer falls back to the explicit legacy
    behaviour for never-claimed rows.
    """
    message = CurrentMessage.get_current_message()
    if message is not None:
        message.options["dispatch_id"] = str(dispatch_id)
        message.options["owner_token"] = str(owner_token)


def _log_event(outcome: str, *, job_id: uuid.UUID, **fields: object) -> None:
    """Emit one bounded, safe structured log line for an ownership outcome.

    The event name is the fixed ``durable.<outcome>`` vocabulary the
    operations guide documents (plan P2/P5); fields carry only the opaque job
    id and dispatch id, never payload content or sensitive references.
    """
    logger.info(f"durable.{outcome}", job_id=str(job_id), **fields)


async def run_claimed(*, job_id: uuid.UUID, handler: Handler) -> None:
    """Claim the next attempt of ``job_id`` and run ``handler`` under ownership.

    Handles deferral, transient release and stale settlement as described in
    the module docstring. Returns normally for a no-op (stale) message and
    re-raises :class:`JobPermanentError` unchanged.
    """
    deferral_started = time.monotonic()
    while True:
        async with async_session_factory() as session:
            result = await jobs_service.claim_dispatch(session, job_id=job_id)

        if result.outcome is jobs_service.ClaimOutcome.STALE:
            _log_event("attempt_skipped", job_id=job_id, reason="terminal_state")
            return

        if result.outcome is jobs_service.ClaimOutcome.DEFERRED:
            deferred_until = result.deferred_until
            assert deferred_until is not None  # DEFERRED always carries the bound
            remaining = (deferred_until - datetime.now(UTC)).total_seconds()
            elapsed = time.monotonic() - deferral_started
            if remaining > MAX_DEFER_WAIT_SECONDS or elapsed > DEFER_TOTAL_BUDGET_SECONDS:
                _log_event(
                    "deferred",
                    job_id=job_id,
                    deferred_seconds=round(remaining, 1),
                    reason="lease_beyond_wait_budget",
                )
                raise jobs_service.DispatchDeferredError(job_id, deferred_until)
            _log_event("deferred", job_id=job_id, deferred_seconds=round(remaining, 1))
            await asyncio.sleep(remaining + _DEFER_MARGIN_SECONDS)
            continue

        # CLAIMED: the caller owns the current dispatch.
        result_dispatch_id = result.dispatch_id
        result_owner_token = result.owner_token
        claimed_job = result.job
        assert result_dispatch_id is not None and claimed_job is not None
        assert result_owner_token is not None
        _stamp_current_message(result_dispatch_id, result_owner_token)
        if result.taken_over:
            _log_event(
                "taken_over",
                job_id=job_id,
                dispatch_id=str(result_dispatch_id),
                attempt_count=claimed_job.attempt_count,
            )
        else:
            _log_event(
                "claimed",
                job_id=job_id,
                dispatch_id=str(result_dispatch_id),
                attempt_count=claimed_job.attempt_count,
            )
        context = DurableJobContext(
            job_id=job_id,
            job=claimed_job,
            dispatch_id=result_dispatch_id,
            owner_token=result_owner_token,
        )
        try:
            async with async_session_factory() as session:
                await handler(context, session)
            return
        except jobs_service.JobPermanentError:
            # The handler already settled the durable row itself; the retry
            # policy declares this exception in ``throws``, so the message is
            # never retried.
            raise
        except jobs_service.StaleDispatchError:
            _log_event(
                "settled_stale",
                job_id=job_id,
                dispatch_id=str(result_dispatch_id),
                reason="dispatch_superseded",
            )
            return
        except Exception:
            # Transient failure: release the owned attempt so a genuine retry
            # of the same dispatch can re-claim it, then propagate for the
            # Retries middleware. Releasing from a fresh session keeps the
            # release independent of whatever state the failing handler left
            # the session in.
            try:
                async with async_session_factory() as session:
                    released = await jobs_service.release_dispatch(
                        session,
                        job_id=job_id,
                        owner_token=result_owner_token,
                    )
                if released:
                    _log_event(
                        "released", job_id=job_id, dispatch_id=str(result_dispatch_id)
                    )
                else:
                    _log_event(
                        "settled_stale",
                        job_id=job_id,
                        dispatch_id=str(result_dispatch_id),
                        reason="already_terminal",
                    )
            except jobs_service.StaleDispatchError:
                _log_event(
                    "settled_stale",
                    job_id=job_id,
                    dispatch_id=str(result_dispatch_id),
                    reason="release_superseded",
                )
            except Exception:
                logger.warning(
                    "durable.release_failed",
                    job_id=str(job_id),
                    dispatch_id=str(result_dispatch_id),
                    exc_info=True,
                )
            raise
