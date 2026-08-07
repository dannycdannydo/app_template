import { VueQueryPlugin } from '@tanstack/vue-query'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import PlatformInviteUserView from '@/views/PlatformInviteUserView.vue'
import { queryClient } from '@/queries/queryClient'

vi.mock('@/api/client', () => ({
  client: {
    POST: vi.fn<PostSignature>(),
  },
}))

type PostSignature = (url: string, init?: { params?: unknown; body?: unknown }) => Promise<unknown>

import { client } from '@/api/client'

const postMock = vi.mocked(client.POST as unknown as PostSignature)

const ORGANISATION_ID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'

/**
 * Platform invite form tests (Scope §6.5/§6.9, acceptance §5.6).
 *
 * The form posts an `InvitationCreate` (email + role) through the generated
 * client and navigates back to the organisation detail on success; the
 * validation schema mirrors the backend (email required + well-formed, role
 * required). No membership is expected — invitation only, login-time linking
 * creates the membership later.
 */
function buildRouter() {
  const push = vi.fn<() => void>()
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      {
        path: '/platform/organisations/:organisationId',
        name: 'platform-organisation-detail',
        component: { template: '<div>detail</div>' },
      },
    ],
  })
  router.push = push as unknown as typeof router.push
  return router
}

async function flushPromises(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0))
}

async function fillAndSubmit(wrapper: ReturnType<typeof mount>): Promise<void> {
  await wrapper.find('input[type="email"]').setValue('invitee@example.com')
  await wrapper.find('select').setValue('manager')
  await wrapper.find('form').trigger('submit')
  await flushPromises()
}

describe('PlatformInviteUserView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    queryClient.clear()
    postMock.mockReset()
  })

  it('sends the invitation through the generated client with email and role', async () => {
    postMock.mockResolvedValue({
      data: {
        id: 'inv-1',
        organisation_id: ORGANISATION_ID,
        email: 'invitee@example.com',
        role_code: 'manager',
        workos_invitation_id: 'inv_workos_1',
        invited_by_user_id: 'u1',
        status: 'sent',
        expires_at: '2026-02-01T00:00:00Z',
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:00:00Z',
      },
      error: undefined,
    })

    const router = buildRouter()
    const wrapper = mount(PlatformInviteUserView, {
      props: { organisationId: ORGANISATION_ID },
      global: { plugins: [VueQueryPlugin, router, createPinia()], stubs: { Toaster: true } },
    })
    await fillAndSubmit(wrapper)

    expect(postMock).toHaveBeenCalledTimes(1)
    const calls = postMock.mock.calls
    const [path, options] = calls[0]!
    expect(path).toBe('/api/v1/platform/organisations/{organisation_id}/invitations')
    expect(options?.params).toEqual({ path: { organisation_id: ORGANISATION_ID } })
    expect(options?.body).toEqual({ email: 'invitee@example.com', role_code: 'manager' })
    expect(router.push).toHaveBeenCalledWith({
      name: 'platform-organisation-detail',
      params: { organisationId: ORGANISATION_ID },
    })
  })

  it('blocks an invalid email before any request is made', async () => {
    const router = buildRouter()
    const wrapper = mount(PlatformInviteUserView, {
      props: { organisationId: ORGANISATION_ID },
      global: { plugins: [VueQueryPlugin, router], stubs: { Toaster: true } },
    })

    await wrapper.find('input[type="email"]').setValue('not-an-email')
    await wrapper.find('form').trigger('submit')
    await vi.waitFor(() => expect(wrapper.text()).toContain('Enter a valid email address.'))

    expect(postMock).not.toHaveBeenCalled()
  })
})
