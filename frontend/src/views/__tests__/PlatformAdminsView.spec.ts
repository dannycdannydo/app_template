import { VueQueryPlugin } from '@tanstack/vue-query'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { queryClient } from '@/queries/queryClient'
import PlatformAdminsView from '@/views/PlatformAdminsView.vue'

vi.mock('@/api/client', () => ({
  client: {
    DELETE: vi.fn<DeleteSignature>(),
    GET: vi.fn<GetSignature>(),
    POST: vi.fn<PostSignature>(),
  },
}))

type GetSignature = (url: string, init?: { params?: unknown }) => Promise<unknown>
type PostSignature = (url: string, init?: { body?: unknown }) => Promise<unknown>
type DeleteSignature = (url: string, init?: { params?: unknown }) => Promise<unknown>

import { client } from '@/api/client'

const getMock = vi.mocked(client.GET as unknown as GetSignature)
const postMock = vi.mocked(client.POST as unknown as PostSignature)
const deleteMock = vi.mocked(client.DELETE as unknown as DeleteSignature)

const ADMIN = {
  id: 'pm-1',
  user_id: 'user-1',
  user_name: 'Ada Lovelace',
  user_email: 'ada@example.com',
  role_code: 'platform_admin',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

async function flushPromises(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0))
}

function mountView() {
  return mount(PlatformAdminsView, {
    global: { plugins: [VueQueryPlugin, createPinia()], stubs: { Toaster: true } },
  })
}

describe('PlatformAdminsView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    queryClient.clear()
    getMock.mockReset()
    postMock.mockReset()
    deleteMock.mockReset()
  })

  it('grants the selected enabled user through the platform query layer', async () => {
    getMock.mockImplementation(async (url: string) => {
      if (url === '/api/v1/platform/admins') {
        return { data: { items: [ADMIN], page: 1, page_size: 50, total: 1 }, error: undefined }
      }
      return {
        data: {
          items: [{ id: 'user-2', name: 'Grace Hopper', email: 'grace@example.com' }],
          page: 1,
          page_size: 100,
          total: 1,
        },
        error: undefined,
      }
    })
    postMock.mockResolvedValue({
      data: { ...ADMIN, id: 'pm-2', user_id: 'user-2' },
      error: undefined,
    })
    const wrapper = mountView()
    await flushPromises()
    await flushPromises()

    await wrapper
      .find('select[aria-label="User to grant platform administrator"]')
      .setValue('user-2')
    await wrapper.get('button').trigger('click')
    await flushPromises()

    expect(postMock).toHaveBeenCalledWith('/api/v1/platform/admins', {
      body: { user_id: 'user-2' },
    })
  })

  it('prevents revoking the final administrator in the UI', async () => {
    getMock.mockImplementation(async (url: string) =>
      url === '/api/v1/platform/admins'
        ? { data: { items: [ADMIN], page: 1, page_size: 50, total: 1 }, error: undefined }
        : { data: { items: [], page: 1, page_size: 100, total: 0 }, error: undefined },
    )
    const wrapper = mountView()
    await flushPromises()
    await flushPromises()

    const revoke = wrapper.findAll('button').find((button) => button.text() === 'Revoke')
    expect(revoke?.attributes('disabled')).toBeDefined()
    expect(revoke?.attributes('title')).toContain('At least one platform administrator')
    expect(deleteMock).not.toHaveBeenCalled()
  })

  it('revokes an administrator when another recovery administrator remains', async () => {
    getMock.mockImplementation(async (url: string) =>
      url === '/api/v1/platform/admins'
        ? {
            data: {
              items: [ADMIN, { ...ADMIN, id: 'pm-2', user_email: 'grace@example.com' }],
              page: 1,
              page_size: 50,
              total: 2,
            },
            error: undefined,
          }
        : { data: { items: [], page: 1, page_size: 100, total: 0 }, error: undefined },
    )
    deleteMock.mockResolvedValue({ data: ADMIN, error: undefined })
    const wrapper = mountView()
    await flushPromises()
    await flushPromises()

    await wrapper
      .findAll('button')
      .find((button) => button.text() === 'Revoke')!
      .trigger('click')
    await flushPromises()

    expect(deleteMock).toHaveBeenCalledWith('/api/v1/platform/admins/{platform_membership_id}', {
      params: { path: { platform_membership_id: 'pm-1' } },
    })
  })
})
