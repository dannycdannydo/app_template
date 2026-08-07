import { VueQueryPlugin } from '@tanstack/vue-query'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import PlatformAuditView from '@/views/PlatformAuditView.vue'
import { queryClient } from '@/queries/queryClient'

vi.mock('@/api/client', () => ({
  client: {
    GET: vi.fn<GetSignature>(),
  },
}))

type GetSignature = (url: string, init?: { params?: unknown }) => Promise<unknown>

import { client } from '@/api/client'

const getMock = vi.mocked(client.GET as unknown as GetSignature)

const ORGANISATION_ID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'

/**
 * Platform audit table tests (Scope §6.1/§6.9, blueprint §29, acceptance §5.1).
 *
 * The read-only audit listing renders events from the platform query layer
 * with the standard pagination envelope, and the action filter is an approved
 * API query parameter (blueprint §12).
 */
function auditEnvelope() {
  return {
    items: [
      {
        id: 'a1',
        organisation_id: ORGANISATION_ID,
        actor_user_id: 'u1',
        action: 'invitation.sent',
        resource_type: 'invitation',
        resource_id: 'inv-1',
        metadata: {},
        created_at: '2026-01-02T00:00:00Z',
      },
      {
        id: 'a2',
        organisation_id: null,
        actor_user_id: null,
        action: 'platform.bootstrap_granted',
        resource_type: 'platform_membership',
        resource_id: 'pm-1',
        metadata: {},
        created_at: '2026-01-01T00:00:00Z',
      },
    ],
    page: 1,
    page_size: 25,
    total: 2,
  }
}

async function flushPromises(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0))
}

describe('PlatformAuditView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    queryClient.clear()
    getMock.mockReset()
  })

  it('renders the append-only audit events from the platform query layer', async () => {
    getMock.mockResolvedValue({ data: auditEnvelope(), error: undefined })
    const wrapper = mount(PlatformAuditView, {
      global: { plugins: [VueQueryPlugin] },
    })
    await flushPromises()
    await flushPromises()

    const rows = wrapper.findAll('tbody tr')
    expect(rows).toHaveLength(2)
    expect(wrapper.text()).toContain('invitation.sent')
    expect(wrapper.text()).toContain('platform.bootstrap_granted')
    // System-driven events render a "system" actor.
    expect(wrapper.text()).toContain('system')

    const calls = getMock.mock.calls
    const [path, options] = calls[0]!
    expect(path).toBe('/api/v1/platform/audit-events')
    expect(options?.params).toEqual({ query: { page: 1, page_size: 25 } })
  })

  it('passes the action filter through to the API as a query parameter', async () => {
    getMock.mockResolvedValue({ data: auditEnvelope(), error: undefined })
    const wrapper = mount(PlatformAuditView, {
      global: { plugins: [VueQueryPlugin] },
    })
    await flushPromises()
    await flushPromises()

    await wrapper.find('[data-testid="platform-audit-action-filter"]').setValue('invitation.sent')
    await flushPromises()
    await flushPromises()

    const calls = getMock.mock.calls
    const lastCall = calls[calls.length - 1]!
    expect(lastCall[0]).toBe('/api/v1/platform/audit-events')
    expect(lastCall[1]?.params).toEqual({
      query: { page: 1, page_size: 25, action: 'invitation.sent' },
    })
  })

  it('shows the empty state when there are no events', async () => {
    getMock.mockResolvedValue({
      data: { items: [], page: 1, page_size: 25, total: 0 },
      error: undefined,
    })
    const wrapper = mount(PlatformAuditView, {
      global: { plugins: [VueQueryPlugin] },
    })
    await flushPromises()
    await flushPromises()

    expect(wrapper.text()).toContain('No audit events match the current filters.')
  })
})
