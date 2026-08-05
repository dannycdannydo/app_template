import createClient from 'openapi-fetch'

import type { paths } from './generated/openapi'

/**
 * Typed OpenAPI client (blueprint §15).
 *
 * Generated types are the single source of truth for the HTTP surface; the
 * client is never pointed at hand-written interfaces. The base URL is taken
 * from Vite's dev-server proxy in development and from `VITE_API_BASE_URL`
 * elsewhere.
 */
export const client = createClient<paths>({
  baseUrl: import.meta.env.VITE_API_BASE_URL ?? '',
})
