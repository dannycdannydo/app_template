<script setup lang="ts">
import { computed, h, ref } from 'vue'

import type { ApiError } from '@/api/errors'
import type { components } from '@/api/generated/openapi'
import DataTable from '@/components/application/DataTable.vue'
import type { DataTableColumn } from '@/components/application/DataTable.vue'
import FileRowActions from '@/components/application/FileRowActions.vue'
import FileStatusBadge from '@/components/application/FileStatusBadge.vue'
import FileUpload from '@/components/application/FileUpload.vue'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { formatBytes, formatDateTime } from '@/lib/format'
import { useFilePermissions } from '@/lib/permissions'
import { useFilesQuery } from '@/queries/files'

type FileListItem = components['schemas']['FileListItem']

/**
 * Files list screen (Scope §6.6, blueprint §17 flow, §14, §12, §16).
 *
 * The v0.5 documents page: a `DataTable` fed by the org-scoped
 * `useFilesQuery` (paginated through the standard envelope) plus the
 * `FileUpload` component. The upload component walks the direct-upload flow
 * (intent → signed PUT → complete → job poll) and calls `onFileProcessed`
 * when the processing job settles so this view refetches and the row's
 * status advances to `ready`.
 *
 * The actions column is a `FileRowActions` component per row (download +
 * two-step delete confirm). Delete is gated by the documents.delete role
 * bundle from `/me` (`useFilePermissions`); the backend stays the
 * enforcement point.
 */
const page = ref(1)
const pageSize = 25

const { data, isPending, isError, error, refetch } = useFilesQuery(
  computed(() => ({ page: page.value, pageSize })),
)

const { permissions, mePending } = useFilePermissions()

const files = computed(() => data.value?.items ?? [])

const pagination = computed(() => ({
  page: data.value?.page ?? page.value,
  pageSize: data.value?.page_size ?? pageSize,
  total: data.value?.total ?? 0,
}))

const tableError = computed<ApiError | null>(() =>
  isError.value ? (error.value as ApiError) : null,
)

const baseColumns: DataTableColumn<FileListItem>[] = [
  { key: 'original_filename', header: 'Name' },
  {
    key: 'size_bytes',
    header: 'Size',
    cell: (row) => formatBytes(row.size_bytes),
  },
  {
    key: 'status',
    header: 'Status',
    cell: (row) => h(FileStatusBadge, { status: row.status }),
  },
  {
    key: 'created_at',
    header: 'Uploaded',
    cell: (row) => formatDateTime(row.created_at),
  },
]

const actionColumn: DataTableColumn<FileListItem> = {
  key: 'actions',
  header: '',
  align: 'right',
  className: 'w-52',
  cell: (row) => h(FileRowActions, { file: row, canDelete: permissions.value.canDelete }),
}

/**
 * The columns array is computed so permission changes (role edits while the
 * screen is mounted) are reflected without a reload. The actions column is
 * always present (download is a `documents.read` action); only the delete
 * affordance inside it is gated.
 */
const columns = computed<DataTableColumn<FileListItem>[]>(() => [...baseColumns, actionColumn])

function onFileProcessed(): void {
  void refetch()
}
</script>

<template>
  <div class="space-y-6">
    <div>
      <h1 class="text-2xl font-semibold">Files</h1>
      <p class="text-muted-foreground mt-1 text-sm">
        Files scoped to your selected organisation, uploaded directly to storage.
      </p>
    </div>

    <Card v-if="!mePending && permissions.canUpload" data-testid="files-upload-card">
      <CardHeader>
        <CardTitle>Upload a file</CardTitle>
        <CardDescription>
          The file goes straight to storage through a signed URL; a background job marks it ready.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <FileUpload :on-file-processed="onFileProcessed" />
      </CardContent>
    </Card>

    <Card>
      <CardHeader>
        <CardTitle>All files</CardTitle>
        <CardDescription>
          {{ pagination.total }} file{{ pagination.total === 1 ? '' : 's' }} in this organisation.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <DataTable
          :columns="columns"
          :data="files"
          row-key="id"
          :pagination="pagination"
          :loading="isPending"
          :error="tableError"
          empty-message="No files yet. Upload the first one to get started."
          @update:page="page = $event"
        />
      </CardContent>
    </Card>
  </div>
</template>
