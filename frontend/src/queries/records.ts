import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import type { MaybeRefOrGetter } from 'vue'
import { computed, toValue } from 'vue'

import { client } from '@/api/client'
import type { components } from '@/api/generated/openapi'
import { useOrganisationStore } from '@/stores/organisation'

type RecordCreate = components['schemas']['RecordCreate']
type RecordDetail = components['schemas']['RecordDetail']
type RecordUpdate = components['schemas']['RecordUpdate']

/**
 * List parameters accepted by the records query layer.
 *
 * TS side is camelCase; the mapping to the API's snake_case query parameters
 * happens here, in one place (v0.3 Scope §6.4, blueprint §12 pagination
 * conventions: `?page=1&page_size=50`). When the backend adds filter or sort
 * fields, extend this type with `search`, `status`, `sort` etc. following the
 * blueprint §12 conventions; only fields the API actually accepts may be sent.
 */
export interface RecordsListParams {
  page: number
  pageSize: number
}

/**
 * Query-key factory for the records domain (v0.3 Scope §6.4).
 *
 * Keys are per-organisation: every org-scoped key starts with
 * `['organisations', <orgId>]` so switching the selected organisation
 * automatically addresses a different cache partition, and a single
 * invalidation predicate (`queryKey[0] === 'organisations'`) can refetch the
 * whole org-scoped subtree (see `src/queries/organisationInvalidation.ts`).
 * The `['organisations']` root is reserved for that predicate and must never
 * be used as a query key itself.
 *
 * List keys carry the normalized params object as the final segment so
 * pagination changes address distinct cache entries.
 */
export const recordsQueryKeys = {
  all: ['organisations'] as const,
  orgRoot: (organisationId: string) => ['organisations', organisationId] as const,
  lists: (organisationId: string) => ['organisations', organisationId, 'records', 'list'] as const,
  list: (organisationId: string, params: RecordsListParams) =>
    ['organisations', organisationId, 'records', 'list', params] as const,
  details: (organisationId: string) =>
    ['organisations', organisationId, 'records', 'detail'] as const,
  detail: (organisationId: string, recordId: string) =>
    ['organisations', organisationId, 'records', 'detail', recordId] as const,
}

/**
 * Paginated, organisation-scoped records list (v0.3 Scope §6.4).
 *
 * Reads the selected organisation from the Pinia store; without one the query
 * stays disabled (there is nothing to list). Components consume the returned
 * query state only and never touch the HTTP client (blueprint §14, §15).
 */
export function useRecordsQuery(params: MaybeRefOrGetter<RecordsListParams>) {
  const organisation = useOrganisationStore()
  const organisationId = computed(() => organisation.selectedOrganisationId)
  const resolvedParams = computed(() => toValue(params))

  return useQuery({
    queryKey: computed(() =>
      organisationId.value === null
        ? (['organisations', null] as const)
        : recordsQueryKeys.list(organisationId.value, resolvedParams.value),
    ),
    queryFn: async () => {
      if (organisationId.value === null) {
        throw new Error('Cannot list records without a selected organisation')
      }
      const { data, error } = await client.GET('/api/v1/records', {
        params: {
          query: { page: resolvedParams.value.page, page_size: resolvedParams.value.pageSize },
        },
      })
      if (error) throw error
      if (!data) throw new Error('Empty records list response')
      return data
    },
    enabled: computed(() => organisationId.value !== null),
    retry: false,
    staleTime: 30_000,
  })
}

/**
 * Single-record detail query, scoped to the selected organisation
 * (v0.3 Scope §6.4).
 */
