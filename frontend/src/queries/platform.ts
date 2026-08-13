import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import type { MaybeRefOrGetter } from 'vue'
import { computed, toValue } from 'vue'

import { client } from '@/api/client'
import type { components } from '@/api/generated/openapi'
import { useMeQuery } from '@/queries/me'

type PlatformOrganisationResponse = components['schemas']['PlatformOrganisationResponse']
type PlatformOrganisationListResponse = components['schemas']['PlatformOrganisationListResponse']
type PlatformOrganisationCreate = components['schemas']['PlatformOrganisationCreate']
type PlatformOrganisationUpdate = components['schemas']['PlatformOrganisationUpdate']
type PlatformMembershipListItem = components['schemas']['PlatformMembershipListItem']
type PlatformMembershipListResponse = components['schemas']['PlatformMembershipListResponse']
type PlatformMembershipRoleAssign = components['schemas']['PlatformMembershipRoleAssign']
type PlatformMembershipStatusUpdate = components['schemas']['PlatformMembershipStatusUpdate']
type InvitationCreate = components['schemas']['InvitationCreate']
type InvitationListItem = components['schemas']['InvitationListItem']
type InvitationListResponse = components['schemas']['InvitationListResponse']
type AuditEventListResponse = components['schemas']['AuditEventListResponse']
type PlatformFeatureFlagListResponse = components['schemas']['PlatformFeatureFlagListResponse']
type PlatformFeatureFlagItem = components['schemas']['PlatformFeatureFlagItem']
type PlatformFeatureFlagUpdate = components['schemas']['PlatformFeatureFlagUpdate']
type PlatformAdminListItem = components['schemas']['PlatformAdminListItem']
type PlatformAdminListResponse = components['schemas']['PlatformAdminListResponse']
type PlatformUserListResponse = components['schemas']['PlatformUserListResponse']
type PlatformOrganisationAISettingsResponse =
  components['schemas']['PlatformOrganisationAISettingsResponse']
type PlatformOrganisationAISettingsUpdate =
  components['schemas']['PlatformOrganisationAISettingsUpdate']

/**
 * Pagination parameters accepted by the platform query layer (Scope §6.9).
 *
 * TS side is camelCase; the mapping to the API's snake_case query parameters
 * happens here, in one place (blueprint §12: `?page=1&page_size=50`).
 */
export interface PlatformListParams {
  page: number
  pageSize: number
}

/**
 * Audit listing filters (Scope §6.1): all optional, all approved by the API.
 */
export interface PlatformAuditParams extends PlatformListParams {
  organisationId?: string
  actorUserId?: string
  action?: string
}

/**
 * Query-key factory for the platform plane (Scope §6.9, blueprint §14).
 *
 * Keys are cross-organisation server state, so they live under the `platform`
 * root rather than the org-scoped `organisations` subtree — the platform
 * admin centre administers organisations the caller does not belong to, so
 * the organisation-switch invalidator must not touch it. List keys carry the
 * normalized params object as the final segment, like the records layer.
 */
export const platformQueryKeys = {
  all: ['platform'] as const,
  organisations: ['platform', 'organisations'] as const,
  admins: ['platform', 'admins'] as const,
  adminsList: (params: PlatformListParams) => ['platform', 'admins', 'list', params] as const,
  usersList: (params: PlatformListParams) => ['platform', 'users', 'list', params] as const,
  organisationsList: (params: PlatformListParams) =>
    ['platform', 'organisations', 'list', params] as const,
  organisation: (organisationId: string) =>
    ['platform', 'organisations', 'detail', organisationId] as const,
  membershipsList: (organisationId: string, params: PlatformListParams) =>
    ['platform', 'organisations', organisationId, 'memberships', 'list', params] as const,
  invitationsList: (organisationId: string, params: PlatformListParams) =>
    ['platform', 'organisations', organisationId, 'invitations', 'list', params] as const,
  featureFlags: (organisationId?: string) =>
    organisationId
      ? (['platform', 'feature-flags', organisationId] as const)
      : (['platform', 'feature-flags'] as const),
  aiSettings: (organisationId: string) =>
    ['platform', 'organisations', organisationId, 'ai-settings'] as const,
  auditList: (params: PlatformAuditParams) => ['platform', 'audit', 'list', params] as const,
}

