import './assets/main.css'

import { VueQueryPlugin } from '@tanstack/vue-query'
import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import { getSession } from './features/auth/workos'
import router from './router'
import { useSessionStore } from './stores/session'

async function bootstrap(): Promise<void> {
  const app = createApp(App)
  const pinia = createPinia()

  app.use(pinia)
  app.use(router)
  app.use(VueQueryPlugin)

  // Restore the WorkOS session (if any) before the app mounts so the API
  // client can attach the Bearer token from the first request on.
  //
  // Ordering is load-bearing: the router must be installed (and its history
  // snapshot taken) before boot-restore runs. createWebHistory captures the
  // current URL at module-import time (vue-router.js), and the SDK strips the
  // callback `code` via a raw window.history.replaceState that does not fire
  // vue-router's popstate listener. Because the snapshot predates that, the
  // callback URL (and with it route.query.code in AuthCallbackView) survives;
  // boot-restoring before the router is installed would silently drop the
  // code and the callback would redirect to /login?error=invalid_callback.
  const session = useSessionStore(pinia)
  const token = await getSession()
  if (token) session.setSession(token)
  // The router guard waits for this before deciding between /login and the
  // shell, so an authenticated reload lands inside the shell (Scope §6.3).
  session.markBootRestored()

  app.mount('#app')
}

void bootstrap()
