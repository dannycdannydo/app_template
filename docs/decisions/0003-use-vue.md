# ADR 0003: Use Vue 3 for the Frontend

Status: Accepted

## Context

The frontend needs a componentised SPA framework with a large ecosystem, good TypeScript support, and a low learning curve for a mixed team. It must integrate cleanly with Tailwind and a headless component library.

## Options considered

- **Vue 3 (Composition API)**: approachable, excellent TypeScript support, single-file components, strong ecosystem (Router, Pinia, TanStack Query), and a gentle upgrade path.
- **React**: largest ecosystem, but heavier idioms (hooks, context) and more boilerplate for equivalent features; no single canonical router/state choice.
- **Svelte/SvelteKit**: smaller API surface, but a smaller ecosystem and less enterprise adoption for this team profile.
- **Angular**: batteries included but verbose and opinionated; slower to adopt for small feature teams.

## Decision

Use **Vue 3** with the Composition API, TypeScript, Vue Router, Pinia, and TanStack Vue Query. shadcn-vue provides the component layer (see ADR 0005).

## Consequences

- Single-file components and a generated typed API client keep feature work fast and type-safe.
- The team must maintain Vue 3 idioms (Composition API, script setup) consistently across the template.
- Frontend API types are generated from the OpenAPI contract, never hand-written.

---
