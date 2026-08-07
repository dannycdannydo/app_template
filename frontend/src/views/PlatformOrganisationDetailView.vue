<script setup lang="ts">
import { toTypedSchema } from '@vee-validate/zod'
import { ArrowLeftIcon, LoaderCircleIcon, UserPlusIcon } from '@lucide/vue'
import { computed, h, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useForm } from 'vee-validate'
import { z } from 'zod'

import type { ApiError } from '@/api/errors'
import type { components } from '@/api/generated/openapi'
import DataTable from '@/components/application/DataTable.vue'
import type { DataTableColumn } from '@/components/application/DataTable.vue'
import NativeSelect from '@/components/application/NativeSelect.vue'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { FormControl, FormField, FormItem, FormLabel, FormMessage } from '@/components/ui/form'
import { Input } from '@/components/ui/input'
import { ORGANISATION_ROLE_CODES } from '@/lib/permissions'
import { showApiErrorToast, showSuccessToast } from '@/lib/toast'
import {
  useAssignMembershipRoleMutation,
  usePlatformAuditEventsQuery,
  usePlatformFeatureFlagsQuery,
  usePlatformInvitationsQuery,
  usePlatformMembershipsQuery,
  usePlatformOrganisationQuery,
  useRemoveMembershipMutation,
  useRemoveMembershipRoleMutation,
  useRevokeInvitationMutation,
  useSetFeatureFlagMutation,
  useSetMembershipStatusMutation,
  useUpdatePlatformOrganisationMutation,
} from '@/queries/platform'
import type { PlatformListParams } from '@/queries/platform'

type PlatformMembershipListItem = components['schemas']['PlatformMembershipListItem']
type InvitationListItem = components['schemas']['InvitationListItem']
type AuditEventListItem = components['schemas']['AuditEventListItem']

/**
 * Platform organisation detail (Scope §6.9, acceptance §5.10).
 *
 * The admin centre's single-organisation screen: the edit-name form, the
 * memberships table (role select, suspend/reactivate, remove), the
 * invitations list (revoke), the feature-flag toggles and the organisation's
 * audit events. Every mutation round-trips through the platform query layer
 * and toasts its result; the backend is the enforcement point for each.
 *
 * Memberships are a compact read-mostly table: the role select in each row
 * assigns a role, the small × removes one, the status arm suspends/reactivates,
 * and remove is the two-step inline confirm used across the template.
 */
const props = defineProps<{ organisationId: string }>()

const router = useRouter()

const {
  data: organisation,
  isPending: orgPending,
  isError: orgError,
  error: orgErrorValue,
} = usePlatformOrganisationQuery(() => props.organisationId)

const membershipsPage = ref(1)
const membershipsPageSize = 25
const invitationsPage = ref(1)
const invitationsPageSize = 25
const auditPage = ref(1)
const auditPageSize = 25

const membershipsParams = computed<PlatformListParams>(() => ({
  page: membershipsPage.value,
  pageSize: membershipsPageSize,
}))
const invitationsParams = computed<PlatformListParams>(() => ({
  page: invitationsPage.value,
  pageSize: invitationsPageSize,
}))
const auditParams = computed(() => ({
  page: auditPage.value,
  pageSize: auditPageSize,
  organisationId: props.organisationId,
}))

const { data: membershipsData, isPending: membershipsPending } = usePlatformMembershipsQuery(
  () => props.organisationId,
  membershipsParams,
)
const { data: invitationsData, isPending: invitationsPending } = usePlatformInvitationsQuery(
  () => props.organisationId,
  invitationsParams,
)
const { data: flagsData, isPending: flagsPending } = usePlatformFeatureFlagsQuery(
  () => props.organisationId,
)
const { data: auditData, isPending: auditPending } = usePlatformAuditEventsQuery(auditParams)

// --- Edit-name form (Scope §6.9) ---

const nameFormSchema = toTypedSchema(
  z.object({
    name: z
      .string()
      .trim()
      .min(1, 'Name is required.')
      .max(255, 'Name must be 255 characters or fewer.'),
  }),
)

const { handleSubmit, isSubmitting, setValues, resetForm } = useForm<{ name: string }>({
  validationSchema: nameFormSchema,
  initialValues: { name: '' },
})

// Hydrate the form once the detail query resolves (like RecordForm's watch).
watch(
  () => organisation.value,
  (value) => {
    if (value) {
      resetForm({ values: { name: value.name } })
    }
  },
)

const updateMutation = useUpdatePlatformOrganisationMutation({
  onSuccess: (updated) => {
    setValues({ name: updated.name })
    showSuccessToast('Organisation updated')
  },
})

