import { VueQueryPlugin } from '@tanstack/vue-query'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import type { components } from '@/api/generated/openapi'
import PlatformOrganisationAISettingsView from '@/views/PlatformOrganisationAISettingsView.vue'
import { queryClient } from '@/queries/queryClient'

type GetSignature = (url: string, init?: unknown) => Promise<unknown>
type PutSignature = (url: string, init?: { params?: unknown; body?: unknown }) => Promise<unknown>

vi.mock('@/api/client', () => ({
  client: {
    GET: vi.fn<GetSignature>(),
    PUT: vi.fn<PutSignature>(),
  },
}))

import { client } from '@/api/client'

const getMock = vi.mocked(client.GET as unknown as GetSignature)
const putMock = vi.mocked(client.PUT as unknown as PutSignature)

type AISettings = components['schemas']['PlatformOrganisationAISettingsResponse']

const ORGANISATION_ID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'

function settingsRow(overrides: Partial<AISettings> = {}): AISettings {
  return {
    organisation_id: ORGANISATION_ID,
    version: 1,
    enabled: false,
    allowed_provider_ids: [],
    allowed_model_ids: [],
    provider_override: null,
    model_override: null,
    monthly_budget: null,
    retention_policy_days: null,
    allowed_transfer_modes: ['inline'],
    max_large_attachment_bytes: 50000000,
    updated_by_user_id: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

async function flushPromises(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0))
}

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

function mountView() {
  const pinia = createPinia()
  setActivePinia(pinia)
  return mount(PlatformOrganisationAISettingsView, {
    props: { organisationId: ORGANISATION_ID },
    global: { plugins: [pinia, [VueQueryPlugin, { queryClient }], buildRouter()] },
  })
}

describe('PlatformOrganisationAISettingsView', () => {
  beforeEach(() => {
    queryClient.clear()
    getMock.mockReset()
    putMock.mockReset()
  })

  it('hydrates the form from the fetched settings', async () => {
    getMock.mockResolvedValue({
      data: settingsRow({
        enabled: true,
        allowed_provider_ids: ['vertex'],
        allowed_transfer_modes: ['inline', 'storage_reference'],
      }),
      error: undefined,
    })
    const wrapper = mountView()
    await flushPromises()
    await flushPromises()

    const enabled = wrapper.find('[data-testid="ai-settings-enabled"]').element as HTMLInputElement
    expect(enabled.checked).toBe(true)
    const vertex = wrapper.find('[data-testid="ai-settings-provider-vertex"]')
      .element as HTMLInputElement
    expect(vertex.checked).toBe(true)
    const fake = wrapper.find('[data-testid="ai-settings-provider-fake"]')
      .element as HTMLInputElement
    expect(fake.checked).toBe(false)
    const storage = wrapper.find('[data-testid="ai-settings-mode-storage_reference"]')
      .element as HTMLInputElement
    expect(storage.checked).toBe(true)
    expect(getMock).toHaveBeenCalledWith(
      '/api/v1/platform/organisations/{organisation_id}/ai-settings',
      { params: { path: { organisation_id: ORGANISATION_ID } } },
    )
  })

  it('saves the policy with the optimistic-concurrency version', async () => {
    getMock.mockResolvedValue({ data: settingsRow(), error: undefined })
    putMock.mockResolvedValue({
      data: settingsRow({ enabled: true, version: 2 }),
      error: undefined,
    })
    const wrapper = mountView()
    await flushPromises()
    await flushPromises()

    await wrapper.find('[data-testid="ai-settings-enabled"]').setValue(true)
    await wrapper.find('[data-testid="ai-settings-provider-vertex"]').setValue(true)
    await wrapper.find('[data-testid="ai-settings-mode-storage_reference"]').setValue(true)
    await wrapper.find('[data-testid="ai-settings-save"]').trigger('click')
    await flushPromises()
    await flushPromises()

    expect(putMock).toHaveBeenCalledWith(
      '/api/v1/platform/organisations/{organisation_id}/ai-settings',
      {
        params: { path: { organisation_id: ORGANISATION_ID } },
        body: expect.objectContaining({
          version: 1,
          enabled: true,
          allowed_provider_ids: ['vertex'],
          allowed_transfer_modes: ['inline', 'storage_reference'],
          max_large_attachment_bytes: 50000000,
        }),
      },
    )
  })

  it('keeps the inline transfer mode always selected and never sends it off', async () => {
    getMock.mockResolvedValue({ data: settingsRow(), error: undefined })
    const wrapper = mountView()
    await flushPromises()
    await flushPromises()

    const inline = wrapper.find('[data-testid="ai-settings-mode-inline"]')
      .element as HTMLInputElement
    expect(inline.checked).toBe(true)
    expect(inline.hasAttribute('disabled')).toBe(true)
  })

  it('saves when the API returns the budget as a JSON number (Decimal encoding)', async () => {
    // FastAPI's jsonable_encoder serializes the Decimal budget as a number,
    // even though the OpenAPI schema declares a string — the form must
    // tolerate both shapes without throwing.
    getMock.mockResolvedValue({
      data: settingsRow({ monthly_budget: 25.5 as unknown as string }),
      error: undefined,
    })
    putMock.mockResolvedValue({ data: settingsRow({ version: 2 }), error: undefined })
    const wrapper = mountView()
    await flushPromises()
    await flushPromises()

    await wrapper.find('[data-testid="ai-settings-save"]').trigger('click')
    await flushPromises()
    await flushPromises()

    expect(putMock).toHaveBeenCalledTimes(1)
    expect(putMock).toHaveBeenCalledWith(
      '/api/v1/platform/organisations/{organisation_id}/ai-settings',
      expect.objectContaining({
        body: expect.objectContaining({ monthly_budget: '25.5' }),
      }),
    )
  })

  it('refetches and flags a conflict when a stale version is rejected', async () => {
    getMock.mockResolvedValue({ data: settingsRow(), error: undefined })
    putMock.mockRejectedValue({
      message: 'conflict: ai_settings version mismatch',
    })
    const wrapper = mountView()
    await flushPromises()
    await flushPromises()

    await wrapper.find('[data-testid="ai-settings-enabled"]').setValue(true)
    await wrapper.find('[data-testid="ai-settings-save"]').trigger('click')
    await flushPromises()
    await flushPromises()

    expect(wrapper.find('[data-testid="ai-settings-conflict"]').exists()).toBe(true)
    // The conflict triggered a refetch of the latest version.
    expect(getMock.mock.calls.length).toBeGreaterThanOrEqual(2)
  })
})
