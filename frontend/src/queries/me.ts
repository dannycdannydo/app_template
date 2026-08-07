import { useQuery } from '@tanstack/vue-query'

import { client } from '@/api/client'

/**
 * Shared `/me` query definition (blueprint §14, §15, v0.3 Scope §6.3).
 *
 * One source of truth for the `GET /api/v1/me` request, consumed by both
 * `useMeQuery` (the server-state composable) and the `requiresPlatformAdmin`
 * router guard (Scope §6.9). Both read the same `['me']` cache entry, so the
 * guard never issues a second HTTP call once the payload is cached. Errors
 * surface as the typed client error (blueprint §13).
 */
export const meQueryOptions = {
  queryKey: ['me'] as const,
  queryFn: async () => {
    const { data, error } = await client.GET('/api/v1/me')
    if (error) throw error
    if (!data) throw new Error('Empty /me response')
    return data
  },
  retry: false,
  staleTime: 30_000,
}

/**
 * Server-state composable for the current user (blueprint §14, §15, v0.3 Scope §6.3).
 *
 * Drives the user menu (name, email) and the organisation selector
 * (memberships). All API calls live behind query composables; visual
 * components only consume query state and never touch the HTTP client
 * directly.
 */
export function useMeQuery() {
  return useQuery(meQueryOptions)
}
