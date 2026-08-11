"""AI persistence layer (v0.7 Scope §6.5, BP §9-§11, §27-§29).

This package owns the database-backed half of the AI platform contract:

- :class:`OrganisationAISettings` — one row per organisation, default off;
- :class:`AIRequestRecord` / :class:`AIOutputRecord` — the durable usage, cost
  and privacy-safe output records (references and digests, never attachment
  bytes);
- the :class:`AIPersistencePort` implementation that ``AIService`` calls at
  request time (policy load, budget reservation, settlement, output records);
- the platform-gated management API and the retention/deletion jobs.

The package deliberately lives under ``app/ai/`` (a platform package, not a
generic business module, ADR-0017) and imports provider SDKs nowhere.
"""
