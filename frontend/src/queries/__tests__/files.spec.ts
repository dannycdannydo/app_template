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
    DELETE: vi.fn<typeof client.DELETE>(),
  },
}))

vi.mock('@/lib/upload', () => ({
  putFile: vi.fn<typeof putFile>(),
}))

import { client } from '@/api/client'
import type { components } from '@/api/generated/openapi'
import { putFile } from '@/lib/upload'
import {
  filesQueryKeys,
  useCompleteUploadMutation,
  useCreateUploadIntentMutation,
  useDeleteFileMutation,
  useDownloadFileMutation,
  useFileQuery,
  useFilesQuery,
  useUploadFileMutation,
} from '@/queries/files'
import { queryClient } from '@/queries/queryClient'
import { useOrganisationStore } from '@/stores/organisation'

type FileListResponse = components['schemas']['FileListResponse']
type FileListItem = components['schemas']['FileListItem']
type FileDetail = components['schemas']['FileDetail']
type FileCompleteResponse = components['schemas']['FileCompleteResponse']

const ORG_A = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const FILE_ID = '11111111-1111-4111-8111-111111111111'
const JOB_ID = '22222222-2222-4222-8222-222222222222'

const getMock = vi.mocked(client.GET)
const postMock = vi.mocked(client.POST)
const deleteMock = vi.mocked(client.DELETE)
const putFileMock = vi.mocked(putFile)

const listItem: FileListItem = {
  id: FILE_ID,
  original_filename: 'report.pdf',
  content_type: 'application/pdf',
  size_bytes: 1024,
  status: 'ready',
  created_by_user_id: 'u1',
  created_at: '2026-01-01T00:00:00Z',
}

const fileDetail: FileDetail = {
  ...listItem,
  checksum: null,
  updated_at: '2026-01-01T00:00:00Z',
}

const completeResponse: FileCompleteResponse = {
  ...fileDetail,
  status: 'uploaded',
  processing_job_id: JOB_ID,
}

function listEnvelope(items: FileListItem[] = []): FileListResponse {
  return { items, page: 1, page_size: 50, total: items.length }
}

