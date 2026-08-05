import { mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { afterEach, describe, expect, it } from 'vitest'

import { ApiError } from '@/api/errors'
import DataTable from '@/components/application/DataTable.vue'
import type { DataTableColumn } from '@/components/application/DataTable.vue'

interface TestRecord {
  id: string
  title: string
  status: 'active' | 'archived'
  created_at: string
}

const records: TestRecord[] = [
  {
    id: '11111111-1111-4111-8111-111111111111',
    title: 'First record',
    status: 'active',
    created_at: '2026-01-01T00:00:00Z',
  },
  {
    id: '22222222-2222-4222-8222-222222222222',
    title: 'Second record',
    status: 'archived',
    created_at: '2026-01-02T00:00:00Z',
  },
  {
    id: '33333333-3333-4333-8333-333333333333',
    title: 'Third record',
    status: 'active',
    created_at: '2026-01-03T00:00:00Z',
  },
]

const columns = [
  { key: 'title', header: 'Title' },
  {
    key: 'status',
    header: 'Status',
    align: 'right' as const,
    cell: (row: TestRecord) => (row.status === 'active' ? 'Active' : 'Archived'),
  },
] satisfies DataTableColumn<TestRecord>[]

const pagination = { page: 1, pageSize: 50, total: records.length }

const mountedWrappers: VueWrapper[] = []

interface TableProps {
  columns?: DataTableColumn<TestRecord>[]
  data?: TestRecord[]
  pagination?: { page: number; pageSize: number; total: number }
  loading?: boolean
  error?: ApiError | null
  emptyMessage?: string
}

function mountTable(props: TableProps = {}): VueWrapper {
  // `DataTable` is generic; vue-tsc cannot bind `T` through `mount`, so the
  // props are cast to `never` after being checked against the typed
  // `TableProps` above.
  const wrapper = mount(DataTable, {
    props: {
      columns,
      data: records,
      pagination,
      ...props,
    } as never,
  })
  mountedWrappers.push(wrapper)
  return wrapper
}

describe('DataTable', () => {
  afterEach(() => {
    for (const wrapper of mountedWrappers.splice(0)) wrapper.unmount()
  })

  it('renders header labels and row cells from the query result', () => {
    const wrapper = mountTable()

    expect(wrapper.find('[data-testid="data-table"]').exists()).toBe(true)
    const headers = wrapper.findAll('th')
    expect(headers.map((h) => h.text())).toEqual(['Title', 'Status'])

    const rows = wrapper.findAll('[data-testid="data-table-row"]')
    expect(rows).toHaveLength(3)
    expect(rows[0]?.text()).toContain('First record')
    expect(rows[0]?.text()).toContain('Active')
    // Cell formatter maps the raw value to display text.
    expect(rows[1]?.text()).toContain('Archived')
  })

  it('applies per-column alignment and class overrides', () => {
    const wrapper = mountTable({
      columns: [
        { key: 'title', header: 'Title' },
        { key: 'created_at', header: 'Created', align: 'right', className: 'w-48' },
      ],
    })

    const head = wrapper.findAll('th')
    expect(head[1]?.classes()).toContain('text-right')
    const cell = wrapper.findAll('[data-testid="data-table-row"] td')[1]
    expect(cell?.classes()).toContain('text-right')
    expect(cell?.classes()).toContain('w-48')
  })

  it('renders the pagination envelope and range label', () => {
    const wrapper = mountTable()

    expect(wrapper.find('[data-testid="data-table-pagination"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="data-table-range"]').text()).toBe('Showing 1–3 of 3')
    expect(wrapper.find('[data-testid="data-table-page-label"]').text()).toBe('Page 1 of 1')
  })

  it('emits update:page on navigation and updates the page label', async () => {
    const wrapper = mountTable({
      data: records,
      pagination: { page: 1, pageSize: 10, total: 25 },
    })

    const prev = wrapper.find('[data-testid="data-table-prev"]')
    const next = wrapper.find('[data-testid="data-table-next"]')
    const first = wrapper.find('[data-testid="data-table-first"]')
    const last = wrapper.find('[data-testid="data-table-last"]')

    // On the first of three pages, only forward navigation is enabled.
    expect(prev.attributes('disabled')).toBeDefined()
    expect(first.attributes('disabled')).toBeDefined()
    expect(next.attributes('disabled')).toBeUndefined()
    expect(last.attributes('disabled')).toBeUndefined()

    await next.trigger('click')
    await wrapper.vm.$nextTick()

    expect(wrapper.emitted('update:page')).toEqual([[2]])
    expect(wrapper.find('[data-testid="data-table-page-label"]').text()).toBe('Page 2 of 3')
    expect(wrapper.find('[data-testid="data-table-range"]').text()).toBe('Showing 11–20 of 25')

    await last.trigger('click')
    expect(wrapper.emitted('update:page')).toEqual([[2], [3]])

    await wrapper.setProps({ pagination: { page: 3, pageSize: 10, total: 25 } })
    await wrapper.vm.$nextTick()

    // On the last page, forward navigation is disabled again.
    expect(next.attributes('disabled')).toBeDefined()
    expect(last.attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-testid="data-table-page-label"]').text()).toBe('Page 3 of 3')
  })

  it('syncs the page label when the envelope prop changes externally', async () => {
    const wrapper = mountTable({
      pagination: { page: 1, pageSize: 10, total: 25 },
    })

    await wrapper.setProps({ pagination: { page: 2, pageSize: 10, total: 25 } })
    await wrapper.vm.$nextTick()

    expect(wrapper.find('[data-testid="data-table-page-label"]').text()).toBe('Page 2 of 3')
  })

  it('renders the loading state and hides rows and pagination', () => {
    const wrapper = mountTable({ loading: true })

    expect(wrapper.find('[data-testid="data-table-loading"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="data-table-row"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="data-table-pagination"]').exists()).toBe(false)
  })

  it('renders the empty state with a custom message', () => {
    const wrapper = mountTable({
      data: [],
      pagination: { page: 1, pageSize: 50, total: 0 },
      emptyMessage: 'No records yet',
    })

    expect(wrapper.find('[data-testid="data-table-empty"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="data-table-empty"]').text()).toContain('No records yet')
    expect(wrapper.find('[data-testid="data-table-row"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="data-table-pagination"]').exists()).toBe(false)
  })

  it('renders the typed client error envelope', () => {
    const error = new ApiError(422, {
      code: 'validation_error',
      message: 'The request contains invalid data.',
      details: null,
      request_id: 'req-123',
    })
    const wrapper = mountTable({ error })

    const alert = wrapper.find('[data-testid="data-table-error"]')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toContain('The request contains invalid data.')
    expect(alert.text()).toContain('validation_error')
    expect(alert.text()).toContain('req-123')
    expect(wrapper.find('[data-testid="data-table-pagination"]').exists()).toBe(false)
  })
})
