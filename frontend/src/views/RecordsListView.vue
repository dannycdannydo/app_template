<script setup lang="ts">
import { PlusIcon } from '@lucide/vue'
import { computed, h, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'

import type { ApiError } from '@/api/errors'
import type { components } from '@/api/generated/openapi'
import DataTable from '@/components/application/DataTable.vue'
import type { DataTableColumn } from '@/components/application/DataTable.vue'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { cn } from '@/lib/utils'
import { useRecordPermissions } from '@/lib/permissions'
import { useRecordsQuery } from '@/queries/records'

type RecordListItem = components['schemas']['RecordListItem']

/**
 * Records list screen (Scope §6.7, acceptance §5.9, blueprint §14, §12).
 *
 * The shell's first real feature screen: a `DataTable` fed by the
 * org-scoped `useRecordsQuery`, paginated through the standard envelope
 * (`items`, `page`, `page_size`, `total`). The selected organisation comes
 * from the shell header's selector (Pinia client state, blueprint §14), so
 * this view holds no organisation logic of its own.
 *
 * Permission-aware UI is derived from `/me` roles (Scope §6.7): a viewer
 * sees no create or edit affordances at all, the Actions column only exists
 * for roles with `records.update`. The backend stays the enforcement point
 * (blueprint §9 default deny) — this only hides actions.
 *
 * Pagination state is local view state (a single `page` ref) handed to the
 * query as params; TanStack Query caches per params object, so changing the
 * page addresses a distinct cache entry and back-navigation does not refetch
 * (blueprint §14).
 */
const router = useRouter()

const page = ref(1)
const pageSize = 25

const { data, isPending, isError, error } = useRecordsQuery(
  computed(() => ({ page: page.value, pageSize })),
)

const { permissions } = useRecordPermissions()

const baseColumns: DataTableColumn<RecordListItem>[] = [
  { key: 'title', header: 'Title' },
  { key: 'created_at', header: 'Created' },
  { key: 'updated_at', header: 'Updated' },
]

/**
 * Row action column, present only for roles with `records.update` (Scope
 * §6.7). The `Edit` cell is a router link rendered through the standard
 * `DataTable` VNode cell support; a viewer never sees the column.
 */
const actionColumn: DataTableColumn<RecordListItem> = {
  key: 'actions',
  header: '',
  align: 'right',
  className: 'w-16',
  cell: (row) =>
    h(
      RouterLink,
      {
        to: { name: 'record-edit', params: { recordId: row.id } },
        class: cn(
          'text-muted-foreground hover:text-foreground focus-visible:ring-ring/50',
          'rounded-md px-2 py-1 text-sm font-medium outline-none',
          'focus-visible:ring-3 [&.router-link-active]:text-foreground',
        ),
      },
      { default: () => 'Edit' },
    ),
}

const columns = computed<DataTableColumn<RecordListItem>[]>(() =>
  permissions.value.canUpdate ? [...baseColumns, actionColumn] : baseColumns,
)

const records = computed(() => data.value?.items ?? [])

const pagination = computed(() => ({
  page: data.value?.page ?? page.value,
  pageSize: data.value?.page_size ?? pageSize,
  total: data.value?.total ?? 0,
}))

const tableError = computed<ApiError | null>(() =>
  isError.value ? (error.value as ApiError) : null,
)

function goToCreate(): void {
  void router.push({ name: 'record-create' })
}
</script>

<template>
  <div class="space-y-6">
    <div class="flex items-start justify-between gap-4">
      <div>
        <h1 class="text-2xl font-semibold">Records</h1>
        <p class="text-muted-foreground mt-1 text-sm">
          Records scoped to your selected organisation.
        </p>
      </div>
      <Button v-if="permissions.canCreate" data-testid="records-create-button" @click="goToCreate">
        <PlusIcon class="size-4" />
        New record
      </Button>
    </div>

    <Card>
      <CardHeader>
        <CardTitle>All records</CardTitle>
        <CardDescription>
          {{ pagination.total }} record{{ pagination.total === 1 ? '' : 's' }} in this organisation.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <DataTable
          :columns="columns"
          :data="records"
          row-key="id"
          :pagination="pagination"
          :loading="isPending"
          :error="tableError"
          empty-message="No records yet. Create the first one to get started."
          @update:page="page = $event"
        />
      </CardContent>
    </Card>
  </div>
</template>
