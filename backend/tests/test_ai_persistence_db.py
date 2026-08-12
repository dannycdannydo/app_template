"""Real-database integration tests for AI persistence (v0.7 Scope §6.5).

The unit tests in ``test_ai_persistence.py`` prove the pure logic and the
AIService-recorder flow but never execute SQL, so the settings persistence,
the budget reservation/settlement math under the row lock, idempotency, the
retention sweep and cross-organisation isolation could silently regress. These
tests run the real migration and the real services against a reachable
PostgreSQL, using the same skip pattern as the other ``*_db.py`` modules:
migrated to head up front, reverted to base afterwards.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.ai.errors import BudgetExceededError
from app.ai.persistence import service as ai_persistence
from app.ai.persistence.models import (
    AIOutputRecord,
    AIRequestRecord,
    AIRequestStatus,
    OrganisationAISettings,
)
from app.ai.persistence.service import (
    AIPersistencePortImpl,
    ai_scratch_prefix,
    create_default_settings,
)
from app.ai.registry import load_registry_bundle
from app.ai.schemas import CostEstimate, TokenUsage
from app.core.exceptions import ValidationError
from app.modules.audit.service import (
    ACTION_AI_BUDGET_DENIED,
    ACTION_AI_REQUEST_COMPLETED,
    ACTION_AI_RETENTION_DELETED,
    ACTION_AI_SETTINGS_UPDATED,
)
from app.modules.organisations.models import Organisation
from app.modules.users.models import User
from app.observability.metrics import AI_BUDGET_DENIALS_TOTAL
from app.storage.fake import FakeObjectStorage


def _counter_value(counter: Any, labels: dict[str, str] | None = None) -> float:
    """Current value of one Prometheus counter via the public collect() API.

    ``labels`` selects one series explicitly so the assertion stays stable
    when a labelled counter gains more series.
    """
    for metric in counter.collect():
        for sample in metric.samples:
            if labels is not None and sample.labels != labels:
                continue
            return float(sample.value)
    return 0.0


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _database_reachable(database_url: str) -> bool:
    """Probe the configured database with a short async engine connect."""

    async def _probe() -> bool:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            return True
        except Exception:
            return False
        finally:
            await engine.dispose()

    return asyncio.run(_probe())


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[str]:
    """Migrate a reachable PostgreSQL to head, and revert to base afterwards."""
    database_url = os.environ["DATABASE_URL"]
    if not _database_reachable(database_url):
        pytest.skip("no reachable PostgreSQL at DATABASE_URL")

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    command.upgrade(config, "head")
    yield database_url
    command.downgrade(config, "base")


def _session_factory(database_url: str):
    engine = create_async_engine(database_url, poolclass=NullPool)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed_organisation(session: AsyncSession) -> Organisation:
    organisation = Organisation(name=f"AI Org {uuid.uuid4().hex[:8]}")
    session.add(organisation)
    await session.commit()
    return organisation


async def _seed_actor(session: AsyncSession) -> User:
    actor = User(
        workos_user_id=f"ai_admin_{uuid.uuid4().hex[:8]}",
        email="ai-platform@example.com",
        name="AI Platform Admin",
    )
    session.add(actor)
    await session.commit()
    return actor


async def _seed_settings(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    **overrides: object,
) -> OrganisationAISettings:
    settings_row = await create_default_settings(session, organisation_id=organisation_id)
    await session.commit()
    return settings_row


def _known_model() -> tuple[str, str]:
    """Return a (provider, model id) pair from the checked-in registry."""
    model = load_registry_bundle().models.all()[0]
    return model.provider, model.id


async def _request_rows(session: AsyncSession, organisation_id: uuid.UUID) -> list[AIRequestRecord]:
    return list(
        (
            await session.scalars(
                select(AIRequestRecord).where(AIRequestRecord.organisation_id == organisation_id)
            )
        ).all()
    )


# --- Settings persistence and the default-off invariant ---


async def test_default_settings_row_is_off_and_policy_is_default_deny(
    migrated_database: str,
) -> None:
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            organisation = await _seed_organisation(session)
            settings_row = await _seed_settings(session, organisation.id)

            assert settings_row.enabled is False
            assert settings_row.allowed_provider_ids == []
            assert settings_row.allowed_model_ids == []
            assert settings_row.monthly_budget is None
            assert settings_row.retention_policy_days is None

            policy = await ai_persistence.get_organisation_policy(
                session, organisation_id=organisation.id
            )
            assert policy.enabled is False
    finally:
        await engine.dispose()


async def test_missing_settings_row_resolves_to_default_deny(migrated_database: str) -> None:
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            organisation = await _seed_organisation(session)
            policy = await ai_persistence.get_organisation_policy(
                session, organisation_id=organisation.id
            )
            assert policy.enabled is False
    finally:
        await engine.dispose()


async def test_update_settings_persists_and_audits(migrated_database: str) -> None:
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            actor = await _seed_actor(session)
            organisation = await _seed_organisation(session)
            await _seed_settings(session, organisation.id)
            provider, model_id = _known_model()

            settings_row = await ai_persistence.update_ai_settings(
                session,
                actor=actor,
                organisation_id=organisation.id,
                enabled=True,
                allowed_provider_ids=[provider],
                allowed_model_ids=[model_id],
                provider_override=None,
                model_override=None,
                monthly_budget=Decimal("10.000000"),
                retention_policy_days=30,
            )
            assert settings_row.enabled is True
            assert settings_row.allowed_provider_ids == [provider]
            assert settings_row.allowed_model_ids == [model_id]
            assert settings_row.monthly_budget == Decimal("10.000000")
            assert settings_row.retention_policy_days == 30
            assert settings_row.updated_by_user_id == actor.id

            actions = (
                await session.execute(
                    text(
                        "SELECT action FROM audit_events "
                        "WHERE resource_type = 'organisation_ai_settings' "
                        "AND resource_id = :oid"
                    ).bindparams(oid=str(organisation.id))
                )
            ).all()
            assert [action for (action,) in actions] == [ACTION_AI_SETTINGS_UPDATED]
    finally:
        await engine.dispose()


async def test_update_settings_rejects_unknown_registry_ids(migrated_database: str) -> None:
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            actor = await _seed_actor(session)
            organisation = await _seed_organisation(session)
            await _seed_settings(session, organisation.id)
            _, model_id = _known_model()

            with pytest.raises(ValidationError):
                await ai_persistence.update_ai_settings(
                    session,
                    actor=actor,
                    organisation_id=organisation.id,
                    enabled=True,
                    allowed_provider_ids=["not-a-provider"],
                    allowed_model_ids=[model_id],
                    provider_override=None,
                    model_override=None,
                    monthly_budget=None,
                    retention_policy_days=None,
                )
            # Nothing was written: the row is still the default off.
            row = await session.scalar(
                select(OrganisationAISettings).where(
                    OrganisationAISettings.organisation_id == organisation.id
                )
            )
            assert row is not None
            assert row.enabled is False
    finally:
        await engine.dispose()


async def test_unknown_organisation_is_a_404(migrated_database: str) -> None:
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            from app.core.exceptions import NotFoundError

            with pytest.raises(NotFoundError):
                await ai_persistence.get_ai_settings(session, organisation_id=uuid.uuid4())
    finally:
        await engine.dispose()


# --- Budget reservation and settlement (documented reservation policy) ---


async def _reserve(
    port: AIPersistencePortImpl,
    *,
    organisation_id: uuid.UUID,
    user_id: uuid.UUID,
    request_id: str,
    estimated_cost: Decimal,
    execution_maximum_estimated_cost: Decimal | None = None,
) -> uuid.UUID:
    reservation = await port.reserve(
        organisation_id=organisation_id,
        user_id=user_id,
        request_id=request_id,
        task="document.classify",
        provider="fake",
        model="fake-model-document.classify",
        prompt_name="classify",
        prompt_version=1,
        routing_reason="first eligible configured model fake.document-classifier",
        fallback_used=False,
        region="",
        estimated_cost=estimated_cost,
        execution_maximum_estimated_cost=execution_maximum_estimated_cost or estimated_cost,
        input_reference=None,
        input_digest=None,
    )
    return reservation.row_id


async def _record_attempt(
    port: AIPersistencePortImpl,
    *,
    organisation_id: uuid.UUID,
    user_id: uuid.UUID,
    request_id: str,
    attempt_number: int,
    estimated_cost: Decimal,
) -> uuid.UUID:
    return await port.record_attempt(
        organisation_id=organisation_id,
        user_id=user_id,
        request_id=request_id,
        attempt_number=attempt_number,
        task="document.classify",
        provider="fake",
        model="fake-model-document.classify",
        prompt_name="classify",
        prompt_version=1,
        routing_reason="first eligible configured model fake.document-classifier",
        fallback_used=False,
        region="",
        estimated_cost=estimated_cost,
        input_reference=None,
        input_digest=None,
    )


async def _settle(
    port: AIPersistencePortImpl,
    *,
    ai_request_id: uuid.UUID,
    organisation_id: uuid.UUID,
    user_id: uuid.UUID | None,
    status: str,
    usage: TokenUsage | None = None,
    cost: Decimal | None = None,
    **overrides: Any,
) -> None:
    await port.settle(
        ai_request_id=ai_request_id,
        organisation_id=organisation_id,
        task="document.classify",
        user_id=user_id,
        status=status,
        error_code=None if status == "succeeded" else "provider_unavailable",
        usage=usage or TokenUsage(input_tokens=10, output_tokens=5),
        cost=CostEstimate(amount=cost if cost is not None else Decimal("0.000020")),
        latency_ms=30,
        routing_provider="fake",
        routing_model="fake-model-document.classify",
        routing_prompt_name="classify",
        routing_prompt_version=1,
        routing_reason="first eligible configured model fake.document-classifier",
        fallback_used=False,
        region="",
        **overrides,
    )


async def test_reserve_then_settle_records_actuals(migrated_database: str) -> None:
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            actor = await _seed_actor(session)
            organisation = await _seed_organisation(session)
            await _seed_settings(session, organisation.id)
            await ai_persistence.update_ai_settings(
                session,
                actor=actor,
                organisation_id=organisation.id,
                enabled=True,
                allowed_provider_ids=[],
                allowed_model_ids=[],
                provider_override=None,
                model_override=None,
                monthly_budget=Decimal("1.000000"),
                retention_policy_days=None,
            )
            port = AIPersistencePortImpl(session)
            ai_request_id = await _reserve(
                port,
                organisation_id=organisation.id,
                request_id="req-1",
                user_id=actor.id,
                estimated_cost=Decimal("0.002000"),
                execution_maximum_estimated_cost=Decimal("0.008000"),
            )
            row = await session.scalar(
                select(AIRequestRecord).where(AIRequestRecord.id == ai_request_id)
            )
            assert row is not None
            assert row.status == AIRequestStatus.RUNNING
            assert row.cost == Decimal("0.008000")  # bounded execution reservation
            assert row.estimated_cost == Decimal("0.002000")  # this dispatch's estimate

            await port.settle(
                ai_request_id=ai_request_id,
                organisation_id=organisation.id,
                task="document.classify",
                user_id=actor.id,
                status="succeeded",
                error_code=None,
                usage=TokenUsage(input_tokens=100, output_tokens=50),
                cost=CostEstimate(amount=Decimal("0.000150")),
                latency_ms=250,
                routing_provider="fake",
                routing_model="fake-model-document.classify",
                routing_prompt_name="classify",
                routing_prompt_version=1,
                routing_reason="first eligible configured model fake.document-classifier",
                fallback_used=False,
                region="",
            )
            row = await session.scalar(
                select(AIRequestRecord).where(AIRequestRecord.id == ai_request_id)
            )
            assert row is not None
            assert row.status == AIRequestStatus.SUCCEEDED
            assert row.cost == Decimal("0.000150")  # the actual usage-priced cost
            assert row.input_tokens == 100
            assert row.output_tokens == 50
            assert row.latency_ms == 250

            actions = (
                await session.execute(
                    text(
                        "SELECT action FROM audit_events "
                        "WHERE resource_type = 'ai_request' AND resource_id = 'req-1'"
                    )
                )
            ).all()
            assert [action for (action,) in actions] == [ACTION_AI_REQUEST_COMPLETED]
    finally:
        await engine.dispose()


async def test_budget_denial_blocks_second_reservation(migrated_database: str) -> None:
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            actor = await _seed_actor(session)
            organisation = await _seed_organisation(session)
            await _seed_settings(session, organisation.id)
            await ai_persistence.update_ai_settings(
                session,
                actor=actor,
                organisation_id=organisation.id,
                enabled=True,
                allowed_provider_ids=[],
                allowed_model_ids=[],
                provider_override=None,
                model_override=None,
                monthly_budget=Decimal("0.010000"),
                retention_policy_days=None,
            )
            port = AIPersistencePortImpl(session)
            await _reserve(
                port,
                organisation_id=organisation.id,
                request_id="req-budget-1",
                user_id=actor.id,
                estimated_cost=Decimal("0.006000"),
            )
            # The second reservation sees the first running row in the month's
            # spend: 0.006 + 0.006 > 0.01, so it is denied before dispatch.
            denials_before = _counter_value(AI_BUDGET_DENIALS_TOTAL, {"task": "document.classify"})
            with pytest.raises(BudgetExceededError):
                await _reserve(
                    port,
                    organisation_id=organisation.id,
                    request_id="req-budget-2",
                    user_id=actor.id,
                    estimated_cost=Decimal("0.006000"),
                )
            # The budget-denial metric counts the denial (v0.7 Scope §6.7).
            assert (
                _counter_value(AI_BUDGET_DENIALS_TOTAL, {"task": "document.classify"})
                == denials_before + 1
            )
            rows = await _request_rows(session, organisation.id)
            assert len(rows) == 1  # the denied reservation wrote no row

            actions = (
                await session.execute(
                    text(
                        "SELECT action FROM audit_events "
                        "WHERE action = :action AND resource_id = 'req-budget-2'"
                    ).bindparams(action=ACTION_AI_BUDGET_DENIED)
                )
            ).all()
            assert len(actions) == 1
    finally:
        await engine.dispose()


async def test_reserve_is_idempotent_on_request_id(migrated_database: str) -> None:
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            actor = await _seed_actor(session)
            organisation = await _seed_organisation(session)
            await _seed_settings(session, organisation.id)
            await ai_persistence.update_ai_settings(
                session,
                actor=actor,
                organisation_id=organisation.id,
                enabled=True,
                allowed_provider_ids=[],
                allowed_model_ids=[],
                provider_override=None,
                model_override=None,
                monthly_budget=Decimal("0.001000"),
                retention_policy_days=None,
            )
            port = AIPersistencePortImpl(session)
            first = await _reserve(
                port,
                organisation_id=organisation.id,
                request_id="req-idem",
                user_id=actor.id,
                estimated_cost=Decimal("0.000500"),
            )
            second = await _reserve(
                port,
                organisation_id=organisation.id,
                request_id="req-idem",
                user_id=actor.id,
                estimated_cost=Decimal("0.000500"),
            )
            assert first == second  # the retried job re-uses the same row
            rows = await _request_rows(session, organisation.id)
            assert len(rows) == 1
    finally:
        await engine.dispose()


async def test_settle_is_idempotent(migrated_database: str) -> None:
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            actor = await _seed_actor(session)
            organisation = await _seed_organisation(session)
            await _seed_settings(session, organisation.id)
            await ai_persistence.update_ai_settings(
                session,
                actor=actor,
                organisation_id=organisation.id,
                enabled=True,
                allowed_provider_ids=[],
                allowed_model_ids=[],
                provider_override=None,
                model_override=None,
                monthly_budget=None,
                retention_policy_days=None,
            )
            port = AIPersistencePortImpl(session)
            ai_request_id = await _reserve(
                port,
                organisation_id=organisation.id,
                request_id="req-settle-idem",
                user_id=actor.id,
                estimated_cost=Decimal("0.000100"),
            )
            await port.settle(
                ai_request_id=ai_request_id,
                organisation_id=organisation.id,
                task="document.classify",
                user_id=actor.id,
                status="succeeded",
                error_code=None,
                usage=TokenUsage(input_tokens=10, output_tokens=5),
                cost=CostEstimate(amount=Decimal("0.000020")),
                latency_ms=30,
                routing_provider="fake",
                routing_model="fake-model-document.classify",
                routing_prompt_name="classify",
                routing_prompt_version=1,
                routing_reason="first eligible configured model fake.document-classifier",
                fallback_used=False,
                region="",
            )
            # A re-delivered message settles again: terminal states are never
            # re-run, so the row and the audit trail stay single.
            await port.settle(
                ai_request_id=ai_request_id,
                organisation_id=organisation.id,
                task="document.classify",
                user_id=actor.id,
                status="succeeded",
                error_code=None,
                usage=TokenUsage(input_tokens=10, output_tokens=5),
                cost=CostEstimate(amount=Decimal("0.000020")),
                latency_ms=30,
                routing_provider="fake",
                routing_model="fake-model-document.classify",
                routing_prompt_name="classify",
                routing_prompt_version=1,
                routing_reason="first eligible configured model fake.document-classifier",
                fallback_used=False,
                region="",
            )
            row = await session.scalar(
                select(AIRequestRecord).where(AIRequestRecord.id == ai_request_id)
            )
            assert row is not None
            assert row.status == AIRequestStatus.SUCCEEDED
            assert row.cost == Decimal("0.000020")
            actions = (
                await session.execute(
                    text(
                        "SELECT action FROM audit_events "
                        "WHERE resource_type = 'ai_request' AND resource_id = 'req-settle-idem'"
                    )
                )
            ).all()
            assert [action for (action,) in actions] == [ACTION_AI_REQUEST_COMPLETED]
    finally:
        await engine.dispose()


async def test_record_output_stores_references_and_digests_only(migrated_database: str) -> None:
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            actor = await _seed_actor(session)
            organisation = await _seed_organisation(session)
            await _seed_settings(session, organisation.id)
            port = AIPersistencePortImpl(session)
            ai_request_id = await _reserve(
                port,
                organisation_id=organisation.id,
                request_id="req-output",
                user_id=actor.id,
                estimated_cost=Decimal("0.000100"),
            )
            await port.settle(
                ai_request_id=ai_request_id,
                organisation_id=organisation.id,
                task="document.classify",
                user_id=actor.id,
                status="succeeded",
                error_code=None,
                usage=TokenUsage(input_tokens=10, output_tokens=5),
                cost=CostEstimate(amount=Decimal("0.000020")),
                latency_ms=30,
                routing_provider="fake",
                routing_model="fake-model-document.classify",
                routing_prompt_name="classify",
                routing_prompt_version=1,
                routing_reason="first eligible configured model fake.document-classifier",
                fallback_used=False,
                region="",
                output={"task": "document.classify", "confidence": 0.9},
                output_reference=ai_scratch_prefix(organisation.id) + "result.json",
                output_digest="abc123",
                retain_content=True,
                input_reference=f"organisations/{organisation.id}/documents/file/lease.pdf",
                input_digest="def456",
            )
            output = await session.scalar(
                select(AIOutputRecord).where(AIOutputRecord.ai_request_id == ai_request_id)
            )
            assert output is not None
            assert output.output_json == {"task": "document.classify", "confidence": 0.9}
            assert output.output_reference is not None
            assert output.output_digest == "abc123"
            assert output.input_digest == "def456"
            assert output.approved is False
    finally:
        await engine.dispose()


async def test_settle_success_without_retention_opt_in_stores_no_content(
    migrated_database: str,
) -> None:
    """The safe default: without the retention opt-in the output record is
    references/digests only, never the content itself (v0.7 Scope §2)."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            actor = await _seed_actor(session)
            organisation = await _seed_organisation(session)
            await _seed_settings(session, organisation.id)
            port = AIPersistencePortImpl(session)
            ai_request_id = await _reserve(
                port,
                organisation_id=organisation.id,
                request_id="req-output-safety",
                user_id=actor.id,
                estimated_cost=Decimal("0.000100"),
            )
            await port.settle(
                ai_request_id=ai_request_id,
                organisation_id=organisation.id,
                task="document.classify",
                user_id=actor.id,
                status="succeeded",
                error_code=None,
                usage=TokenUsage(input_tokens=10, output_tokens=5),
                cost=CostEstimate(amount=Decimal("0.000020")),
                latency_ms=30,
                routing_provider="fake",
                routing_model="fake-model-document.classify",
                routing_prompt_name="classify",
                routing_prompt_version=1,
                routing_reason="first eligible configured model fake.document-classifier",
                fallback_used=False,
                region="",
                output={"task": "document.classify", "content": "sensitive"},
                retain_content=False,
                input_reference="organisations/x/documents/file/lease.pdf",
                input_digest="def456",
            )
            output = await session.scalar(
                select(AIOutputRecord).where(AIOutputRecord.ai_request_id == ai_request_id)
            )
            assert output is not None
            assert output.output_json is None  # content never stored
            assert "sensitive" not in str(output.output_json)
            assert output.input_digest == "def456"
    finally:
        await engine.dispose()


