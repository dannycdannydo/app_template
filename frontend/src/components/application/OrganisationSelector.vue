<script setup lang="ts">
import { computed, watch } from 'vue'
import { BuildingIcon, ChevronsUpDownIcon } from '@lucide/vue'

import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { useMeQuery } from '@/queries/me'
import { useOrganisationStore } from '@/stores/organisation'

/**
 * Organisation selector in the shell header (Scope §6.3, acceptance §5.6).
 *
 * The options are the active memberships returned by `/me` (server data owned
 * by the query layer). Selection is client state: it lives in the
 * organisation store, persists in localStorage and is attached as `X-Org-Id`
 * by the client middleware on every subsequent request (blueprint §14 client-
 * state boundary).
 *
 * If the persisted selection is no longer among the memberships (revoked,
 * removed, fresh account) the first active membership is selected instead.
 */
const { data, isPending, isError } = useMeQuery()
const organisation = useOrganisationStore()

const memberships = computed(
  () => data.value?.memberships.filter((membership) => membership.status === 'active') ?? [],
)

const current = computed(() => {
  const selectedId = organisation.selectedOrganisationId
  if (!selectedId) return null
  return memberships.value.find((membership) => membership.organisation_id === selectedId) ?? null
})

watch(
  memberships,
  (list) => {
    if (list.length === 0) {
      if (organisation.selectedOrganisationId !== null) {
        organisation.setSelectedOrganisation(null)
      }
      return
    }
    if (
      !list.some((membership) => membership.organisation_id === organisation.selectedOrganisationId)
    ) {
      organisation.setSelectedOrganisation(list[0]!.organisation_id)
    }
  },
  { immediate: true },
)

function selectOrganisation(value: unknown): void {
  const id = typeof value === 'string' ? value : null
  if (id && id !== organisation.selectedOrganisationId) {
    organisation.setSelectedOrganisation(id)
  }
}
</script>

<template>
  <DropdownMenu>
    <DropdownMenuTrigger as-child>
      <Button
        variant="outline"
        class="h-9 gap-2 rounded-md px-2.5"
        data-testid="org-selector-trigger"
        :disabled="isPending || memberships.length === 0"
      >
        <BuildingIcon class="text-muted-foreground size-4 shrink-0" />
        <span class="max-w-40 truncate text-sm font-medium">
          <template v-if="isPending">Loading…</template>
          <template v-else-if="isError">Unavailable</template>
          <template v-else-if="current">
            {{ current.organisation_name }}
          </template>
          <template v-else>No organisation</template>
        </span>
        <ChevronsUpDownIcon class="text-muted-foreground size-3.5 shrink-0" />
      </Button>
    </DropdownMenuTrigger>

    <DropdownMenuContent align="end" class="w-64" data-testid="org-selector-content">
      <DropdownMenuLabel>Switch organisation</DropdownMenuLabel>
      <DropdownMenuSeparator />
      <DropdownMenuRadioGroup
        :model-value="organisation.selectedOrganisationId ?? ''"
        @update:model-value="selectOrganisation($event)"
      >
        <DropdownMenuRadioItem
          v-for="membership in memberships"
          :key="membership.organisation_id"
          :value="membership.organisation_id"
          data-testid="org-selector-option"
        >
          {{ membership.organisation_name }}
        </DropdownMenuRadioItem>
      </DropdownMenuRadioGroup>
    </DropdownMenuContent>
  </DropdownMenu>
</template>