/**
 * Paginated list of every organisation on the platform plane (Scope §6.9).
 *
 * Unlike the org-scoped records queries this never reads the selected
 * organisation: the caller is a platform administrator administering
 * organisations they may not belong to (blueprint §9 — the platform plane is
 * a separate authorisation plane). The query is always enabled; the backend
 * gate answers `403 platform_admin_required` for non-admins.
 */
export function usePlatformOrganisationsQuery(params: MaybeRefOrGetter<PlatformListParams>) {
  const resolvedParams = computed(() => toValue(params))
  return useQuery({
    queryKey: computed(() => platformQueryKeys.organisationsList(resolvedParams.value)),
    queryFn: async () => {
      const { data, error } = await client.GET('/api/v1/platform/organisations', {
        params: {
          query: { page: resolvedParams.value.page, page_size: resolvedParams.value.pageSize },
        },
      })
      if (error) throw error
      if (!data) throw new Error('Empty platform organisations response')
      return data as PlatformOrganisationListResponse
    },
    retry: false,
    staleTime: 30_000,
  })
}

/** Paginated list of explicit platform administrators. */
export function usePlatformAdminsQuery(params: MaybeRefOrGetter<PlatformListParams>) {
  const resolvedParams = computed(() => toValue(params))
  return useQuery({
    queryKey: computed(() => platformQueryKeys.adminsList(resolvedParams.value)),
    queryFn: async () => {
      const { data, error } = await client.GET('/api/v1/platform/admins', {
        params: {
          query: { page: resolvedParams.value.page, page_size: resolvedParams.value.pageSize },
        },
      })
      if (error) throw error
      if (!data) throw new Error('Empty platform administrators response')
      return data as PlatformAdminListResponse
    },
    retry: false,
    staleTime: 30_000,
  })
}

/** Enabled users that a platform admin may select for a platform-role grant. */
export function usePlatformUsersQuery(params: MaybeRefOrGetter<PlatformListParams>) {
  const resolvedParams = computed(() => toValue(params))
  return useQuery({
    queryKey: computed(() => platformQueryKeys.usersList(resolvedParams.value)),
    queryFn: async () => {
      const { data, error } = await client.GET('/api/v1/platform/users', {
        params: {
          query: { page: resolvedParams.value.page, page_size: resolvedParams.value.pageSize },
        },
      })
      if (error) throw error
      if (!data) throw new Error('Empty platform users response')
      return data as PlatformUserListResponse
    },
    retry: false,
    staleTime: 30_000,
  })
}

/** Grant the platform-admin role to an existing enabled user. */
export function useGrantPlatformAdminMutation(options?: {
  onSuccess?: (admin: PlatformAdminListItem) => void
}) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (userId: string) => {
      const { data, error } = await client.POST('/api/v1/platform/admins', {
        body: { user_id: userId },
      })
      if (error) throw error
      if (!data) throw new Error('Empty platform administrator response')
      return data as PlatformAdminListItem
    },
    onSuccess: (admin) => {
      void queryClient.invalidateQueries({ queryKey: platformQueryKeys.admins })
      options?.onSuccess?.(admin)
    },
  })
}

/** Revoke one platform-admin membership. The backend protects the final admin. */
export function useRevokePlatformAdminMutation(options?: {
  onSuccess?: (admin: PlatformAdminListItem) => void
}) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (platformMembershipId: string) => {
      const { data, error } = await client.DELETE(
        '/api/v1/platform/admins/{platform_membership_id}',
        { params: { path: { platform_membership_id: platformMembershipId } } },
      )
      if (error) throw error
      if (!data) throw new Error('Empty revoked platform administrator response')
      return data as PlatformAdminListItem
    },
    onSuccess: (admin) => {
      void queryClient.invalidateQueries({ queryKey: platformQueryKeys.admins })
      options?.onSuccess?.(admin)
    },
  })
}