# --- Cross-organisation isolation ---


async def test_policy_and_spend_are_organisation_scoped(migrated_database: str) -> None:
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            actor = await _seed_actor(session)
            organisation_a = await _seed_organisation(session)
            organisation_b = await _seed_organisation(session)
            await _seed_settings(session, organisation_a.id)
            await _seed_settings(session, organisation_b.id)

            # Enable only organisation A.
            await ai_persistence.update_ai_settings(
                session,
                actor=actor,
                organisation_id=organisation_a.id,
                enabled=True,
                allowed_provider_ids=[],
                allowed_model_ids=[],
                provider_override=None,
                model_override=None,
                monthly_budget=Decimal("1.000000"),
                retention_policy_days=None,
            )
            policy_a = await ai_persistence.get_organisation_policy(
                session, organisation_id=organisation_a.id
            )
            policy_b = await ai_persistence.get_organisation_policy(
                session, organisation_id=organisation_b.id
            )
            assert policy_a.enabled is True
            assert policy_b.enabled is False

            # A's reservation never appears in B's spend: B's budget is not
            # configured, but B's running-row count stays zero.
            port = AIPersistencePortImpl(session)
            await _reserve(
                port,
                organisation_id=organisation_a.id,
                request_id="req-org-a",
                user_id=actor.id,
                estimated_cost=Decimal("0.100000"),
            )
            rows_b = await _request_rows(session, organisation_b.id)
            assert rows_b == []
    finally:
        await engine.dispose()


