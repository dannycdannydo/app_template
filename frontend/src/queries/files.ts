import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import type { MaybeRefOrGetter } from 'vue'
import { computed, toValue } from 'vue'

import { client } from '@/api/client'
import type { components } from '@/api/generated/openapi'
import { putFile } from '@/lib/upload'
import { useOrganisationStore } from '@/stores/organisation'

type FileStatus = components['schemas']['FileStatus']
type FileUploadIntentResponse = components['schemas']['FileUploadIntentResponse']
type FileCompleteResponse = components['schemas']['FileCompleteResponse']

/**
 * List parameters accepted by the files query layer (Scope §6.6).
 *
 * TS side is camelCase; the mapping to the API's snake_case query parameters
 * happens here, in one place (blueprint §12 pagination conventions). The
 * status filter is optional and only the enum values the API accepts may be
 * sent (blueprint §12: only approved filter fields).
 */
export interface FilesListParams {
  page: number
  pageSize: number
  status?: FileStatus | null
}

/** Progress reported while the browser PUTs bytes to the signed URL. */
export interface UploadFileProgress {
  loaded: number
  total: number
}

/**
 * Query-key factory for the files domain (Scope §6.6).
 *
 * Keys are per-organisation: every org-scoped key starts with
 * `['organisations', <orgId>]` so switching the selected organisation
 * automatically addresses a different cache partition, and the boot-time
 * invalidation predicate (src/queries/organisationInvalidation.ts) covers
 * the whole org-scoped subtree. The `['organisations']` root is reserved for
 * that predicate and must never be used as a query key itself.
 */
export const filesQueryKeys = {
  all: ['organisations'] as const,
  lists: (organisationId: string) => ['organisations', organisationId, 'files', 'list'] as const,
  list: (organisationId: string, params: FilesListParams) =>
    ['organisations', organisationId, 'files', 'list', params] as const,
  details: (organisationId: string) =>
    ['organisations', organisationId, 'files', 'detail'] as const,
  detail: (organisationId: string, fileId: string) =>
    ['organisations', organisationId, 'files', 'detail', fileId] as const,
}

/**
 * Paginated, organisation-scoped files list (Scope §6.6).
 *
 * Reads the selected organisation from the Pinia store; without one the query
 * stays disabled (there is nothing to list). Components consume the returned
 * query state only and never touch the HTTP client (blueprint §14, §15).
 */
export function useFilesQuery(params: MaybeRefOrGetter<FilesListParams>) {
  const organisation = useOrganisationStore()
  const organisationId = computed(() => organisation.selectedOrganisationId)
  const resolvedParams = computed(() => toValue(params))

  return useQuery({
    queryKey: computed(() =>
      organisationId.value === null
        ? (['organisations', null] as const)
        : filesQueryKeys.list(organisationId.value, resolvedParams.value),
    ),
    queryFn: async () => {
      if (organisationId.value === null) {
        throw new Error('Cannot list files without a selected organisation')
      }
      const { data, error } = await client.GET('/api/v1/files', {
        params: {
          query: {
            page: resolvedParams.value.page,
            page_size: resolvedParams.value.pageSize,
            status: resolvedParams.value.status ?? undefined,
          },
        },
      })
      if (error) throw error
      if (!data) throw new Error('Empty files list response')
      return data
    },
    enabled: computed(() => organisationId.value !== null),
    retry: false,
    staleTime: 30_000,
  })
}

/**
 * Single-file detail query, scoped to the selected organisation (Scope §6.6).
 */
export function useFileQuery(fileId: MaybeRefOrGetter<string>) {
  const organisation = useOrganisationStore()
  const organisationId = computed(() => organisation.selectedOrganisationId)
  const resolvedFileId = computed(() => toValue(fileId))

  return useQuery({
    queryKey: computed(() =>
      organisationId.value === null
        ? (['organisations', null] as const)
        : filesQueryKeys.detail(organisationId.value, resolvedFileId.value),
    ),
    queryFn: async () => {
      if (organisationId.value === null) {
        throw new Error('Cannot load a file without a selected organisation')
      }
      const { data, error } = await client.GET('/api/v1/files/{file_id}', {
        params: { path: { file_id: resolvedFileId.value } },
      })
      if (error) throw error
      if (!data) throw new Error('Empty file response')
      return data
    },
    enabled: computed(() => organisationId.value !== null),
    retry: false,
    staleTime: 30_000,
  })
}

/**
 * Upload-intent mutation (Scope §6.6, blueprint §17 step 1).
 *
 * Validates the declared filename/content-type/size and returns the signed
 * PUT URL. No client-supplied `object_key` or `storage_provider` exists on
 * the generated request type — the backend forbids both (`extra="forbid"`).
 */
export function useCreateUploadIntentMutation(options?: {
  onSuccess?: (intent: FileUploadIntentResponse) => void
}) {
  const organisation = useOrganisationStore()

  return useMutation({
    mutationFn: async (body: {
      original_filename: string
      content_type: string
      size_bytes: number
    }) => {
      const organisationId = organisation.selectedOrganisationId
      if (!organisationId) {
        throw new Error('Cannot start an upload without a selected organisation')
      }
      const { data, error } = await client.POST('/api/v1/files', { body })
      if (error) throw error
      if (!data) throw new Error('Empty upload intent response')
      return { organisationId, intent: data }
    },
    onSuccess: (result) => {
      options?.onSuccess?.(result.intent)
    },
  })
}

