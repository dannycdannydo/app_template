import { beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent } from 'vue'
import { VueQueryPlugin } from '@tanstack/vue-query'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import type { Pinia } from 'pinia'

vi.mock('@/api/client', () => ({
  client: {
    GET: vi.fn<typeof client.GET>(),
    POST: vi.fn<typeof client.POST>(),
    PATCH: vi.fn<typeof client.PATCH>(),
    DELETE: vi.fn<typeof client.DELETE>(),
  },
}))

import { client } from '@/api/client'
import type { components } from '@/api/generated/openapi'
import {
  recordsQueryKeys,
  useCreateRecordMutation,
  useDeleteRecordMutation,
  useRecordQuery,
  useRecordsQuery,
  useUpdateRecordMutation,
} from '@/queries/records'
import { queryClient } from '@/queries/queryClient'
import { useOrganisationStore } from '@/stores/organisation'

type RecordListItem = components['schemas']['RecordListItem']
type RecordListResponse = components['schemas']['RecordListResponse']
type RecordDetail = components['schemas']['RecordDetail']

const ORG_A = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const RECORD_ID = '11111111-1111-4111-8111-111111111111'

const getMock = vi.mocked(client.GET)
const postMock = vi.mocked(client.POST)
const patchMock = vi.mocked(client.PATCH)
const deleteMock = vi.mocked(client.DELETE)

const listItem: RecordListItem = {
  id: RECORD_ID,
  title: 'First record',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

const recordDetail: RecordDetail = {
  ...listItem,
  body: 'A body',
}

function listEnvelope(items: RecordListItem[] = []): RecordListResponse {
  return { items, page: 1, page_size: 50, total: items.length }
}

function flushPromises(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

let pinia: Pinia

let captured!: {
  list: ReturnType<typeof useRecordsQuery>
  detail: ReturnType<typeof useRecordQuery>
  create: ReturnType<typeof useCreateRecordMutation>
  update: ReturnType<typeof useUpdateRecordMutation>
  remove: ReturnType<typeof useDeleteRecordMutation>
}

function mountQueries(): void {
  const CapturingComponent = defineComponent({
    setup() {
      captured = {
        list: useRecordsQuery({ page: 1, pageSize: 50 }),
        detail: useRecordQuery(RECORD_ID),
        create: useCreateRecordMutation(),
        update: useUpdateRecordMutation(),
        remove: useDeleteRecordMutation(),
      }
      return {}
    },
    template: '<div />',
  })
  mount(CapturingComponent, {
    global: { plugins: [pinia, [VueQueryPlugin, { queryClient }]] },
  })
}

const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries')

describe('records query composables', () => {
  beforeEach(() => {
    localStorage.clear()
    pinia = createPinia()
    setActivePinia(pinia)
    getMock.mockReset()
    postMock.mockReset()
    patchMock.mockReset()
    deleteMock.mockReset()
    queryClient.clear()
    invalidateSpy.mockClear()
  })

  it('maps camelCase list params to the snake_case API query and returns the envelope', async () => {
    const organisation = useOrganisationStore()
    organisation.setSelectedOrganisation(ORG_A)
    getMock.mockResolvedValue({ data: listEnvelope([listItem]), error: undefined })

    mountQueries()
    await flushPromises()
    await flushPromises()

    expect(getMock).toHaveBeenCalledWith('/api/v1/records', {
      params: { query: { page: 1, page_size: 50 } },
    })
    expect(captured.list.isSuccess.value).toBe(true)
    expect(captured.list.data.value?.total).toBe(1)
    expect(captured.list.data.value?.items[0]?.title).toBe('First record')
  })

  it('stays disabled without a selected organisation', async () => {
    mountQueries()
    await flushPromises()
    await flushPromises()

    expect(getMock).not.toHaveBeenCalled()
  })

  it('fetches a single record through the generated client', async () => {
    const organisation = useOrganisationStore()
    organisation.setSelectedOrganisation(ORG_A)
    getMock.mockResolvedValue({ data: recordDetail, error: undefined })

    mountQueries()
    await flushPromises()
    await flushPromises()

    expect(getMock).toHaveBeenCalledWith('/api/v1/records/{record_id}', {
      params: { path: { record_id: RECORD_ID } },
    })
    expect(captured.detail.isSuccess.value).toBe(true)
    expect(captured.detail.data.value?.id).toBe(RECORD_ID)
  })

  it('create mutation posts the payload and invalidates the org list', async () => {
    const organisation = useOrganisationStore()
    organisation.setSelectedOrganisation(ORG_A)
    getMock.mockResolvedValue({ data: listEnvelope([]), error: undefined })
    postMock.mockResolvedValue({ data: recordDetail, error: undefined, response: new Response() })

    mountQueries()
    await flushPromises()
    await flushPromises()

    captured.create.mutate({ title: 'New record', body: 'Body' })
    await flushPromises()
    await flushPromises()

    expect(postMock).toHaveBeenCalledWith('/api/v1/records', {
      body: { title: 'New record', body: 'Body' },
    })
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: recordsQueryKeys.lists(ORG_A) })
  })

  it('update mutation patches the record and invalidates list and detail', async () => {
    const organisation = useOrganisationStore()
    organisation.setSelectedOrganisation(ORG_A)
    getMock.mockResolvedValue({ data: recordDetail, error: undefined })
    patchMock.mockResolvedValue({ data: recordDetail, error: undefined, response: new Response() })

    mountQueries()
    await flushPromises()
    await flushPromises()

    captured.update.mutate({ recordId: RECORD_ID, payload: { title: 'Renamed' } })
    await flushPromises()
    await flushPromises()

    expect(patchMock).toHaveBeenCalledWith('/api/v1/records/{record_id}', {
      params: { path: { record_id: RECORD_ID } },
      body: { title: 'Renamed' },
    })
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: recordsQueryKeys.lists(ORG_A) })
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: recordsQueryKeys.detail(ORG_A, RECORD_ID),
    })
  })

  it('delete mutation removes the record and invalidates list and detail', async () => {
    const organisation = useOrganisationStore()
    organisation.setSelectedOrganisation(ORG_A)
    getMock.mockResolvedValue({ data: recordDetail, error: undefined })
    deleteMock.mockResolvedValue({ data: undefined, error: undefined, response: new Response() })

    mountQueries()
    await flushPromises()
    await flushPromises()

    captured.remove.mutate(RECORD_ID)
    await flushPromises()
    await flushPromises()

    expect(deleteMock).toHaveBeenCalledWith('/api/v1/records/{record_id}', {
      params: { path: { record_id: RECORD_ID } },
    })
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: recordsQueryKeys.lists(ORG_A) })
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: recordsQueryKeys.detail(ORG_A, RECORD_ID),
    })
  })

  it('surfaces the client error when the list request fails', async () => {
    const organisation = useOrganisationStore()
    organisation.setSelectedOrganisation(ORG_A)
    const mockError = { code: 'internal_error', message: 'Boom' }
    getMock.mockResolvedValue({ data: undefined, error: mockError })

    mountQueries()
    await flushPromises()
    await flushPromises()

    expect(captured.list.isError.value).toBe(true)
    // The composable throws the client error (a typed ApiError in production,
    // blueprint §13) without wrapping or swallowing it.
    expect(captured.list.error.value).toEqual(mockError)
  })
})
