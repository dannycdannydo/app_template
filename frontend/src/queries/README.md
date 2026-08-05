# Query Layer Conventions

Server state is owned by TanStack Vue Query (blueprint §14): fetching, caching,
pagination, refetching, mutations, loading/error state and invalidation.
Pinia holds client state only (sidebar, selected organisation) and never
becomes a backend-data cache. Components consume query composables and never
touch the HTTP client directly (blueprint §15 flow: generated client → query
composables → components).

## Composables

| Composable                                                                        | Scope                               | Source                   |
| --------------------------------------------------------------------------------- | ----------------------------------- | ------------------------ |
| `useMeQuery`                                                                      | global identity (`/api/v1/me`)      | `src/queries/me.ts`      |
| `useHealthQuery`                                                                  | health probe (`/health`)            | `src/queries/health.ts`  |
| `useRecordsQuery(params)`                                                         | org-scoped paginated list           | `src/queries/records.ts` |
| `useRecordQuery(recordId)`                                                        | org-scoped detail                   | `src/queries/records.ts` |
| `useCreateRecordMutation` / `useUpdateRecordMutation` / `useDeleteRecordMutation` | org-scoped writes with invalidation | `src/queries/records.ts` |

## Query-key convention

Keys are per-organisation. Every org-scoped key starts with
`['organisations', <orgId>]`:

```text
['organisations', <orgId>, 'records', 'list', { page, pageSize }]
['organisations', <orgId>, 'records', 'detail', <recordId>]
```

Rules:

- The `['organisations']` root is reserved for the invalidation predicate in
  `src/queries/organisationInvalidation.ts` and is never used as an active or
  fetching query key; disabled queries without a selected organisation
  register under `['organisations', null]` as a placeholder.
- A new org-scoped domain must keep its keys under
  `['organisations', <orgId>, <domain>, …]` so switching the selected
  organisation addresses a different cache partition and the boot-time
  invalidator covers it automatically.
- Global (non-org-scoped) keys are short literals: `['me']`, `['health']`.
- List keys carry the normalized params object as the final segment, so
  pagination changes address distinct cache entries.

## Organisation-switch invalidation

`installOrganisationSwitchInvalidation()` (installed once in `main.ts`)
watches the selected organisation in the Pinia store and invalidates the whole
org-scoped subtree on change: active queries refetch immediately and cached
entries from other organisations are marked stale.

## Mapping to the API conventions (blueprint §12)

Composable parameters are camelCase; the snake_case API query parameters are
produced inside the composable, in one place. Today the records API accepts
only `page` and `page_size`:

```text
useRecordsQuery({ page: 2, pageSize: 25 }) → ?page=2&page_size=25
```

The API returns the pagination envelope `{ items, page, page_size, total }`
which is passed through to components unchanged. When the backend adds filter
or sort fields, extend `RecordsListParams` with `search`, `status`, `sort`
etc. following blueprint §12 (`?search=…&status=…&sort=-created_at`) — only
fields the API actually accepts may be sent.
