"""Coordinator publication loop (durable delivery plan P3, blueprint §19).

The coordinator is the only production component that turns durable outbox
events into Dramatiq messages. Each cycle:

1. reclaims ``publishing`` rows whose publication lease expired (a crashed
   coordinator), then claims a bounded batch of due ``pending`` rows with
   ``FOR UPDATE SKIP LOCKED`` in a short transaction, writing a claim token
   and ``claimed_at`` so settlement stays owner-checked;
2. publishes each claimed event *outside* the row-lock transaction through the
   allow-listed :class:`DispatchRegistry` — a durable job actor receives only
   its ``job_id``, a maintenance actor no arguments;
3. settles the row with a single owner-checked conditional ``UPDATE``
   (``status = 'publishing'`` and the claim token must still match):
   ``published`` on success, ``dead`` with a bounded error when the event is
   permanently invalid (unknown contract, unknown registry target, missing or
   inconsistent job aggregate), and ``pending`` on a transient infrastructure
   failure with ``available_at`` advanced by the durable, jittered capped
   backoff — so another coordinator (or a restarted process) cannot reclaim
   the row before its retry time. A coordinator crash between the Redis
   publish and the settlement leaves the row ``publishing``; the stale-claim
   recovery republishes it, which may duplicate the broker message — exactly
   the at-least-once window the P2 execution ownership makes safe.

Multiple coordinators may run: ``FOR UPDATE SKIP LOCKED`` and the claim token
prevent two coordinators from publishing the same row deliberately; the
documented crash-window duplicate is handled by worker-side ownership, never
by coordination.
"""

from __future__ import annotations

import asyncio
import math
import signal
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from random import uniform
from typing import Any, cast

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.job_coordinator.registry import (
    DispatchRegistry,
    RegistryCompletenessError,
    RegistryError,
    build_default_registry,
)
from app.modules.jobs.models import Job
from app.modules.outbox.contracts import (
    EVENT_TYPE_JOB_DISPATCH,
    OutboxContractError,
    validate_payload,
)
from app.modules.outbox.models import OutboxEvent, OutboxEventStatus
from app.modules.outbox.queries import (
    due_outbox_events_statement,
    stale_claim_events_statement,
)

logger = structlog.get_logger()

#: Bounded error surface stored on outbox rows (plan: errors never carry
#: payload content); mirrors the database column width.
MAX_ERROR_CHARS = 500


class PublishOutcome(StrEnum):
    """Result of one claimed event's publication attempt."""

    PUBLISHED = "published"
    DEAD = "dead"
    TRANSIENT = "transient"


@dataclass(frozen=True)
class ClaimedEvent:
    """An outbox row claimed by this coordinator, with its settlement token."""

    event: OutboxEvent
    claim_token: str


@dataclass
class CycleStats:
    """Aggregate counts for one coordinator cycle (log-only, no content)."""

    claimed: int = 0
    stale_reclaimed: int = 0
    published: int = 0
    dead: int = 0
    released: int = 0
    settled_stale: int = 0

    @property
    def transient(self) -> bool:
        """True when any event could not be published this cycle."""
        return self.released > 0


def _new_claim_token() -> str:
    """Return an opaque, unpredictable settlement token."""
    return uuid.uuid4().hex


def bounded_error(exc: Exception) -> str:
    """Return a fixed, payload-free reason code for persistence and logging.

    Exception messages are deliberately excluded: validation libraries can
    render rejected input values, while broker/database exceptions can embed
    connection URLs or credentials. Operators correlate the safe reason with
    the opaque event id and investigate the underlying service separately.
    """
    if isinstance(exc, OutboxContractError):
        return "invalid_outbox_contract"
    if isinstance(exc, RegistryError):
        return "invalid_dispatch_target"
    return "transient_infrastructure_failure"


async def claim_due_events(
    session: AsyncSession,
    *,
    limit: int,
    now: datetime,
) -> list[ClaimedEvent]:
    """Claim up to ``limit`` due pending rows with ``FOR UPDATE SKIP LOCKED``.

    One short transaction: the claim transitions ``pending`` -> ``publishing``,
    records ``claimed_at``, rotates an opaque claim token and increments the
    attempt counter. Rows locked by another coordinator are skipped, never
    blocked on, so any number of coordinators may run. Publication happens
    after this transaction commits (outside the row lock).
    """
    statement = due_outbox_events_statement(at_or_before=now, limit=limit).with_for_update(
        skip_locked=True
    )
    rows = (await session.scalars(statement)).all()
    claimed: list[ClaimedEvent] = []
    for event in rows:
        token = _new_claim_token()
        event.status = OutboxEventStatus.PUBLISHING
        event.claimed_at = now
        event.claim_token = token
        event.attempt_count = event.attempt_count + 1
        claimed.append(ClaimedEvent(event=event, claim_token=token))
    await session.commit()
    return claimed


