"""Inspect or explicitly create durable queued-job recovery intents.

This operator tool has no public API surface. The default is read-only; an
operator must set ``CONFIRM_RECONCILE=1`` and pass ``--apply`` before it uses
the same bounded reconciliation service as the coordinator.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from datetime import UTC, datetime


async def run(*, apply: bool) -> int:
    if apply and os.environ.get("CONFIRM_RECONCILE") != "1":
        print("refusing mutation: set CONFIRM_RECONCILE=1 and use --apply")
        return 2
    # Delay application imports so ``--help`` remains available without a
    # configured database, while real inspection/apply still validates all
    # settings before touching PostgreSQL.
    from app.core.config import get_settings
    from app.db.session import coordinator_session_factory
    from app.job_coordinator.reconciliation import (
        reconcile_queued_jobs,
        reconciliation_candidates,
    )

    settings = get_settings()
    now = datetime.now(UTC)
    async with coordinator_session_factory() as session:
        candidates = await reconciliation_candidates(
            session,
            now=now,
            threshold_seconds=settings.job_reconcile_threshold_seconds,
            cooldown_seconds=settings.job_reconcile_cooldown_seconds,
            limit=settings.coordinator_publication_batch_size,
        )
    if not apply:
        print(f"reconciliation candidates: {len(candidates)}")
        for job_id in candidates:
            print(job_id)
        return 0
    async with coordinator_session_factory() as session:
        reconciled = await reconcile_queued_jobs(
            session,
            now=now,
            threshold_seconds=settings.job_reconcile_threshold_seconds,
            cooldown_seconds=settings.job_reconcile_cooldown_seconds,
            limit=settings.coordinator_publication_batch_size,
        )
    print(f"reconciliation events created: {len(reconciled)}")
    for job_id in reconciled:
        print(job_id)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect or recover queued durable jobs")
    parser.add_argument("--apply", action="store_true", help="create recovery dispatch intents")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(apply=args.apply)))


if __name__ == "__main__":
    main()
