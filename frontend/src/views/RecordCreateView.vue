<script setup lang="ts">
import { ShieldXIcon } from '@lucide/vue'
import { computed } from 'vue'
import { useRouter } from 'vue-router'

import RecordForm from '@/components/application/RecordForm.vue'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { useRecordPermissions } from '@/lib/permissions'

/**
 * Record create screen (Scope §6.7, acceptance §5.9).
 *
 * Wraps the standard `RecordForm` in create mode inside the shell's card
 * pattern. The form round-trips through the generated client via the records
 * query composables, shows API errors as toasts and navigates back to the
 * list on success (blueprint §13, §15, Scope §6.6).
 *
 * Write routes are permission-aware (Scope §6.7): while `/me` is loading
 * nothing renders, and a viewer landing here directly sees a read-only
 * notice instead of the form. Cosmetic only — the backend still rejects the
 * write with `403` (blueprint §9 default deny), so this gate never grants
 * access it merely shapes the UI.
 */
const router = useRouter()
const { permissions, mePending } = useRecordPermissions()

const canCreate = computed(() => permissions.value.canCreate)
</script>

<template>
  <div class="space-y-6">
    <div>
      <h1 class="text-2xl font-semibold">New record</h1>
      <p class="text-muted-foreground mt-1 text-sm">Create a record in your organisation.</p>
    </div>

    <Card v-if="mePending">
      <CardContent>
        <p class="text-muted-foreground text-sm">Checking permissions…</p>
      </CardContent>
    </Card>

    <Card v-else-if="!canCreate" data-testid="records-create-denied">
      <CardHeader>
        <CardTitle class="flex items-center gap-2">
          <ShieldXIcon class="text-muted-foreground size-4" />
          Read only
        </CardTitle>
        <CardDescription>
          Your role does not allow creating records. Ask an owner or administrator to change your
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
        <CardDescription>Title and optional body. Both match the backend schema.</CardDescription>
      </CardHeader>
      <CardContent>
        <RecordForm mode="create" list-route-name="records" />
      </CardContent>
    </Card>
  </div>
</template>