/**
 * Single-organisation detail on the platform plane (Scope §6.9).
 */
export function usePlatformOrganisationQuery(organisationId: MaybeRefOrGetter<string>) {
  const resolvedId = computed(() => toValue(organisationId))
  return useQuery({
    queryKey: computed(() => platformQueryKeys.organisation(resolvedId.value)),
    queryFn: async () => {
      const { data, error } = await client.GET('/api/v1/platform/organisations/{organisation_id}', {
        params: { path: { organisation_id: resolvedId.value } },
      })
      if (error) throw error
      if (!data) throw new Error('Empty platform organisation response')
      return data
    },
    enabled: computed(() => resolvedId.value !== ''),
    retry: false,
    staleTime: 30_000,
  })
}

/**
 * Create an organisation on the platform plane (Scope §6.3/§6.9), then
 * invalidate the platform organisations list.
 */
export function useCreatePlatformOrganisationMutation(options?: {
  onSuccess?: (organisation: PlatformOrganisationResponse) => void
}) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (body: PlatformOrganisationCreate) => {
      const { data, error } = await client.POST('/api/v1/platform/organisations', { body })
      if (error) throw error
      if (!data) throw new Error('Empty create platform organisation response')
      return data
    },
    onSuccess: (organisation) => {
      void queryClient.invalidateQueries({ queryKey: platformQueryKeys.organisations })
      options?.onSuccess?.(organisation)
    },
  })
}

/**
 * Rename an organisation on the platform plane (Scope §6.9), invalidating the
 * list and the edited organisation's detail.
 */
export function useUpdatePlatformOrganisationMutation(options?: {
  onSuccess?: (organisation: PlatformOrganisationResponse) => void
}) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({
      organisationId,
      payload,
    }: {
      organisationId: string
      payload: PlatformOrganisationUpdate
    }) => {
      const { data, error } = await client.PATCH(
        '/api/v1/platform/organisations/{organisation_id}',
        {
          params: { path: { organisation_id: organisationId } },
          body: payload,
        },
      )
      if (error) throw error
      if (!data) throw new Error('Empty update platform organisation response')
      return data
    },
    onSuccess: (organisation) => {
      void queryClient.invalidateQueries({ queryKey: platformQueryKeys.organisations })
      void queryClient.invalidateQueries({
        queryKey: platformQueryKeys.organisation(organisation.id),
      })
      options?.onSuccess?.(organisation)
    },
  })
}

/**
 * Paginated memberships of one organisation on the platform plane (Scope §6.6).
 */
export function usePlatformMembershipsQuery(
  organisationId: MaybeRefOrGetter<string>,
  params: MaybeRefOrGetter<PlatformListParams>,
) {
  const resolvedId = computed(() => toValue(organisationId))
  const resolvedParams = computed(() => toValue(params))
  return useQuery({
    queryKey: computed(() =>
      platformQueryKeys.membershipsList(resolvedId.value, resolvedParams.value),
    ),
    queryFn: async () => {
      const { data, error } = await client.GET(
        '/api/v1/platform/organisations/{organisation_id}/memberships',
        {
          params: {
            path: { organisation_id: resolvedId.value },
            query: { page: resolvedParams.value.page, page_size: resolvedParams.value.pageSize },
          },
        },
      )
      if (error) throw error
      if (!data) throw new Error('Empty platform memberships response')
      return data as PlatformMembershipListResponse
    },
    enabled: computed(() => resolvedId.value !== ''),
    retry: false,
    staleTime: 30_000,
  })
}

/** Invalidate one organisation's memberships list. */
function invalidateMemberships(
  queryClient: ReturnType<typeof useQueryClient>,
  organisationId: string,
): void {
  void queryClient.invalidateQueries({
    queryKey: ['platform', 'organisations', organisationId, 'memberships'],
  })
}