# --- Retention / deletion sweep (v0.7 Scope §6.5 item 4) ---


async def _seed_output(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    request: AIRequestRecord,
    created_days_ago: int,
    output_reference: str | None = None,
) -> AIOutputRecord:
    output = AIOutputRecord(
        ai_request_id=request.id,
        organisation_id=organisation_id,
        output_json={"task": "document.classify"},
        output_reference=output_reference,
        output_digest="digest",
    )
    output.created_at = datetime.now(UTC) - timedelta(days=created_days_ago)
    session.add(output)
    await session.commit()
    return output


async def _seed_request(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    request_id: str,
    status: AIRequestStatus = AIRequestStatus.SUCCEEDED,
    created_days_ago: int = 0,
) -> AIRequestRecord:
    record = AIRequestRecord(
        organisation_id=organisation_id,
        user_id=None,
        request_id=request_id,
        task="document.classify",
        provider="fake",
        model="fake-model-document.classify",
        prompt_name="classify",
        prompt_version=1,
        routing_reason="first eligible configured model fake.document-classifier",
        status=status,
        cost=Decimal("0.000010"),
    )
    record.created_at = datetime.now(UTC) - timedelta(days=created_days_ago)
    session.add(record)
    await session.commit()
    return record


async def test_retention_sweep_deletes_expired_outputs_and_scratch_only(
    migrated_database: str,
) -> None:
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            actor = await _seed_actor(session)
            organisation = await _seed_organisation(session)
            await _seed_settings(session, organisation.id)
            await ai_persistence.update_ai_settings(
                session,
                actor=actor,
                organisation_id=organisation.id,
                enabled=True,
                allowed_provider_ids=[],
                allowed_model_ids=[],
                provider_override=None,
                model_override=None,
                monthly_budget=None,
                retention_policy_days=30,
            )
            request = await _seed_request(
                session, organisation_id=organisation.id, request_id="req-ret"
            )
            old_output = await _seed_output(
                session,
                organisation_id=organisation.id,
                request=request,
                created_days_ago=40,
                output_reference=ai_scratch_prefix(organisation.id) + "old-analyse.pdf",
            )

            # Storage: an old scratch object referenced by the expired output,
            # a fresh scratch object (in-flight work), and a keep-flow object
            # (feature-owned, must never be touched by the AI layer).
            storage = FakeObjectStorage(bucket="test-bucket")
            await storage.put(
                ai_scratch_prefix(organisation.id) + "old-analyse.pdf",
                b"old scratch",
                "application/pdf",
            )
            storage._objects[ai_scratch_prefix(organisation.id) + "old-analyse.pdf"].created_at = (  # type: ignore[reportPrivateUsage]
                datetime.now(UTC) - timedelta(days=40)
            )
            fresh_key = ai_scratch_prefix(organisation.id) + "fresh-analyse.pdf"
            await storage.put(fresh_key, b"fresh scratch", "application/pdf")
            keep_flow_key = f"organisations/{organisation.id}/documents/file/lease.pdf"
            await storage.put(keep_flow_key, b"keep flow", "application/pdf")

            summary = await ai_persistence.enforce_ai_retention(
                session, storage, now=datetime.now(UTC)
            )
            assert summary["organisations_purged"] == 1
            assert summary["outputs_deleted"] == 1
            assert summary["scratch_objects_deleted"] == 1  # the old object only

            remaining_output = await session.scalar(
                select(AIOutputRecord).where(AIOutputRecord.id == old_output.id)
            )
            assert remaining_output is None  # the expired row is purged
            assert (
                await storage.head_object(ai_scratch_prefix(organisation.id) + "old-analyse.pdf")
                is None
            )
            assert await storage.head_object(fresh_key) is not None  # fresh work kept
            assert await storage.head_object(keep_flow_key) is not None  # feature-owned kept

            actions = (
                await session.execute(
                    text(
                        "SELECT action FROM audit_events "
                        "WHERE action = :action AND resource_id = :oid"
                    ).bindparams(action=ACTION_AI_RETENTION_DELETED, oid=str(organisation.id))
                )
            ).all()
            assert len(actions) == 1
    finally:
        await engine.dispose()


