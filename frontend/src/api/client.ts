import createClient, { type Middleware } from 'openapi-fetch'

import { useOrganisationStore } from '@/stores/organisation'
import { useSessionStore } from '@/stores/session'
import { signOut } from '@/features/auth/workos'

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
  window.location.assign('/login?error=session_invalid')
}

let handlingUnauthorized = false

async function handleUnauthorized(): Promise<void> {
  if (handlingUnauthorized) return
  handlingUnauthorized = true

  const session = useSessionStore()
  const organisation = useOrganisationStore()
  session.clearSession()
  organisation.setSelectedOrganisation(null)

  try {
    // Pinia is only this app's in-memory state. AuthKit persists its own
    // session, so clearing Pinia alone would immediately rehydrate the same
    // rejected token after the redirect and create an infinite login loop.
    await signOut()
  } catch {
    // Local app state is already cleared. The redirect must still happen when
    // WorkOS logout is unavailable, otherwise a rejected session strands the
    // user in a broken authenticated shell.
  } finally {
    redirectToLogin()
  }
}

const sessionMiddleware: Middleware = {
  onRequest({ request }) {
    const session = useSessionStore()
    const token = session.token
    if (token && !request.headers.has('Authorization')) {
      request.headers.set('Authorization', `Bearer ${token}`)
    }
    // Tenant context (v0.3 Scope §6.3, acceptance §5.6): the selected organisation
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
      await handleUnauthorized()
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
