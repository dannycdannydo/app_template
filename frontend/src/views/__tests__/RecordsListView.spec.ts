import { mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ref, toValue } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import type { Router } from 'vue-router'

import { ApiError } from '@/api/errors'
import type { components } from '@/api/generated/openapi'
import type { RecordsListParams } from '@/queries/records'
import type { MaybeRefOrGetter } from 'vue'

const mockUseMeQuery = vi.hoisted(() => vi.fn<() => unknown>())
vi.mock('@/queries/me', () => ({
  useMeQuery: mockUseMeQuery,
}))

const mockUseRecordsQuery = vi.hoisted(() =>
  vi.fn<(params: MaybeRefOrGetter<RecordsListParams>) => unknown>(),
)
vi.mock('@/queries/records', () => ({
  useRecordsQuery: mockUseRecordsQuery,
}))

import RecordsListView from '@/views/RecordsListView.vue'

type MeResponse = components['schemas']['MeResponse']
type RecordListItem = components['schemas']['RecordListItem']

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

const records: RecordListItem[] = [
  {
    id: '11111111-1111-4111-8111-111111111111',
    title: 'First record',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-02T00:00:00Z',
  },
  {
    id: '22222222-2222-4222-8222-222222222222',
    title: 'Second record',
    created_at: '2026-01-03T00:00:00Z',
    updated_at: '2026-01-03T00:00:00Z',
  },
]

function listResponse(page = 1, pageSize = 25, items: RecordListItem[] = records) {
  return {
    items,
    page,
    page_size: pageSize,
    total: items.length,
  }
}

/** 26 records → two pages at the default page size, so the Next control is enabled. */
function manyRecords(): RecordListItem[] {
  return Array.from({ length: 26 }, (_, index) => ({
    id: `record-${index}`,
    title: `Record ${index + 1}`,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  }))
}

async function mountList(): Promise<{ wrapper: VueWrapper; router: Router }> {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', name: 'records', component: RecordsListView },
      { path: '/records/new', name: 'record-create', component: { template: '<div>create</div>' } },
      {
        path: '/records/:recordId/edit',
        name: 'record-edit',
        component: { template: '<div>edit</div>' },
      },
    ],
  })
  await router.push('/')
  await router.isReady()
  const wrapper = mount(RecordsListView, { global: { plugins: [createPinia(), router] } })
  return { wrapper, router }
}

describe('RecordsListView', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
    mockUseMeQuery.mockReset()
    mockUseRecordsQuery.mockReset()
    mockUseMeQuery.mockReturnValue({ data: ref(me(['owner'])) })
  })

  afterEach(() => {
    document.body.innerHTML = ''
  })

  function stubRecordsQuery(overrides: Partial<ReturnType<typeof mockUseRecordsQuery>> = {}) {
    mockUseRecordsQuery.mockReturnValue({
      data: ref(listResponse()),
      isPending: ref(false),
      isError: ref(false),
      error: ref(null),
      ...overrides,
    })
  }

  it('renders records from the org-scoped query', async () => {
    stubRecordsQuery()
    const { wrapper } = await mountList()

    const rows = wrapper.findAll('[data-testid="data-table-row"]')
    expect(rows).toHaveLength(2)
    expect(wrapper.text()).toContain('First record')
    expect(wrapper.text()).toContain('Second record')
    expect(wrapper.text()).toContain('2 records')
  })

  it('shows the create action for roles with records.create', async () => {
    stubRecordsQuery()
    const { wrapper } = await mountList()

    expect(wrapper.find('[data-testid="records-create-button"]').exists()).toBe(true)
  })

  it('hides the create action for a viewer (read-only UI)', async () => {
    mockUseMeQuery.mockReturnValue({ data: ref(me(['viewer'])) })
    stubRecordsQuery()
    const { wrapper } = await mountList()

    expect(wrapper.find('[data-testid="records-create-button"]').exists()).toBe(false)
  })

  it('shows an edit action per row for roles with records.update', async () => {
    stubRecordsQuery()
    const { wrapper } = await mountList()

    const editLinks = wrapper.findAll('a[href*="/edit"]')
    expect(editLinks).toHaveLength(2)
    expect(editLinks[0]!.text()).toBe('Edit')
  })

  it('omits row edit actions for a viewer', async () => {
    mockUseMeQuery.mockReturnValue({ data: ref(me(['viewer'])) })
    stubRecordsQuery()
    const { wrapper } = await mountList()

    expect(wrapper.findAll('a[href*="/edit"]')).toHaveLength(0)
  })

  it('navigates to the create screen when the action is clicked', async () => {
    stubRecordsQuery()
    const { wrapper, router } = await mountList()

    await wrapper.find('[data-testid="records-create-button"]').trigger('click')
    await new Promise((resolve) => setTimeout(resolve, 0))

    expect(router.currentRoute.value.name).toBe('record-create')
  })

  it('flows page changes from the table controls into the query params', async () => {
    stubRecordsQuery({ data: ref(listResponse(1, 25, manyRecords())) })
    const { wrapper } = await mountList()

    expect(wrapper.find('[data-testid="data-table-pagination"]').exists()).toBe(true)

    const calls = mockUseRecordsQuery.mock.calls
    const lastCallParams = calls[calls.length - 1]?.[0]
    expect(lastCallParams).toBeDefined()
    // The composable receives a getter; evaluate it before and after the page
    // change to prove view state flows into the query params object.
    const paramsBefore = toValue(lastCallParams)
    expect(paramsBefore?.page).toBe(1)

    await wrapper.find('[data-testid="data-table-next"]').trigger('click')

    const paramsAfter = toValue(lastCallParams)
    expect(paramsAfter?.page).toBe(2)
  })

  it('renders the empty state when there are no records', async () => {
    stubRecordsQuery({ data: ref(listResponse(1, 25, [])) })
    const { wrapper } = await mountList()

    expect(wrapper.find('[data-testid="data-table-empty"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('No records yet')
  })

  it('renders the typed error envelope in the error state', async () => {
    const apiError = new ApiError(500, {
      code: 'records.list_failed',
      message: 'Could not list records',
      request_id: 'req-1',
    })
    stubRecordsQuery({ isError: ref(true), error: ref(apiError) })
    const { wrapper } = await mountList()

    const errorState = wrapper.find('[data-testid="data-table-error"]')
    expect(errorState.exists()).toBe(true)
    expect(errorState.text()).toContain('Could not list records')
    expect(errorState.text()).toContain('records.list_failed')
  })
})