async def test_retention_reconciles_stale_running_reservations(migrated_database: str) -> None:
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            actor = await _seed_actor(session)
            organisation = await _seed_organisation(session)
            await _seed_settings(session, organisation.id)
            await ai_persistence.update_ai_settings(
                session,
                actor=actor,
                organisation_id=organisation.id,
                enabled=True,
                allowed_provider_ids=[],
                allowed_model_ids=[],
                provider_override=None,
                model_override=None,
                monthly_budget=Decimal("0.500000"),
                retention_policy_days=30,
            )
            # A crashed execution: the running row is 48h old and never settled.
            stale = await _seed_request(
                session,
                organisation_id=organisation.id,
                request_id="req-crashed",
                status=AIRequestStatus.RUNNING,
                created_days_ago=2,
            )
            storage = FakeObjectStorage(bucket="test-bucket")
            summary = await ai_persistence.enforce_ai_retention(
                session, storage, now=datetime.now(UTC)
            )
            assert summary["stale_requests_reconciled"] == 1

            row = await session.scalar(
                select(AIRequestRecord).where(AIRequestRecord.id == stale.id)
            )
            assert row is not None
            assert row.status == AIRequestStatus.FAILED
            assert row.error_code == ai_persistence.ERROR_CODE_WORKER_CRASHED
            assert row.cost == Decimal("0.000010")  # reserved cost is never released
    finally:
        await engine.dispose()