/**
 * Upload-completion mutation (Scope §6.6, blueprint §17 step 3).
 *
 * Verifies the stored object server-side, transitions the file to
 * `uploaded` and enqueues the processing job; the response carries the
 * `processing_job_id` the client polls via `useJobQuery`. Success
 * invalidates the files lists and details so the table reflects the new
 * status immediately.
 */
export function useCompleteUploadMutation(options?: {
  onSuccess?: (result: { organisationId: string; file: FileCompleteResponse }) => void
}) {
  const queryClient = useQueryClient()
  const organisation = useOrganisationStore()

  return useMutation({
    mutationFn: async ({ fileId, checksum }: { fileId: string; checksum?: string | null }) => {
      const organisationId = organisation.selectedOrganisationId
      if (!organisationId) {
        throw new Error('Cannot complete an upload without a selected organisation')
      }
      const { data, error } = await client.POST('/api/v1/files/{file_id}/complete', {
        params: { path: { file_id: fileId } },
        body: checksum ? { checksum } : {},
      })
      if (error) throw error
      if (!data) throw new Error('Empty upload completion response')
      return { organisationId, file: data }
    },
    onSuccess: (result) => {
      void queryClient.invalidateQueries({
        queryKey: filesQueryKeys.lists(result.organisationId),
      })
      void queryClient.invalidateQueries({
        queryKey: filesQueryKeys.details(result.organisationId),
      })
      options?.onSuccess?.(result)
    },
  })
}

/**
 * Delete mutation invalidating the list and the deleted file's detail
 * (Scope §6.6). Deletion is a soft delete server-side; the object is removed
 * from storage and the row is marked `deleted` (acceptance §5.4).
 */
export function useDeleteFileMutation(options?: { onSuccess?: (fileId: string) => void }) {
  const queryClient = useQueryClient()
  const organisation = useOrganisationStore()

  return useMutation({
    mutationFn: async (fileId: string) => {
      const organisationId = organisation.selectedOrganisationId
      if (!organisationId) {
        throw new Error('Cannot delete a file without a selected organisation')
      }
      const { error } = await client.DELETE('/api/v1/files/{file_id}', {
        params: { path: { file_id: fileId } },
      })
      if (error) throw error
      return { organisationId, fileId }
    },
    onSuccess: (result) => {
      void queryClient.invalidateQueries({
        queryKey: filesQueryKeys.lists(result.organisationId),
      })
      // The deleted file's detail key is invalidated deliberately: a
      // still-mounted detail view refetches and surfaces the 404, which is
      // the correct behaviour.
      void queryClient.invalidateQueries({
        queryKey: filesQueryKeys.detail(result.organisationId, result.fileId),
      })
      options?.onSuccess?.(result.fileId)
    },
  })
}

/**
 * Download mutation (Scope §6.6, blueprint §17 signed GET).
 *
 * Fetches the short-lived signed GET URL on demand and resolves with it, so
 * the caller can hand it to `triggerDownload` (`src/lib/download.ts`). It is
 * modelled as a mutation because it runs from a row-action gesture and has a
 * side effect (opening the download), not because it writes anything — the
 * endpoint is a plain `GET` that creates no state.
 */
export function useDownloadFileMutation(options?: { onSuccess?: (url: string) => void }) {
  const organisation = useOrganisationStore()

  return useMutation({
    mutationFn: async (fileId: string) => {
      const organisationId = organisation.selectedOrganisationId
      if (!organisationId) {
        throw new Error('Cannot download a file without a selected organisation')
      }
      const { data, error } = await client.GET('/api/v1/files/{file_id}/download-url', {
        params: { path: { file_id: fileId } },
      })
      if (error) throw error
      if (!data) throw new Error('Empty download URL response')
      return data.download_url
    },
    onSuccess: (url) => {
      options?.onSuccess?.(url)
    },
  })
}

/**
 * Full upload flow: intent → direct PUT → complete (Scope §6.6, blueprint
 * §17 direct upload flow).
 *
 * One mutation orchestrates the three steps the browser drives. The PUT goes
 * to the signed URL through `putFile` (never the generated client — the URL
 * may be cross-origin and progress needs XHR events); progress is reported
 * through `onProgress` so the upload component can render a real progress
 * bar. The resolved value is the completion response including the
 * `processing_job_id` the client polls to follow the file to `ready`.
 */
export function useUploadFileMutation(options?: {
  onProgress?: (progress: UploadFileProgress) => void
  onSuccess?: (result: { organisationId: string; file: FileCompleteResponse }) => void
}) {
  const organisation = useOrganisationStore()
  const intentMutation = useCreateUploadIntentMutation()
  const completeMutation = useCompleteUploadMutation({
    onSuccess: (result) => {
      options?.onSuccess?.(result)
    },
  })

  return useMutation({
    mutationFn: async (file: File) => {
      const organisationId = organisation.selectedOrganisationId
      if (!organisationId) {
        throw new Error('Cannot upload a file without a selected organisation')
      }
      const result = await intentMutation.mutateAsync({
        original_filename: file.name,
        content_type: file.type || 'application/octet-stream',
        size_bytes: file.size,
      })
      await putFile(result.intent.upload_url, file, options?.onProgress)
      return completeMutation.mutateAsync({ fileId: result.intent.file_id })
    },
  })
}
