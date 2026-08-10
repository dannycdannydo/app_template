<script setup lang="ts">
import { ref } from 'vue'
import { useRoute } from 'vue-router'

import { Button } from '@/components/ui/button'
import { startLogin } from '@/features/auth/workos'

const route = useRoute()

const isStarting = ref(false)
const errorMessage = ref<string | null>(null)

const ERROR_MESSAGES: Record<string, string> = {
  access_denied: 'Sign-in was not completed. Please try again.',
  invalid_callback: 'The sign-in link is invalid or incomplete. Please sign in again.',
  login_failed: 'Sign-in failed. Please try again.',
  session_invalid: 'Your session expired or could not be validated. Please sign in again.',
  signed_out: 'You have signed out.',
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
    const returnTo = typeof route.query.returnTo === 'string' ? route.query.returnTo : undefined
    await startLogin({ returnTo })
  } catch (error) {
    errorMessage.value =
      error instanceof Error ? error.message : 'Could not start sign-in. Please try again.'
    isStarting.value = false
  }
}
</script>

<template>
  <div class="mx-auto flex max-w-md flex-col items-center justify-center gap-4 py-16">
    <p v-if="errorMessage" data-testid="login-error" class="text-sm text-destructive">
      {{ errorMessage }}
    </p>
    <Button data-testid="login-button" :disabled="isStarting" @click="handleStartLogin">
      {{ isStarting ? 'Redirecting…' : 'Login' }}
    </Button>
  </div>
</template>
