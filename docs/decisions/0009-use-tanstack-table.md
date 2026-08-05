# ADR 0009: Use TanStack Table for Standard Tables

Status: Accepted

## Context

Every list screen in the template needs a table with consistent column rendering, alignment, and server-driven pagination bound to the standard envelope (`items`, `page`, `page_size`, `total`, blueprint §12). The blueprint's default for ordinary tables is "shadcn-vue + TanStack Table", with advanced grids explicitly deferred to a project-specific dedicated library (blueprint §16).

The table primitives (`table`, `table-header`, `table-body`, `table-row`, `table-head`, `table-cell`, `table-caption`, `table-footer`, `table-empty`) are vendored shadcn-vue components, owned application code. What is missing is the table logic layer: column definitions, row-model state, and page/index coordination for envelope-driven pagination.

## Options considered

- **Hand-rolled table component**: full control, but every application re-derives column configuration, sorting state, and pagination coordination that TanStack Table already provides and tests.
- **@tanstack/vue-table**: headless table toolkit (column defs, row model, sorting, pagination state) built for the framework in use; pairs directly with the vendored shadcn-vue table primitives, which take a plain `<table>` DOM contract.
- **Full component library grid (AG Grid, Handsontable)**: deferred by the blueprint to projects that genuinely need advanced grids; wrong default for ordinary tables.

## Decision

Use **@tanstack/vue-table** (with `@tanstack/vue-virtual` as its peer dependency, installed but not used until a table needs virtualization) as the table logic layer, composed inside an application-level `DataTable` component. The `DataTable` application component:

- accepts columns, row data, the pagination envelope (`page`, `pageSize`, `total`) and loading/error state as props;
- owns column defs and row rendering through TanStack Table;
- renders the vendored shadcn-vue table primitives with loading, empty and error states;
- emits page/page-size changes upward, so pagination stays wired to the parent's query state (TanStack Query owns fetching, blueprint §14).

## Consequences

- List screens get one consistent table: column rendering, alignment, pagination controls and state presentation never vary between modules.
- TanStack Table's tested row-model and pagination-index handling (0-based `pageIndex` mapped to the API's 1-based `page`) live in one place.
- `DataTable` stays intentionally small: sorting, filtering, row selection and virtualization are added only when a module actually needs them, per blueprint §16 (no speculative features).
- A new runtime dependency is introduced; it is small, headless, and already the blueprint's stated default. The peer dependency `@tanstack/vue-virtual` is added alongside so pnpm resolution stays clean even though it is not yet imported.

---