const onRename = handleSubmit(async (values) => {
  try {
    await updateMutation.mutateAsync({ organisationId: props.organisationId, payload: values })
  } catch (error) {
    showApiErrorToast(error, { title: 'Could not update organisation' })
  }
})

// --- Memberships (Scope §6.6) ---

const memberships = computed(() => membershipsData.value?.items ?? [])
const membershipsPagination = computed(() => ({
  page: membershipsData.value?.page ?? membershipsPage.value,
  pageSize: membershipsData.value?.page_size ?? membershipsPageSize,
  total: membershipsData.value?.total ?? 0,
}))

const confirmRemoveMembershipId = ref<string | null>(null)

const assignRoleMutation = useAssignMembershipRoleMutation({
  onSuccess: (membership) => {
    showSuccessToast(`Role assigned to ${membership.user_name}`)
  },
})

const removeRoleMutation = useRemoveMembershipRoleMutation({
  onSuccess: (membership) => {
    showSuccessToast(`Role removed from ${membership.user_name}`)
  },
})

const statusMutation = useSetMembershipStatusMutation({
  onSuccess: (membership) => {
    showSuccessToast(
      membership.status === 'active'
        ? `${membership.user_name} reactivated`
        : `${membership.user_name} suspended`,
    )
  },
})

const removeMembershipMutation = useRemoveMembershipMutation({
  onSuccess: (membership) => {
    confirmRemoveMembershipId.value = null
    showSuccessToast(`${membership.user_name} removed from the organisation`)
  },
})

async function onRoleSelected(
  membership: PlatformMembershipListItem,
  roleCode: string,
): Promise<void> {
  if (!roleCode) return
  try {
    await assignRoleMutation.mutateAsync({
      organisationId: props.organisationId,
      membershipId: membership.id,
      roleCode,
    })
  } catch (error) {
    showApiErrorToast(error, { title: 'Could not assign role' })
  }
}

async function onRemoveRole(
  membership: PlatformMembershipListItem,
  roleCode: string,
): Promise<void> {
  try {
    await removeRoleMutation.mutateAsync({
      organisationId: props.organisationId,
      membershipId: membership.id,
      roleCode,
    })
  } catch (error) {
    showApiErrorToast(error, { title: 'Could not remove role' })
  }
}

async function onToggleStatus(membership: PlatformMembershipListItem): Promise<void> {
  try {
    await statusMutation.mutateAsync({
      organisationId: props.organisationId,
      membershipId: membership.id,
      status: membership.status === 'active' ? 'suspended' : 'active',
    })
  } catch (error) {
    showApiErrorToast(error, { title: 'Could not update membership status' })
  }
}

async function onRemoveMembership(membership: PlatformMembershipListItem): Promise<void> {
  try {
    await removeMembershipMutation.mutateAsync({
      organisationId: props.organisationId,
      membershipId: membership.id,
    })
  } catch (error) {
    showApiErrorToast(error, { title: 'Could not remove membership' })
  }
}

const roleOptions = ORGANISATION_ROLE_CODES.filter((code) => code !== 'owner')

const membershipColumns: DataTableColumn<PlatformMembershipListItem>[] = [
  { key: 'user_name', header: 'Member' },
  { key: 'user_email', header: 'Email' },
  {
    key: 'status',
    header: 'Status',
    cell: (row) => row.status,
  },
  {
    key: 'roles',
    header: 'Roles',
    cell: (row) =>
      h('div', { class: 'flex flex-wrap items-center gap-1' }, [
        ...row.roles.map((role) =>
          h(
            'span',
            {
              key: role,
              class:
                'border-border bg-muted text-muted-foreground inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-xs',
            },
            [
              role,
              h(
                'button',
                {
                  type: 'button',
                  class: 'text-muted-foreground hover:text-foreground pl-0.5 outline-none',
                  'aria-label': `Remove role ${role}`,
                  onClick: () => void onRemoveRole(row, role),
                },
                '×',
              ),
            ],
          ),
        ),
        h(
          NativeSelect,
          {
            class: 'h-7 w-28 text-xs',
            'aria-label': 'Assign a role',
            modelValue: '',
            'onUpdate:modelValue': (code: string) => void onRoleSelected(row, code),
          },
          {
            default: () => [
              h('option', { value: '', disabled: true }, 'Assign…'),
              ...roleOptions.map((code) => h('option', { key: code, value: code }, code)),
            ],
          },
        ),
      ]),
  },
  {
    key: 'actions',
    header: '',
    align: 'right',
    className: 'w-40',
    cell: (row) => {
      const isConfirming = confirmRemoveMembershipId.value === row.id
      return h('div', { class: 'flex items-center justify-end gap-1' }, [
        h(
          'button',
          {
            type: 'button',
            class: 'text-muted-foreground hover:text-foreground text-sm font-medium outline-none',
            onClick: () => void onToggleStatus(row),
          },
          row.status === 'active' ? 'Suspend' : 'Reactivate',
        ),
        isConfirming
          ? h('span', { class: 'flex items-center gap-1' }, [
              h(
                'button',
                {
                  type: 'button',
                  class:
                    'text-destructive hover:text-destructive/80 text-sm font-medium outline-none',
                  onClick: () => void onRemoveMembership(row),
                },
                'Confirm',
              ),
              h(
                'button',
                {
                  type: 'button',
                  class: 'text-muted-foreground text-sm outline-none',
                  onClick: () => {
                    confirmRemoveMembershipId.value = null
                  },
                },
                'Cancel',
              ),
            ])
          : h(
              'button',
              {
                type: 'button',
                class:
                  'text-muted-foreground hover:text-foreground text-sm font-medium outline-none',
                onClick: () => {
                  confirmRemoveMembershipId.value = row.id
                },
              },
              'Remove',
            ),
      ])
    },
  },
]

