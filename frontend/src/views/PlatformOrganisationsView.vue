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
import { usePlatformOrganisationsQuery } from '@/queries/platform'

type PlatformOrganisationResponse = components['schemas']['PlatformOrganisationResponse']

/**
 * Platform organisations list (Scope §6.9, acceptance §5.10).
 *
 * The admin centre's catalogue over the whole tenant fleet, fed by the
 * platform query layer (cross-organisation server state, blueprint §14). Each
 * row links to the organisation detail; a platform admin can also create an
 * organisation here. Pagination state is local view state, like the records
 * list.
 */
const router = useRouter()

const page = ref(1)
const pageSize = 25

const { data, isPending, isError, error } = usePlatformOrganisationsQuery(
  computed(() => ({ page: page.value, pageSize })),
)

const columns: DataTableColumn<PlatformOrganisationResponse>[] = [
  { key: 'name', header: 'Organisation' },
  {
    key: 'workos_organisation_id',
    header: 'WorkOS mapping',
    cell: (row) => row.workos_organisation_id ?? '—',
  },
  { key: 'created_at', header: 'Created' },
  {
    key: 'actions',
    header: '',
    align: 'right',
    className: 'w-16',
    cell: (row) =>
      h(
        RouterLink,
        {
          to: { name: 'platform-organisation-detail', params: { organisationId: row.id } },
          class: cn(
            'text-muted-foreground hover:text-foreground focus-visible:ring-ring/50',
            'rounded-md px-2 py-1 text-sm font-medium outline-none',
            'focus-visible:ring-3 [&.router-link-active]:text-foreground',
          ),
        },
        { default: () => 'View' },
      ),
  },
]

const organisations = computed(() => data.value?.items ?? [])

const pagination = computed(() => ({
  page: data.value?.page ?? page.value,
  pageSize: data.value?.page_size ?? pageSize,
  total: data.value?.total ?? 0,
}))

const tableError = computed<ApiError | null>(() =>
  isError.value ? (error.value as ApiError) : null,
)

function goToCreate(): void {
  void router.push({ name: 'platform-organisation-new' })
}
</script>

<template>
  <div class="space-y-6">
    <div class="flex items-start justify-between gap-4">
      <div>
        <h1 class="text-2xl font-semibold">Organisations</h1>
        <p class="text-muted-foreground mt-1 text-sm">
          Every organisation on the platform, with its WorkOS mapping.
        </p>
      </div>
      <Button data-testid="platform-organisation-create-button" @click="goToCreate">
        <PlusIcon class="size-4" />
        New organisation
      </Button>
    </div>

    <Card>
      <CardHeader>
        <CardTitle>All organisations</CardTitle>
        <CardDescription>
          {{ pagination.total }} organisation{{ pagination.total === 1 ? '' : 's' }} on the
          platform.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <DataTable
          :columns="columns"
          :data="organisations"
          row-key="id"
          :pagination="pagination"
          :loading="isPending"
          :error="tableError"
          empty-message="No organisations yet. Create the first one to get started."
          @update:page="page = $event"
        />
      </CardContent>
    </Card>
  </div>
</template>
