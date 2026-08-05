import { ref } from 'vue'
import { defineStore } from 'pinia'

/**
 * Client-side UI state (blueprint §14).
 *
 * Pinia holds client state only: sidebar state, UI preferences, temporary
 * wizard state. Server state belongs to TanStack Vue Query.
 */
export const useUiStore = defineStore('ui', () => {
  const sidebarOpen = ref(false)

  function toggleSidebar() {
    sidebarOpen.value = !sidebarOpen.value
  }

  return { sidebarOpen, toggleSidebar }
})
