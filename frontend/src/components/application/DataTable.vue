<script setup lang="ts" generic="T">
import { FlexRender, getCoreRowModel, useVueTable } from '@tanstack/vue-table'
import type {
  ColumnDef,
  PaginationState,
  TableOptionsWithReactiveData,
  Updater,
} from '@tanstack/vue-table'
import {
  AlertTriangleIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  ChevronsLeftIcon,
  ChevronsRightIcon,
  InboxIcon,
  LoaderCircleIcon,
} from '@lucide/vue'
import { computed, ref, watch } from 'vue'
import type { VNode } from 'vue'

import type { ApiError } from '@/api/errors'
import { Button } from '@/components/ui/button'
import {
  Table,
  TableBody,
  TableCell,
  TableEmpty,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { cn } from '@/lib/utils'

/**
 * Column definition for the standard `DataTable` (Scope §6.5, blueprint §16
 * data grids). `key` addresses the property on each row object (dot paths are
 * supported by TanStack Table); `cell` is an optional formatter for values
 * that are not plain display text. A formatter may return a `VNode` (e.g. a
 * `RouterLink` or action button), which TanStack's `FlexRender` renders
 * directly — that is how row actions stay inside the standard table instead
 * of hand-built layouts (Scope §6.7).
 */
export interface DataTableColumn<T> {
  key: string
  header: string
  align?: 'left' | 'center' | 'right'
  className?: string
  cell?: (row: T) => string | number | VNode
}

const props = withDefaults(
  defineProps<{
    columns: DataTableColumn<T>[]
    data: T[]
    rowKey?: keyof T
    pagination: { page: number; pageSize: number; total: number }
    loading?: boolean
    error?: ApiError | null
    emptyMessage?: string
  }>(),
  {
    loading: false,
    error: null,
    emptyMessage: 'No results found.',
  },
)

const emit = defineEmits<{
  (e: 'update:page', page: number): void
  (e: 'update:pageSize', pageSize: number): void
}>()

/**
 * Pagination lives in a local ref (the TanStack Vue "controlled state"
 * pattern) so the table drives page changes through `onPaginationChange`,
 * which forwards them to the parent's query state. The `watch` below syncs
 * the ref back from the props envelope, so a page change initiated elsewhere
 * (URL navigation, query invalidation) is reflected without user input.
 */
const paginationState = ref<PaginationState>({
  pageIndex: Math.max(0, props.pagination.page - 1),
  pageSize: Math.max(1, props.pagination.pageSize),
})

watch(
  () => props.pagination,
  (p) => {
    const pageSize = Math.max(1, p.pageSize)
    const pageCount = Math.max(1, Math.ceil(p.total / pageSize))
    const pageIndex = Math.min(Math.max(0, p.page - 1), pageCount - 1)
    if (
      pageIndex !== paginationState.value.pageIndex ||
      pageSize !== paginationState.value.pageSize
    ) {
      paginationState.value = { pageIndex, pageSize }
    }
  },
  { deep: true },
)

function displayValue(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

const columnDefs = computed<ColumnDef<T, unknown>[]>(() =>
  props.columns.map((col) => {
    const format = col.cell
    return {
      id: col.key,
      accessorKey: col.key,
      header: col.header,
      cell: format ? (info) => format(info.row.original) : (info) => displayValue(info.getValue()),
    }
  }),
)

const columnStyles = computed(() => {
  const styles = new Map<string, { align?: DataTableColumn<T>['align']; className?: string }>()
  for (const col of props.columns) {
    styles.set(col.key, { align: col.align, className: col.className })
  }
  return styles
})

const totalPages = computed(() =>
  Math.max(1, Math.ceil(props.pagination.total / Math.max(1, props.pagination.pageSize))),
)

/**
 * The options object uses the documented TanStack Vue "getter" pattern so the
 * table re-reads reactive `data`, `columns` and `pageCount` on every render.
 * The published option types do not (yet) express lazy accessors, hence the
 * cast; runtime behaviour is the documented one.
 */
const table = useVueTable({
  get data() {
    return props.data
  },
  get columns() {
    return columnDefs.value
  },
  getCoreRowModel: getCoreRowModel(),
  manualPagination: true,
  get pageCount() {
    return totalPages.value
  },
  ...(props.rowKey !== undefined && {
    getRowId: (row: T) => String(row[props.rowKey as keyof T]),
  }),
  state: {
    get pagination() {
      return paginationState.value
    },
  },
  onPaginationChange: (updater: Updater<PaginationState>) => {
    const next = updater instanceof Function ? updater(paginationState.value) : updater
    paginationState.value = next
    emit('update:page', next.pageIndex + 1)
    if (next.pageSize !== props.pagination.pageSize) {
      emit('update:pageSize', next.pageSize)
    }
  },
} as unknown as TableOptionsWithReactiveData<T>)

function alignClass(align: DataTableColumn<T>['align']): string {
  if (align === 'right') return 'text-right'
  if (align === 'center') return 'text-center'
  return 'text-left'
}

function headClass(key: string): string {
  const style = columnStyles.value.get(key)
  return cn(alignClass(style?.align), style?.className)
}

function cellClass(key: string): string {
  const style = columnStyles.value.get(key)
  return cn(alignClass(style?.align), style?.className)
}

const rangeStart = computed(() =>
  props.pagination.total === 0
    ? 0
    : paginationState.value.pageIndex * paginationState.value.pageSize + 1,
)

const rangeEnd = computed(() =>
  Math.min(
    paginationState.value.pageIndex * paginationState.value.pageSize +
      paginationState.value.pageSize,
    props.pagination.total,
  ),
)
</script>

<template>
  <div class="w-full" data-testid="data-table">
    <div
      v-if="error"
      data-testid="data-table-error"
      role="alert"
      class="border-destructive/30 bg-destructive/5 text-destructive ring-destructive/10 flex flex-col items-start gap-2 rounded-xl border px-4 py-6 text-sm ring-1"
    >
      <div class="flex items-center gap-2 font-medium">
        <AlertTriangleIcon class="size-4 shrink-0" />
        <span>{{ error.message }}</span>
      </div>
      <p class="text-destructive/80 text-xs">
        <code class="rounded bg-current/10 px-1.5 py-0.5 font-mono">{{ error.code }}</code>
        <template v-if="error.requestId"> &nbsp;·&nbsp; request {{ error.requestId }} </template>
      </p>
    </div>

    <template v-else>
      <div class="rounded-xl border bg-card">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead v-for="col in columns" :key="col.key" :class="headClass(col.key)">
                {{ col.header }}
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            <TableEmpty v-if="loading" :colspan="columns.length" data-testid="data-table-loading">
              <LoaderCircleIcon class="size-5 animate-spin" aria-hidden="true" />
              <span class="text-muted-foreground ml-2">Loading…</span>
            </TableEmpty>
            <TableEmpty
              v-else-if="table.getRowModel().rows.length === 0"
              :colspan="columns.length"
              data-testid="data-table-empty"
            >
              <InboxIcon class="text-muted-foreground size-8" aria-hidden="true" />
              <span class="text-muted-foreground mt-1">{{ emptyMessage }}</span>
            </TableEmpty>
            <template v-else>
              <TableRow
                v-for="row in table.getRowModel().rows"
                :key="String(row.id)"
                data-testid="data-table-row"
              >
                <TableCell
                  v-for="cell in row.getVisibleCells()"
                  :key="cell.id"
                  :class="cellClass(cell.column.id)"
                >
                  <FlexRender :render="cell.column.columnDef.cell" :props="cell.getContext()" />
                </TableCell>
              </TableRow>
            </template>
          </TableBody>
        </Table>
      </div>

      <div
        v-if="!loading && props.pagination.total > 0"
        class="text-muted-foreground mt-4 flex flex-wrap items-center justify-between gap-3"
        data-testid="data-table-pagination"
      >
        <p class="text-sm" data-testid="data-table-range">
          Showing {{ rangeStart }}–{{ rangeEnd }} of {{ props.pagination.total }}
        </p>
        <div class="flex items-center gap-1.5">
          <Button
            variant="outline"
            size="icon-sm"
            aria-label="First page"
            data-testid="data-table-first"
            :disabled="!table.getCanPreviousPage()"
            @click="table.setPageIndex(0)"
          >
            <ChevronsLeftIcon />
          </Button>
          <Button
            variant="outline"
            size="icon-sm"
            aria-label="Previous page"
            data-testid="data-table-prev"
            :disabled="!table.getCanPreviousPage()"
            @click="table.previousPage()"
          >
            <ChevronLeftIcon />
          </Button>
          <span class="px-2 text-sm" data-testid="data-table-page-label">
            Page {{ paginationState.pageIndex + 1 }} of {{ totalPages }}
          </span>
          <Button
            variant="outline"
            size="icon-sm"
            aria-label="Next page"
            data-testid="data-table-next"
            :disabled="!table.getCanNextPage()"
            @click="table.nextPage()"
          >
            <ChevronRightIcon />
          </Button>
          <Button
            variant="outline"
            size="icon-sm"
            aria-label="Last page"
            data-testid="data-table-last"
            :disabled="!table.getCanNextPage()"
            @click="table.setPageIndex(table.getPageCount() - 1)"
          >
            <ChevronsRightIcon />
          </Button>
        </div>
      </div>
    </template>
  </div>
</template>