/** Assign an organisation role to a membership (Scope §6.6). */
export function useAssignMembershipRoleMutation(options?: {
  onSuccess?: (membership: PlatformMembershipListItem) => void
}) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({
      organisationId,
      membershipId,
      roleCode,
    }: {
      organisationId: string
      membershipId: string
      roleCode: string
    }) => {
      const body: PlatformMembershipRoleAssign = { role_code: roleCode }
      const { data, error } = await client.POST(
        '/api/v1/platform/organisations/{organisation_id}/memberships/{membership_id}/roles',
        {
          params: { path: { organisation_id: organisationId, membership_id: membershipId } },
          body,
        },
      )
      if (error) throw error
      if (!data) throw new Error('Empty assign membership role response')
      return { organisationId, membership: data }
    },
    onSuccess: (result) => {
      invalidateMemberships(queryClient, result.organisationId)
      options?.onSuccess?.(result.membership)
    },
  })
}

/** Remove an organisation role from a membership (Scope §6.6). */
export function useRemoveMembershipRoleMutation(options?: {
  onSuccess?: (membership: PlatformMembershipListItem) => void
}) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({
      organisationId,
      membershipId,
      roleCode,
    }: {
      organisationId: string
      membershipId: string
      roleCode: string
    }) => {
      const { data, error } = await client.DELETE(
        '/api/v1/platform/organisations/{organisation_id}/memberships/{membership_id}/roles/{role_code}',
        {
          params: {
            path: {
              organisation_id: organisationId,
              membership_id: membershipId,
              role_code: roleCode,
            },
          },
        },
      )
      if (error) throw error
      if (!data) throw new Error('Empty remove membership role response')
      return { organisationId, membership: data }
    },
    onSuccess: (result) => {
      invalidateMemberships(queryClient, result.organisationId)
      options?.onSuccess?.(result.membership)
    },
  })
}

/** Suspend or reactivate a membership (Scope §6.6). */
export function useSetMembershipStatusMutation(options?: {
  onSuccess?: (membership: PlatformMembershipListItem) => void
}) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({
      organisationId,
      membershipId,
      status,
    }: {
      organisationId: string
      membershipId: string
      status: 'active' | 'suspended'
    }) => {
      const body: PlatformMembershipStatusUpdate = { status }
      const { data, error } = await client.PATCH(
        '/api/v1/platform/organisations/{organisation_id}/memberships/{membership_id}/status',
        {
          params: { path: { organisation_id: organisationId, membership_id: membershipId } },
          body,
        },
      )
      if (error) throw error
      if (!data) throw new Error('Empty membership status response')
      return { organisationId, membership: data }
    },
    onSuccess: (result) => {
      invalidateMemberships(queryClient, result.organisationId)
      options?.onSuccess?.(result.membership)
    },
  })
}

/** Remove a membership (Scope §6.6). */
export function useRemoveMembershipMutation(options?: {
  onSuccess?: (membership: PlatformMembershipListItem) => void
}) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({
      organisationId,
      membershipId,
    }: {
      organisationId: string
      membershipId: string
    }) => {
      const { data, error } = await client.DELETE(
        '/api/v1/platform/organisations/{organisation_id}/memberships/{membership_id}',
        {
          params: { path: { organisation_id: organisationId, membership_id: membershipId } },
        },
      )
      if (error) throw error
      if (!data) throw new Error('Empty remove membership response')
      return { organisationId, membership: data }
    },
    onSuccess: (result) => {
      invalidateMemberships(queryClient, result.organisationId)
      options?.onSuccess?.(result.membership)
    },
  })
}

/**
 * Paginated invitations of one organisation on the platform plane (Scope §6.5).
 */
export function usePlatformInvitationsQuery(
  organisationId: MaybeRefOrGetter<string>,
  params: MaybeRefOrGetter<PlatformListParams>,
) {
  const resolvedId = computed(() => toValue(organisationId))
  const resolvedParams = computed(() => toValue(params))
  return useQuery({
    queryKey: computed(() =>
      platformQueryKeys.invitationsList(resolvedId.value, resolvedParams.value),
    ),
    queryFn: async () => {
      const { data, error } = await client.GET(
        '/api/v1/platform/organisations/{organisation_id}/invitations',
        {
          params: {
            path: { organisation_id: resolvedId.value },
            query: { page: resolvedParams.value.page, page_size: resolvedParams.value.pageSize },
          },
        },
      )
      if (error) throw error
      if (!data) throw new Error('Empty platform invitations response')
      return data as InvitationListResponse
    },
    enabled: computed(() => resolvedId.value !== ''),
    retry: false,
    staleTime: 30_000,
  })
}

