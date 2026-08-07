<script setup lang="ts">
import { HomeIcon, InfoIcon, ListIcon, ShieldIcon, type LucideIcon } from '@lucide/vue'
import { computed } from 'vue'
import { RouterLink } from 'vue-router'

import { usePlatformAdminStatus } from '@/queries/platform'

/**
 * Shell navigation (Scope §6.3). Rendered in both the desktop sidebar and the
 * mobile sheet. When `collapsed` (desktop only), labels hide and only the
 * icons remain; `router-link-active` provides the active state.
 *
 * The Platform Admin Centre entry (Scope §6.9, acceptance §5.10) is shown
 * only when `/me` reports `platform_roles` (Scope §6.2) — the backend stays
 * the enforcement point, this only shapes what non-admins see. While `/me`
 * is still loading the entry stays hidden so it never flashes for a
 * non-admin.
 */
const props = withDefaults(
  defineProps<{
    collapsed?: boolean
    onNavigate?: () => void
  }>(),
  {
    collapsed: false,
    onNavigate: undefined,
  },
)

interface NavItem {
  to: string
  label: string
  icon: LucideIcon
}

const NAV_ITEMS: NavItem[] = [
  { to: '/', label: 'Home', icon: HomeIcon },
  { to: '/records', label: 'Records', icon: ListIcon },
  { to: '/about', label: 'About', icon: InfoIcon },
]

const { isPlatformAdmin, mePending } = usePlatformAdminStatus()

const visibleItems = computed<NavItem[]>(() => {
  const items = [...NAV_ITEMS]
  if (!mePending.value && isPlatformAdmin.value) {
    items.push({ to: '/platform', label: 'Platform Admin', icon: ShieldIcon })
  }
  return items
})

function navigate(): void {
  props.onNavigate?.()
}
</script>

<template>
  <nav class="flex flex-col gap-1" aria-label="Main">
    <RouterLink
      v-for="item in visibleItems"
      :key="item.to"
      :to="item.to"
      :title="collapsed ? item.label : undefined"
      class="text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:ring-ring/50 flex h-9 shrink-0 items-center gap-2 rounded-md px-2 text-sm font-medium transition-colors outline-none focus-visible:ring-3 [&.router-link-exact-active]:bg-accent [&.router-link-exact-active]:text-accent-foreground"
      @click="navigate"
    >
      <component :is="item.icon" class="size-4 shrink-0" />
      <span v-if="!collapsed">{{ item.label }}</span>
      <span v-else class="sr-only">{{ item.label }}</span>
    </RouterLink>
  </nav>
</template>
