import { mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick, ref } from 'vue'

import type { components } from '@/api/generated/openapi'

const mockUseUploadFileMutation = vi.hoisted(() =>
  vi.fn<(options?: unknown) => unknown>(),
)
const mockUseJobQuery = vi.hoisted(() => vi.fn<() => unknown>())
const mockShowApiErrorToast = vi.hoisted(() =>
  vi.fn<(error: unknown, options?: { title?: string }) => void>(),
)

vi.mock('@/queries/files', () => ({
  useUploadFileMutation: mockUseUploadFileMutation,
}))

vi.mock('@/queries/jobs', () => ({
  useJobQuery: mockUseJobQuery,
}))

vi.mock('@/lib/toast', () => ({
  showApiErrorToast: mockShowApiErrorToast,
  showSuccessToast: vi.fn<(message: string) => void>(),
}))

import FileUpload from '@/components/application/FileUpload.vue'

type JobDetail = components['schemas']['JobDetail']

const ORG_A = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const FILE_ID = '11111111-1111-4111-8111-111111111111'
const JOB_ID = '22222222-2222-4222-8222-222222222222'

interface UploadMutationHandle {
  isPending: { value: boolean }
  mutateAsync: ReturnType<typeof vi.fn>
}

interface UploadMutationOptions {
  onProgress?: (progress: { loaded: number; total: number }) => void
  onSuccess?: (result: {
    organisationId: string
    file: { id: string; processing_job_id: string | null }
  }) => void
}

function runningJob(progress: number): JobDetail {
  return {
    id: JOB_ID,
    job_type: 'file.processing',
    status: 'running',
    progress,
    attempt_count: 1,
    created_by_user_id: 'u1',
    created_at: '2026-01-01T00:00:00Z',
    started_at: '2026-01-01T00:00:01Z',
    completed_at: null,
    input_reference: `file:${FILE_ID}`,
    result_reference: null,
    error_code: null,
    error_message: null,
  }
}

let wrapper: VueWrapper
let jobData: ReturnType<typeof ref<JobDetail | null>>
let jobError: ReturnType<typeof ref<boolean>>
let mutationHandle: UploadMutationHandle
let mutationOptions: UploadMutationOptions

function mountUpload(onFileProcessed = vi.fn<() => void>()): ReturnType<typeof vi.fn> {
  jobData = ref<JobDetail | null>(null)
  jobError = ref(false)
  mockUseJobQuery.mockReturnValue({ data: jobData, isError: jobError })

  mutationOptions = {}
  mutationHandle = {
    isPending: ref(false),
    mutateAsync: vi.fn<(file: File) => Promise<void>>().mockResolvedValue(undefined),
  }
  mockUseUploadFileMutation.mockImplementation((options?: unknown) => {
    mutationOptions = (options ?? {}) as UploadMutationOptions
    return mutationHandle
  })

  wrapper = mount(FileUpload, {
    props: { onFileProcessed },
  })
  return onFileProcessed
}

async function pickFile(name = 'notes.txt', type = 'text/plain'): Promise<void> {
  const input = wrapper.find('[data-testid="file-upload-input"]').element as HTMLInputElement
  const file = new File(['hello'], name, { type })
  Object.defineProperty(input, 'files', { value: [file], configurable: true })
  await input.dispatchEvent(new Event('change'))
  await nextTick()
}