/** Invalidate one organisation's invitations list. */
function invalidateInvitations(
  queryClient: ReturnType<typeof useQueryClient>,
  organisationId: string,
): void {
  void queryClient.invalidateQueries({
    queryKey: ['platform', 'organisations', organisationId, 'invitations'],
  })
}

/** Invite a user into an organisation through WorkOS (Scope §6.5). */
export function useCreateInvitationMutation(options?: {
  onSuccess?: (invitation: InvitationListItem) => void
}) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({
      organisationId,
      body,
    }: {
      organisationId: string
      body: InvitationCreate
    }) => {
      const { data, error } = await client.POST(
        '/api/v1/platform/organisations/{organisation_id}/invitations',
        {
          params: { path: { organisation_id: organisationId } },
          body,
        },
      )
      if (error) throw error
      if (!data) throw new Error('Empty create invitation response')
      return { organisationId, invitation: data }
    },
    onSuccess: (result) => {
      invalidateInvitations(queryClient, result.organisationId)
      options?.onSuccess?.(result.invitation)
    },
  })
}

/** Revoke a pending invitation (Scope §6.5). */
export function useRevokeInvitationMutation(options?: {
  onSuccess?: (invitation: InvitationListItem) => void
}) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({
      organisationId,
      invitationId,
    }: {
      organisationId: string
      invitationId: string
    }) => {
      const { data, error } = await client.DELETE(
        '/api/v1/platform/organisations/{organisation_id}/invitations/{invitation_id}',
        {
          params: { path: { organisation_id: organisationId, invitation_id: invitationId } },
        },
      )
      if (error) throw error
      if (!data) throw new Error('Empty revoke invitation response')
      return { organisationId, invitation: data }
    },
    onSuccess: (result) => {
      invalidateInvitations(queryClient, result.organisationId)
      options?.onSuccess?.(result.invitation)
    },
  })
}

/**
 * Feature-flag catalogue, optionally merged with one organisation's overrides
 * (Scope §6.7). Without an organisation id every flag shows its default.
 */
export function usePlatformFeatureFlagsQuery(organisationId: MaybeRefOrGetter<string | null>) {
  const resolvedId = computed(() => toValue(organisationId))
  return useQuery({
    queryKey: computed(() => platformQueryKeys.featureFlags(resolvedId.value ?? undefined)),
    queryFn: async () => {
      const { data, error } = await client.GET('/api/v1/platform/feature-flags', {
        params: {
          query: resolvedId.value ? { organisation_id: resolvedId.value } : undefined,
        },
      })
      if (error) throw error
      if (!data) throw new Error('Empty feature-flag catalogue response')
      return data as PlatformFeatureFlagListResponse
    },
    retry: false,
    staleTime: 30_000,
  })
}

/** Set one organisation's override for a feature flag (Scope §6.7). */
export function useSetFeatureFlagMutation(options?: {
  onSuccess?: (flag: PlatformFeatureFlagItem) => void
}) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({
      featureKey,
      organisationId,
      enabled,
    }: {
      featureKey: string
      organisationId: string
      enabled: boolean
    }) => {
      const body: PlatformFeatureFlagUpdate = { organisation_id: organisationId, enabled }
      const { data, error } = await client.PUT('/api/v1/platform/feature-flags/{feature_key}', {
        params: { path: { feature_key: featureKey } },
        body,
      })
      if (error) throw error
      if (!data) throw new Error('Empty feature-flag update response')
      return { organisationId, flag: data }
    },
    onSuccess: (result) => {
      void queryClient.invalidateQueries({
        queryKey: platformQueryKeys.featureFlags(result.organisationId),
      })
      options?.onSuccess?.(result.flag)
    },
  })
}