# --- Cross-organisation isolation on request/output operations (BP §9) ---


async def test_reused_request_id_is_organisation_scoped(migrated_database: str) -> None:
    """A caller request id reused across organisations reserves a separate row
    in each tenant: the idempotency key is (organisation_id, request_id), so a
    reserve can never return another organisation's row."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            actor = await _seed_actor(session)
            org_a = await _seed_organisation(session)
            org_b = await _seed_organisation(session)
            await _seed_settings(session, org_a.id)
            await _seed_settings(session, org_b.id)
            port = AIPersistencePortImpl(session)
            id_a = await _reserve(
                port,
                organisation_id=org_a.id,
                request_id="req-shared",
                user_id=actor.id,
                estimated_cost=Decimal("0.000100"),
            )
            id_b = await _reserve(
                port,
                organisation_id=org_b.id,
                request_id="req-shared",
                user_id=actor.id,
                estimated_cost=Decimal("0.000100"),
            )
            assert id_a != id_b
            row_a = await session.scalar(select(AIRequestRecord).where(AIRequestRecord.id == id_a))
            row_b = await session.scalar(select(AIRequestRecord).where(AIRequestRecord.id == id_b))
            assert row_a is not None and row_a.organisation_id == org_a.id
            assert row_b is not None and row_b.organisation_id == org_b.id
    finally:
        await engine.dispose()


async def test_cross_org_settle_is_denied(migrated_database: str) -> None:
    """Settling another organisation's row id is indistinguishable from a
    missing row: the org-scoped lookup never mutates foreign data."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            from app.core.exceptions import NotFoundError

            actor = await _seed_actor(session)
            org_a = await _seed_organisation(session)
            org_b = await _seed_organisation(session)
            await _seed_settings(session, org_a.id)
            await _seed_settings(session, org_b.id)
            port = AIPersistencePortImpl(session)
            id_a = await _reserve(
                port,
                organisation_id=org_a.id,
                request_id="req-cross-settle",
                user_id=actor.id,
                estimated_cost=Decimal("0.000100"),
            )
            with pytest.raises(NotFoundError):
                await _settle(
                    port,
                    ai_request_id=id_a,
                    organisation_id=org_b.id,
                    user_id=actor.id,
                    status="succeeded",
                )
            # Org A's row is untouched and still running.
            row = await session.scalar(select(AIRequestRecord).where(AIRequestRecord.id == id_a))
            assert row is not None and row.status == AIRequestStatus.RUNNING
    finally:
        await engine.dispose()