async def reclaim_stale_claims(
    session: AsyncSession,
    *,
    claimed_before: datetime,
    limit: int,
    now: datetime,
) -> list[ClaimedEvent]:
    """Reclaim ``publishing`` rows whose publication lease expired.

    A coordinator crash after claiming (but before settlement) leaves the row
    ``publishing`` past its lease; this moves it back into the publication
    path with a fresh token. The old coordinator's settlement then fails the
    token check and is logged as stale instead of double-settling.
    """
    statement = stale_claim_events_statement(
        claimed_before=claimed_before, limit=limit
    ).with_for_update(skip_locked=True)
    rows = (await session.scalars(statement)).all()
    reclaimed: list[ClaimedEvent] = []
    for event in rows:
        token = _new_claim_token()
        event.claimed_at = now
        event.claim_token = token
        event.attempt_count = event.attempt_count + 1
        reclaimed.append(ClaimedEvent(event=event, claim_token=token))
    await session.commit()
    return reclaimed


async def _resolve_job_for_dispatch(
    session: AsyncSession, event: OutboxEvent, payload: dict[str, Any]
) -> Job:
    """Return the durable job a dispatch event names, verifying consistency.

    Raises :class:`OutboxContractError` for a missing aggregate or an
    event/job mismatch, which the coordinator treats as permanent (dead).
    """
    raw_job_id = payload.get("job_id")
    if raw_job_id is None:
        raise OutboxContractError("job.dispatch_requested payload carries no job_id")
    try:
        job_id = uuid.UUID(raw_job_id)
    except (TypeError, ValueError) as exc:
        raise OutboxContractError("job.dispatch_requested payload has an invalid job_id") from exc
    job = await session.scalar(select(Job).where(Job.id == job_id))
    if job is None:
        raise OutboxContractError(f"job.dispatch_requested names missing job {job_id}")
    if event.aggregate_id != job.id or event.organisation_id != job.organisation_id:
        raise OutboxContractError(
            "job.dispatch_requested event does not match its job aggregate or organisation"
        )
    return job


async def publish_event(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    event: OutboxEvent,
    registry: DispatchRegistry,
) -> tuple[PublishOutcome, str | None]:
    """Publish one claimed event through the registry (outside any row lock).

    Returns the outcome and a bounded error for permanent failures. The
    payload is re-validated against its closed contract before publication so
    a persisted row can never smuggle an unapproved message shape into the
    broker path.
    """
    try:
        payload = validate_payload(event.event_type, event.event_version, event.payload)
        if event.event_type == EVENT_TYPE_JOB_DISPATCH:
            async with session_factory() as session:
                job = await _resolve_job_for_dispatch(session, event, payload)
            registry.publish_job_dispatch(job.job_type, str(job.id))
        else:
            registry.publish_maintenance(event.event_type)
    except (OutboxContractError, RegistryError) as exc:
        # Permanent: malformed event, unknown contract, unknown job/event
        # type, missing or inconsistent aggregate. The row becomes dead and
        # requires operator investigation.
        return PublishOutcome.DEAD, bounded_error(exc)
    except Exception as exc:
        # Transient infrastructure failure (broker or database unavailable):
        # the claim is released and retried after the capped backoff. The
        # error is logged, never persisted verbatim, and never names payload
        # content.
        logger.warning(
            "coordinator.publish_failed",
            event_id=str(event.id),
            event_type=event.event_type,
            reason=bounded_error(exc),
        )
        return PublishOutcome.TRANSIENT, None
    return PublishOutcome.PUBLISHED, None


async def settle_published(
    session: AsyncSession,
    *,
    event_id: uuid.UUID,
    claim_token: str,
) -> bool:
    """Mark a claimed event ``published``, owner-checked by the claim token.

    One conditional ``UPDATE`` carries the ownership predicate — the row must
    still be ``publishing`` *and* hold this coordinator's claim token — so a
    stale publisher whose claim was taken over affects zero rows and can never
    overwrite the newer owner's state (the race the plain ``get``-then-set
    path allowed). The affected-row count is the result: ``True`` exactly
    when this coordinator still owned the row. ``processed_at`` is the
    database clock at the moment of this ``UPDATE``, not the cycle start, so
    a slow publish records its actual settlement time.
    """
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.id == event_id,
                OutboxEvent.status == OutboxEventStatus.PUBLISHING,
                OutboxEvent.claim_token == claim_token,
            )
            .values(
                status=OutboxEventStatus.PUBLISHED,
                processed_at=func.now(),
                claimed_at=None,
                claim_token=None,
            )
        ),
    )
    await session.commit()
    return result.rowcount == 1


