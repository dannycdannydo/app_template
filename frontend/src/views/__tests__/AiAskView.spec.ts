import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'

import type { components } from '@/api/generated/openapi'

const mockUseAskMutation = vi.hoisted(() =>
  vi.fn<
    () => {
      data: ReturnType<typeof ref>
      error: ReturnType<typeof ref>
      isPending: ReturnType<typeof ref<boolean>>
      isError: ReturnType<typeof ref<boolean>>
      mutateAsync: ReturnType<typeof vi.fn>
      reset: ReturnType<typeof vi.fn>
    }
  >(),
)
const mockUseFilePermissions = vi.hoisted(() => vi.fn<() => unknown>())
const mockShowApiErrorToast = vi.hoisted(() => vi.fn<(error: unknown, options?: unknown) => void>())

vi.mock('@/queries/ai', () => ({
  useAskMutation: mockUseAskMutation,
}))

vi.mock('@/lib/permissions', () => ({
  useFilePermissions: mockUseFilePermissions,
}))

vi.mock('@/lib/toast', () => ({
  showApiErrorToast: mockShowApiErrorToast,
}))

vi.mock('@/components/application/FileUpload.vue', () => ({
  default: {
    name: 'FileUpload',
    props: {},
    emits: ['uploaded'],
    template:
      '<button data-testid="file-upload-stub" @click="$emit(\'uploaded\', storageReference)">upload</button>',
    setup() {
      return {
        storageReference:
          'organisations/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/documents/99999999-9999-4999-8999-999999999999/original',
      }
    },
  },
}))

import AiAskView from '@/views/AiAskView.vue'
import { useOrganisationStore } from '@/stores/organisation'

type AskResponse = components['schemas']['DocumentAskResponse']

const ORG_ID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'

function askResponse(): AskResponse {
  return {
    request_id: 'request-1',
    output: 'The renewal term is twelve months.',
    routing: {
      provider: 'vertex',
      model: 'gemini-2.0-flash',
      prompt_name: 'document.ask',
      prompt_version: 1,
      fallback_used: false,
      region: 'europe-west1',
    },
    usage: { input_tokens: 12, output_tokens: 8 },
    cost: { amount: '0.000100', currency: 'USD' },
    completed_at: '2026-01-01T00:00:00Z',
  }
}

interface MutationShape {
  data: ReturnType<typeof ref<AskResponse | undefined>>
  error: ReturnType<typeof ref<Error | undefined>>
  isPending: ReturnType<typeof ref<boolean>>
  isError: ReturnType<typeof ref<boolean>>
  mutateAsync: ReturnType<typeof vi.fn>
  reset: ReturnType<typeof vi.fn>
}

function stubMutation(mutation: Partial<MutationShape> = {}): MutationShape {
  const shape: MutationShape = {
    data: ref<AskResponse | undefined>(undefined),
    error: ref<Error | undefined>(undefined),
    isPending: ref(false),
    isError: ref(false),
    mutateAsync: vi.fn<() => Promise<unknown>>(async () => undefined),
    reset: vi.fn<() => void>(() => undefined),
    ...mutation,
  }
  mockUseAskMutation.mockReturnValue(shape)
  return shape
}

function stubPermissions(canUpload: boolean): void {
  mockUseFilePermissions.mockReturnValue({
    permissions: ref({ canUpload, canDelete: false }),
    mePending: ref(false),
  })
}

function mountView(): VueWrapper {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useOrganisationStore()
  store.setSelectedOrganisation(ORG_ID)
  return mount(AiAskView, { global: { plugins: [pinia] } })
}

describe('AiAskView', () => {
  beforeEach(() => {
    mockShowApiErrorToast.mockClear()
  })

  it('renders the upload and question cards for a user who can upload', () => {
    stubPermissions(true)
    stubMutation()
    const wrapper = mountView()

    expect(wrapper.find('[data-testid="ai-ask-upload-card"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="ai-ask-question-card"]').exists()).toBe(true)
  })

  it('hides the upload card for a read-only viewer (documents.read only)', () => {
    stubPermissions(false)
    stubMutation()
    const wrapper = mountView()

    expect(wrapper.find('[data-testid="ai-ask-upload-card"]').exists()).toBe(false)
  })

  it('keeps the ask button disabled until a document is uploaded and a question is typed', async () => {
    stubPermissions(true)
    stubMutation()
    const wrapper = mountView()

    const submit = wrapper.find('[data-testid="ai-ask-submit"]')
    expect(submit.exists()).toBe(false) // no document uploaded yet: question card shows placeholder

    // Upload a document (FileUpload stub emits the server-provided reference).
    await wrapper.find('[data-testid="file-upload-stub"]').trigger('click')

    const button = wrapper.find('[data-testid="ai-ask-submit"]')
    expect(button.exists()).toBe(true)
    expect(button.attributes('disabled')).toBeDefined()

    await wrapper.find('[data-testid="ai-ask-question-input"]').setValue('What is the term?')
    expect(button.attributes('disabled')).toBeUndefined()
  })

  it('submits the server-provided storage reference and shows the answer', async () => {
    stubPermissions(true)
    const response = askResponse()
    const mutation = stubMutation({
      data: ref<AskResponse | undefined>(response),
    })
    mutation.mutateAsync.mockResolvedValue(response)
    const wrapper = mountView()

    await wrapper.find('[data-testid="file-upload-stub"]').trigger('click')
    await wrapper.find('[data-testid="ai-ask-question-input"]').setValue('What is the term?')
    await wrapper.find('[data-testid="ai-ask-submit"]').trigger('click')
    await flushPromises()

    expect(mutation.mutateAsync).toHaveBeenCalledWith({
      storage_reference: `organisations/${ORG_ID}/documents/99999999-9999-4999-8999-999999999999/original`,
      question: 'What is the term?',
    })
    expect(wrapper.find('[data-testid="ai-ask-answer"]').text()).toBe(
      'The renewal term is twelve months.',
    )
    expect(wrapper.find('[data-testid="ai-ask-answer-card"]').text()).toContain('gemini-2.0-flash')
    expect(wrapper.find('[data-testid="ai-ask-answer-card"]').text()).toContain('vertex')
  })

  it('surfaces a failed ask through the error toast', async () => {
    stubPermissions(true)
    const mutation = stubMutation()
    mutation.mutateAsync.mockRejectedValue(new Error('provider unavailable'))
    const wrapper = mountView()

    await wrapper.find('[data-testid="file-upload-stub"]').trigger('click')
    await wrapper.find('[data-testid="ai-ask-question-input"]').setValue('What is the term?')
    await wrapper.find('[data-testid="ai-ask-submit"]').trigger('click')
    await flushPromises()

    expect(mockShowApiErrorToast).toHaveBeenCalledWith(
      expect.any(Error),
      expect.objectContaining({ title: 'Could not ask the document' }),
    )
  })
})