async def test_cross_org_output_record_is_denied(migrated_database: str) -> None:
    """Recording an output under a foreign organisation fails: the output is
    written only for the owning organisation's row."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            from app.core.exceptions import NotFoundError

            actor = await _seed_actor(session)
            org_a = await _seed_organisation(session)
            org_b = await _seed_organisation(session)
            await _seed_settings(session, org_a.id)
            await _seed_settings(session, org_b.id)
            port = AIPersistencePortImpl(session)
            id_a = await _reserve(
                port,
                organisation_id=org_a.id,
                request_id="req-cross-output",
                user_id=actor.id,
                estimated_cost=Decimal("0.000100"),
            )
            with pytest.raises(NotFoundError):
                await port.settle(
                    ai_request_id=id_a,
                    organisation_id=org_b.id,
                    task="document.classify",
                    user_id=actor.id,
                    status="succeeded",
                    error_code=None,
                    usage=TokenUsage(input_tokens=10, output_tokens=5),
                    cost=CostEstimate(amount=Decimal("0.000020")),
                    latency_ms=30,
                    routing_provider="fake",
                    routing_model="fake-model-document.classify",
                    routing_prompt_name="classify",
                    routing_prompt_version=1,
                    routing_reason="first eligible configured model fake.document-classifier",
                    fallback_used=False,
                    region="",
                    output={"task": "document.classify"},
                )
            outputs = (
                await session.scalars(
                    select(AIOutputRecord).where(AIOutputRecord.organisation_id == org_a.id)
                )
            ).all()
            assert outputs == []
    finally:
        await engine.dispose()


# --- Atomic success/output persistence (BP §11) ---


async def test_output_failure_rolls_back_success_settlement(migrated_database: str) -> None:
    """Terminal success plus output/audit are one transaction: an output
    persistence failure rolls everything back, leaving the row running with no
    audit trail, so a success can never be durable without its output."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            from sqlalchemy.exc import DBAPIError

            actor = await _seed_actor(session)
            organisation = await _seed_organisation(session)
            await _seed_settings(session, organisation.id)
            port = AIPersistencePortImpl(session)
            ai_request_id = await _reserve(
                port,
                organisation_id=organisation.id,
                request_id="req-atomic",
                user_id=actor.id,
                estimated_cost=Decimal("0.000100"),
            )
            with pytest.raises(DBAPIError):
                await port.settle(
                    ai_request_id=ai_request_id,
                    organisation_id=organisation.id,
                    task="document.classify",
                    user_id=actor.id,
                    status="succeeded",
                    error_code=None,
                    usage=TokenUsage(input_tokens=10, output_tokens=5),
                    cost=CostEstimate(amount=Decimal("0.000020")),
                    latency_ms=30,
                    routing_provider="fake",
                    routing_model="fake-model-document.classify",
                    routing_prompt_name="classify",
                    routing_prompt_version=1,
                    routing_reason="first eligible configured model fake.document-classifier",
                    fallback_used=False,
                    region="",
                    output={"task": "document.classify"},
                    output_reference="x" * 2000,  # exceeds the 1024-char column
                    retain_content=True,
                )
            await session.rollback()
            # The row is still running, and no completion event was written.
            row = await session.scalar(
                select(AIRequestRecord).where(AIRequestRecord.id == ai_request_id)
            )
            assert row is not None and row.status == AIRequestStatus.RUNNING
            actions = (
                await session.execute(
                    text(
                        "SELECT action FROM audit_events "
                        "WHERE resource_type = 'ai_request' AND resource_id = 'req-atomic'"
                    )
                )
            ).all()
            assert actions == []
    finally:
        await engine.dispose()


