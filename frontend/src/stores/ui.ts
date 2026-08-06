import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

/**
 * Client-side UI state (blueprint §14).
 *
 * Pinia holds client state only: sidebar state, UI preferences, temporary
 * wizard state. Server state belongs to TanStack Vue Query.
 *
 * The sidebar collapsed state is persisted to localStorage (v0.3 Scope §6.3,
 * acceptance §5.5: the collapsed state survives reloads) and hydrated on store
 * creation. Storage access is guarded so the store stays testable outside a
 * full browser environment.
 */
const SIDEBAR_STORAGE_KEY = 'app-template:sidebar-collapsed'

function readSidebarCollapsed(): boolean {
  if (typeof localStorage === 'undefined') return false
  return localStorage.getItem(SIDEBAR_STORAGE_KEY) === 'true'
}

export const useUiStore = defineStore('ui', () => {
  const sidebarCollapsed = ref(readSidebarCollapsed())

  const sidebarExpanded = computed(() => !sidebarCollapsed.value)

  function persistSidebar(): void {
    if (typeof localStorage === 'undefined') return
    localStorage.setItem(SIDEBAR_STORAGE_KEY, String(sidebarCollapsed.value))
  }

  function setSidebarCollapsed(collapsed: boolean) {
    sidebarCollapsed.value = collapsed
    persistSidebar()
  }

  function toggleSidebar() {
    sidebarCollapsed.value = !sidebarCollapsed.value
    persistSidebar()
  }

  return { sidebarCollapsed, sidebarExpanded, setSidebarCollapsed, toggleSidebar }
})
