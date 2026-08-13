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
  },
}))

import { client } from '@/api/client'
import type { components } from '@/api/generated/openapi'
import {
  aiQueryKeys,
  isClassifyAccepted,
  useAskMutation,
  useClassifyMutation,
  useClassifyResultQuery,
} from '@/queries/ai'
import { queryClient } from '@/queries/queryClient'
import { useOrganisationStore } from '@/stores/organisation'

type ClassifyAcceptedResponse = components['schemas']['DocumentClassifyAcceptedResponse']
type ClassifyResultResponse = components['schemas']['DocumentClassifyResultResponse']
type ClassifySyncResponse = components['schemas']['DocumentClassifySyncResponse']

const ORG_A = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const REQUEST_ID = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
const JOB_ID = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
const STORAGE_REF = `organisations/${ORG_A}/ai/scratch/doc.txt`

const getMock = vi.mocked(client.GET)
const postMock = vi.mocked(client.POST)

function result(status: ClassifyResultResponse['status']): ClassifyResultResponse {
  return {
    request_id: REQUEST_ID,
    status,
    error_code: null,
    output: null,
    routing: null,
    usage: null,
    cost: null,
    completed_at: null,
  }
}

function accepted(): ClassifyAcceptedResponse {
  return { job_id: JOB_ID, request_id: REQUEST_ID, status: 'queued' }
}

function syncResult(): ClassifySyncResponse {
  return {
    request_id: REQUEST_ID,
    output: { category: 'lease', confidence: 0.99, summary: 'A fixture classification.' },
    routing: {
      provider: 'fake',
      model: 'fake-model-document.classify',
      prompt_name: 'document.classify',
      prompt_version: 1,
      fallback_used: false,
      region: '',
    },
    usage: { input_tokens: 10, output_tokens: 5 },
    cost: { amount: '0.000000', currency: 'USD' },
    completed_at: '2026-01-01T00:00:00Z',
  }
}