// --- Invitations (Scope §6.5) ---

const invitations = computed(() => invitationsData.value?.items ?? [])
const invitationsPagination = computed(() => ({
  page: invitationsData.value?.page ?? invitationsPage.value,
  pageSize: invitationsData.value?.page_size ?? invitationsPageSize,
  total: invitationsData.value?.total ?? 0,
}))

const revokeInvitationMutation = useRevokeInvitationMutation({
  onSuccess: (invitation) => {
    showSuccessToast(`Invitation to ${invitation.email} revoked`)
  },
})

async function onRevokeInvitation(invitation: InvitationListItem): Promise<void> {
  try {
    await revokeInvitationMutation.mutateAsync({
      organisationId: props.organisationId,
      invitationId: invitation.id,
    })
  } catch (error) {
    showApiErrorToast(error, { title: 'Could not revoke invitation' })
  }
}

const invitationColumns: DataTableColumn<InvitationListItem>[] = [
  { key: 'email', header: 'Email' },
  { key: 'role_code', header: 'Role' },
  { key: 'status', header: 'Status' },
  { key: 'expires_at', header: 'Expires' },
  {
    key: 'actions',
    header: '',
    align: 'right',
    className: 'w-24',
    cell: (row) =>
      row.status === 'sent'
        ? h(
            'button',
            {
              type: 'button',
              class: 'text-muted-foreground hover:text-foreground text-sm font-medium outline-none',
              onClick: () => void onRevokeInvitation(row),
            },
            'Revoke',
          )
        : '',
  },
]

// --- Feature flags (Scope §6.7) ---

const flags = computed(() => flagsData.value?.items ?? [])

const flagMutation = useSetFeatureFlagMutation({
  onSuccess: (flag) => {
    showSuccessToast(`Feature "${flag.name}" updated`)
  },
})

async function onToggleFlag(featureKey: string, enabled: boolean): Promise<void> {
  try {
    await flagMutation.mutateAsync({
      featureKey,
      organisationId: props.organisationId,
      enabled,
    })
  } catch (error) {
    showApiErrorToast(error, { title: 'Could not update feature flag' })
  }
}

// --- Org audit events (Scope §6.1) ---

const auditEvents = computed(() => auditData.value?.items ?? [])
const auditPagination = computed(() => ({
  page: auditData.value?.page ?? auditPage.value,
  pageSize: auditData.value?.page_size ?? auditPageSize,
  total: auditData.value?.total ?? 0,
}))

const auditColumns: DataTableColumn<AuditEventListItem>[] = [
  { key: 'created_at', header: 'When' },
  { key: 'action', header: 'Action' },
  { key: 'resource_type', header: 'Resource' },
  { key: 'resource_id', header: 'Resource id' },
  {
    key: 'actor_user_id',
    header: 'Actor',
    cell: (row) => row.actor_user_id ?? 'system',
  },
]

const loadError = computed<ApiError | null>(() =>
  orgError.value ? (orgErrorValue.value as ApiError) : null,
)
</script>

