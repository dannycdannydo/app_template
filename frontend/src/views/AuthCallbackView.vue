<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { completeLogin } from '@/features/auth/workos'
import { useSessionStore } from '@/stores/session'

const route = useRoute()
const router = useRouter()
const session = useSessionStore()

const isCompleting = ref(true)

function fail(error: string): void {
  void router.replace({ path: '/login', query: { error } })
}

onMounted(async () => {
  if (typeof route.query.error === 'string') {
    // WorkOS reported a denied/failed flow (e.g. ?error=access_denied).
    fail(route.query.error)
    return
  }
  if (typeof route.query.code !== 'string') {
    fail('invalid_callback')
    return
  }
  try {
    const completedLogin = await completeLogin()
    session.setSession(completedLogin.accessToken)
    await router.replace(completedLogin.returnTo ?? '/')
  } catch {
    // Nothing is stored on failure; the user returns to the login page.
    fail('login_failed')
  } finally {
    isCompleting.value = false
  }
})
</script>

<template>
  <div class="mx-auto flex max-w-md flex-col justify-center py-16">
    <Card>
      <CardHeader>
        <CardTitle>Signing you in…</CardTitle>
        <CardDescription>Finishing your sign-in with WorkOS.</CardDescription>
      </CardHeader>
      <CardContent>
        <p v-if="isCompleting" class="text-sm text-muted-foreground">Completing sign-in…</p>
      </CardContent>
    </Card>
  </div>
</template>
