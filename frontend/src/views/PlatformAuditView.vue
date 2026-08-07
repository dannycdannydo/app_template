<script setup lang="ts">
import { computed, ref } from 'vue'

import type { ApiError } from '@/api/errors'
import type { components } from '@/api/generated/openapi'
import DataTable from '@/components/application/DataTable.vue'
import type { DataTableColumn } from '@/components/application/DataTable.vue'
import NativeSelect from '@/components/application/NativeSelect.vue'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { usePlatformAuditEventsQuery } from '@/queries/platform'

type AuditEventListItem = components['schemas']['AuditEventListItem']

/**
 * Platform audit history (Scope §6.1/§6.9, blueprint §29, acceptance §5.1).
 *
 * The append-only audit trail across every organisation, filterable by
 * action (and organisation id via the detail screen's narrower view). There
 * is no write path in the API, so this table is read-only by construction.
 * Filters are approved API query parameters only (blueprint §12).
 */
const page = ref(1)
const pageSize = 25
const action = ref('')

const params = computed(() => ({
  page: page.value,
  pageSize,
  action: action.value.trim() === '' ? undefined : action.value.trim(),
}))

const { data, isPending, isError, error } = usePlatformAuditEventsQuery(params)

const events = computed(() => data.value?.items ?? [])

const pagination = computed(() => ({
  page: data.value?.page ?? page.value,
  pageSize: data.value?.page_size ?? pageSize,
  total: data.value?.total ?? 0,
}))

const tableError = computed<ApiError | null>(() =>
  isError.value ? (error.value as ApiError) : null,
)

const AUDIT_ACTIONS = [
  'organisation.created',
  'organisation.updated',
  'invitation.sent',
  'invitation.revoked',
  'invitation.accepted',
  'membership.role_changed',
  'membership.suspended',
  'membership.reactivated',
  'membership.removed',
  'feature_flag.changed',
  'platform.bootstrap_granted',
]

const columns: DataTableColumn<AuditEventListItem>[] = [
  { key: 'created_at', header: 'When' },
  { key: 'action', header: 'Action' },
  { key: 'resource_type', header: 'Resource' },
  { key: 'resource_id', header: 'Resource id' },
  {
    key: 'organisation_id',
    header: 'Organisation',
    cell: (row) => row.organisation_id ?? '—',
  },
  {
    key: 'actor_user_id',
    header: 'Actor',
    cell: (row) => row.actor_user_id ?? 'system',
  },
]

function resetPage(): void {
  page.value = 1
}

function onActionFilter(value: string): void {
  action.value = value
  resetPage()
}
</script>

<template>
  <div class="space-y-6">
    <div>
      <h1 class="text-2xl font-semibold">Audit history</h1>
      <p class="text-muted-foreground mt-1 text-sm">
        Append-only events across the platform. Read-only by construction.
      </p>
    </div>

    <Card>
      <CardHeader>
        <CardTitle>Events</CardTitle>
        <CardDescription>Filter by action to narrow the trail.</CardDescription>
      </CardHeader>
      <CardContent class="space-y-4">
        <div class="grid max-w-md gap-4">
          <label class="text-sm font-medium">
            Action
            <NativeSelect
              class="mt-1"
              :model-value="action"
              aria-label="Filter by action"
              data-testid="platform-audit-action-filter"
              @update:model-value="onActionFilter"
            >
              <option value="">All actions</option>
              <option v-for="code in AUDIT_ACTIONS" :key="code" :value="code">
                {{ code }}
              </option>
            </NativeSelect>
          </label>
        </div>

        <DataTable
          :columns="columns"
          :data="events"
          row-key="id"
          :pagination="pagination"
          :loading="isPending"
          :error="tableError"
          empty-message="No audit events match the current filters."
          @update:page="page = $event"
        />
      </CardContent>
    </Card>
  </div>
</template>
