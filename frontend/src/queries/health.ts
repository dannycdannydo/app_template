import { useQuery } from '@tanstack/vue-query'

import { client } from '@/api/client'

/**
 * Server-state composable for the health endpoint (blueprint §14, §15).
 *
 * All API calls live behind query composables; visual components only consume
 * query state and never touch the HTTP client directly.
 */
export function useHealthQuery() {
  return useQuery({
    queryKey: ['health'],
    queryFn: async () => {
      const { data, error } = await client.GET('/health')
      if (error || !data) {
        throw new Error('Health check failed')
      }
      return data
    },
    retry: false,
    refetchInterval: 30_000,
  })
}
