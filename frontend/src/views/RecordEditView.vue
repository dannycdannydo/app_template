<script setup lang="ts">
import { LoaderCircleIcon, ShieldXIcon, Trash2Icon } from '@lucide/vue'
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'

import type { ApiError } from '@/api/errors'
import RecordForm from '@/components/application/RecordForm.vue'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { showApiErrorToast, showSuccessToast } from '@/lib/toast'
import { useRecordPermissions } from '@/lib/permissions'
import { useDeleteRecordMutation, useRecordQuery } from '@/queries/records'

/**
 * Record edit screen (Scope §6.7, acceptance §5.9).
 *
 * Edit mode of the standard form, hydrated from the org-scoped detail query,
 * plus the delete action with an inline confirmation step. Both actions are
 * permission-aware from `/me` (Scope §6.7): a viewer sees neither the form
 * nor the delete button; a manager can edit but cannot delete (mirroring the
 * backend `ROLE_PERMISSION_MAP`). The backend remains the enforcement point.
 *
 * Delete confirmation is an explicit two-step pattern: the action arm
 * expands into a destructive confirm + cancel pair, so an accidental click
 * cannot destroy data without a second, deliberate click.
 */
const props = defineProps<{ recordId: string }>()

const router = useRouter()
const { permissions, mePending } = useRecordPermissions()

const { data: record, isPending, isError, error } = useRecordQuery(() => props.recordId)

const confirmDeleteOpen = ref(false)

const initialValues = computed<{ title: string; body: string } | undefined>(() => {
  if (!record.value) return undefined
  return { title: record.value.title, body: record.value.body }
})

const canEdit = computed(() => permissions.value.canUpdate)
const canDelete = computed(() => permissions.value.canDelete)

const loadError = computed<ApiError | null>(() =>
  isError.value ? (error.value as ApiError) : null,
)

const deleteMutation = useDeleteRecordMutation({
  onSuccess: () => {
    showSuccessToast('Record deleted')
    void router.push({ name: 'records' })
  },
})
const isDeletePending = deleteMutation.isPending

function cancelDelete(): void {
  confirmDeleteOpen.value = false
}

async function confirmDelete(): Promise<void> {
  try {
    await deleteMutation.mutateAsync(props.recordId)
  } catch (deleteError) {
    showApiErrorToast(deleteError, { title: 'Could not delete record' })
  } finally {
    confirmDeleteOpen.value = false
  }
}
</script>

<template>
  <div class="space-y-6">
    <div class="flex items-start justify-between gap-4">
      <div>
        <h1 class="text-2xl font-semibold">Edit record</h1>
        <p class="text-muted-foreground mt-1 text-sm">Update or delete this record.</p>
      </div>
      <div v-if="!mePending && canDelete" class="flex items-center gap-2">
        <template v-if="confirmDeleteOpen">
          <p class="text-muted-foreground text-sm" data-testid="records-delete-confirm-text">
            Delete this record? This cannot be undone.
          </p>
          <Button
            variant="outline"
            size="sm"
            :disabled="isDeletePending"
            data-testid="records-delete-cancel"
            @click="cancelDelete"
          >
            Cancel
          </Button>
          <Button
            variant="destructive"
            size="sm"
            :disabled="isDeletePending"
            data-testid="records-delete-confirm"
            @click="confirmDelete"
          >
            <LoaderCircleIcon v-if="isDeletePending" class="animate-spin" />
            <Trash2Icon v-else class="size-4" />
            Delete
          </Button>
        </template>
        <Button
          v-else
          variant="destructive"
          size="sm"
          data-testid="records-delete-button"
          @click="confirmDeleteOpen = true"
        >
          <Trash2Icon class="size-4" />
          Delete record
        </Button>
      </div>
    </div>

    <Card v-if="mePending">
      <CardContent>
        <p class="text-muted-foreground text-sm">Checking permissions…</p>
      </CardContent>
    </Card>

    <Card v-else-if="!canEdit" data-testid="records-edit-denied">
      <CardHeader>
        <CardTitle class="flex items-center gap-2">
          <ShieldXIcon class="text-muted-foreground size-4" />
          Read only
        </CardTitle>
        <CardDescription>
          Your role does not allow editing records. Ask an owner or administrator to change your
          role.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <Button variant="outline" size="sm" @click="router.push({ name: 'records' })">
          Back to records
        </Button>
      </CardContent>
    </Card>

    <Card v-else>
      <CardHeader>
        <CardTitle>Record details</CardTitle>
        <CardDescription
          >Changes are saved with PATCH semantics via the generated client.</CardDescription
        >
      </CardHeader>
      <CardContent>
        <p v-if="isPending" class="text-muted-foreground text-sm">Loading record…</p>
        <div v-else-if="loadError" data-testid="records-edit-load-error" role="alert">
          <p class="text-destructive text-sm">{{ loadError.message }}</p>
          <Button
            variant="outline"
            size="sm"
            class="mt-3"
            @click="router.push({ name: 'records' })"
          >
            Back to records
          </Button>
        </div>
        <RecordForm
          v-else
          mode="edit"
          :record-id="recordId"
          :initial-values="initialValues"
          list-route-name="records"
        />
      </CardContent>
    </Card>
  </div>
</template>
