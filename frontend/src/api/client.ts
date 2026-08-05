import createClient, { type Middleware } from 'openapi-fetch'

import { useOrganisationStore } from '@/stores/organisation'
import { useSessionStore } from '@/stores/session'

import { normalizeErrorResponse } from './errors'
import type { paths } from './generated/openapi'

/**
 * Typed OpenAPI client (blueprint §15, §13).
 *
 * Generated types are the single source of truth for the HTTP surface; the
 * client is never pointed at hand-written interfaces. The base URL is taken
 * from Vite's dev-server proxy in development and from `VITE_API_BASE_URL`
 * elsewhere.
 *
 * A single middleware owns the two central cross-cutting concerns:
 *
 * - Bearer-token injection: the current session token (Pinia client state,
 *   blueprint §14) is attached to every request as `Authorization: Bearer`.
 * - Central `401`/error handling: a `401` clears the session store and
 *   redirects to `/login`; every other non-2xx response is normalized into the
 *   typed `ApiError` envelope (blueprint §13) so toasts and forms consume one
 *   error shape.
 */
export function redirectToLogin(): void {
  window.location.assign('/login')
}

const sessionMiddleware: Middleware = {
  onRequest({ request }) {
    const session = useSessionStore()
    const token = session.token
    if (token && !request.headers.has('Authorization')) {
      request.headers.set('Authorization', `Bearer ${token}`)
    }
    // Tenant context (Scope §6.3, acceptance §5.6): the selected organisation
    // is client state owned by Pinia and attached as X-Org-Id on every
    // request. The backend resolves the caller's membership from it.
    const organisation = useOrganisationStore()
    const organisationId = organisation.selectedOrganisationId
    if (organisationId && !request.headers.has('X-Org-Id')) {
      request.headers.set('X-Org-Id', organisationId)
    }
    return request
  },
  async onResponse({ response }) {
    if (response.status === 401) {
      const session = useSessionStore()
      session.clearSession()
      redirectToLogin()
    }
    if (!response.ok) {
      throw await normalizeErrorResponse(response)
    }
  },
}

export const client = createClient<paths>({
  baseUrl: import.meta.env.VITE_API_BASE_URL ?? '',
})
client.use(sessionMiddleware)
