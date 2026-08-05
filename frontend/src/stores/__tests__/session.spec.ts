import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'

import { useSessionStore } from '@/stores/session'

describe('session store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('starts unauthenticated with no token', () => {
    const session = useSessionStore()
    expect(session.isAuthenticated).toBe(false)
    expect(session.token).toBeNull()
  })

  it('becomes authenticated once a token is set', () => {
    const session = useSessionStore()
    session.setSession('token-123')
    expect(session.token).toBe('token-123')
    expect(session.isAuthenticated).toBe(true)
  })

  it('clears the token and returns to the unauthenticated state', () => {
    const session = useSessionStore()
    session.setSession('token-123')
    session.clearSession()
    expect(session.token).toBeNull()
    expect(session.isAuthenticated).toBe(false)
  })
})
