<script setup lang="ts">
import { ref } from 'vue'
import { useRoute } from 'vue-router'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { startLogin } from '@/features/auth/workos'

const route = useRoute()

const isStarting = ref(false)
const errorMessage = ref<string | null>(null)

const ERROR_MESSAGES: Record<string, string> = {
  access_denied: 'Sign-in was not completed. Please try again.',
  invalid_callback: 'The sign-in link is invalid or incomplete. Please sign in again.',
  login_failed: 'Sign-in failed. Please try again.',
}

const errorParam = typeof route.query.error === 'string' ? route.query.error : null
if (errorParam) {
  errorMessage.value = ERROR_MESSAGES[errorParam] ?? 'Sign-in failed. Please try again.'
}

async function handleStartLogin(): Promise<void> {
  if (isStarting.value) return
  isStarting.value = true
  errorMessage.value = null
  try {
    await startLogin()
  } catch (error) {
    errorMessage.value =
      error instanceof Error ? error.message : 'Could not start sign-in. Please try again.'
    isStarting.value = false
  }
}
</script>

<template>
  <div class="mx-auto flex max-w-md flex-col justify-center py-16">
    <Card>
      <CardHeader>
        <CardTitle>Sign in</CardTitle>
        <CardDescription>
          Continue with WorkOS to access your account. Authentication happens on WorkOS's side, so
          no password is ever entered here or sent to this application.
        </CardDescription>
      </CardHeader>
      <CardContent class="space-y-4">
        <p v-if="errorMessage" data-testid="login-error" class="text-sm text-destructive">
          {{ errorMessage }}
        </p>
        <Button class="w-full" :disabled="isStarting" @click="handleStartLogin">
          {{ isStarting ? 'Redirecting to WorkOS…' : 'Continue with WorkOS' }}
        </Button>
      </CardContent>
    </Card>
  </div>
</template>