# --- Per-attempt rows (v0.7 Scope §2) and concurrency ---


async def test_record_attempt_creates_and_reuses_second_row(migrated_database: str) -> None:
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            actor = await _seed_actor(session)
            organisation = await _seed_organisation(session)
            await _seed_settings(session, organisation.id)
            port = AIPersistencePortImpl(session)
            await _reserve(
                port,
                organisation_id=organisation.id,
                request_id="req-attempts",
                user_id=actor.id,
                estimated_cost=Decimal("0.000100"),
            )
            second = await _record_attempt(
                port,
                organisation_id=organisation.id,
                user_id=actor.id,
                request_id="req-attempts",
                attempt_number=2,
                estimated_cost=Decimal("0.000100"),
            )
            reused = await _record_attempt(
                port,
                organisation_id=organisation.id,
                user_id=actor.id,
                request_id="req-attempts",
                attempt_number=2,
                estimated_cost=Decimal("0.000100"),
            )
            assert second == reused
            rows = await _request_rows(session, organisation.id)
            assert len(rows) == 2
            assert sorted(row.attempt_number for row in rows) == [1, 2]
            second_row = next(row for row in rows if row.attempt_number == 2)
            assert second_row.cost == Decimal("0")  # covered by the first-row reservation
            assert second_row.estimated_cost == Decimal("0.000100")
    finally:
        await engine.dispose()


