import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import type { Router } from 'vue-router'

import type { components } from '@/api/generated/openapi'

type RecordDetail = components['schemas']['RecordDetail']
type RecordMutationHooks = { onSuccess?: (record: RecordDetail) => void }
type DeleteMutationHooks = { onSuccess?: (recordId: string) => void }

const mockUseMeQuery = vi.hoisted(() => vi.fn<() => unknown>())
vi.mock('@/queries/me', () => ({
  useMeQuery: mockUseMeQuery,
}))

const mockUseRecordQuery = vi.hoisted(() => vi.fn<() => unknown>())
const mockUseCreateRecordMutation = vi.hoisted(() =>
  vi.fn<(options?: RecordMutationHooks) => unknown>(),
)
const mockUseDeleteRecordMutation = vi.hoisted(() =>
  vi.fn<(options?: DeleteMutationHooks) => unknown>(),
)
const mockUseUpdateRecordMutation = vi.hoisted(() =>
  vi.fn<(options?: RecordMutationHooks) => unknown>(),
)
vi.mock('@/queries/records', () => ({
  useRecordQuery: mockUseRecordQuery,
  useCreateRecordMutation: mockUseCreateRecordMutation,
  useDeleteRecordMutation: mockUseDeleteRecordMutation,
  useUpdateRecordMutation: mockUseUpdateRecordMutation,
}))

import RecordEditView from '@/views/RecordEditView.vue'

type MeResponse = components['schemas']['MeResponse']

const ORG_ID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const RECORD_ID = '11111111-1111-4111-8111-111111111111'

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
        organisation_name: 'Example Organisation',
        user_id: 'u1',
        status: 'active',
        created_at: '2026-01-01T00:00:00Z',
      },
    ],
    roles,
    platform_roles: [],
  }
}

const record: RecordDetail = {
  id: RECORD_ID,
  title: 'Existing record',
  body: 'Existing body',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-02T00:00:00Z',
}

async function mountEdit(): Promise<{ wrapper: VueWrapper; router: Router }> {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      {
        path: '/records/:recordId/edit',
        name: 'record-edit',
        component: RecordEditView,
        props: true,
      },
      { path: '/records', name: 'records', component: { template: '<div>list</div>' } },
    ],
  })
  await router.push(`/records/${RECORD_ID}/edit`)
  await router.isReady()
  const wrapper = mount(RecordEditView, {
    props: { recordId: RECORD_ID },
    global: { plugins: [createPinia(), router] },
  })
  return { wrapper, router }
}

describe('RecordEditView', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
    mockUseMeQuery.mockReset()
    mockUseRecordQuery.mockReset()
    mockUseCreateRecordMutation.mockReset()
    mockUseDeleteRecordMutation.mockReset()
    mockUseUpdateRecordMutation.mockReset()

    mockUseMeQuery.mockReturnValue({ data: ref(me(['owner'])) })
    mockUseRecordQuery.mockReturnValue({
      data: ref(record),
      isPending: ref(false),
      isError: ref(false),
      error: ref(null),
    })
    mockUseCreateRecordMutation.mockReturnValue({
      mutateAsync: vi.fn<() => Promise<unknown>>(),
      isPending: ref(false),
      error: ref(null),
    })
    mockUseDeleteRecordMutation.mockReturnValue({
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

  it('renders the edit form hydrated from the detail query', async () => {
    const { wrapper } = await mountEdit()

    expect(wrapper.find('[data-testid="record-form"]').exists()).toBe(true)
    const titleInput = wrapper.find('input[type="text"]')
    expect((titleInput.element as HTMLInputElement).value).toBe('Existing record')
  })

  it('shows a read-only notice and no form to a viewer', async () => {
    mockUseMeQuery.mockReturnValue({ data: ref(me(['viewer'])) })
    const { wrapper } = await mountEdit()

    expect(wrapper.find('[data-testid="records-edit-denied"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="record-form"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="records-delete-button"]').exists()).toBe(false)
  })

  it('hides the delete action for a manager (no records.delete)', async () => {
    mockUseMeQuery.mockReturnValue({ data: ref(me(['manager'])) })
    const { wrapper } = await mountEdit()

    expect(wrapper.find('[data-testid="record-form"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="records-delete-button"]').exists()).toBe(false)
  })

  it('shows a read-only notice to a member (no records.update)', async () => {
    mockUseMeQuery.mockReturnValue({ data: ref(me(['member'])) })
    const { wrapper } = await mountEdit()

    expect(wrapper.find('[data-testid="records-edit-denied"]').exists()).toBe(true)
  })

  it('requires confirmation before deleting and deletes on the second click', async () => {
    const deleteRecordId = vi.fn<(recordId: string) => void>()
    const mutateAsync = vi
      .fn<(recordId: string) => Promise<void>>()
      .mockImplementation(async (recordId: string) => {
        deleteRecordId(recordId)
        const calls = mockUseDeleteRecordMutation.mock.calls
        calls[calls.length - 1]?.[0]?.onSuccess?.(recordId)
      })
    mockUseDeleteRecordMutation.mockReturnValue({
      mutateAsync,
      isPending: ref(false),
      error: ref(null),
    })
    const { wrapper, router } = await mountEdit()

    // First click arms the confirmation; nothing is deleted yet.
    await wrapper.find('[data-testid="records-delete-button"]').trigger('click')
    expect(deleteRecordId).not.toHaveBeenCalled()
    expect(wrapper.find('[data-testid="records-delete-confirm"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Delete this record?')

    // Second click performs the delete and navigates to the list.
    await wrapper.find('[data-testid="records-delete-confirm"]').trigger('click')
    await flushPromises()

    expect(deleteRecordId).toHaveBeenCalledWith(RECORD_ID)
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('records')
  })

  it('cancelling the confirmation leaves the record untouched', async () => {
    const mutateAsync = vi.fn<() => Promise<unknown>>()
    mockUseDeleteRecordMutation.mockReturnValue({
      mutateAsync,
      isPending: ref(false),
      error: ref(null),
    })
    const { wrapper } = await mountEdit()

    await wrapper.find('[data-testid="records-delete-button"]').trigger('click')
    await wrapper.find('[data-testid="records-delete-cancel"]').trigger('click')

    expect(mutateAsync).not.toHaveBeenCalled()
    expect(wrapper.find('[data-testid="records-delete-button"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="records-delete-confirm"]').exists()).toBe(false)
  })

  it('renders the load error with a way back to the list', async () => {
    mockUseRecordQuery.mockReturnValue({
      data: ref(undefined),
      isPending: ref(false),
      isError: ref(true),
      error: ref(new Error('404: record not found')),
    })
    const { wrapper } = await mountEdit()

    const errorState = wrapper.find('[data-testid="records-edit-load-error"]')
    expect(errorState.exists()).toBe(true)
    expect(errorState.text()).toContain('404: record not found')
  })
})