describe('FileUpload', () => {
  beforeEach(() => {
    mockUseUploadFileMutation.mockReset()
    mockUseJobQuery.mockReset()
    mockShowApiErrorToast.mockReset()
  })

  afterEach(() => {
    wrapper?.unmount()
  })

  it('shows the file picker before a file is chosen', () => {
    mountUpload()
    expect(wrapper.find('[data-testid="file-upload-picker"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="file-upload-submit"]').exists()).toBe(false)
  })

  it('picking a file reveals its name, size and the upload action', async () => {
    mountUpload()
    await pickFile()

    expect(wrapper.find('[data-testid="file-upload-name"]').text()).toBe('notes.txt')
    expect(wrapper.find('[data-testid="file-upload-submit"]').exists()).toBe(true)
  })

  it('runs the upload flow and reports PUT progress before polling the job', async () => {
    const onFileProcessed = mountUpload()
    await pickFile()

    await wrapper.find('[data-testid="file-upload-submit"]').trigger('click')
    await nextTick()

    // Intent in flight → the raw PUT progress drives the bar → 50%.
    expect(mutationHandle.mutateAsync).toHaveBeenCalledWith(expect.any(File))
    mutationOptions.onProgress?.({ loaded: 50, total: 100 })
    await nextTick()

    const progress = wrapper.find('[data-testid="file-upload-progress"]')
    expect(progress.exists()).toBe(true)
    expect(progress.text()).toContain('50%')

    // Completion returns a processing job id → the component starts polling.
    mutationOptions.onSuccess?.({
      organisationId: ORG_A,
      file: { id: FILE_ID, processing_job_id: JOB_ID },
    })
    await nextTick()
    expect(wrapper.find('[data-testid="file-upload-progress"]').text()).toContain('100%')
    const uploadedEvents = wrapper.emitted('uploaded')
    expect(uploadedEvents?.[uploadedEvents.length - 1]).toEqual([FILE_ID])

    // The polled job drives progress from here on.
    jobData.value = runningJob(60)
    await nextTick()
    expect(wrapper.find('[data-testid="file-upload-progress"]').text()).toContain('60%')

    // Terminal state: done, list refresh requested, polling UI gone.
    jobData.value = { ...runningJob(100), status: 'succeeded' }
    await nextTick()
    expect(wrapper.find('[data-testid="file-upload-progress"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="file-upload-name"]').exists()).toBe(true)
    expect(onFileProcessed).toHaveBeenCalledTimes(1)
  })

  it('shows the job error and asks the parent to refresh when processing fails', async () => {
    const onFileProcessed = mountUpload()
    await pickFile()
    await wrapper.find('[data-testid="file-upload-submit"]').trigger('click')
    await nextTick()

    mutationOptions.onSuccess?.({
      organisationId: ORG_A,
      file: { id: FILE_ID, processing_job_id: JOB_ID },
    })
    await nextTick()

    jobData.value = { ...runningJob(30), status: 'failed', error_message: 'Checksum mismatch' }
    await nextTick()

    const error = wrapper.find('[data-testid="file-upload-error"]')
    expect(error.exists()).toBe(true)
    expect(error.text()).toContain('Checksum mismatch')
    expect(onFileProcessed).toHaveBeenCalledTimes(1)
    // Retry is offered from the error state.
    expect(wrapper.find('[data-testid="file-upload-submit"]').text()).toContain('Try again')
  })

  it('surfaces a PUT failure with a toast and a retry affordance', async () => {
    mountUpload()
    await pickFile()

    mutationHandle.mutateAsync.mockRejectedValue(new Error('Upload failed with status 403'))
    await wrapper.find('[data-testid="file-upload-submit"]').trigger('click')
    await nextTick()
    await nextTick()

    const error = wrapper.find('[data-testid="file-upload-error"]')
    expect(error.exists()).toBe(true)
    expect(error.text()).toContain('Upload failed with status 403')
    expect(mockShowApiErrorToast).toHaveBeenCalled()
    expect(wrapper.find('[data-testid="file-upload-submit"]').text()).toContain('Try again')
  })

  it('clears the selection back to the picker after a finished upload', async () => {
    mountUpload()
    await pickFile()

    mutationOptions.onSuccess?.({
      organisationId: ORG_A,
      file: { id: FILE_ID, processing_job_id: null },
    })
    await nextTick()

    expect(wrapper.find('[data-testid="file-upload-progress"]').exists()).toBe(false)
    await wrapper.find('[data-testid="file-upload-clear"]').trigger('click')
    await nextTick()

    expect(wrapper.find('[data-testid="file-upload-picker"]').exists()).toBe(true)
  })
})
