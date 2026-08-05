import { useQuery } from '@tanstack/vue-query'

import { client } from '@/api/client'

/**
 * Server-state composable for the current user (blueprint §14, §15, Scope §6.3).
 *
 * Drives the user menu (name, email) and the organisation selector
 * (memberships). All API calls live behind query composables; visual
 * components only consume query state and never touch the HTTP client
 * directly. Errors surface as the typed client error (blueprint §13).
 */
export function useMeQuery() {
  return useQuery({
    queryKey: ['me'],
    queryFn: async () => {
      const { data, error } = await client.GET('/api/v1/me')
      if (error) throw error
      if (!data) throw new Error('Empty /me response')
      return data
    },
    retry: false,
    staleTime: 30_000,
  })
}
