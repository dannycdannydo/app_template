import { QueryClient } from '@tanstack/vue-query'

/**
 * Single query client shared by the app and by boot-time infrastructure
 * (organisation-switch invalidation, v0.3 Scope §6.4).
 *
 * Registered on the VueQueryPlugin in main.ts, so component composables
 * resolve the same instance through `useQueryClient()`. Composables set their
 * own per-query options (retry, staleness); the defaults are left untouched.
 */
export const queryClient = new QueryClient()
