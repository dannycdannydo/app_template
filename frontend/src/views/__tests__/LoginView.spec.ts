import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Router } from 'vue-router'
import { createMemoryHistory, createRouter } from 'vue-router'

vi.mock('@/features/auth/workos', () => ({
  startLogin: vi.fn<() => Promise<void>>(),
}))

import { startLogin } from '@/features/auth/workos'
import LoginView from '@/views/LoginView.vue'

const startLoginMock = vi.mocked(startLogin)

async function mountLogin(query: Record<string, string> = {}): Promise<{
  wrapper: VueWrapper
  router: Router
}> {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/login', name: 'login', component: LoginView }],
  })
  await router.push({ path: '/login', query })
  await router.isReady()
  const wrapper = mount(LoginView, { global: { plugins: [router] } })
  return { wrapper, router }
}

describe('LoginView', () => {
  beforeEach(() => {
    startLoginMock.mockReset()
    startLoginMock.mockResolvedValue(undefined)
  })

  it('renders a WorkOS entry point and never collects identity fields', async () => {
    const { wrapper } = await mountLogin()

    expect(wrapper.text()).toContain('Continue with WorkOS')
    expect(wrapper.findAll('input')).toHaveLength(0)
    expect(wrapper.find('input[type="email"]').exists()).toBe(false)
    expect(wrapper.find('input[type="password"]').exists()).toBe(false)
  })

  it('starts the WorkOS flow when the button is clicked', async () => {
    const { wrapper } = await mountLogin()

    await wrapper.find('button').trigger('click')

    expect(startLoginMock).toHaveBeenCalledOnce()
  })

  it('shows the adapter error when the flow cannot start', async () => {
    startLoginMock.mockRejectedValueOnce(new Error('VITE_WORKOS_CLIENT_ID is not configured.'))
    const { wrapper } = await mountLogin()

    await wrapper.find('button').trigger('click')
    await flushPromises()

    const error = wrapper.find('[data-testid="login-error"]')
    expect(error.exists()).toBe(true)
    expect(error.text()).toContain('VITE_WORKOS_CLIENT_ID')
  })

  it('shows a message when WorkOS reported a denied flow', async () => {
    const { wrapper } = await mountLogin({ error: 'access_denied' })

    expect(wrapper.find('[data-testid="login-error"]').text()).toContain(
      'Sign-in was not completed',
    )
  })

  it('falls back to a generic message for unknown error codes', async () => {
    const { wrapper } = await mountLogin({ error: 'unexpected_code' })

    expect(wrapper.find('[data-testid="login-error"]').text()).toContain('Sign-in failed')
  })
})
