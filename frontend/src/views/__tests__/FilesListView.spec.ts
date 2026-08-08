import { mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'

import { ApiError } from '@/api/errors'
import type { components } from '@/api/generated/openapi'
import FileUpload from '@/components/application/FileUpload.vue'
import type { FilesListParams } from '@/queries/files'
import type { MaybeRefOrGetter } from 'vue'

const mockUseFilesQuery = vi.hoisted(() =>
  vi.fn<(params: MaybeRefOrGetter<FilesListParams>) => unknown>(),
)
const mockUseFilePermissions = vi.hoisted(() => vi.fn<() => unknown>())
const mockUseDeleteFileMutation = vi.hoisted(() => vi.fn<(options?: unknown) => unknown>())
const mockUseDownloadFileMutation = vi.hoisted(() => vi.fn<(options?: unknown) => unknown>())
const mockUseUploadFileMutation = vi.hoisted(() => vi.fn<(options?: unknown) => unknown>())
const mockUseJobQuery = vi.hoisted(() => vi.fn<() => unknown>())
const mockTriggerDownload = vi.hoisted(() => vi.fn<(url: string) => void>())

vi.mock('@/queries/files', () => ({
  useFilesQuery: mockUseFilesQuery,
  useDeleteFileMutation: mockUseDeleteFileMutation,
  useDownloadFileMutation: mockUseDownloadFileMutation,
  useUploadFileMutation: mockUseUploadFileMutation,
}))

vi.mock('@/queries/jobs', () => ({
  useJobQuery: mockUseJobQuery,
}))

vi.mock('@/lib/permissions', () => ({
  useFilePermissions: mockUseFilePermissions,
}))

vi.mock('@/lib/download', () => ({
  triggerDownload: mockTriggerDownload,
}))

vi.mock('@/lib/toast', () => ({
  showApiErrorToast: vi.fn<(error: unknown, options?: { title?: string }) => void>(),
  showSuccessToast: vi.fn<(message: string) => void>(),
}))

import FilesListView from '@/views/FilesListView.vue'

type FileListItem = components['schemas']['FileListItem']
type FileListResponse = components['schemas']['FileListResponse']

const FILE_ID = '11111111-1111-4111-8111-111111111111'

const files: FileListItem[] = [
  {
    id: FILE_ID,
    original_filename: 'report.pdf',
    content_type: 'application/pdf',
    size_bytes: 2048,
    status: 'ready',
    created_by_user_id: 'u1',
    created_at: '2026-01-01T00:00:00Z',
  },
  {
    id: '22222222-2222-4222-8222-222222222222',
    original_filename: 'notes.txt',
    content_type: 'text/plain',
    size_bytes: 512,
    status: 'processing',
    created_by_user_id: 'u1',
    created_at: '2026-01-02T00:00:00Z',
  },
]

function listResponse(items: FileListItem[] = files): FileListResponse {
  return { items, page: 1, page_size: 25, total: items.length }
}

interface PermissionsShape {
  canUpload: boolean
  canDelete: boolean
}

let wrapper: VueWrapper

function stubPermissions(permissions: PermissionsShape, mePending = false): void {
  mockUseFilePermissions.mockReturnValue({
    permissions: ref(permissions),
    mePending: ref(mePending),
  })
}

function stubFilesQuery(overrides: Record<string, unknown> = {}): {
  data: ReturnType<typeof ref<FileListResponse>>
  refetch: ReturnType<typeof vi.fn>
} {
  const data = ref(listResponse())
  const refetch = vi.fn<() => void>()
  mockUseFilesQuery.mockReturnValue({
    data,
    isPending: ref(false),
    isError: ref(false),
    error: ref(null),
    refetch,
    ...overrides,
  })
  return { data, refetch }
}

async function mountView(): Promise<void> {
  wrapper = mount(FilesListView, { global: { plugins: [createPinia()] } })
}

describe('FilesListView', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
    mockUseFilesQuery.mockReset()
    mockUseFilePermissions.mockReset()
    mockUseDeleteFileMutation.mockReset()
    mockUseDownloadFileMutation.mockReset()
    mockTriggerDownload.mockReset()
    stubPermissions({ canUpload: true, canDelete: true })
    stubFilesQuery()
    mockUseDeleteFileMutation.mockReturnValue({
      isPending: ref(false),
      mutateAsync: vi.fn<(fileId: string) => Promise<void>>().mockResolvedValue(undefined),
    })
    mockUseDownloadFileMutation.mockImplementation((options?: unknown) => {
      const downloadOptions = (options ?? {}) as { onSuccess?: (url: string) => void }
      return {
        isPending: ref(false),
        mutateAsync: vi
          .fn<(fileId: string) => Promise<string>>()
          .mockImplementation(async (_fileId: string) => {
            const url = 'https://storage.example.com/dl'
            downloadOptions.onSuccess?.(url)
            return url
          }),
      }
    })
    mockUseUploadFileMutation.mockReturnValue({
      isPending: ref(false),
      mutateAsync: vi.fn<(file: File) => Promise<void>>().mockResolvedValue(undefined),
    })
    mockUseJobQuery.mockReturnValue({ data: ref(null), isError: ref(false) })
  })

  afterEach(() => {
    wrapper?.unmount()
    document.body.innerHTML = ''
  })

  it('renders files from the org-scoped query with formatted size and status badge', async () => {
    await mountView()

    const rows = wrapper.findAll('[data-testid="data-table-row"]')
    expect(rows).toHaveLength(2)
    expect(wrapper.text()).toContain('report.pdf')
    expect(wrapper.text()).toContain('2 KB')
    expect(wrapper.find('[data-testid="file-status-ready"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="file-status-processing"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('2 files')
  })

  it('shows the upload card for roles with documents.upload', async () => {
    stubPermissions({ canUpload: true, canDelete: false })
    await mountView()

    expect(wrapper.find('[data-testid="files-upload-card"]').exists()).toBe(true)
  })

  it('hides the upload card for a viewer (read-only UI)', async () => {
    stubPermissions({ canUpload: false, canDelete: false })
    await mountView()

    expect(wrapper.find('[data-testid="files-upload-card"]').exists()).toBe(false)
  })

  it('downloads a file through the signed URL', async () => {
    await mountView()

    const downloadButton = wrapper.find('[data-testid="file-download-button"]')
    await downloadButton.trigger('click')
    await new Promise((resolve) => setTimeout(resolve, 0))

    expect(mockTriggerDownload).toHaveBeenCalledWith('https://storage.example.com/dl')
  })

  it('deletes a file only after the explicit confirmation step', async () => {
    await mountView()
    const deleteMutation = mockUseDeleteFileMutation.mock.results[0]?.value as {
      mutateAsync: ReturnType<typeof vi.fn>
    }

    const firstRow = wrapper.findAll('[data-testid="data-table-row"]')[0]!
    await firstRow.find('[data-testid="file-delete-button"]').trigger('click')

    // Confirmation step appears; nothing has been deleted yet.
    expect(deleteMutation.mutateAsync).not.toHaveBeenCalled()
    const confirmRow = wrapper.findAll('[data-testid="data-table-row"]')[0]!
    expect(confirmRow.find('[data-testid="file-delete-confirm"]').exists()).toBe(true)

    await confirmRow.find('[data-testid="file-delete-confirm"]').trigger('click')
    await new Promise((resolve) => setTimeout(resolve, 0))

    expect(deleteMutation.mutateAsync).toHaveBeenCalledWith(FILE_ID)
  })

  it('hides the delete affordance for roles without documents.delete', async () => {
    stubPermissions({ canUpload: true, canDelete: false })
    await mountView()

    expect(wrapper.find('[data-testid="file-delete-button"]').exists()).toBe(false)
    // Download is a read action and stays available.
    expect(wrapper.find('[data-testid="file-download-button"]').exists()).toBe(true)
  })

  it('renders the empty state when there are no files', async () => {
    stubFilesQuery({ data: ref(listResponse([])) })
    await mountView()

    expect(wrapper.find('[data-testid="data-table-empty"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('No files yet')
  })

  it('renders the typed error envelope in the error state', async () => {
    const apiError = new ApiError(500, {
      code: 'files.list_failed',
      message: 'Could not list files',
      request_id: 'req-1',
    })
    stubFilesQuery({ isError: ref(true), error: ref(apiError) })
    await mountView()

    const errorState = wrapper.find('[data-testid="data-table-error"]')
    expect(errorState.exists()).toBe(true)
    expect(errorState.text()).toContain('Could not list files')
    expect(errorState.text()).toContain('files.list_failed')
  })

  it('refetches the list when the processing job settles', async () => {
    const { refetch } = stubFilesQuery()
    await mountView()

    // The upload component receives the view's refetch as its
    // `onFileProcessed` callback; invoking it (as FileUpload does when the
    // job reaches a terminal state) refetches the org-scoped list.
    const upload = wrapper.findComponent(FileUpload)
    expect(upload.exists()).toBe(true)
    expect(refetch).not.toHaveBeenCalled()

    const onFileProcessed = upload.props('onFileProcessed') as () => void
    onFileProcessed()
    expect(refetch).toHaveBeenCalledTimes(1)
  })
})
