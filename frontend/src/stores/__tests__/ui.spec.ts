import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'

import { useUiStore } from '@/stores/ui'

describe('ui store', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
  })

  it('starts with the sidebar expanded', () => {
    const ui = useUiStore()
    expect(ui.sidebarCollapsed).toBe(false)
    expect(ui.sidebarExpanded).toBe(true)
  })

  it('toggles the collapsed state', () => {
    const ui = useUiStore()
    ui.toggleSidebar()
    expect(ui.sidebarCollapsed).toBe(true)
    ui.toggleSidebar()
    expect(ui.sidebarCollapsed).toBe(false)
  })

  it('persists the collapsed state to localStorage', () => {
    const ui = useUiStore()
    ui.setSidebarCollapsed(true)
    expect(localStorage.getItem('app-template:sidebar-collapsed')).toBe('true')
  })

  it('hydrates the collapsed state from localStorage', () => {
    localStorage.setItem('app-template:sidebar-collapsed', 'true')
    const ui = useUiStore()
    expect(ui.sidebarCollapsed).toBe(true)
  })
})