async def test_concurrent_duplicate_request_id_reservations_are_safe(
    migrated_database: str,
) -> None:
    """Two concurrent reservations with the same (org, execution id) both
    succeed and share one first-attempt row: the loser of the unique-constraint
    race falls back to the winner's row instead of erroring."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            actor = await _seed_actor(session)
            organisation = await _seed_organisation(session)
            await _seed_settings(session, organisation.id)
            await ai_persistence.update_ai_settings(
                session,
                actor=actor,
                organisation_id=organisation.id,
                enabled=True,
                allowed_provider_ids=[],
                allowed_model_ids=[],
                provider_override=None,
                model_override=None,
                # Exactly enough for one execution. The concurrent duplicate
                # must reuse that row after the lock, not fail a second budget
                # check against the winner's reservation.
                monthly_budget=Decimal("0.000300"),
                retention_policy_days=None,
            )

            async def _concurrent_reserve() -> tuple[uuid.UUID, bool]:
                async with session_factory() as other_session:
                    other_port = AIPersistencePortImpl(other_session)
                    reservation = await other_port.reserve(
                        organisation_id=organisation.id,
                        user_id=actor.id,
                        request_id="req-race",
                        task="document.classify",
                        provider="fake",
                        model="fake-model-document.classify",
                        prompt_name="classify",
                        prompt_version=1,
                        routing_reason="first eligible configured model fake.document-classifier",
                        fallback_used=False,
                        region="",
                        estimated_cost=Decimal("0.000100"),
                        execution_maximum_estimated_cost=Decimal("0.000300"),
                        input_reference=None,
                        input_digest=None,
                    )
                    return reservation.row_id, reservation.created

            reservations = await asyncio.gather(_concurrent_reserve(), _concurrent_reserve())
            assert reservations[0][0] == reservations[1][0]
            assert sorted(created for _, created in reservations) == [False, True]
            rows = await _request_rows(session, organisation.id)
            assert len(rows) == 1
            denials = (
                await session.execute(
                    text(
                        "SELECT action FROM audit_events "
                        "WHERE action = :action AND resource_id = 'req-race'"
                    ).bindparams(action=ACTION_AI_BUDGET_DENIED)
                )
            ).all()
            assert denials == []
    finally:
        await engine.dispose()


# --- Retention: stale reconciliation independent of retention policy ---


async def test_stale_reservations_reconciled_without_retention_policy(
    migrated_database: str,
) -> None:
    """An organisation without a retention policy must still have its crashed
    running reservations reconciled, or it would lose budget headroom forever."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            organisation = await _seed_organisation(session)
            # No settings row at all: no retention policy configured.
            stale = await _seed_request(
                session,
                organisation_id=organisation.id,
                request_id="req-crashed-nopolicy",
                status=AIRequestStatus.RUNNING,
                created_days_ago=2,
            )
            storage = FakeObjectStorage(bucket="test-bucket")
            summary = await ai_persistence.enforce_ai_retention(
                session, storage, now=datetime.now(UTC)
            )
            assert summary["stale_requests_reconciled"] == 1

            row = await session.scalar(
                select(AIRequestRecord).where(AIRequestRecord.id == stale.id)
            )
            assert row is not None
            assert row.status == AIRequestStatus.FAILED
            assert row.error_code == ai_persistence.ERROR_CODE_WORKER_CRASHED
            assert row.cost == Decimal("0.000010")  # reserved cost is never released
            actions = (
                await session.execute(
                    text(
                        "SELECT action FROM audit_events "
                        "WHERE action = :action AND resource_id = :oid"
                    ).bindparams(action=ACTION_AI_RETENTION_DELETED, oid=str(organisation.id))
                )
            ).all()
            assert len(actions) == 1
    finally:
        await engine.dispose()


async def test_scratch_sweep_pages_past_the_first_listing(migrated_database: str) -> None:
    """An expired scratch object beyond the first listing page is still swept:
    the sweep advances past every page instead of stranding keys behind fresh
    objects (v0.7 Scope §6.5 item 4)."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            actor = await _seed_actor(session)
            organisation = await _seed_organisation(session)
            await _seed_settings(session, organisation.id)
            await ai_persistence.update_ai_settings(
                session,
                actor=actor,
                organisation_id=organisation.id,
                enabled=True,
                allowed_provider_ids=[],
                allowed_model_ids=[],
                provider_override=None,
                model_override=None,
                monthly_budget=None,
                retention_policy_days=30,
            )
            storage = FakeObjectStorage(bucket="test-bucket")
            prefix = ai_scratch_prefix(organisation.id)
            now = datetime.now(UTC)
            # Fill a first page with fresh objects and place the expired object
            # just after them (lexicographically later than 1000 fresh keys).
            for index in range(ai_persistence.SCRATCH_SWEEP_PAGE_SIZE):
                fresh_key = f"{prefix}fresh-{index:05d}.pdf"
                await storage.put(fresh_key, b"fresh", "application/pdf")
            expired_key = f"{prefix}zz-expired.pdf"
            await storage.put(expired_key, b"expired", "application/pdf")
            storage._objects[expired_key].created_at = now - timedelta(days=40)  # type: ignore[reportPrivateUsage]
            for index in range(ai_persistence.SCRATCH_SWEEP_PAGE_SIZE):
                storage._objects[f"{prefix}fresh-{index:05d}.pdf"].created_at = now  # type: ignore[reportPrivateUsage]

            summary = await ai_persistence.enforce_ai_retention(session, storage, now=now)
            assert summary["scratch_objects_deleted"] == 1
            assert await storage.head_object(expired_key) is None
            assert await storage.head_object(f"{prefix}fresh-00000.pdf") is not None
    finally:
        await engine.dispose()


async def test_database_rejects_output_for_foreign_request_organisation(
    migrated_database: str,
) -> None:
    """The request/output tenant relationship is a database invariant: an
    output row whose organisation differs from its parent request's is
    rejected by the composite foreign key (BP §9)."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            from sqlalchemy.exc import DBAPIError

            actor = await _seed_actor(session)
            org_a = await _seed_organisation(session)
            org_b = await _seed_organisation(session)
            await _seed_settings(session, org_a.id)
            await _seed_settings(session, org_b.id)
            port = AIPersistencePortImpl(session)
            id_a = await _reserve(
                port,
                organisation_id=org_a.id,
                request_id="req-db-isolation",
                user_id=actor.id,
                estimated_cost=Decimal("0.000100"),
            )
            session.add(
                AIOutputRecord(
                    ai_request_id=id_a,
                    organisation_id=org_b.id,  # foreign organisation
                    output_json={"task": "document.classify"},
                )
            )
            with pytest.raises(DBAPIError):
                await session.commit()
    finally:
        await engine.dispose()