/**
 * Paginated audit history, filterable by organisation, actor and action
 * (Scope §6.1, blueprint §29). The platform audit trail is read-only.
 */
export function usePlatformAuditEventsQuery(params: MaybeRefOrGetter<PlatformAuditParams>) {
  const resolvedParams = computed(() => toValue(params))
  return useQuery({
    queryKey: computed(() => platformQueryKeys.auditList(resolvedParams.value)),
    queryFn: async () => {
      const { data, error } = await client.GET('/api/v1/platform/audit-events', {
        params: {
          query: {
            page: resolvedParams.value.page,
            page_size: resolvedParams.value.pageSize,
            organisation_id: resolvedParams.value.organisationId ?? undefined,
            actor_user_id: resolvedParams.value.actorUserId ?? undefined,
            action: resolvedParams.value.action ?? undefined,
          },
        },
      })
      if (error) throw error
      if (!data) throw new Error('Empty audit events response')
      return data as AuditEventListResponse
    },
    retry: false,
    staleTime: 30_000,
  })
}

/**
 * Reactive flag: is the current user a platform administrator?
 *
 * Derives from the `platform_roles` array of `/me` (Scope §6.2), which is
 * empty for non-admins. The backend remains the enforcement point; this only
 * gates nav visibility and the router guard. Reuses the shared `useMeQuery`
 * so the whole app reads one `['me']` cache entry.
 */
export function usePlatformAdminStatus() {
  const { data, isPending, isError } = useMeQuery()
  return {
    platformRoles: computed<readonly string[]>(() => data.value?.platform_roles ?? []),
    isPlatformAdmin: computed<boolean>(() => (data.value?.platform_roles ?? []).length > 0),
    mePending: isPending,
    meError: isError,
  }
}

/**
 * One organisation's AI policy as the admin centre sees it (v0.7 Scope §6.5,
 * v0.8 Scope §6.2).
 *
 * Platform-gated server-side (never org-scoped, no `X-Org-Id`); the caller is
 * a platform administrator administering an organisation they may not belong
 * to, so the key lives under the `platform` root like every platform query.
 */
export function usePlatformAISettingsQuery(organisationId: MaybeRefOrGetter<string>) {
  const resolvedId = computed(() => toValue(organisationId))
  return useQuery({
    queryKey: computed(() =>
      resolvedId.value === ''
        ? (['platform', 'organisations', null] as const)
        : platformQueryKeys.aiSettings(resolvedId.value),
    ),
    queryFn: async () => {
      const { data, error } = await client.GET(
        '/api/v1/platform/organisations/{organisation_id}/ai-settings',
        { params: { path: { organisation_id: resolvedId.value } } },
      )
      if (error) throw error
      if (!data) throw new Error('Empty platform AI settings response')
      return data as PlatformOrganisationAISettingsResponse
    },
    enabled: computed(() => resolvedId.value !== ''),
    retry: false,
    staleTime: 30_000,
  })
}

/**
 * Replace one organisation's AI policy (v0.7 Scope §6.5, v0.8 Scope §6.2).
 *
 * The payload carries the optimistic-concurrency `version` from the GET; a
 * stale update is rejected server-side with a conflict. Success invalidates
 * the settings key so the form and any other consumer re-read the new
 * version.
 */
export function useUpdatePlatformAISettingsMutation(options?: {
  onSuccess?: (settings: PlatformOrganisationAISettingsResponse) => void
}) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({
      organisationId,
      payload,
    }: {
      organisationId: string
      payload: PlatformOrganisationAISettingsUpdate
    }) => {
      const { data, error } = await client.PUT(
        '/api/v1/platform/organisations/{organisation_id}/ai-settings',
        {
          params: { path: { organisation_id: organisationId } },
          body: payload,
        },
      )
      if (error) throw error
      if (!data) throw new Error('Empty update platform AI settings response')
      return data
    },
    onSuccess: (settings) => {
      void queryClient.invalidateQueries({
        queryKey: platformQueryKeys.aiSettings(settings.organisation_id),
      })
      options?.onSuccess?.(settings)
    },
  })
}
