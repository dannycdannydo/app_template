import { beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent } from 'vue'
import { VueQueryPlugin } from '@tanstack/vue-query'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import type { Pinia } from 'pinia'

vi.mock('@/api/client', () => ({
  client: {
    GET: vi.fn<typeof client.GET>(),
  },
}))

import { client } from '@/api/client'
import type { components } from '@/api/generated/openapi'
import { jobsQueryKeys, useJobQuery, useJobsQuery } from '@/queries/jobs'
import { queryClient } from '@/queries/queryClient'
import { useOrganisationStore } from '@/stores/organisation'

type JobDetail = components['schemas']['JobDetail']
type JobListResponse = components['schemas']['JobListResponse']

const ORG_A = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const JOB_ID = '22222222-2222-4222-8222-222222222222'

const getMock = vi.mocked(client.GET)

function job(status: JobDetail['status'], progress: number): JobDetail {
  return {
    id: JOB_ID,
    job_type: 'file.processing',
    status,
    progress,
    attempt_count: 1,
    created_by_user_id: 'u1',
    created_at: '2026-01-01T00:00:00Z',
    started_at: '2026-01-01T00:00:01Z',
    completed_at: null,
    input_reference: 'file:11111111-1111-4111-8111-111111111111',
    result_reference: null,
    error_code: null,
    error_message: null,
  }
}

function jobListEnvelope(): JobListResponse {
  return { items: [], page: 1, page_size: 50, total: 0 }
}

function flushPromises(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

let pinia: Pinia

let captured!: {
  list: ReturnType<typeof useJobsQuery>
  detail: ReturnType<typeof useJobQuery>
}

function mountQueries(): void {
  const CapturingComponent = defineComponent({
    setup() {
      captured = {
        list: useJobsQuery({ page: 1, pageSize: 50 }),
        detail: useJobQuery(JOB_ID),
      }
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
 * evaluate it against the query's current cache state (the same shape
 * TanStack Query passes when it decides whether to keep polling).
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

describe('jobs query composables', () => {
  beforeEach(() => {
    localStorage.clear()
    pinia = createPinia()
    setActivePinia(pinia)
    getMock.mockReset()
    queryClient.clear()
  })

  it('maps camelCase list params (including filters) to the API query', async () => {
    const organisation = useOrganisationStore()
    organisation.setSelectedOrganisation(ORG_A)
    getMock.mockResolvedValue({ data: jobListEnvelope(), error: undefined })

    const CapturingComponent = defineComponent({
      setup() {
        captured = {
          ...captured,
          list: useJobsQuery({
            page: 2,
            pageSize: 25,
            status: 'running',
            jobType: 'file.processing',
          }),
        }
        return {}
      },
      template: '<div />',
    })
    mount(CapturingComponent, {
      global: { plugins: [pinia, [VueQueryPlugin, { queryClient }]] },
    })
    await flushPromises()
    await flushPromises()

    expect(getMock).toHaveBeenCalledWith('/api/v1/jobs', {
      params: {
        query: { page: 2, page_size: 25, status: 'running', job_type: 'file.processing' },
      },
    })
    expect(captured.list.isSuccess.value).toBe(true)
  })

  it('stays disabled without a selected organisation', async () => {
    mountQueries()
    await flushPromises()
    await flushPromises()

    expect(getMock).not.toHaveBeenCalled()
  })

  it('fetches a single job by id through the generated client', async () => {
    const organisation = useOrganisationStore()
    organisation.setSelectedOrganisation(ORG_A)
    getMock.mockResolvedValue({ data: job('running', 40), error: undefined })

    mountQueries()
    await flushPromises()
    await flushPromises()

    expect(getMock).toHaveBeenCalledWith('/api/v1/jobs/{job_id}', {
      params: { path: { job_id: JOB_ID } },
    })
    expect(captured.detail.isSuccess.value).toBe(true)
    expect(captured.detail.data.value?.status).toBe('running')
    expect(captured.detail.data.value?.progress).toBe(40)
  })

  it('polls while the job is queued or running and stops at a terminal status', async () => {
    const organisation = useOrganisationStore()
    organisation.setSelectedOrganisation(ORG_A)
    getMock.mockResolvedValue({ data: job('running', 40), error: undefined })

    mountQueries()
    await flushPromises()
    await flushPromises()

    // Active status: the interval is armed (1s polling).
    expect(pollDecision(jobsQueryKeys.detail(ORG_A, JOB_ID))).toBe(1000)

    // The worker finishes: the next refetch returns the terminal state and
    // the interval decision flips to false.
    getMock.mockResolvedValue({ data: job('succeeded', 100), error: undefined })
    await queryClient.refetchQueries({ queryKey: jobsQueryKeys.detail(ORG_A, JOB_ID) })
    await flushPromises()
    await flushPromises()

    expect(captured.detail.data.value?.status).toBe('succeeded')
    expect(pollDecision(jobsQueryKeys.detail(ORG_A, JOB_ID))).toBe(false)
  })

  it('queued jobs poll too (they have not started but will run)', async () => {
    const organisation = useOrganisationStore()
    organisation.setSelectedOrganisation(ORG_A)
    getMock.mockResolvedValue({ data: job('queued', 0), error: undefined })

    mountQueries()
    await flushPromises()
    await flushPromises()

    expect(pollDecision(jobsQueryKeys.detail(ORG_A, JOB_ID))).toBe(1000)
  })

  it('surfaces the client error when the job request fails', async () => {
    const organisation = useOrganisationStore()
    organisation.setSelectedOrganisation(ORG_A)
    const mockError = { code: 'jobs.get_failed', message: 'Boom' }
    getMock.mockResolvedValue({ data: undefined, error: mockError })

    mountQueries()
    await flushPromises()
    await flushPromises()

    expect(captured.detail.isError.value).toBe(true)
    expect(captured.detail.error.value).toEqual(mockError)
  })
})
