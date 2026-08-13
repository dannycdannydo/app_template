import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import type { MaybeRefOrGetter } from 'vue'
import { computed, toValue } from 'vue'

import { client } from '@/api/client'
import type { components } from '@/api/generated/openapi'
import { useOrganisationStore } from '@/stores/organisation'
import { jobsQueryKeys } from '@/queries/jobs'
import { putFile, type UploadProgress } from '@/lib/upload'

type ClassifyRequest = components['schemas']['DocumentClassifyRequest']
type ClassifySyncResponse = components['schemas']['DocumentClassifySyncResponse']
type ClassifyAcceptedResponse = components['schemas']['DocumentClassifyAcceptedResponse']
type ClassifyResultResponse = components['schemas']['DocumentClassifyResultResponse']
type AskRequest = components['schemas']['DocumentAskRequest']
type AskResponse = components['schemas']['DocumentAskResponse']
type ScratchUploadIntentRequest = components['schemas']['ScratchUploadIntentRequest']
type ScratchUploadCompleteResponse = components['schemas']['ScratchUploadCompleteResponse']

/**
 * Query-key factory for the AI classification demonstration (v0.7 Scope §6.6).
 * Keys are per-organisation under the `organisations` root so the
 * organisation-switch invalidator covers them automatically (the same
 * convention ``src/queries/records.ts`` documents).
 */
export const aiQueryKeys = {
  all: ['organisations'] as const,
  results: (organisationId: string) =>
    ['organisations', organisationId, 'ai', 'classify', 'result'] as const,
  result: (organisationId: string, requestId: string) =>
    ['organisations', organisationId, 'ai', 'classify', 'result', requestId] as const,
}

/** Classification statuses that are not yet terminal and therefore warrant polling. */
const ACTIVE_CLASSIFY_STATUSES: ReadonlySet<string> = new Set(['queued', 'running'])

/**
 * Determine whether a classify POST response acknowledged a durable job (the
 * async path) rather than returning the synchronous result inline. The accepted
 * body carries ``job_id`` and ``status``; the sync body carries the validated
 * classification ``output`` (v0.7 Scope §6.6).
 */
export function isClassifyAccepted(
  response: ClassifySyncResponse | ClassifyAcceptedResponse,
): response is ClassifyAcceptedResponse {
  return (response as ClassifyAcceptedResponse).job_id !== undefined
}

/**
 * Submit a ``document.classify`` classification (v0.7 Scope §6.6).
 *
 * ``sync=true`` runs synchronously and the mutation resolves with the validated
 * result; the default durable path is acknowledged with a durable job id the
 * caller then polls through {@link useClassifyResultQuery} (and the existing
 * jobs API for raw status/progress). Like every org-scoped call it reads the
 * selected organisation from Pinia. No Vue component or Pinia store imports the
 * API client directly (BP §15); this composable is the only seam.
 */
export function useClassifyMutation() {
  const organisation = useOrganisationStore()
  const organisationId = computed(() => organisation.selectedOrganisationId)
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: async (payload: ClassifyRequest) => {
      if (organisationId.value === null) {
        throw new Error('Cannot classify without a selected organisation')
      }
      const { data, error } = await client.POST('/api/v1/ai/classify', { body: payload })
      if (error) throw error
      if (!data) throw new Error('Empty classify response')
      return data
    },
    onSuccess: (data) => {
      if (isClassifyAccepted(data)) {
        // Invalidate the job list so the newly queued ``ai.execute`` job is
        // visible to the existing jobs surface, and prefetch its result slot.
        if (organisationId.value !== null) {
          void queryClient.invalidateQueries({
            queryKey: jobsQueryKeys.lists(organisationId.value),
          })
        }
      }
    },
  })
}

/**
 * The durable classification record for one request id (v0.7 Scope §6.6).
 *
 * After an async submission the caller polls the jobs API for raw status, then
 * reads the validated result here. While the durable status is still
 * ``queued``/``running`` the query refetches on a one-second interval; the
 * moment it turns ``succeeded``/``failed`` polling stops automatically. A
 * cross-organisation request id resolves to a 404 server-side.
 */
