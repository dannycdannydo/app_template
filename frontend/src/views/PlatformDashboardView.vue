<script setup lang="ts">
import { computed, h, ref } from 'vue'
import { RouterLink } from 'vue-router'

import type { ApiError } from '@/api/errors'
import type { components } from '@/api/generated/openapi'
import DataTable from '@/components/application/DataTable.vue'
import type { DataTableColumn } from '@/components/application/DataTable.vue'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { cn } from '@/lib/utils'
import {
  usePlatformAdminsQuery,
  usePlatformAuditEventsQuery,
  usePlatformOrganisationsQuery,
} from '@/queries/platform'

type AuditEventListItem = components['schemas']['AuditEventListItem']

/**
 * Platform Admin Centre dashboard (Scope §6.9, acceptance §5.10).
 *
 * A read-only landing surface: the organisation count (from the platform
 * organisations list) and the most recent audit events (the append-only
 * trail, Scope §6.1). Both queries are cross-organisation server state from
 * the platform query layer; the cards link into the full screens.
 */
const organisationsPage = ref(1)
const organisationsPageSize = 1

const { data: organisationsData, isPending: organisationsPending } = usePlatformOrganisationsQuery(
  computed(() => ({ page: organisationsPage.value, pageSize: organisationsPageSize })),
)

const {
  data: auditData,
  isPending: auditPending,
  isError,
  error,
} = usePlatformAuditEventsQuery(computed(() => ({ page: 1, pageSize: 8 })))

const organisationCount = computed(() => organisationsData.value?.total ?? 0)

const { data: adminsData, isPending: adminsPending } = usePlatformAdminsQuery(
  computed(() => ({ page: 1, pageSize: 1 })),
)
const adminCount = computed(() => adminsData.value?.total ?? 0)

const auditEvents = computed(() => auditData.value?.items ?? [])

const tableError = computed<ApiError | null>(() =>
  isError.value ? (error.value as ApiError) : null,
)

const auditColumns: DataTableColumn<AuditEventListItem>[] = [
  { key: 'created_at', header: 'When' },
  { key: 'action', header: 'Action' },
  { key: 'resource_type', header: 'Resource' },
  {
    key: 'organisation_id',
    header: 'Organisation',
    cell: (row) => row.organisation_id ?? '—',
  },
  {
    key: 'link',
    header: '',
    align: 'right',
    className: 'w-16',
    cell: (row) =>
      row.organisation_id
        ? h(
            RouterLink,
            {
              to: {
                name: 'platform-organisation-detail',
                params: { organisationId: row.organisation_id },
              },
              class: cn(
                'text-muted-foreground hover:text-foreground focus-visible:ring-ring/50',
                'rounded-md px-2 py-1 text-sm font-medium outline-none',
                'focus-visible:ring-3',
              ),
            },
            { default: () => 'View' },
          )
        : '',
  },
]
</script>

<template>
  <div class="space-y-6">
    <div>
      <h1 class="text-2xl font-semibold">Platform Admin Centre</h1>
      <p class="text-muted-foreground mt-1 text-sm">
        Administer organisations, memberships, invitations, feature flags and the audit trail.
      </p>
    </div>

    <div class="grid gap-4 sm:grid-cols-3">
      <RouterLink
        :to="{ name: 'platform-organisations' }"
        class="focus-visible:ring-ring/50 rounded-xl outline-none focus-visible:ring-3"
      >
        <Card
          class="hover:bg-muted/50 transition-colors"
          data-testid="platform-dashboard-organisations"
        >
          <CardHeader>
            <CardTitle>Organisations</CardTitle>
            <CardDescription>
              <span v-if="organisationsPending" class="text-muted-foreground text-sm">
                Counting…
              </span>
              <span v-else class="text-3xl font-semibold">{{ organisationCount }}</span>
            </CardDescription>
          </CardHeader>
        </Card>
      </RouterLink>

      <RouterLink
        :to="{ name: 'platform-admins' }"
        class="focus-visible:ring-ring/50 rounded-xl outline-none focus-visible:ring-3"
      >
        <Card class="hover:bg-muted/50 transition-colors" data-testid="platform-dashboard-admins">
          <CardHeader>
            <CardTitle>Administrators</CardTitle>
            <CardDescription>
              <span v-if="adminsPending" class="text-muted-foreground text-sm">Loading…</span>
              <span v-else class="text-3xl font-semibold">{{ adminCount }}</span>
            </CardDescription>
          </CardHeader>
        </Card>
      </RouterLink>

      <RouterLink
        :to="{ name: 'platform-audit' }"
        class="focus-visible:ring-ring/50 rounded-xl outline-none focus-visible:ring-3"
      >
        <Card class="hover:bg-muted/50 transition-colors" data-testid="platform-dashboard-audit">
          <CardHeader>
            <CardTitle>Audit trail</CardTitle>
            <CardDescription>
              <span v-if="auditPending" class="text-muted-foreground text-sm">Loading…</span>
              <span v-else class="text-3xl font-semibold">{{ auditData?.total ?? 0 }}</span>
              <span class="text-muted-foreground text-sm"> recent events</span>
            </CardDescription>
          </CardHeader>
        </Card>
      </RouterLink>
    </div>

    <Card>
      <CardHeader>
        <CardTitle>Recent activity</CardTitle>
        <CardDescription>The latest append-only audit events across the platform.</CardDescription>
      </CardHeader>
      <CardContent>
        <DataTable
          :columns="auditColumns"
          :data="auditEvents"
          row-key="id"
          :pagination="{ page: 1, pageSize: 8, total: auditEvents.length }"
          :loading="auditPending"
          :error="tableError"
          empty-message="No audit events yet."
        />
      </CardContent>
    </Card>
  </div>
</template>
