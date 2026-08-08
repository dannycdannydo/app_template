<script setup lang="ts">
import { DownloadIcon, LoaderCircleIcon, Trash2Icon } from '@lucide/vue'
import { ref } from 'vue'

import type { components } from '@/api/generated/openapi'
import { Button } from '@/components/ui/button'
import { triggerDownload } from '@/lib/download'
import { showApiErrorToast, showSuccessToast } from '@/lib/toast'
import { useDeleteFileMutation, useDownloadFileMutation } from '@/queries/files'

type FileListItem = components['schemas']['FileListItem']

/**
 * Row actions for the files table (Scope §6.6, blueprint §16).
 *
 * Rendered by the `DataTable` actions column as a VNode component, so each
 * row keeps its own interactive state (the two-step delete confirmation)
 * inside a component boundary instead of leaking per-row state into the
 * table. Download resolves the short-lived signed GET URL through the query
 * layer and opens it; delete runs the soft-delete mutation with the same
 * explicit confirm/cancel pair as the record edit screen.
 *
 * `canDelete` is the parent's documents.delete gate (from `/me` roles); the
 * backend stays the enforcement point.
 */
const props = defineProps<{
  file: FileListItem
  canDelete: boolean
}>()

const confirmOpen = ref(false)
const deleting = ref(false)

const downloadMutation = useDownloadFileMutation({
  onSuccess: (url) => triggerDownload(url),
})

const deleteMutation = useDeleteFileMutation({
  onSuccess: () => showSuccessToast('File deleted'),
})

async function download(): Promise<void> {
  try {
    await downloadMutation.mutateAsync(props.file.id)
  } catch (error) {
    showApiErrorToast(error, { title: 'Could not prepare download' })
  }
}

function openConfirm(): void {
  confirmOpen.value = true
}

function cancelDelete(): void {
  confirmOpen.value = false
}

async function confirmDelete(): Promise<void> {
  deleting.value = true
  try {
    await deleteMutation.mutateAsync(props.file.id)
    confirmOpen.value = false
  } catch (error) {
    showApiErrorToast(error, { title: 'Could not delete file' })
  } finally {
    deleting.value = false
  }
}
</script>

<template>
  <div class="flex items-center justify-end gap-1">
    <template v-if="confirmOpen">
      <span class="text-muted-foreground text-xs">Delete this file?</span>
      <Button
        variant="outline"
        size="sm"
        :disabled="deleting"
        data-testid="file-delete-cancel"
        @click="cancelDelete"
      >
        Cancel
      </Button>
      <Button
        variant="destructive"
        size="sm"
        :disabled="deleting"
        data-testid="file-delete-confirm"
        @click="confirmDelete"
      >
        <LoaderCircleIcon v-if="deleting" class="animate-spin" />
        <Trash2Icon v-else class="size-4" />
        Delete
      </Button>
    </template>

    <template v-else>
      <Button
        variant="ghost"
        size="sm"
        :disabled="downloadMutation.isPending.value"
        data-testid="file-download-button"
        @click="download"
      >
        <DownloadIcon class="size-4" />
        Download
      </Button>
      <Button
        v-if="canDelete"
        variant="ghost"
        size="sm"
        data-testid="file-delete-button"
        @click="openConfirm"
      >
        <Trash2Icon class="size-4" />
        Delete
      </Button>
    </template>
  </div>
</template>
