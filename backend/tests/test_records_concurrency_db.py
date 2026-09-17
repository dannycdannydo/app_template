"""Real-database, two-session tests for plan P8 record integrity.

Plan P8 gives the representative records module optimistic concurrency,
bounded immutable history and database-enforced append-only audit tables. The
unit/request-flow tests never execute SQL, so the row-lock serialisation, the
conditional-update 409, revision reconstruction, actor-identity survival,
tenant scoping and the append-only trigger could silently regress.

These tests run the real migration and the real services against a reachable
PostgreSQL, using the same skip pattern as ``test_records_db.py``: migrated to
head up front, reverted to base afterwards. The stale-write races hold a real
``FOR UPDATE`` lock in one session and assert the competing service call is
still pending before the lock is released, so a green result is not timing
luck.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.exceptions import BadRequestError, ConflictError, NotFoundError
from app.core.feature_flags import FEATURE_RECORDS_DELETION
from app.modules.audit.models import AuditEvent
from app.modules.feature_flags.models import OrganisationFeature
from app.modules.organisations.models import Organisation
from app.modules.records import service
from app.modules.records.models import Record, RecordRevisionAction
from app.modules.records.queries import record_revisions_statement
from app.modules.users.models import User

BACKEND_ROOT = Path(__file__).resolve().parents[1]

BLOCKED_TIMEOUT_SECONDS = 0.25


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


def _unique() -> str:
    return uuid.uuid4().hex[:10]


async def _seed_organisation(session: AsyncSession, label: str = "records") -> Organisation:
    organisation = Organisation(name=f"{label.title()} {_unique()} Ltd")
    session.add(organisation)
    await session.commit()
    return organisation


async def _enable_records_deletion(session: AsyncSession, organisation_id: uuid.UUID) -> None:
    """Enable the platform-controlled destructive flag for one organisation."""
    session.add(
        OrganisationFeature(
            organisation_id=organisation_id,
            feature_key=FEATURE_RECORDS_DELETION,
            enabled=True,
        )
    )
    await session.commit()


async def _seed_actor(session: AsyncSession) -> User:
    unique = _unique()
    user = User(
        workos_user_id=f"records_actor_{unique}",
        email=f"records_actor_{unique}@example.com",
        name="Records Actor",
    )
    session.add(user)
    await session.commit()
    return user


async def test_two_session_update_serialises_on_the_row_lock(migrated_database: str) -> None:
    """A second conditional update waits for the first writer's row lock."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            organisation = await _seed_organisation(session)
            record = await service.create_record(
                session, organisation_id=organisation.id, title="Original", body="Body"
            )
            record_id = record.id
            organisation_id = organisation.id

        # Session A holds the row lock but has not committed; the service's
        # ``SELECT ... FOR UPDATE`` must block behind it.
        holder = session_factory()
        locked = await holder.scalar(select(Record).where(Record.id == record_id).with_for_update())
        assert locked is not None and locked.version == 1

        async def _competing_update() -> Record:
            async with session_factory() as session:
                return await service.update_record(
                    session,
                    organisation_id=organisation_id,
                    record_id=record_id,
                    expected_version=1,
                    title="Competing",
                    body=None,
                )

        competing = asyncio.create_task(_competing_update())
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(competing), timeout=BLOCKED_TIMEOUT_SECONDS)

        # The first writer commits version 2; the waiting update now acquires
        # the lock, sees the version moved and must conflict rather than write.
        locked.title = "Committed first"
        locked.version = 2
        await holder.commit()
        await holder.close()

        with pytest.raises(ConflictError) as excinfo:
            await competing
        assert excinfo.value.code == "record_version_conflict"

        async with session_factory() as session:
            fresh = await session.get(Record, record_id)
            assert fresh is not None
            assert fresh.title == "Committed first"
            assert fresh.version == 2
    finally:
        await engine.dispose()