export function useRecordQuery(recordId: MaybeRefOrGetter<string>) {
  const organisation = useOrganisationStore()
  const organisationId = computed(() => organisation.selectedOrganisationId)
  const resolvedRecordId = computed(() => toValue(recordId))

  return useQuery({
    queryKey: computed(() =>
      organisationId.value === null
        ? (['organisations', null] as const)
        : recordsQueryKeys.detail(organisationId.value, resolvedRecordId.value),
    ),
    queryFn: async () => {
      if (organisationId.value === null) {
        throw new Error('Cannot load a record without a selected organisation')
      }
      const { data, error } = await client.GET('/api/v1/records/{record_id}', {
        params: { path: { record_id: resolvedRecordId.value } },
      })
      if (error) throw error
      if (!data) throw new Error('Empty record response')
      return data
    },
    enabled: computed(() => organisationId.value !== null),
    retry: false,
    staleTime: 30_000,
  })
}

/**
 * Create mutation with list invalidation (v0.3 Scope §6.4).
 *
 * The organisation is captured when the mutation runs, so the invalidation
 * always targets the list of the organisation the record was actually created
 * in, even if the user switches organisations while the request is in flight.
 */
export function useCreateRecordMutation(options?: { onSuccess?: (record: RecordDetail) => void }) {
  const queryClient = useQueryClient()
  const organisation = useOrganisationStore()

  return useMutation({
    mutationFn: async (body: RecordCreate) => {
      const organisationId = organisation.selectedOrganisationId
      if (!organisationId) {
        throw new Error('Cannot create a record without a selected organisation')
      }
      const { data, error } = await client.POST('/api/v1/records', { body })
      if (error) throw error
      if (!data) throw new Error('Empty create record response')
      return { organisationId, record: data }
    },
    onSuccess: (result) => {
      void queryClient.invalidateQueries({
        queryKey: recordsQueryKeys.lists(result.organisationId),
      })
      options?.onSuccess?.(result.record)
    },
  })
}

/**
 * Update mutation invalidating both the list and the updated record's detail
 * (v0.3 Scope §6.4).
 */
export function useUpdateRecordMutation(options?: { onSuccess?: (record: RecordDetail) => void }) {
  const queryClient = useQueryClient()
  const organisation = useOrganisationStore()

  return useMutation({
    mutationFn: async ({ recordId, payload }: { recordId: string; payload: RecordUpdate }) => {
      const organisationId = organisation.selectedOrganisationId
      if (!organisationId) {
        throw new Error('Cannot update a record without a selected organisation')
      }
      const { data, error } = await client.PATCH('/api/v1/records/{record_id}', {
        params: { path: { record_id: recordId } },
        body: payload,
      })
      if (error) throw error
      if (!data) throw new Error('Empty update record response')
      return { organisationId, record: data }
    },
    onSuccess: (result) => {
      void queryClient.invalidateQueries({
        queryKey: recordsQueryKeys.lists(result.organisationId),
      })
      void queryClient.invalidateQueries({
        queryKey: recordsQueryKeys.detail(result.organisationId, result.record.id),
      })
      options?.onSuccess?.(result.record)
    },
  })
}

/**
 * Delete mutation invalidating the list and the deleted record's detail
 * (v0.3 Scope §6.4).
 */
export function useDeleteRecordMutation(options?: { onSuccess?: (recordId: string) => void }) {
  const queryClient = useQueryClient()
  const organisation = useOrganisationStore()

  return useMutation({
    mutationFn: async (recordId: string) => {
      const organisationId = organisation.selectedOrganisationId
      if (!organisationId) {
        throw new Error('Cannot delete a record without a selected organisation')
      }
      const { error } = await client.DELETE('/api/v1/records/{record_id}', {
        params: { path: { record_id: recordId } },
      })
      if (error) throw error
      return { organisationId, recordId }
    },
    onSuccess: (result) => {
      void queryClient.invalidateQueries({
        queryKey: recordsQueryKeys.lists(result.organisationId),
      })
      // The deleted record's detail key is invalidated deliberately:
      // invalidateQueries only refetches active (observed) queries, so a
      // still-mounted detail view refetches and surfaces the 404, which is
      // the correct behaviour.
      void queryClient.invalidateQueries({
        queryKey: recordsQueryKeys.detail(result.organisationId, result.recordId),
      })
      options?.onSuccess?.(result.recordId)
    },
  })
}
