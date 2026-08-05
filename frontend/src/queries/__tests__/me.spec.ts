import { beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent } from 'vue'
import { VueQueryPlugin } from '@tanstack/vue-query'
import { mount } from '@vue/test-utils'

vi.mock('@/api/client', () => ({
  client: {
    GET: vi.fn<typeof client.GET>(),
  },
}))

import { client } from '@/api/client'
import { useMeQuery } from '@/queries/me'

const getMock = vi.mocked(client.GET)

function mountQuery(): ReturnType<typeof useMeQuery> {
  let query!: ReturnType<typeof useMeQuery>
  const CapturingComponent = defineComponent({
    setup() {
      query = useMeQuery()
      return {}
    },
    template: '<div />',
  })
  mount(CapturingComponent, {
    global: { plugins: [VueQueryPlugin] },
  })
  return query
}

function flushPromises(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

describe('useMeQuery', () => {
  beforeEach(() => {
    getMock.mockReset()
  })

  it('returns the current user, memberships and roles from /me', async () => {
    getMock.mockResolvedValue({
      data: {
        user: {
          id: 'u1',
          email: 'ada@example.com',
          name: 'Ada Lovelace',
          is_active: true,
          created_at: '2026-01-01T00:00:00Z',
        },
        memberships: [],
        roles: ['owner'],
      },
      error: undefined,
    })
    const query = mountQuery()

    await flushPromises()
    await flushPromises()

    expect(getMock).toHaveBeenCalledWith('/api/v1/me')
    expect(query.isSuccess.value).toBe(true)
    expect(query.data.value?.user.email).toBe('ada@example.com')
    expect(query.data.value?.roles).toEqual(['owner'])
  })

  it('surfaces the client error when /me fails', async () => {
    const mockError = { code: 'unauthorized', message: 'Not signed in.' }
    getMock.mockResolvedValue({ data: undefined, error: mockError })
    const query = mountQuery()

    await flushPromises()
    await flushPromises()

    expect(query.isError.value).toBe(true)
    // The composable throws the client error (a typed ApiError in production,
    // blueprint §13) without wrapping or swallowing it.
    expect(query.error.value).toEqual(mockError)
  })
})