async def settle_dead(
    session: AsyncSession,
    *,
    event_id: uuid.UUID,
    claim_token: str,
    error: str,
) -> bool:
    """Mark a permanently invalid event ``dead`` with a bounded, safe error.

    The same single conditional ``UPDATE`` as :func:`settle_published`: the
    row is only touched while it still belongs to this claim, so a stale
    publisher cannot condemn a row a newer owner has already republished.
    ``processed_at`` is the database clock at this settlement, matching the
    actual settlement time used by :func:`release_claim`.
    """
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.id == event_id,
                OutboxEvent.status == OutboxEventStatus.PUBLISHING,
                OutboxEvent.claim_token == claim_token,
            )
            .values(
                status=OutboxEventStatus.DEAD,
                last_error=error[:MAX_ERROR_CHARS],
                processed_at=func.now(),
                claimed_at=None,
                claim_token=None,
            )
        ),
    )
    await session.commit()
    return result.rowcount == 1


async def release_claim(
    session: AsyncSession,
    *,
    event_id: uuid.UUID,
    claim_token: str,
    retry_after_delay: float,
) -> bool:
    """Release a transiently failed claim back to ``pending`` for retry.

    The next due time is advanced by the jittered capped backoff so the retry
    is durable: another coordinator — or this process after a restart — cannot
    reclaim the row before ``available_at``. The claim token is cleared and
    the ownership predicate makes the release a no-op for a superseded claim.
    ``available_at`` is computed relative to the database clock at the moment
    of this ``UPDATE``, so the full persisted backoff begins when the claim is
    released — a slow publish or a late row in a sequential batch can never
    consume part of the retry delay before the release (the review's
    cycle-start-origin defect).
    """
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.id == event_id,
                OutboxEvent.status == OutboxEventStatus.PUBLISHING,
                OutboxEvent.claim_token == claim_token,
            )
            .values(
                status=OutboxEventStatus.PENDING,
                available_at=func.now() + timedelta(seconds=retry_after_delay),
                claimed_at=None,
                claim_token=None,
            )
        ),
    )
    await session.commit()
    return result.rowcount == 1


async def _handle_claimed(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    claimed: ClaimedEvent,
    registry: DispatchRegistry,
    stats: CycleStats,
    backoff_initial_seconds: float,
    backoff_max_seconds: float,
) -> None:
    """Publish one claimed event outside the lock, then settle or release.

    Settlement timestamps come from the database clock at the moment of each
    conditional ``UPDATE`` (``func.now()``), never from the cycle's claim
    snapshot, so ``processed_at`` and the released row's ``available_at``
    reflect the actual settlement/release time.
    """
    event = claimed.event
    outcome, error = await publish_event(session_factory, event=event, registry=registry)
    async with session_factory() as session:
        if outcome is PublishOutcome.PUBLISHED:
            if await settle_published(session, event_id=event.id, claim_token=claimed.claim_token):
                stats.published += 1
                _log_published(event)
            else:
                stats.settled_stale += 1
                logger.warning(
                    "coordinator.settled_stale",
                    event_id=str(event.id),
                    reason="claim_superseded",
                )
        elif outcome is PublishOutcome.DEAD:
            assert error is not None
            if await settle_dead(
                session, event_id=event.id, claim_token=claimed.claim_token, error=error
            ):
                stats.dead += 1
                logger.warning(
                    "coordinator.dead",
                    event_id=str(event.id),
                    event_type=event.event_type,
                    reason=error,
                )
            else:
                stats.settled_stale += 1
                logger.warning(
                    "coordinator.settled_stale",
                    event_id=str(event.id),
                    reason="claim_superseded",
                )
        else:
            # Transient failure: release the claim with a durable, jittered
            # retry time derived from the row's attempt count so the row is
            # not reclaimable until its backoff expires — even by a different
            # coordinator or a restarted process.
            retry_delay = backoff_delay(
                consecutive_failures=event.attempt_count,
                initial_seconds=backoff_initial_seconds,
                max_seconds=backoff_max_seconds,
            )
            if await release_claim(
                session,
                event_id=event.id,
                claim_token=claimed.claim_token,
                retry_after_delay=retry_delay,
            ):
                stats.released += 1
                logger.warning(
                    "coordinator.released",
                    event_id=str(event.id),
                    event_type=event.event_type,
                    retry_after_delay=round(retry_delay, 2),
                    reason="transient_publish_failure",
                )
            else:
                stats.settled_stale += 1
                logger.warning(
                    "coordinator.settled_stale",
                    event_id=str(event.id),
                    reason="claim_superseded",
                )


