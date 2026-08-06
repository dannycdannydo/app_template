# ADR 0010: Use vee-validate with zod, and vue-sonner, for Forms and Toasts

Status: Accepted

## Context

v0.3 Scope §6.6 needs a standard form and a standard feedback surface: inline
field-validation errors, API errors from the standard envelope (`code`,
`message`, `details`, `request_id`, blueprint §13) surfaced as toasts, and
success feedback after mutations. The vendored shadcn-vue component set
(blueprint §16: "reusable application components above raw UI primitives",
shadcn-vue as the component source, ADR 0005) defines the building blocks:

- shadcn-vue's `form` component is built on **vee-validate** with a **zod**
  schema adapter (`@vee-validate/zod`) and **zod** for the schema definitions;
- shadcn-vue's documented toast component is **vue-sonner**.

Both are new runtime dependencies, and the repo rule is that no dependency is
added without documentation (AGENTS.md; ADR 0005 and 0009 set the precedent).

## Options considered

**Forms**

- **vee-validate + @vee-validate/zod + zod**: the exact stack the vendored
  shadcn-vue form primitives are built on (`Field`, `Form`, `ErrorMessage`,
  `useFormField` all come from vee-validate). Using it means the primitives are
  copied verbatim from the shadcn-vue registry with no rework, and the schema
  language (zod) is a dependency we already take via `@vee-validate/zod`.
- **zod alone with hand-rolled validation wiring**: less dependency surface,
  but every form then re-derives validation state, touched/dirty meta and the
  accessibility wiring the form primitives already provide, which is exactly
  the "do not build custom form logic without need" rule in blueprint §16.
- **A component library form (e.g. FormKit, VeeValidate alternative)**: heavier
  and outside the shadcn-vue conventions the template commits to.

**Toasts**

- **vue-sonner**: shadcn-vue's documented toast integration; the `Toaster`
  component is vendored from the registry and styled with the existing design
  tokens (`--popover`, `--border`, `--radius`). Server-state errors and success
  messages are one consistent surface.
- **Hand-rolled toast/dialog system**: explicitly discouraged by blueprint §16
  ("do not build custom dialog, menu or focus-management logic without need").

## Decision

Add **vee-validate** (+ `@vee-validate/zod`) with **zod** for form validation
and **vue-sonner** for toasts, as the standard form/feedback stack.

- `src/components/ui/form/` vendored from the shadcn-vue registry
  (`Form`, `FormField`, `FormItem`, `FormLabel`, `FormControl`,
  `FormDescription`, `FormMessage`, `useFormField`) — the reusable field error
  presentation is the `FormMessage` primitive wired through `useFormField`'s
  id/aria plumbing.
- `src/components/ui/sonner/` vendored `Toaster`, mounted once in `App.vue`
  (root layout), with `vue-sonner/style.css` imported there.
- `src/lib/toast.ts` is the single mapping from the API error envelope to a
  toast: message, bulleted `details` (field-level) and `request_id` for support
  correlation (blueprint §13). Forms and mutations call
  `showApiErrorToast(error, { title })` / `showSuccessToast(message)`.
- Form schemas mirror backend validation (e.g. the records schema: title
  1..255, body <= 100,000) so inline errors match what the server would reject,
  and the API remains the enforcement point.
- zod is pinned to v3 (`^3.25.76`) because `@vee-validate/zod@4.15.1` requires
  `zod ^3.24.0`; zod 4 is not yet supported by the adapter.

## Consequences

- Every edit screen shares one form pattern: zod schema, inline field errors
  through the vendored primitives, API failures as envelope-mapped toasts,
  success feedback and navigation to the list. `RecordForm` in
  `src/components/application/` is the first consumer and proves the flow for
  the records module.
- The form/toast logic is tested once against real primitives (Vitest renders
  the `Toaster` next to the form and asserts on the DOM), so later modules do
  not re-derive error-handling behavior.
- Two runtime dependencies added (vee-validate, vue-sonner) plus their schema
  adapter and zod; all are small, headless and the documented shadcn-vue stack.
- If the project later switches form or toast libraries, the swap is contained:
  `src/components/ui/form/`, `src/components/ui/sonner/` and `src/lib/toast.ts`.
