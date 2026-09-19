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
- **retry decision**: a transient failure closes the owned attempt and writes
  either a delayed reference-only outbox dispatch or terminal exhaustion in
  the same PostgreSQL transaction. The broker callback is not required.
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

from app.db.rls import bind_organisation_context
from app.db.session import async_session_factory
from app.modules.jobs import service as jobs_service
from app.observability.sentry import capture_exception

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
    """Everything the domain handler needs to run one owned attempt.

    The context deliberately carries only the primitive values the handler
    needs plus the captured ownership credential. The claimed ORM ``Job`` is
    never passed across the claim commit, so a handler cannot accidentally use
    a detached snapshot as authority; consequential mutations go through
    :meth:`ownership` and ``jobs_service.verify_ownership`` (plan P2).
    """

    job_id: uuid.UUID
    organisation_id: uuid.UUID
    job_type: str
    input_reference: str
    created_by_user_id: uuid.UUID | None
    dispatch_id: uuid.UUID
    owner_token: uuid.UUID

    @property
    def ownership(self) -> jobs_service.JobOwnership:
        """Return the captured ownership credential for domain mutations."""
        return jobs_service.JobOwnership(
            job_id=self.job_id,
            owner_token=self.owner_token,
            organisation_id=self.organisation_id,
        )


Handler = Callable[[DurableJobContext, AsyncSession], Awaitable[None]]


def _stamp_current_message(dispatch_id: uuid.UUID, owner_token: uuid.UUID) -> None:
    """Stamp the current broker message with the attempt just claimed.

    The stamp travels with the message (in ``options``, which the Retries
    middleware forwards verbatim to the retries-exhausted handler), so the
    exhausted finalizer can tell which attempt the message actually claimed.
    Both the dispatch id and the rotated owner token are stamped. An
    expired-lease takeover keeps the dispatch identity while rotating the
    token, so the finalizer must correlate by attempt, not dispatch alone, to
    refuse settling a newer owner of the same dispatch.
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

    Handles deferral, durable retry decisions and stale settlement as described in
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

        if result.outcome is jobs_service.ClaimOutcome.EXHAUSTED:
            _log_event("exhausted", job_id=job_id, reason="global_attempt_limit")
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
            organisation_id=claimed_job.organisation_id,
            job_type=claimed_job.job_type,
            input_reference=claimed_job.input_reference,
            created_by_user_id=claimed_job.created_by_user_id,
            dispatch_id=result_dispatch_id,
            owner_token=result_owner_token,
        )
        try:
            async with async_session_factory() as session:
                # Plan P3 group 4b: ``jobs`` and ``job_attempts`` are RLS
                # enforced, so every handler transaction carries the durable
                # row's organisation as transaction-local context before the
                # handler reads or mutates the protected rows. The value comes
                # from the claimed durable row, never the broker (ADR-0022
                # decision 3). Handlers that need a user-private scope (the
                # notifications email worker) bind the user on top of this.
                await bind_organisation_context(session, context.organisation_id)
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
        except Exception as exc:
            # PostgreSQL owns the next action: an attempt closes together
            # with a delayed outbox dispatch or terminal exhaustion. Broker
            # retries remain a fallback only if this settlement cannot commit.
            logger.error(
                "durable.attempt_failed",
                job_id=str(job_id),
                dispatch_id=str(result_dispatch_id),
                exc_info=True,
            )
            capture_exception(exc)
            try:
                async with async_session_factory() as session:
                    decision = await jobs_service.settle_retryable_failure(
                        session,
                        job_id=job_id,
                        owner_token=result_owner_token,
                    )
                if decision is jobs_service.JobStatus.QUEUED:
                    _log_event(
                        "retry_scheduled", job_id=job_id, dispatch_id=str(result_dispatch_id)
                    )
                    return
                if decision is jobs_service.JobStatus.FAILED:
                    _log_event("exhausted", job_id=job_id, dispatch_id=str(result_dispatch_id))
                    return
                if decision is None:
                    _log_event(
                        "settled_stale",
                        job_id=job_id,
                        dispatch_id=str(result_dispatch_id),
                        reason="already_terminal",
                    )
                    return
            except jobs_service.StaleDispatchError:
                _log_event(
                    "settled_stale",
                    job_id=job_id,
                    dispatch_id=str(result_dispatch_id),
                    reason="retry_settlement_superseded",
                )
                return
            except Exception:
                logger.warning(
                    "durable.release_failed",
                    job_id=str(job_id),
                    dispatch_id=str(result_dispatch_id),
                    exc_info=True,
                )
                raise
