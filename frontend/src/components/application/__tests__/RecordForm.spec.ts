import { mount } from '@vue/test-utils'
import { VueQueryPlugin } from '@tanstack/vue-query'
import { createPinia, setActivePinia } from 'pinia'
import type { Router } from 'vue-router'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent } from 'vue'

import { ApiError } from '@/api/errors'
import type { components } from '@/api/generated/openapi'
import RecordForm from '@/components/application/RecordForm.vue'
import type { RecordFormValues } from '@/components/application/RecordForm.vue'
import { Toaster } from '@/components/ui/sonner'
import { queryClient } from '@/queries/queryClient'
import { useOrganisationStore } from '@/stores/organisation'

vi.mock('@/api/client', () => ({
  client: {
    GET: vi.fn<typeof client.GET>(),
    POST: vi.fn<typeof client.POST>(),
    PATCH: vi.fn<typeof client.PATCH>(),
    DELETE: vi.fn<typeof client.DELETE>(),
  },
}))

import { client } from '@/api/client'

type RecordDetail = components['schemas']['RecordDetail']

const ORG_ID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const RECORD_ID = '11111111-1111-4111-8111-111111111111'

const postMock = vi.mocked(client.POST)
const patchMock = vi.mocked(client.PATCH)

const recordDetail: RecordDetail = {
  id: RECORD_ID,
  title: 'Existing record',
  body: 'Existing body',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

const validValues: RecordFormValues = { title: 'New record', body: 'Notes' }

/**
 * Test harness: the form under test plus the app's `Toaster`, so toast
 * feedback (the API-error and success paths of Scope §6.6) is asserted against
 * the rendered DOM rather than against mock internals. Sonner portals to
 * `document.body`, so the wrapper is attached to the body.
 */
const Harness = defineComponent({
  components: { RecordForm, Toaster },
  props: {
    mode: { type: String, required: true },
    recordId: { type: String, default: undefined },
    initialValues: { type: Object, default: undefined },
    listRouteName: { type: String, default: undefined },
  },
  template: `
    <div>
      <RecordForm
        :mode="mode"
        :record-id="recordId"
        :initial-values="initialValues"
        :list-route-name="listRouteName"
      />
      <Toaster />
    </div>
  `,
})

async function flushPromises(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0))
}

/** Submit the form and flush until the mutation has settled (not wall-clock flushes). */
async function submitForm(wrapper: ReturnType<typeof mount>): Promise<void> {
  await wrapper.find('form').trigger('submit')
  await flushPromises()
}

interface MountOptions {
  mode?: 'create' | 'edit'
  recordId?: string
  initialValues?: RecordFormValues
  listRouteName?: string
}

const mountedWrappers: ReturnType<typeof mount>[] = []
let router: Router

function mountForm(options: MountOptions = {}): ReturnType<typeof mount> {
  const pinia = createPinia()
  setActivePinia(pinia)
  useOrganisationStore().setSelectedOrganisation(ORG_ID)

  router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/records', name: 'records', component: { template: '<div />' } },
      { path: '/', name: 'home', component: { template: '<div />' } },
    ],
  })

  const wrapper = mount(Harness, {
    props: {
      mode: options.mode ?? 'create',
      recordId: options.recordId,
      initialValues: options.initialValues,
      listRouteName: options.listRouteName,
    },
    global: {
      plugins: [pinia, router, [VueQueryPlugin, { queryClient }]],
    },
    attachTo: document.body,
  })
  mountedWrappers.push(wrapper)
  return wrapper
}

function formEmits(wrapper: ReturnType<typeof mount>, event: 'created' | 'updated' | 'cancel') {
  return wrapper.findComponent(RecordForm).emitted(event)
}

describe('RecordForm (Scope §6.6 standard form + toast)', () => {
  beforeEach(() => {
    localStorage.clear()
    postMock.mockReset()
    patchMock.mockReset()
    queryClient.clear()
    document.body.innerHTML = ''
  })

  afterEach(() => {
    for (const wrapper of mountedWrappers.splice(0)) {
      wrapper.unmount()
    }
    document.body.innerHTML = ''
  })

  it('surfaces inline field-validation errors and never submits an invalid form', async () => {
    const wrapper = mountForm()

    await submitForm(wrapper)

    await vi.waitFor(() => expect(wrapper.text()).toContain('Title is required.'))
    expect(postMock).not.toHaveBeenCalled()
  })

  it('creates a record through the generated client and navigates to the list', async () => {
    postMock.mockResolvedValue({ data: recordDetail, error: undefined, response: new Response() })
    const wrapper = mountForm({ listRouteName: 'records' })

    await wrapper.find('input').setValue(validValues.title)
    await wrapper.find('textarea').setValue(validValues.body)
    await submitForm(wrapper)

    await vi.waitFor(() =>
      expect(postMock).toHaveBeenCalledWith('/api/v1/records', { body: validValues }),
    )
    expect(formEmits(wrapper, 'created')?.[0]?.[0]).toEqual(recordDetail)
    await vi.waitFor(() => expect(router.currentRoute.value.name).toBe('records'))
    await vi.waitFor(() => expect(document.body.textContent).toContain('Record created'))
  })

  it('shows a success toast and emits updated without navigating when no list route is given', async () => {
    patchMock.mockResolvedValue({ data: recordDetail, error: undefined, response: new Response() })
    const wrapper = mountForm({
      mode: 'edit',
      recordId: RECORD_ID,
      initialValues: { title: 'Existing record', body: 'Existing body' },
    })

    await submitForm(wrapper)

    await vi.waitFor(() =>
      expect(patchMock).toHaveBeenCalledWith('/api/v1/records/{record_id}', {
        params: { path: { record_id: RECORD_ID } },
        body: { title: 'Existing record', body: 'Existing body' },
      }),
    )
    expect(formEmits(wrapper, 'updated')?.[0]?.[0]).toEqual(recordDetail)
    expect(router.currentRoute.value.name).toBe('home')
    await vi.waitFor(() => expect(document.body.textContent).toContain('Record updated'))
  })

  it('maps an API error envelope to an error toast and keeps the form on screen', async () => {
    const conflictError = new ApiError(409, {
      code: 'conflict',
      message: 'A record with this title already exists.',
      details: null,
      request_id: 'req-123',
    })
    postMock.mockRejectedValue(conflictError)
    const wrapper = mountForm()

    await wrapper.find('input').setValue('Duplicate title')
    await submitForm(wrapper)

    await vi.waitFor(() => expect(document.body.textContent).toContain('Could not create record'))
    expect(formEmits(wrapper, 'created')).toBeUndefined()
    expect(router.currentRoute.value.name).toBe('home')
    expect(document.body.textContent).toContain('A record with this title already exists.')
    expect(document.body.textContent).toContain('req-123')
  })

  it('emits cancel without touching the API', async () => {
    const wrapper = mountForm()

    const cancelButton = wrapper.findAll('button').find((b) => b.text().includes('Cancel'))
    expect(cancelButton).toBeDefined()
    await cancelButton!.trigger('click')

    expect(formEmits(wrapper, 'cancel')).toBeDefined()
    expect(postMock).not.toHaveBeenCalled()
  })
})
