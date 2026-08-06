# Frontend

Vue 3 + TypeScript + Vite application shell. See `frontend/` layout in
`Internal_Custom_Application_Starter_Architecture_v2.md` §14.

- `src/api/` — generated OpenAPI types and the typed client wrapper
- `src/components/ui/` — shadcn-vue primitives (button, card, form, input,
  label, sonner, table, textarea)
- `src/components/application/` — reusable application components built on the
  primitives (`DataTable`, `RecordForm`, `UserMenu`, `OrganisationSelector`, `SidebarNav`)
- `src/queries/` — TanStack Vue Query server-state composables
- `src/stores/` — Pinia client state (UI preferences, layout state)
- `src/lib/` — shared helpers (`toast.ts` maps the API error envelope to toasts)
- `src/layouts/`, `src/views/`, `src/router/` — application shell

## Commands

| Command           | Purpose                     |
| ----------------- | --------------------------- |
| `pnpm dev`        | Start the Vite dev server   |
| `pnpm type-check` | `vue-tsc` strict typecheck  |
| `pnpm lint`       | ESLint + oxlint             |
| `pnpm test:unit`  | Vitest unit tests           |
| `pnpm test:e2e`   | Playwright end-to-end tests |
| `pnpm build`      | Production build            |
