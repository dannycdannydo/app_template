<script setup lang="ts">
import { computed, h, ref } from 'vue'

import type { ApiError } from '@/api/errors'
import type { components } from '@/api/generated/openapi'
import DataTable from '@/components/application/DataTable.vue'
import type { DataTableColumn } from '@/components/application/DataTable.vue'
import NativeSelect from '@/components/application/NativeSelect.vue'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { showApiErrorToast, showSuccessToast } from '@/lib/toast'
import {
  useGrantPlatformAdminMutation,
  usePlatformAdminsQuery,
  usePlatformUsersQuery,
  useRevokePlatformAdminMutation,
} from '@/queries/platform'

type PlatformAdminListItem = components['schemas']['PlatformAdminListItem']

const page = ref(1)
const pageSize = 50
const selectedUserId = ref('')
const { data, isPending, isError, error } = usePlatformAdminsQuery(
  computed(() => ({ page: page.value, pageSize })),
)
const admins = computed(() => data.value?.items ?? [])
const total = computed(() => data.value?.total ?? 0)
const { data: usersData, isPending: usersPending } = usePlatformUsersQuery(() => ({
  page: 1,
  pageSize: 100,
}))
const users = computed(() => usersData.value?.items ?? [])

const grant = useGrantPlatformAdminMutation({
  onSuccess: () => {
    selectedUserId.value = ''
    showSuccessToast('Platform administrator granted.')
  },
})
const revoke = useRevokePlatformAdminMutation({
  onSuccess: () => showSuccessToast('Platform administrator revoked.'),
})

function grantAdmin(): void {
  if (!selectedUserId.value) return
  grant.mutate(selectedUserId.value, { onError: (cause) => showApiErrorToast(cause) })
}

function revokeAdmin(admin: PlatformAdminListItem): void {
  if (total.value <= 1) return
  revoke.mutate(admin.id, { onError: (cause) => showApiErrorToast(cause) })
}

const columns: DataTableColumn<PlatformAdminListItem>[] = [
  { key: 'user_name', header: 'Name' },
  { key: 'user_email', header: 'Email' },
  { key: 'role_code', header: 'Role' },
  { key: 'created_at', header: 'Granted' },
  {
    key: 'actions',
    header: '',
    align: 'right',
    cell: (admin) =>
      h(
        Button,
        {
          variant: 'outline',
          size: 'sm',
          disabled: total.value <= 1 || revoke.isPending.value,
          title: total.value <= 1 ? 'At least one platform administrator must remain.' : undefined,
          onClick: () => revokeAdmin(admin),
        },
        { default: () => 'Revoke' },
      ),
  },
]

const tableError = computed<ApiError | null>(() =>
  isError.value ? (error.value as ApiError) : null,
)
</script>

<template>
  <div class="space-y-6">
    <div>
      <h1 class="text-2xl font-semibold">Platform administrators</h1>
      <p class="text-muted-foreground mt-1 text-sm">
        Grant and revoke the separate, cross-organisation platform-admin role.
      </p>
    </div>

    <Card>
      <CardHeader>
        <CardTitle>Grant administrator access</CardTitle>
        <CardDescription>
          Select an existing, enabled user. Users appear after their first sign-in.
        </CardDescription>
      </CardHeader>
      <CardContent class="flex flex-col gap-3 sm:flex-row">
        <NativeSelect
          v-model="selectedUserId"
          aria-label="User to grant platform administrator"
          :disabled="usersPending"
        >
          <option value="">{{ usersPending ? 'Loading users…' : 'Select a user' }}</option>
          <option v-for="user in users" :key="user.id" :value="user.id">
            {{ user.name }} — {{ user.email }}
          </option>
        </NativeSelect>
        <Button :disabled="!selectedUserId || grant.isPending.value" @click="grantAdmin">
          {{ grant.isPending.value ? 'Granting…' : 'Grant admin' }}
        </Button>
      </CardContent>
    </Card>

    <Card>
      <CardHeader>
        <CardTitle>Current administrators</CardTitle>
        <CardDescription>
          The final administrator cannot be revoked, preventing loss of platform access.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <DataTable
          :columns="columns"
          :data="admins"
          row-key="id"
          :pagination="{ page, pageSize, total }"
          :loading="isPending"
          :error="tableError"
          empty-message="No platform administrators found."
          @update:page="page = $event"
        />
      </CardContent>
    </Card>
  </div>
</template>
