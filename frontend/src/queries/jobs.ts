import { useQuery } from '@tanstack/vue-query'
import type { MaybeRefOrGetter } from 'vue'
import { computed, toValue } from 'vue'

import { client } from '@/api/client'
import type { components } from '@/api/generated/openapi'
import { useOrganisationStore } from '@/stores/organisation'

type JobStatus = components['schemas']['JobStatus']

/**
 * List parameters accepted by the jobs query layer (Scope §6.6).
 *
 * camelCase on the TS side; the snake_case API query parameters are produced
 * here, in one place (blueprint §12). `status` and `job_type` are optional
 * filters and only approved values may be sent.
 */
export interface JobsListParams {
  page: number
  pageSize: number
  status?: JobStatus | null
  jobType?: string | null
}

/**
 * Query-key factory for the jobs domain (Scope §6.6). Keys are
 * per-organisation under the `organisations` root so the organisation-switch
 * invalidator covers them automatically (src/queries/records.ts documents
 * the convention).
 */
export const jobsQueryKeys = {
  all: ['organisations'] as const,
  lists: (organisationId: string) => ['organisations', organisationId, 'jobs', 'list'] as const,
  list: (organisationId: string, params: JobsListParams) =>
    ['organisations', organisationId, 'jobs', 'list', params] as const,
  details: (organisationId: string) => ['organisations', organisationId, 'jobs', 'detail'] as const,
  detail: (organisationId: string, jobId: string) =>
    ['organisations', organisationId, 'jobs', 'detail', jobId] as const,
}

/** Job statuses that still make progress and therefore warrant polling. */
const ACTIVE_JOB_STATUSES: ReadonlySet<string> = new Set(['queued', 'running'])

/**
 * Paginated, organisation-scoped jobs list (Scope §6.5, §6.6).
 *
 * The files UI itself never lists jobs; the composable exists so the full
 * jobs API surface has a typed consumer contract for later views and for the
 * Vitest coverage the release requires. Like every org-scoped query it reads
 * the selected organisation from Pinia and stays disabled without one.
 */
export function useJobsQuery(params: MaybeRefOrGetter<JobsListParams>) {
  const organisation = useOrganisationStore()
  const organisationId = computed(() => organisation.selectedOrganisationId)
  const resolvedParams = computed(() => toValue(params))

  return useQuery({
    queryKey: computed(() =>
      organisationId.value === null
        ? (['organisations', null] as const)
        : jobsQueryKeys.list(organisationId.value, resolvedParams.value),
    ),
    queryFn: async () => {
      if (organisationId.value === null) {
        throw new Error('Cannot list jobs without a selected organisation')
      }
      const { data, error } = await client.GET('/api/v1/jobs', {
        params: {
          query: {
            page: resolvedParams.value.page,
            page_size: resolvedParams.value.pageSize,
            status: resolvedParams.value.status ?? undefined,
            job_type: resolvedParams.value.jobType ?? undefined,
          },
        },
      })
      if (error) throw error
      if (!data) throw new Error('Empty jobs list response')
      return data
    },
    enabled: computed(() => organisationId.value !== null),
    retry: false,
    staleTime: 30_000,
  })
}

/**
 * Job detail query with progress polling (Scope §6.6).
 *
 * The upload component polls the file-processing job until it reaches a
 * terminal state: while the cached status is `queued` or `running` the query
 * refetches on a one-second interval (`refetchInterval`), and the moment the
 * status turns `succeeded`/`failed`/`cancelled` polling stops automatically.
 * A cross-organisation job id resolves to a 404 server-side, exactly like the
 * files endpoints.
 */
export function useJobQuery(jobId: MaybeRefOrGetter<string>) {
  const organisation = useOrganisationStore()
  const organisationId = computed(() => organisation.selectedOrganisationId)
  const resolvedJobId = computed(() => toValue(jobId))

  return useQuery({
    queryKey: computed(() =>
      organisationId.value === null
        ? (['organisations', null] as const)
        : jobsQueryKeys.detail(organisationId.value, resolvedJobId.value),
    ),
    queryFn: async () => {
      if (organisationId.value === null) {
        throw new Error('Cannot load a job without a selected organisation')
      }
      const { data, error } = await client.GET('/api/v1/jobs/{job_id}', {
        params: { path: { job_id: resolvedJobId.value } },
      })
      if (error) throw error
      if (!data) throw new Error('Empty job response')
      return data
    },
    enabled: computed(() => organisationId.value !== null && resolvedJobId.value !== ''),
    retry: false,
    staleTime: 30_000,
    refetchInterval: (query) => {
      const status = query.state.data?.status
      return status !== undefined && ACTIVE_JOB_STATUSES.has(status) ? 1000 : false
    },
  })
}