<template>
  <div class="space-y-6">
    <div class="flex items-start justify-between gap-4">
      <div>
        <Button
          variant="ghost"
          size="sm"
          class="text-muted-foreground -ml-2 mb-2"
          data-testid="platform-organisation-back"
          @click="router.push({ name: 'platform-organisations' })"
        >
          <ArrowLeftIcon class="size-4" />
          All organisations
        </Button>
        <h1 class="text-2xl font-semibold" data-testid="platform-organisation-title">
          {{ organisation?.name ?? 'Organisation' }}
        </h1>
        <p class="text-muted-foreground mt-1 text-sm">
          <span v-if="organisation?.workos_organisation_id">
            WorkOS organisation: {{ organisation.workos_organisation_id }}
          </span>
          <span v-else>No WorkOS mapping yet (backfilled at first invite).</span>
        </p>
      </div>
      <Button
        data-testid="platform-invite-user-button"
        @click="router.push({ name: 'platform-invite-user', params: { organisationId } })"
      >
        <UserPlusIcon class="size-4" />
        Invite user
      </Button>
    </div>

    <Card v-if="orgPending" data-testid="platform-organisation-loading">
      <CardContent class="text-muted-foreground text-sm">Loading organisation…</CardContent>
    </Card>

    <template v-else>
      <Card v-if="loadError" data-testid="platform-organisation-error">
        <CardContent class="text-muted-foreground text-sm">{{ loadError.message }}</CardContent>
      </Card>

      <Card v-else>
        <CardHeader>
          <CardTitle>Organisation details</CardTitle>
          <CardDescription>Rename the organisation (audited).</CardDescription>
        </CardHeader>
        <CardContent>
          <form
            data-testid="platform-organisation-rename-form"
            novalidate
            class="grid max-w-md gap-4"
            @submit="onRename"
          >
            <FormField v-slot="{ componentField }" name="name">
              <FormItem>
                <FormLabel>Name</FormLabel>
                <FormControl>
                  <Input
                    type="text"
                    placeholder="Organisation name"
                    :disabled="isSubmitting"
                    v-bind="componentField"
                  />
                </FormControl>
                <FormMessage />
              </FormItem>
            </FormField>
            <div class="flex justify-end">
              <Button
                type="submit"
                size="sm"
                :disabled="isSubmitting"
                data-testid="platform-organisation-rename-submit"
              >
                <LoaderCircleIcon v-if="isSubmitting" class="animate-spin" />
                Save changes
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Memberships</CardTitle>
          <CardDescription>
            Role assignment, suspension and removal — all audited.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <DataTable
            :columns="membershipColumns"
            :data="memberships"
            row-key="id"
            :pagination="membershipsPagination"
            :loading="membershipsPending"
            empty-message="No members yet. Invite someone to get started."
            @update:page="membershipsPage = $event"
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Invitations</CardTitle>
          <CardDescription>Sent through the WorkOS Invitation API.</CardDescription>
        </CardHeader>
        <CardContent>
          <DataTable
            :columns="invitationColumns"
            :data="invitations"
            row-key="id"
            :pagination="invitationsPagination"
            :loading="invitationsPending"
            empty-message="No invitations yet."
            @update:page="invitationsPage = $event"
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Feature flags</CardTitle>
          <CardDescription>Platform-controlled organisation overrides.</CardDescription>
        </CardHeader>
        <CardContent>
          <div v-if="flagsPending" class="text-muted-foreground text-sm">Loading flags…</div>
          <ul v-else class="divide-border divide-y">
            <li
              v-for="flag in flags"
              :key="flag.feature_key"
              class="flex items-center justify-between gap-4 py-3"
            >
              <div>
                <p class="text-sm font-medium">{{ flag.name }}</p>
                <p class="text-muted-foreground text-xs">{{ flag.description }}</p>
                <p v-if="flag.overridden" class="text-muted-foreground mt-0.5 text-xs">
                  Explicitly set for this organisation
                </p>
                <p v-else class="text-muted-foreground mt-0.5 text-xs">
                  Using the catalogue default
                </p>
              </div>
              <button
                type="button"
                role="switch"
                :aria-checked="flag.enabled"
                :aria-label="`Toggle ${flag.name}`"
                :data-testid="`feature-flag-toggle-${flag.feature_key}`"
                class="focus-visible:ring-ring/50 inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors focus-visible:ring-3 outline-none"
                :class="flag.enabled ? 'bg-primary' : 'bg-input'"
                @click="() => void onToggleFlag(flag.feature_key, !flag.enabled)"
              >
                <span
                  class="bg-background size-4 rounded-full shadow-sm transition-transform"
                  :class="flag.enabled ? 'translate-x-6' : 'translate-x-1'"
                />
              </button>
            </li>
          </ul>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Audit history</CardTitle>
          <CardDescription>Append-only events for this organisation.</CardDescription>
        </CardHeader>
        <CardContent>
          <DataTable
            :columns="auditColumns"
            :data="auditEvents"
            row-key="id"
            :pagination="auditPagination"
            :loading="auditPending"
            empty-message="No audit events for this organisation yet."
            @update:page="auditPage = $event"
          />
        </CardContent>
      </Card>
    </template>
  </div>
</template>
