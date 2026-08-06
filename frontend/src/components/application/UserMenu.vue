<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ChevronsUpDownIcon, LogOutIcon } from '@lucide/vue'

import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { signOut } from '@/features/auth/workos'
import { useMeQuery } from '@/queries/me'
import { useSessionStore } from '@/stores/session'

/**
 * User menu in the shell header (Scope §6.3, acceptance §5.5).
 *
 * Identity comes from `GET /api/v1/me` through the query layer (blueprint
 * §14: components never call the HTTP client directly). Logout signs out of
 * WorkOS through the adapter, clears the session store and returns to
 * `/login`; the router guard then treats every other route as public.
 */
const { data } = useMeQuery()
const session = useSessionStore()
const router = useRouter()

const isSigningOut = ref(false)

const user = computed(() => data.value?.user ?? null)

const initials = computed(() => {
  const name = user.value?.name?.trim()
  if (!name) return '?'
  const parts = name.split(/\s+/)
  const first = parts[0]?.[0] ?? ''
  const last = parts.length > 1 ? (parts[parts.length - 1]?.[0] ?? '') : ''
  return `${first}${last}`.toUpperCase()
})

async function handleSignOut(): Promise<void> {
  if (isSigningOut.value) return
  isSigningOut.value = true
  const logoutNavigationStarted = await signOut()
  session.clearSession()
  if (!logoutNavigationStarted) {
    void router.push('/login')
  }
}
</script>

<template>
  <DropdownMenu>
    <DropdownMenuTrigger as-child>
      <Button
        variant="ghost"
        class="hover:bg-muted/60 focus-visible:ring-2 h-9 gap-2 rounded-md px-2 text-sm"
        data-testid="user-menu-trigger"
      >
        <Avatar size="sm">
          <AvatarFallback data-testid="user-menu-fallback">{{ initials }}</AvatarFallback>
        </Avatar>
        <span class="max-w-36 truncate font-medium" data-testid="user-menu-name">
          {{ user?.name || '…' }}
        </span>
        <ChevronsUpDownIcon class="text-muted-foreground hidden size-3.5 sm:block" />
      </Button>
    </DropdownMenuTrigger>

    <DropdownMenuContent align="end" class="w-56" data-testid="user-menu-content">
      <DropdownMenuLabel class="font-normal">
        <div class="flex flex-col gap-0.5">
          <span class="text-foreground text-sm font-medium" data-testid="user-menu-display-name">
            {{ user?.name }}
          </span>
          <span class="text-xs" data-testid="user-menu-email">{{ user?.email }}</span>
        </div>
      </DropdownMenuLabel>
      <DropdownMenuSeparator />
      <DropdownMenuItem
        variant="destructive"
        :disabled="isSigningOut"
        @select="handleSignOut"
        data-testid="user-menu-sign-out"
      >
        <LogOutIcon />
        {{ isSigningOut ? 'Signing out…' : 'Sign out' }}
      </DropdownMenuItem>
    </DropdownMenuContent>
  </DropdownMenu>
</template>