def _log_published(event: OutboxEvent) -> None:
    """Log one successful publication with opaque identifiers only."""
    fields: dict[str, object] = {
        "event_id": str(event.id),
        "event_type": event.event_type,
        "event_version": event.event_version,
    }
    if event.event_type == EVENT_TYPE_JOB_DISPATCH:
        job_id = event.payload.get("job_id")
        fields["job_id"] = str(job_id) if job_id is not None else None
    logger.info("coordinator.published", **fields)


async def run_cycle(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    registry: DispatchRegistry,
    batch_size: int,
    publication_lease_seconds: int,
    backoff_initial_seconds: float = 1.0,
    backoff_max_seconds: float = 300.0,
    shutdown_event: asyncio.Event | None = None,
) -> CycleStats:
    """Run one claim -> publish -> settle cycle and return its counts.

    ``shutdown_event`` (when given) is checked between events and before the
    fresh-work claim, so a SIGTERM mid-cycle stops claiming new work and lets
    in-flight events finish; rows claimed but not yet settled stay
    ``publishing`` under their lease and are recovered by the stale-claim
    path. The configured batch bound covers one cycle's total work: stale
    re-claims come first, then the fresh claim takes only the remaining
    slots, so a cycle never publishes more than ``batch_size`` rows.
    """
    stats = CycleStats()
    # The database clock decides due-ness: ``available_at`` is written by
    # ``func.now()``, so comparing against the database clock keeps a
    # coordinator on a slightly-behind host from missing freshly scheduled
    # rows (and a slightly-ahead host from claiming them early).
    async with session_factory() as session:
        db_now = await session.scalar(select(func.now()))
    now = db_now or datetime.now(UTC)
    if shutdown_event is not None and shutdown_event.is_set():
        return stats
    # Reclaim crashed-coordinator claims first so a stuck row is never
    # starved behind a full batch of fresh pending rows.
    async with session_factory() as session:
        stale = await reclaim_stale_claims(
            session,
            claimed_before=now - timedelta(seconds=publication_lease_seconds),
            limit=batch_size,
            now=now,
        )
    stats.stale_reclaimed = len(stale)
    for claimed in stale:
        if shutdown_event is not None and shutdown_event.is_set():
            break
        await _handle_claimed(
            session_factory,
            claimed=claimed,
            registry=registry,
            stats=stats,
            backoff_initial_seconds=backoff_initial_seconds,
            backoff_max_seconds=backoff_max_seconds,
        )

    # Fresh work takes only the slots the stale re-claims did not use, and
    # only when shutdown has not arrived.
    remaining = max(batch_size - len(stale), 0)
    if remaining and (shutdown_event is None or not shutdown_event.is_set()):
        async with session_factory() as session:
            due = await claim_due_events(session, limit=remaining, now=now)
        stats.claimed = len(due)
        for claimed in due:
            if shutdown_event is not None and shutdown_event.is_set():
                break
            await _handle_claimed(
                session_factory,
                claimed=claimed,
                registry=registry,
                stats=stats,
                backoff_initial_seconds=backoff_initial_seconds,
                backoff_max_seconds=backoff_max_seconds,
            )
    return stats


def backoff_delay(
    *, consecutive_failures: int, initial_seconds: float, max_seconds: float
) -> float:
    """Capped exponential backoff with full jitter (never exceeds ``max``).

    ``consecutive_failures`` is the number of cycles that ended with a
    transient failure; the delay grows 2^(n-1) * ``initial`` up to ``max``.
    The exponent is clamped *before* exponentiation, so an extreme attempt
    count can never construct an unbounded integer (the review's
    ``OverflowError`` at 2000 attempts): the clamp is the first exponent
    whose delay already saturates the cap.
    """
    if initial_seconds <= 0 or max_seconds <= 0:
        # Degenerate configuration; the startup validator rejects it, but the
        # helper stays total and yields an immediate retry.
        return 0.0
    cap_exponent = max(math.ceil(math.log2(max_seconds / initial_seconds)), 0)
    exponent = min(consecutive_failures - 1, cap_exponent)
    cap = min(initial_seconds * (2**exponent), max_seconds)
    return uniform(0.0, cap)


