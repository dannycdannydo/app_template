# ADR 0002: Use SQLAlchemy 2 and Pydantic 2

Status: Accepted

## Context

The backend needs an ORM for persistence, a validation/serialisation layer for API schemas, and migrations. SQLAlchemy 2 and Pydantic 2 are both mature and broadly standard in the FastAPI ecosystem.

## Options considered

- **SQLAlchemy 2 + Pydantic 2**: industry standard; SQLAlchemy 2's typed ORM maps cleanly to Python types; Pydantic 2 (Rust core) is fast and is FastAPI's native validation layer.
- **SQLModel**: convenience of a single model class for ORM and schemas, but it couples persistence to API validation, which conflicts with the template's rule that ORM models are never API request models.
- **Tortoise ORM**: lighter but less mature for complex queries and typed patterns.
- **SQLAlchemy 1.x style + Pydantic 1**: legacy; Pydantic 2 changed the API surface and SQLAlchemy 2 modernised the ORM.

## Decision

Use **SQLAlchemy 2** for persistence models and **Pydantic 2** for API and service schemas, with **Alembic** for migrations. SQLModel is not used. ORM models and API schemas stay separate layers by design.

## Consequences

- Persistence concerns (tables, relationships, indexes, constraints) live in SQLAlchemy models; API and service contracts live in Pydantic schemas.
- Two explicit layers must be maintained, with deliberate conversion between them in services/routers.
- Typed ORM models give strong Pyright/mypy coverage.
- Migrations are schema-driven through Alembic, not auto-generated from model code at runtime.

---
