import { createRouter, createWebHistory } from 'vue-router'
import type { RouteLocationNormalized } from 'vue-router'
import AppShellLayout from '@/layouts/AppShellLayout.vue'
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
): Promise<boolean | { name: string }> {
  const session = useSessionStore()
  await session.waitForBootRestore()

  const needsAuth = to.matched.some((record) => record.meta.requiresAuth === true)

  if (needsAuth && !session.isAuthenticated) {
    return { name: 'login' }
  }
  if (to.name === 'login' && session.isAuthenticated) {
    return { name: 'home' }
  }
  return true
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

export default router