export function useClassifyResultQuery(requestId: MaybeRefOrGetter<string>) {
  const organisation = useOrganisationStore()
  const organisationId = computed(() => organisation.selectedOrganisationId)
  const resolvedRequestId = computed(() => toValue(requestId))

  return useQuery({
    queryKey: computed(() =>
      organisationId.value === null
        ? (['organisations', null] as const)
        : aiQueryKeys.result(organisationId.value, resolvedRequestId.value),
    ),
    queryFn: async () => {
      if (organisationId.value === null) {
        throw new Error('Cannot load a classification result without a selected organisation')
      }
      const { data, error } = await client.GET('/api/v1/ai/classify/requests/{request_id}', {
        params: { path: { request_id: resolvedRequestId.value } },
      })
      if (error) throw error
      if (!data) throw new Error('Empty classification result response')
      return data
    },
    enabled: computed(() => organisationId.value !== null && resolvedRequestId.value !== ''),
    // A bounded retry count so a transient network blip during the queued/
    // running window does not permanently abort polling — the pre-enqueue
    // ``queued`` AI request row means the first fetch returns 200, not 404
    // (v0.7 Scope §5.8), but resilience here is still safer than ``false``.
    retry: 2,
    staleTime: 30_000,
    refetchInterval: (query) => {
      const status = (query.state.data as ClassifyResultResponse | undefined)?.status
      return status !== undefined && ACTIVE_CLASSIFY_STATUSES.has(status) ? 1000 : false
    },
  })
}

/**
 * Submit one ``document.ask`` question about a stored document (v0.8 Scope
 * §2.2/§6.4).
 *
 * Synchronous only: the reference is resolved server-side (inline at or below
 * the 5 MB threshold, Vertex private GCS staging above it) and the mutation
 * resolves with the validated text answer plus safe routing/usage metadata.
 * Like every org-scoped call it reads the selected organisation from Pinia;
 * the generated client is never imported outside this query layer (BP §15).
 */
export function useAskMutation() {
  const organisation = useOrganisationStore()
  const organisationId = computed(() => organisation.selectedOrganisationId)

  return useMutation({
    mutationFn: async (payload: AskRequest) => {
      if (organisationId.value === null) {
        throw new Error('Cannot ask about a document without a selected organisation')
      }
      const { data, error } = await client.POST('/api/v1/ai/ask', { body: payload })
      if (error) throw error
      if (!data) throw new Error('Empty ask response')
      return data as AskResponse
    },
  })
}

/**
 * Full transient upload flow for the AI test screen (v0.8 Scope §2.2/§6.5):
 * scratch intent → direct PUT → complete.
 *
 * The uploaded object lands in the organisation-scoped ``ai/scratch/``
 * namespace, which the AI layer classifies as transient — a >5 MB PDF then
 * routes through the provider-upload mode instead of the retained signed-URL
 * path. No processing job exists for scratch objects: the mutation resolves
 * with the storage reference the caller sends to the ask endpoint. The PUT
 * goes to the signed URL through `putFile` (never the generated client), the
 * same direct-upload transport the files module uses.
 */
export function useScratchUploadMutation(options?: {
  onProgress?: (progress: UploadProgress) => void
  onSuccess?: (storageReference: string) => void
}) {
  const organisation = useOrganisationStore()
  const organisationId = computed(() => organisation.selectedOrganisationId)

  return useMutation({
    mutationFn: async (file: File): Promise<string> => {
      if (organisationId.value === null) {
        throw new Error('Cannot upload without a selected organisation')
      }
      const intent: ScratchUploadIntentRequest = {
        original_filename: file.name,
        content_type: file.type || 'application/octet-stream',
        size_bytes: file.size,
      }
      const { data: intentData, error: intentError } = await client.POST(
        '/api/v1/ai/scratch/uploads',
        { body: intent },
      )
      if (intentError) throw intentError
      if (!intentData) throw new Error('Empty scratch upload intent response')
      await putFile(intentData.upload_url, file, options?.onProgress)
      const { data: completeData, error: completeError } = await client.POST(
        '/api/v1/ai/scratch/uploads/{upload_id}/complete',
        { params: { path: { upload_id: intentData.upload_id } } },
      )
      if (completeError) throw completeError
      if (!completeData) throw new Error('Empty scratch upload completion response')
      return (completeData as ScratchUploadCompleteResponse).storage_reference
    },
    onSuccess: (reference) => {
      options?.onSuccess?.(reference)
    },
  })
}
