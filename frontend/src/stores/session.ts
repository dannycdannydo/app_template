import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

/**
 * Session state (blueprint §14 client-state boundary).
 *
 * Holds the session token, the derived authenticated flag and the boot-restore
 * latch. Client state only: server data (user, memberships, roles) belongs to
 * TanStack Vue Query, never to this store.
 *
 * Boot-restore (Scope §6.3): the router guard must not decide between `/login`
 * and the shell until the WorkOS session has been restored on app boot, or an
 * authenticated reload would bounce to `/login`. `bootstrap()` in main.ts calls
 * `markBootRestored()` after restoring; the guard awaits `waitForBootRestore()`
 * so the first navigation is decided against the restored session. In tests the
 * latch is marked in setup, so the guard never hangs.
 */
export const useSessionStore = defineStore('session', () => {
  const token = ref<string | null>(null)
  const bootRestored = ref(false)

  let resolveBoot: (() => void) | null = null
  const bootPromise = new Promise<void>((resolve) => {
    resolveBoot = resolve
  })

  const isAuthenticated = computed(() => token.value !== null)

  function setSession(newToken: string) {
    token.value = newToken
  }

  function clearSession() {
    token.value = null
  }

  function markBootRestored() {
    if (bootRestored.value) return
    bootRestored.value = true
    resolveBoot?.()
  }

  async function waitForBootRestore(): Promise<void> {
    if (bootRestored.value) return
    await bootPromise
  }

  return {
    token,
    isAuthenticated,
    bootRestored,
    setSession,
    clearSession,
    markBootRestored,
    waitForBootRestore,
  }
})
