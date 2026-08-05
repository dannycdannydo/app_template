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
import { useHealthQuery } from '@/queries/health'

const getMock = vi.mocked(client.GET)

function mountQuery(): ReturnType<typeof useHealthQuery> {
  let query!: ReturnType<typeof useHealthQuery>
  const CapturingComponent = defineComponent({
    setup() {
      query = useHealthQuery()
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

describe('useHealthQuery', () => {
  beforeEach(() => {
    getMock.mockReset()
  })

  it('returns the health payload from the generated client', async () => {
    getMock.mockResolvedValue({ data: { status: 'ok' }, error: undefined })
    const query = mountQuery()

    await flushPromises()
    await flushPromises()

    expect(getMock).toHaveBeenCalledWith('/health')
    expect(query.isSuccess.value).toBe(true)
    expect(query.data.value?.status).toBe('ok')
  })

  it('throws when the backend reports an error', async () => {
    getMock.mockResolvedValue({ data: undefined, error: {} })
    const query = mountQuery()

    await flushPromises()
    await flushPromises()

    expect(query.isError.value).toBe(true)
    expect(query.error.value).toBeInstanceOf(Error)
  })
})