async def test_two_session_delete_serialises_and_preserves_later_writer(
    migrated_database: str,
) -> None:
    """A competing delete waits on the row lock, then conflicts and removes nothing.

    This is the genuine two-session stale-delete proof P8 item 4 asks for, not
    a sequential version check: session A holds the row lock and commits a later
    writer's version 2, while the competing delete (holding version 1) blocks on
    the service's ``SELECT ... FOR UPDATE``. On release the delete must re-read
    the committed version, return ``record_version_conflict`` and leave the
    later writer's row (and its revision history) intact.
    """
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            organisation = await _seed_organisation(session)
            record = await service.create_record(
                session, organisation_id=organisation.id, title="Original", body="Body"
            )
            record_id = record.id
            organisation_id = organisation.id
            await _enable_records_deletion(session, organisation_id)

        # Session A holds the row lock but has not committed; the delete's
        # ``FOR UPDATE`` must block behind it.
        holder = session_factory()
        locked = await holder.scalar(select(Record).where(Record.id == record_id).with_for_update())
        assert locked is not None and locked.version == 1

        async def _competing_delete() -> None:
            async with session_factory() as session:
                await service.delete_record(
                    session,
                    organisation_id=organisation_id,
                    record_id=record_id,
                    expected_version=1,
                )

        competing = asyncio.create_task(_competing_delete())
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(competing), timeout=BLOCKED_TIMEOUT_SECONDS)

        # The later writer commits version 2; the waiting delete now acquires
        # the lock, sees the version moved and must conflict rather than remove
        # the row or append a stale delete revision.
        locked.title = "Committed later"
        locked.version = 2
        await holder.commit()
        await holder.close()

        with pytest.raises(ConflictError) as excinfo:
            await competing
        assert excinfo.value.code == "record_version_conflict"

        async with session_factory() as session:
            fresh = await session.get(Record, record_id)
            assert fresh is not None
            assert fresh.title == "Committed later"
            assert fresh.version == 2
            revisions = await service.list_record_revisions(
                session, organisation_id=organisation_id, record_id=record_id
            )
        assert [revision.action for revision in revisions] == [RecordRevisionAction.CREATED]
    finally:
        await engine.dispose()


async def test_sequential_stale_update_and_delete_each_conflict(
    migrated_database: str,
) -> None:
    """A caller holding an old version cannot update or delete a newer record."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            organisation = await _seed_organisation(session)
            record = await service.create_record(
                session, organisation_id=organisation.id, title="v1", body=""
            )
            record_id = record.id
            organisation_id = organisation.id

        async with session_factory() as session:
            await service.update_record(
                session,
                organisation_id=organisation_id,
                record_id=record_id,
                expected_version=1,
                title="v2",
                body=None,
            )

        async with session_factory() as session:
            with pytest.raises(ConflictError) as update_exc:
                await service.update_record(
                    session,
                    organisation_id=organisation_id,
                    record_id=record_id,
                    expected_version=1,
                    title="stale",
                    body=None,
                )
            assert update_exc.value.code == "record_version_conflict"

        async with session_factory() as session:
            with pytest.raises(ConflictError) as delete_exc:
                await service.delete_record(
                    session,
                    organisation_id=organisation_id,
                    record_id=record_id,
                    expected_version=1,
                )
            assert delete_exc.value.code == "record_version_conflict"

        async with session_factory() as session:
            fresh = await session.get(Record, record_id)
            assert fresh is not None and fresh.version == 2 and fresh.title == "v2"
    finally:
        await engine.dispose()


async def test_revisions_reconstruct_history_and_survive_hard_delete(
    migrated_database: str,
) -> None:
    """Create/update/delete leave an immutable, reconstructable history."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            organisation = await _seed_organisation(session)
            actor = await _seed_actor(session)
            record = await service.create_record(
                session,
                organisation_id=organisation.id,
                title="Draft",
                body="First body",
                actor_user_id=actor.id,
            )
            record_id = record.id
            organisation_id = organisation.id
            await _enable_records_deletion(session, organisation_id)

        async with session_factory() as session:
            await service.update_record(
                session,
                organisation_id=organisation_id,
                record_id=record_id,
                expected_version=1,
                title=None,
                body="Approved body",
                actor_user_id=actor.id,
            )

        async with session_factory() as session:
            await service.delete_record(
                session,
                organisation_id=organisation_id,
                record_id=record_id,
                expected_version=2,
                actor_user_id=actor.id,
            )

        async with session_factory() as session:
            assert await session.get(Record, record_id) is None
            revisions = await service.list_record_revisions(
                session, organisation_id=organisation_id, record_id=record_id
            )

        assert [revision.action for revision in revisions] == [
            RecordRevisionAction.CREATED,
            RecordRevisionAction.UPDATED,
            RecordRevisionAction.DELETED,
        ]
        assert [revision.version for revision in revisions] == [1, 2, 2]
        # The last snapshot reconstructs the exact deleted state, even though
        # the live row is gone.
        deleted_snapshot = revisions[-1]
        assert deleted_snapshot.title == "Draft"
        assert deleted_snapshot.body == "Approved body"
        assert deleted_snapshot.actor_user_id == actor.id

        # The audit trail carries only the safe version, never record content.
        async with session_factory() as session:
            events = (
                await session.scalars(
                    select(AuditEvent)
                    .where(
                        AuditEvent.resource_id == str(record_id),
                        AuditEvent.resource_type == "record",
                    )
                    .order_by(AuditEvent.created_at, AuditEvent.id)
                )
            ).all()
        assert [event.action for event in events] == [
            "record.created",
            "record.updated",
            "record.deleted",
        ]
        assert events[-1].event_metadata["version"] == 2
        for event in events:
            assert "Draft" not in str(event.event_metadata)
            assert "Approved body" not in str(event.event_metadata)
    finally:
        await engine.dispose()