async def run_coordinator(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    registry: DispatchRegistry,
    batch_size: int,
    idle_poll_seconds: float,
    publication_lease_seconds: int,
    backoff_initial_seconds: float,
    backoff_max_seconds: float,
    shutdown_event: asyncio.Event,
) -> None:
    """Run the coordinator until ``shutdown_event`` is set.

    Validates the registry once at startup, then loops: one bounded
    claim/publish/settle cycle per iteration, capped exponential backoff after
    a cycle with transient failures, a short idle poll otherwise. Shutdown is
    checked between cycles, between events within a batch, and during waits,
    so a SIGTERM drains promptly: no new work is claimed after shutdown and
    rows claimed but not yet settled stay ``publishing`` under their lease,
    ready for the stale-claim recovery (or a restart).
    """
    try:
        registry.validate()
    except RegistryCompletenessError as exc:
        logger.critical("coordinator.registry_incomplete", reason=bounded_error(exc))
        raise
    logger.info(
        "coordinator.started",
        batch_size=batch_size,
        publication_lease_seconds=publication_lease_seconds,
    )
    consecutive_failures = 0
    while not shutdown_event.is_set():
        transient = False
        try:
            stats = await run_cycle(
                session_factory,
                registry=registry,
                batch_size=batch_size,
                publication_lease_seconds=publication_lease_seconds,
                backoff_initial_seconds=backoff_initial_seconds,
                backoff_max_seconds=backoff_max_seconds,
                shutdown_event=shutdown_event,
            )
            transient = stats.transient
            logger.info(
                "coordinator.cycle_completed",
                claimed=stats.claimed,
                stale_reclaimed=stats.stale_reclaimed,
                published=stats.published,
                dead=stats.dead,
                released=stats.released,
                settled_stale=stats.settled_stale,
            )
        except Exception as exc:
            # A database-level failure (unreachable PostgreSQL, broken pool)
            # is transient too: back off and try the whole cycle again.
            consecutive_failures += 1
            logger.warning(
                "coordinator.cycle_failed",
                reason=bounded_error(exc),
                consecutive_failures=consecutive_failures,
            )
            await _wait_for_shutdown(
                shutdown_event,
                backoff_delay(
                    consecutive_failures=consecutive_failures,
                    initial_seconds=backoff_initial_seconds,
                    max_seconds=backoff_max_seconds,
                ),
            )
            continue
        if shutdown_event.is_set():
            break
        if transient:
            consecutive_failures += 1
            delay = backoff_delay(
                consecutive_failures=consecutive_failures,
                initial_seconds=backoff_initial_seconds,
                max_seconds=backoff_max_seconds,
            )
            logger.warning(
                "coordinator.backoff",
                delay_seconds=round(delay, 2),
                consecutive_failures=consecutive_failures,
            )
            await _wait_for_shutdown(shutdown_event, delay)
        else:
            consecutive_failures = 0
            await _wait_for_shutdown(shutdown_event, idle_poll_seconds)
    logger.info("coordinator.stopped")


async def _wait_for_shutdown(shutdown_event: asyncio.Event, timeout: float) -> None:
    """Wait up to ``timeout`` seconds, returning early on shutdown."""
    if timeout <= 0:
        return
    with suppress(TimeoutError):
        await asyncio.wait_for(shutdown_event.wait(), timeout=timeout)


async def _async_main() -> None:
    """Wire settings, broker, registry and signals, then run until stopped."""
    from app.core.config import get_settings
    from app.core.logging import configure_logging
    from app.db.session import async_session_factory

    settings = get_settings()
    configure_logging(log_level=settings.log_level, json_logs=not settings.debug)
    # The registry's actor imports register the durable tasks with the
    # process broker; build it only after the broker is installed so the
    # actors bind to Redis (blueprint §36: one image, different commands).
    # ``build_default_registry`` is what triggers those imports, so nothing
    # here (and nothing in the package ``__init__``) may import a task module
    # earlier than this point.
    import dramatiq

    from app.broker import build_broker

    dramatiq.set_broker(build_broker())
    registry = build_default_registry()

    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    with suppress(NotImplementedError, RuntimeError):
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, shutdown_event.set)
    try:
        await run_coordinator(
            async_session_factory,
            registry=registry,
            batch_size=settings.coordinator_publication_batch_size,
            idle_poll_seconds=settings.coordinator_idle_poll_seconds,
            publication_lease_seconds=settings.coordinator_publication_lease_seconds,
            backoff_initial_seconds=settings.coordinator_publication_backoff_initial_seconds,
            backoff_max_seconds=settings.coordinator_publication_backoff_max_seconds,
            shutdown_event=shutdown_event,
        )
    except RegistryCompletenessError as exc:
        raise SystemExit(f"coordinator registry incomplete: {exc}") from exc


def main() -> None:
    """Native coordinator entry point: ``uv run python -m app.job_coordinator``."""
    asyncio.run(_async_main())