function flushPromises(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

let pinia: Pinia

let captured!: {
  result: ReturnType<typeof useClassifyResultQuery>
}

function mountResultQuery(): void {
  const CapturingComponent = defineComponent({
    setup() {
      captured = { result: useClassifyResultQuery(REQUEST_ID) }
      return {}
    },
    template: '<div />',
  })
  mount(CapturingComponent, {
    global: { plugins: [pinia, [VueQueryPlugin, { queryClient }]] },
  })
}

/**
 * Extract the `refetchInterval` decision function registered on a query and
 * evaluate it against the query's current cache state (the same shape TanStack
 * Query passes when it decides whether to keep polling).
 */
function pollDecision(queryKey: readonly unknown[]) {
  const [query] = queryClient.getQueryCache().findAll({ queryKey })
  if (!query) throw new Error(`no query found for ${String(queryKey)}`)
  const options = query.options as unknown as {
    refetchInterval?:
      | number
      | false
      | ((q: { state: { data?: { status?: string } | undefined } }) => number | false)
  }
  const refetchInterval = options.refetchInterval
  if (typeof refetchInterval === 'function') {
    const state = query.state as unknown as { data?: { status?: string } | undefined }
    return refetchInterval({ state })
  }
  return refetchInterval
}

describe('ai query composables', () => {
  beforeEach(() => {
    localStorage.clear()
    pinia = createPinia()
    setActivePinia(pinia)
    getMock.mockReset()
    postMock.mockReset()
    queryClient.clear()
  })

  it('distinguishes the accepted (202) body from the synchronous result', () => {
    expect(isClassifyAccepted(accepted())).toBe(true)
    expect(isClassifyAccepted(syncResult())).toBe(false)
  })

  it('submits a classify request with the storage reference through the generated client', async () => {
    const organisation = useOrganisationStore()
    organisation.setSelectedOrganisation(ORG_A)
    postMock.mockResolvedValue({ data: accepted(), error: undefined })

    const CapturingComponent = defineComponent({
      setup() {
        const mutation = useClassifyMutation()
        return { mutation }
      },
      template: '<div />',
    })
    const wrapper = mount(CapturingComponent, {
      global: { plugins: [pinia, [VueQueryPlugin, { queryClient }]] },
    })
    wrapper.vm.mutation.mutate({ storage_reference: STORAGE_REF, sync: false })
    await flushPromises()
    await flushPromises()

    expect(postMock).toHaveBeenCalledWith('/api/v1/ai/classify', {
      body: { storage_reference: STORAGE_REF, sync: false },
    })
    expect(wrapper.vm.mutation.isSuccess.value).toBe(true)
    expect(wrapper.vm.mutation.data.value).toMatchObject({ job_id: JOB_ID })
  })

  it('fetches the durable result by request id', async () => {
    const organisation = useOrganisationStore()
    organisation.setSelectedOrganisation(ORG_A)
    getMock.mockResolvedValue({ data: result('queued'), error: undefined })

    mountResultQuery()
    await flushPromises()
    await flushPromises()

    expect(getMock).toHaveBeenCalledWith('/api/v1/ai/classify/requests/{request_id}', {
      params: { path: { request_id: REQUEST_ID } },
    })
    expect(captured.result.isSuccess.value).toBe(true)
    expect(captured.result.data.value?.status).toBe('queued')
  })

  it('stays disabled without a selected organisation', async () => {
    mountResultQuery()
    await flushPromises()
    await flushPromises()

    expect(getMock).not.toHaveBeenCalled()
  })

  it('polls through the queued and running window and stops at a terminal status', async () => {
    const organisation = useOrganisationStore()
    organisation.setSelectedOrganisation(ORG_A)
    getMock.mockResolvedValue({ data: result('queued'), error: undefined })

    mountResultQuery()
    await flushPromises()
    await flushPromises()

    // The pre-enqueue queued row makes the first fetch a 200 with status
    // ``queued``, so polling is armed immediately (v0.7 Scope §5.8).
    expect(captured.result.data.value?.status).toBe('queued')
    expect(pollDecision(aiQueryKeys.result(ORG_A, REQUEST_ID))).toBe(1000)

    // Worker dispatches: the next refetch returns ``running`` and polling
    // stays armed.
    getMock.mockResolvedValue({ data: result('running'), error: undefined })
    await queryClient.refetchQueries({ queryKey: aiQueryKeys.result(ORG_A, REQUEST_ID) })
    await flushPromises()
    await flushPromises()
    expect(captured.result.data.value?.status).toBe('running')
    expect(pollDecision(aiQueryKeys.result(ORG_A, REQUEST_ID))).toBe(1000)

    // Terminal status: polling stops automatically.
    getMock.mockResolvedValue({ data: result('succeeded'), error: undefined })
    await queryClient.refetchQueries({ queryKey: aiQueryKeys.result(ORG_A, REQUEST_ID) })
    await flushPromises()
    await flushPromises()
    expect(captured.result.data.value?.status).toBe('succeeded')
    expect(pollDecision(aiQueryKeys.result(ORG_A, REQUEST_ID))).toBe(false)
  })

  it('submits a document question through the generated ask endpoint', async () => {
    const organisation = useOrganisationStore()
    organisation.setSelectedOrganisation(ORG_A)
    postMock.mockResolvedValue({
      data: {
        request_id: REQUEST_ID,
        output: 'The renewal term is twelve months.',
        routing: {
          provider: 'fake',
          model: 'fake-model-document.classify',
          prompt_name: 'document.ask',
          prompt_version: 1,
          fallback_used: false,
          region: '',
        },
        usage: { input_tokens: 10, output_tokens: 5 },
        cost: { amount: '0.000000', currency: 'USD' },
        completed_at: '2026-01-01T00:00:00Z',
      },
      error: undefined,
    })

    const CapturingComponent = defineComponent({
      setup() {
        const mutation = useAskMutation()
        return { mutation }
      },
      template: '<div />',
    })
    const wrapper = mount(CapturingComponent, {
      global: { plugins: [pinia, [VueQueryPlugin, { queryClient }]] },
    })
    wrapper.vm.mutation.mutate({
      storage_reference: STORAGE_REF,
      question: 'What is the renewal term?',
    })
    await flushPromises()
    await flushPromises()

    expect(postMock).toHaveBeenCalledWith('/api/v1/ai/ask', {
      body: { storage_reference: STORAGE_REF, question: 'What is the renewal term?' },
    })
    expect(wrapper.vm.mutation.isSuccess.value).toBe(true)
    expect(wrapper.vm.mutation.data.value?.output).toBe('The renewal term is twelve months.')
  })

  it('rejects an ask without a selected organisation before any HTTP call', async () => {
    const CapturingComponent = defineComponent({
      setup() {
        const mutation = useAskMutation()
        return { mutation }
      },
      template: '<div />',
    })
    const wrapper = mount(CapturingComponent, {
      global: { plugins: [pinia, [VueQueryPlugin, { queryClient }]] },
    })
    wrapper.vm.mutation.mutate({
      storage_reference: STORAGE_REF,
      question: 'What is this?',
    })
    await flushPromises()
    await flushPromises()

    expect(postMock).not.toHaveBeenCalled()
    expect(wrapper.vm.mutation.isError.value).toBe(true)
  })
})