function flushPromises(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

let pinia: Pinia

const uploadProgressEvents: Array<{ loaded: number; total: number }> = []

let captured!: {
  list: ReturnType<typeof useFilesQuery>
  detail: ReturnType<typeof useFileQuery>
  intent: ReturnType<typeof useCreateUploadIntentMutation>
  complete: ReturnType<typeof useCompleteUploadMutation>
  remove: ReturnType<typeof useDeleteFileMutation>
  download: ReturnType<typeof useDownloadFileMutation>
  upload: ReturnType<typeof useUploadFileMutation>
}

function mountQueries(): void {
  const CapturingComponent = defineComponent({
    setup() {
      captured = {
        list: useFilesQuery({ page: 1, pageSize: 50 }),
        detail: useFileQuery(FILE_ID),
        intent: useCreateUploadIntentMutation(),
        complete: useCompleteUploadMutation(),
        remove: useDeleteFileMutation(),
        download: useDownloadFileMutation(),
        upload: useUploadFileMutation({
          onProgress: (p) => uploadProgressEvents.push(p),
        }),
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

describe('files query composables', () => {
  beforeEach(() => {
    localStorage.clear()
    pinia = createPinia()
    setActivePinia(pinia)
    getMock.mockReset()
    postMock.mockReset()
    deleteMock.mockReset()
    putFileMock.mockReset()
    uploadProgressEvents.length = 0
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

    expect(getMock).toHaveBeenCalledWith('/api/v1/files', {
      params: { query: { page: 1, page_size: 50, status: undefined } },
    })
    expect(captured.list.isSuccess.value).toBe(true)
    expect(captured.list.data.value?.total).toBe(1)
    expect(captured.list.data.value?.items[0]?.original_filename).toBe('report.pdf')
  })

  it('forwards the approved status filter to the API', async () => {
    const organisation = useOrganisationStore()
    organisation.setSelectedOrganisation(ORG_A)
    getMock.mockResolvedValue({ data: listEnvelope([]), error: undefined })

    const CapturingComponent = defineComponent({
      setup() {
        captured = {
          ...captured,
          list: useFilesQuery({ page: 2, pageSize: 25, status: 'processing' }),
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

    expect(getMock).toHaveBeenCalledWith('/api/v1/files', {
      params: { query: { page: 2, page_size: 25, status: 'processing' } },
    })
  })

  it('stays disabled without a selected organisation', async () => {
    mountQueries()
    await flushPromises()
    await flushPromises()

    expect(getMock).not.toHaveBeenCalled()
  })

  it('fetches a single file through the generated client', async () => {
    const organisation = useOrganisationStore()
    organisation.setSelectedOrganisation(ORG_A)
    getMock.mockResolvedValue({ data: fileDetail, error: undefined })

    mountQueries()
    await flushPromises()
    await flushPromises()

    expect(getMock).toHaveBeenCalledWith('/api/v1/files/{file_id}', {
      params: { path: { file_id: FILE_ID } },
    })
    expect(captured.detail.isSuccess.value).toBe(true)
    expect(captured.detail.data.value?.id).toBe(FILE_ID)
  })

  it('upload-intent mutation posts the declared file metadata and resolves with the signed URL', async () => {
    const organisation = useOrganisationStore()
    organisation.setSelectedOrganisation(ORG_A)
    postMock.mockResolvedValue({
      data: {
        file_id: FILE_ID,
        upload_url: 'https://storage.example.com/upload',
        expires_at: '2026-01-01T00:01:00Z',
      },
      error: undefined,
      response: new Response(),
    })

    mountQueries()
    await flushPromises()

    const result = await captured.intent.mutateAsync({
      original_filename: 'report.pdf',
      content_type: 'application/pdf',
      size_bytes: 1024,
    })

    expect(postMock).toHaveBeenCalledWith('/api/v1/files', {
      body: { original_filename: 'report.pdf', content_type: 'application/pdf', size_bytes: 1024 },
    })
    expect(result.intent.upload_url).toBe('https://storage.example.com/upload')
  })

  it('complete mutation posts the file id and invalidates lists and details', async () => {
    const organisation = useOrganisationStore()
    organisation.setSelectedOrganisation(ORG_A)
    getMock.mockResolvedValue({ data: listEnvelope([]), error: undefined })
    postMock.mockResolvedValue({
      data: completeResponse,
      error: undefined,
      response: new Response(),
    })

    mountQueries()
    await flushPromises()
    await flushPromises()

    await captured.complete.mutateAsync({ fileId: FILE_ID })
    await flushPromises()

    expect(postMock).toHaveBeenCalledWith('/api/v1/files/{file_id}/complete', {
      params: { path: { file_id: FILE_ID } },
      body: {},
    })
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: filesQueryKeys.lists(ORG_A) })
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: filesQueryKeys.details(ORG_A) })
  })

  it('delete mutation removes the file and invalidates list and detail', async () => {
    const organisation = useOrganisationStore()
    organisation.setSelectedOrganisation(ORG_A)
    getMock.mockResolvedValue({ data: listEnvelope([]), error: undefined })
    deleteMock.mockResolvedValue({
      data: undefined,
      error: undefined,
      response: new Response(),
    })

    mountQueries()
    await flushPromises()
    await flushPromises()

    await captured.remove.mutateAsync(FILE_ID)
    await flushPromises()

    expect(deleteMock).toHaveBeenCalledWith('/api/v1/files/{file_id}', {
      params: { path: { file_id: FILE_ID } },
    })
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: filesQueryKeys.lists(ORG_A) })
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: filesQueryKeys.detail(ORG_A, FILE_ID),
    })
  })

  it('download mutation resolves with the signed GET URL', async () => {
    const organisation = useOrganisationStore()
    organisation.setSelectedOrganisation(ORG_A)
    getMock.mockResolvedValue({
      data: {
        download_url: 'https://storage.example.com/download?X-Amz-Signature=abc',
        expires_at: '2026-01-01T00:02:00Z',
      },
      error: undefined,
    })

    mountQueries()
    await flushPromises()

    const url = await captured.download.mutateAsync(FILE_ID)
    expect(getMock).toHaveBeenCalledWith('/api/v1/files/{file_id}/download-url', {
      params: { path: { file_id: FILE_ID } },
    })
    expect(url).toContain('X-Amz-Signature')
  })

  it('upload flow PUTs the file to the signed URL, reports progress and completes with the job id', async () => {
    const organisation = useOrganisationStore()
    organisation.setSelectedOrganisation(ORG_A)
    getMock.mockResolvedValue({ data: listEnvelope([]), error: undefined })
    postMock
      .mockResolvedValueOnce({
        data: {
          file_id: FILE_ID,
          upload_url: 'https://storage.example.com/upload',
          expires_at: '2026-01-01T00:01:00Z',
        },
        error: undefined,
        response: new Response(),
      })
      .mockResolvedValueOnce({
        data: completeResponse,
        error: undefined,
        response: new Response(),
      })

    mountQueries()
    await flushPromises()
    await flushPromises()

    const file = new File(['pdf-bytes'], 'report.pdf', { type: 'application/pdf' })
    const result = await captured.upload.mutateAsync(file)

    // Intent first, then the raw PUT against the signed URL (never the
    // generated client), then completion.
    expect(postMock).toHaveBeenNthCalledWith(1, '/api/v1/files', {
      body: {
        original_filename: 'report.pdf',
        content_type: 'application/pdf',
        size_bytes: file.size,
      },
    })
    expect(putFileMock).toHaveBeenCalledWith(
      'https://storage.example.com/upload',
      file,
      expect.any(Function),
    )
    expect(postMock).toHaveBeenNthCalledWith(2, '/api/v1/files/{file_id}/complete', {
      params: { path: { file_id: FILE_ID } },
      body: {},
    })
    expect(result.file.processing_job_id).toBe(JOB_ID)

    // Progress events from the PUT flow through the composable's callback.
    const progressCallback = putFileMock.mock.calls[0]?.[2]
    progressCallback?.({ loaded: 512, total: 1024 })
    expect(uploadProgressEvents).toEqual([{ loaded: 512, total: 1024 }])
  })

  it('surfaces the client error when the list request fails', async () => {
    const organisation = useOrganisationStore()
    organisation.setSelectedOrganisation(ORG_A)
    const mockError = { code: 'files.list_failed', message: 'Boom' }
    getMock.mockResolvedValue({ data: undefined, error: mockError })

    mountQueries()
    await flushPromises()
    await flushPromises()

    expect(captured.list.isError.value).toBe(true)
    expect(captured.list.error.value).toEqual(mockError)
  })
})