async def test_hard_delete_is_permanent_and_restore_is_rejected(
    migrated_database: str,
) -> None:
    """Deletion is permanent: the service never resurrects a deleted record.

    P8 item 3 makes hard-delete/restore semantics explicit and item 4 requires
    restoration evidence. The chosen contract is a permanent hard delete with
    no restore path: the immutable revisions remain readable for
    reconstruction, but ``get``/``update`` on the deleted id stay 404 and the
    explicit :func:`service.restore_record` rejection returns the stable
    ``record_restore_unsupported`` code. A new create is a fresh identity, not
    a silent restoration of the deleted row.
    """
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            organisation = await _seed_organisation(session)
            record = await service.create_record(
                session, organisation_id=organisation.id, title="Gone", body="soon"
            )
            record_id = record.id
            organisation_id = organisation.id
            await _enable_records_deletion(session, organisation_id)

        async with session_factory() as session:
            await service.delete_record(
                session,
                organisation_id=organisation_id,
                record_id=record_id,
                expected_version=1,
            )

        async with session_factory() as session:
            assert await session.get(Record, record_id) is None
            with pytest.raises(NotFoundError):
                await service.get_record(
                    session, organisation_id=organisation_id, record_id=record_id
                )
            with pytest.raises(NotFoundError):
                await service.update_record(
                    session,
                    organisation_id=organisation_id,
                    record_id=record_id,
                    expected_version=1,
                    title="Back from the dead",
                    body=None,
                )
            with pytest.raises(BadRequestError) as excinfo:
                await service.restore_record(
                    session, organisation_id=organisation_id, record_id=record_id
                )
            assert excinfo.value.code == "record_restore_unsupported"

        # A subsequent create is a brand-new identity at version 1; the deleted
        # record is not silently restored under its old id.
        async with session_factory() as session:
            replacement = await service.create_record(
                session, organisation_id=organisation_id, title="Fresh", body=""
            )
            assert replacement.id != record_id
            assert replacement.version == 1
            history = await service.list_record_revisions(
                session, organisation_id=organisation_id, record_id=record_id
            )
        assert [revision.action for revision in history] == [
            RecordRevisionAction.CREATED,
            RecordRevisionAction.DELETED,
        ]
    finally:
        await engine.dispose()


