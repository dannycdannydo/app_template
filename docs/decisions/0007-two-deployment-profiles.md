# ADR 0007: Support Two Deployment Profiles

Status: Accepted

## Context

The template targets UK SME/corporate clients with different operational budgets: a low-cost self-managed profile and a fully managed cloud profile. The application code and container image must remain identical across both.

## Options considered

- **Hybrid VPS only**: lowest cost, but no managed option for clients that require Azure.
- **Fully managed only (Azure)**: enterprise-friendly, but overspending for small clients and non-Azure shops.
- **Both, shared image**: a single immutable backend image (API and worker) deployed to a VPS (Caddy, Vue static assets, PostgreSQL/object storage/WorkOS as external services) or to a managed container platform (Azure Container Apps, ECS/Fargate, Cloud Run).

## Decision

Support **two deployment profiles** in the template (blueprint §35):

1. **Hybrid VPS**: Caddy, Vue static frontend, FastAPI, Dramatiq worker, Redis on the VPS; managed PostgreSQL, object storage, WorkOS, email, and monitoring externally.
2. **Fully managed**: containers on a managed platform with managed PostgreSQL, Redis, object storage, static frontend/CDN, and monitoring.

The same immutable backend image is used everywhere; provider-specific infrastructure files live under `deploy/` (`hybrid-vps/`, `managed/`). The starter ships one complete managed reference deployment, likely Azure, and adds AWS/GCP when a real project requires them.

## Consequences

- Deployment is a configuration exercise, not a code fork.
- The template must keep the two profiles documented and validated so neither rots.
- Hybrid VPS mandates operational protections (firewall, SSH keys only, non-public Redis, backups, monitoring) that are documented in `SECURITY.md` and blueprint §35.1.

---
