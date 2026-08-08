import { createRouter, createWebHistory } from 'vue-router'
import type { RouteLocationNormalized } from 'vue-router'
import AppShellLayout from '@/layouts/AppShellLayout.vue'
import { meQueryOptions } from '@/queries/me'
import { queryClient } from '@/queries/queryClient'
import { useSessionStore } from '@/stores/session'

/**
 * Authentication guard (v0.3 Scope §6.3, acceptance §5.4).
 *
 * - A route whose matched records carry `meta.requiresAuth` is reachable only
 *   with a session; otherwise the user is sent to `/login`.
 * - An authenticated user visiting `/login` is sent back to the shell.
 * - `/auth/callback` carries no `requiresAuth` meta and is always public.
 *
 * The guard awaits boot-restore (`waitForBootRestore`) so the first
 * navigation of the app is decided against the WorkOS session restored in
 * `main.ts` bootstrap, not against the pre-restore empty state. Once
 * boot-restore completes the promise resolves immediately, so later
 * navigations are synchronous.
 */
export async function requiresAuth(
  to: RouteLocationNormalized,
): Promise<boolean | { name: string; query?: Record<string, string> }> {
  const session = useSessionStore()
  await session.waitForBootRestore()

  const needsAuth = to.matched.some((record) => record.meta.requiresAuth === true)

  if (needsAuth && !session.isAuthenticated) {
    return { name: 'login', query: { returnTo: to.fullPath } }
  }
  if (to.name === 'login' && session.isAuthenticated) {
    return { name: 'home' }
  }
  return true
}

/**
 * Platform Admin Centre guard (Scope §6.9, acceptance §5.10).
 *
 * A route whose matched records carry `meta.requiresPlatformAdmin` is
 * reachable only by a user whose `/me` payload reports at least one
 * `platform_roles` entry (Scope §6.2 — the platform authorisation plane).
 *
 * The `/me` payload is fetched through the shared query client using
 * `meQueryOptions`, the same query definition `useMeQuery` consumes, so this
 * guard and the `useMeQuery` components read one `['me']` cache entry and the
 * guard never performs its own HTTP call once the payload is cached. Failure
 * modes are conservative:
 *
 * - unauthenticated → `/login` (with returnTo, like `requiresAuth`);
 * - authenticated without platform roles → `/home` (the centre is invisible);
 * - `/me` request failure → `/home` (never a hard block on the shell).
 *
 * The backend remains the enforcement point (blueprint §9): every platform
 * route is gated server-side by `require_platform_permission("platform.admin")`,
 * so this guard only shapes navigation, it grants nothing.
 */
export async function requiresPlatformAdmin(
  to: RouteLocationNormalized,
): Promise<boolean | { name: string; query?: Record<string, string> }> {
  if (!to.matched.some((record) => record.meta.requiresPlatformAdmin === true)) {
    return true
  }

  const session = useSessionStore()
  await session.waitForBootRestore()
  if (!session.isAuthenticated) {
    return { name: 'login', query: { returnTo: to.fullPath } }
  }

  try {
    const me = await queryClient.fetchQuery(meQueryOptions)
    const isPlatformAdmin = (me.platform_roles ?? []).length > 0
    return isPlatformAdmin ? true : { name: 'home' }
  } catch {
    return { name: 'home' }
  }
}

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: '/',
      component: AppShellLayout,
      meta: { requiresAuth: true },
      children: [
        {
          path: '',
          name: 'home',
          component: () => import('@/views/HomeView.vue'),
        },
        {
          path: 'about',
          name: 'about',
          component: () => import('@/views/AboutView.vue'),
        },
        {
          path: 'records',
          name: 'records',
          component: () => import('@/views/RecordsListView.vue'),
        },
        {
          path: 'records/new',
          name: 'record-create',
          component: () => import('@/views/RecordCreateView.vue'),
        },
        {
          path: 'records/:recordId/edit',
          name: 'record-edit',
          component: () => import('@/views/RecordEditView.vue'),
          props: true,
        },
        {
          path: 'files',
          name: 'files',
          component: () => import('@/views/FilesListView.vue'),
        },
        // Platform Admin Centre (Scope §6.9): gated by the platform
        // authorisation plane (Scope §6.2). Every route carries
        // `requiresPlatformAdmin`; the backend still enforces each one
        // server-side.
        {
          path: 'platform',
          name: 'platform',
          component: () => import('@/views/PlatformDashboardView.vue'),
          meta: { requiresPlatformAdmin: true },
        },
        {
          path: 'platform/organisations',
          name: 'platform-organisations',
          component: () => import('@/views/PlatformOrganisationsView.vue'),
          meta: { requiresPlatformAdmin: true },
        },
        {
          path: 'platform/organisations/new',
          name: 'platform-organisation-new',
          component: () => import('@/views/PlatformOrganisationFormView.vue'),
          meta: { requiresPlatformAdmin: true },
        },
        {
          path: 'platform/organisations/:organisationId',
          name: 'platform-organisation-detail',
          component: () => import('@/views/PlatformOrganisationDetailView.vue'),
          props: true,
          meta: { requiresPlatformAdmin: true },
        },
        {
          path: 'platform/organisations/:organisationId/invite',
          name: 'platform-invite-user',
          component: () => import('@/views/PlatformInviteUserView.vue'),
          props: true,
          meta: { requiresPlatformAdmin: true },
        },
        {
          path: 'platform/feature-flags',
          name: 'platform-feature-flags',
          component: () => import('@/views/PlatformFeatureFlagsView.vue'),
          meta: { requiresPlatformAdmin: true },
        },
        {
          path: 'platform/admins',
          name: 'platform-admins',
          component: () => import('@/views/PlatformAdminsView.vue'),
          meta: { requiresPlatformAdmin: true },
        },
        {
          path: 'platform/audit',
          name: 'platform-audit',
          component: () => import('@/views/PlatformAuditView.vue'),
          meta: { requiresPlatformAdmin: true },
        },
      ],
    },
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/LoginView.vue'),
    },
    {
      path: '/auth/callback',
      name: 'auth-callback',
      component: () => import('@/views/AuthCallbackView.vue'),
    },
  ],
})

router.beforeEach(requiresAuth)
router.beforeEach(requiresPlatformAdmin)

export default router