async def test_actor_identity_survives_user_deletion(
    migrated_database: str,
) -> None:
    """Deleting the actor keeps the revision/audit provenance (ADR-0020)."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            organisation = await _seed_organisation(session)
            actor = await _seed_actor(session)
            record = await service.create_record(
                session,
                organisation_id=organisation.id,
                title="Provenance",
                body="",
                actor_user_id=actor.id,
            )
            organisation_id = organisation.id
            actor_id = actor.id
            record_id = record.id

        # Hard-delete the actor directly, bypassing the service, to prove the
        # ledger does not depend on a foreign key surviving.
        async with session_factory() as session:
            user = await session.get(User, actor_id)
            assert user is not None
            await session.delete(user)
            await session.commit()

        async with session_factory() as session:
            assert await session.get(User, actor_id) is None
            revisions = await service.list_record_revisions(
                session, organisation_id=organisation_id, record_id=record_id
            )
            assert [revision.actor_user_id for revision in revisions] == [actor_id]
            events = (
                await session.scalars(
                    select(AuditEvent).where(AuditEvent.resource_id == str(record_id))
                )
            ).all()
        assert [event.actor_user_id for event in events] == [actor_id]
    finally:
        await engine.dispose()


async def test_organisation_identity_survives_organisation_deletion(
    migrated_database: str,
) -> None:
    """Deleting the organisation keeps tenant provenance on both ledgers (AC18).

    The records FK cascades the live row away, and ADR-0020 claims
    organisation deletion cannot touch either ledger because both
    ``record_revisions.organisation_id`` and ``audit_events.organisation_id``
    are opaque non-foreign-key UUIDs. This exercises the actual deletion rather
    than asserting the schema shape.
    """
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            organisation = await _seed_organisation(session)
            actor = await _seed_actor(session)
            record = await service.create_record(
                session,
                organisation_id=organisation.id,
                title="Org provenance",
                body="",
                actor_user_id=actor.id,
            )
            organisation_id = organisation.id
            record_id = record.id

        # Hard-delete the organisation with a Core statement (no ORM cascade
        # side effects); the records FK cascades the live row away, but the
        # opaque revision/audit identities must not be touched.
        async with session_factory() as session:
            await session.execute(delete(Organisation).where(Organisation.id == organisation_id))
            await session.commit()

        async with session_factory() as session:
            assert await session.get(Organisation, organisation_id) is None
            assert await session.get(Record, record_id) is None
            revisions = await service.list_record_revisions(
                session, organisation_id=organisation_id, record_id=record_id
            )
            assert [revision.organisation_id for revision in revisions] == [organisation_id]
            events = (
                await session.scalars(
                    select(AuditEvent).where(AuditEvent.resource_id == str(record_id))
                )
            ).all()
        assert [event.organisation_id for event in events] == [organisation_id]
    finally:
        await engine.dispose()


async def test_revision_history_is_tenant_scoped(migrated_database: str) -> None:
    """A revision read never crosses organisations."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            org_a = await _seed_organisation(session, label="tenant_a")
            org_b = await _seed_organisation(session, label="tenant_b")
            record_a = await service.create_record(
                session, organisation_id=org_a.id, title="A", body=""
            )
            record_b = await service.create_record(
                session, organisation_id=org_b.id, title="B", body=""
            )

        async with session_factory() as session:
            cross = await service.list_record_revisions(
                session, organisation_id=org_a.id, record_id=record_b.id
            )
            assert cross == []
            own = await service.list_record_revisions(
                session, organisation_id=org_a.id, record_id=record_a.id
            )
            assert len(own) == 1 and own[0].title == "A"
            scoped = (
                await session.scalars(record_revisions_statement(organisation_id=org_b.id))
            ).all()
            assert {revision.record_id for revision in scoped} == {record_b.id}
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("table", "statement"),
    [
        ("audit_events", "UPDATE audit_events SET action = 'tampered'"),
        ("audit_events", "DELETE FROM audit_events"),
        ("audit_events", "TRUNCATE audit_events"),
        ("record_revisions", "UPDATE record_revisions SET title = 'tampered'"),
        ("record_revisions", "DELETE FROM record_revisions"),
        ("record_revisions", "TRUNCATE record_revisions"),
    ],
)
async def test_append_only_tables_reject_mutation(
    migrated_database: str, table: str, statement: str
) -> None:
    """The database boundary, not just the absent API, enforces append-only.

    ``TRUNCATE`` is included because it fires no row-level trigger: only the
    statement-level ``BEFORE TRUNCATE`` trigger stops a table-owning role from
    emptying the ledger, and the application connects as that owner.
    """
    engine, session_factory = _session_factory(migrated_database)
    try:
        # Seed at least one row on each table (the create writes both a
        # revision and an audit event) so the denial is proven against a
        # non-empty ledger, not merely an empty table.
        async with session_factory() as session:
            organisation = await _seed_organisation(session)
            await service.create_record(
                session, organisation_id=organisation.id, title="Immutable", body=""
            )

        async with session_factory() as session:
            with pytest.raises(DBAPIError) as excinfo:
                await session.execute(text(statement))
            assert "append-only" in str(excinfo.value)
            await session.rollback()
    finally:
        await engine.dispose()
