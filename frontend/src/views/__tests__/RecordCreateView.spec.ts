import { mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import type { Router } from 'vue-router'

import type { components } from '@/api/generated/openapi'

type RecordDetail = components['schemas']['RecordDetail']
type MutationHooks = { onSuccess?: (record: RecordDetail) => void }

const mockUseMeQuery = vi.hoisted(() => vi.fn<() => unknown>())
vi.mock('@/queries/me', () => ({
  useMeQuery: mockUseMeQuery,
}))

const mockUseCreateRecordMutation = vi.hoisted(() => vi.fn<(options?: MutationHooks) => unknown>())
const mockUseUpdateRecordMutation = vi.hoisted(() => vi.fn<(options?: MutationHooks) => unknown>())
vi.mock('@/queries/records', () => ({
  useCreateRecordMutation: mockUseCreateRecordMutation,
  useUpdateRecordMutation: mockUseUpdateRecordMutation,
}))

import RecordCreateView from '@/views/RecordCreateView.vue'

type MeResponse = components['schemas']['MeResponse']

const ORG_ID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'

function me(roles: string[]): MeResponse {
  return {
    user: {
      id: 'u1',
      email: 'ada@example.com',
      name: 'Ada Lovelace',
      is_active: true,
      created_at: '2026-01-01T00:00:00Z',
    },
    memberships: [
      {
        id: 'm1',
        organisation_id: ORG_ID,
        user_id: 'u1',
        status: 'active',
        created_at: '2026-01-01T00:00:00Z',
      },
    ],
    roles,
    platform_roles: [],
  }
}

const createdRecord: RecordDetail = {
  id: '33333333-3333-4333-8333-333333333333',
  title: 'Created record',
  body: '',
  created_at: '2026-02-01T00:00:00Z',
  updated_at: '2026-02-01T00:00:00Z',
}

async function mountCreate(): Promise<{ wrapper: VueWrapper; router: Router }> {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/records/new', name: 'record-create', component: RecordCreateView },
      { path: '/records', name: 'records', component: { template: '<div>list</div>' } },
    ],
  })
  await router.push('/records/new')
  await router.isReady()
  const wrapper = mount(RecordCreateView, { global: { plugins: [createPinia(), router] } })
  return { wrapper, router }
}

describe('RecordCreateView', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
    mockUseMeQuery.mockReset()
    mockUseCreateRecordMutation.mockReset()
    mockUseUpdateRecordMutation.mockReset()
    mockUseCreateRecordMutation.mockReturnValue({
      mutateAsync: vi.fn<() => Promise<unknown>>(),
      isPending: ref(false),
      error: ref(null),
    })
    mockUseUpdateRecordMutation.mockReturnValue({
      mutateAsync: vi.fn<() => Promise<unknown>>(),
      isPending: ref(false),
      error: ref(null),
    })
  })

  afterEach(() => {
    document.body.innerHTML = ''
  })

  it('renders the standard create form for roles with records.create', async () => {
    mockUseMeQuery.mockReturnValue({ data: ref(me(['owner'])) })
    const { wrapper } = await mountCreate()

    expect(wrapper.find('[data-testid="record-form"]').exists()).toBe(true)
    expect(wrapper.find('input[type="text"]').attributes('placeholder')).toBe('Record title')
  })

  it('shows a read-only notice to a viewer and no form', async () => {
    mockUseMeQuery.mockReturnValue({ data: ref(me(['viewer'])) })
    const { wrapper } = await mountCreate()

    expect(wrapper.find('[data-testid="records-create-denied"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="record-form"]').exists()).toBe(false)
  })

  it('waits for /me before deciding between form and denial', async () => {
    mockUseMeQuery.mockReturnValue({
      data: ref(undefined),
      isPending: ref(true),
      isError: ref(false),
      error: ref(null),
    })
    const { wrapper } = await mountCreate()

    expect(wrapper.text()).toContain('Checking permissions…')
    expect(wrapper.find('[data-testid="record-form"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="records-create-denied"]').exists()).toBe(false)
  })

  it('round-trips through the create mutation and navigates to the list', async () => {
    mockUseMeQuery.mockReturnValue({ data: ref(me(['owner'])) })
    const mutateAsync = vi.fn<() => Promise<void>>().mockImplementation(async () => {
      const calls = mockUseCreateRecordMutation.mock.calls
      calls[calls.length - 1]?.[0]?.onSuccess?.(createdRecord)
    })
    mockUseCreateRecordMutation.mockReturnValue({
      mutateAsync,
      isPending: ref(false),
      error: ref(null),
    })
    const { wrapper, router } = await mountCreate()

    await wrapper.find('input[type="text"]').setValue('Created record')
    await wrapper.find('form').trigger('submit')

    await vi.waitFor(() => expect(mutateAsync).toHaveBeenCalledOnce())
    expect(mutateAsync).toHaveBeenCalledWith({ title: 'Created record', body: '' })
    await vi.waitFor(() => expect(router.currentRoute.value.name).toBe('records'))
  })
})
