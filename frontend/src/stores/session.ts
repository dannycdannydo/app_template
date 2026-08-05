import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

/**
 * Session state (blueprint §14 client-state boundary).
 *
 * Holds the session token and the derived authenticated flag. Client state
 * only: server data (user, memberships, roles) belongs to TanStack Vue Query,
 * never to this store. The auth flow (WorkOS adapter, login/callback routes)
 * is added in Scope §6.2; the API client reads this store when attaching the
 * Bearer token and clears it on a central `401`.
 */
export const useSessionStore = defineStore('session', () => {
  const token = ref<string | null>(null)

  const isAuthenticated = computed(() => token.value !== null)

  function setSession(newToken: string) {
    token.value = newToken
  }

  function clearSession() {
    token.value = null
  }

  return { token, isAuthenticated, setSession, clearSession }
})
