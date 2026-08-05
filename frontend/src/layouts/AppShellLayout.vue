<script setup lang="ts">
import { ref } from 'vue'
import { MenuIcon, PanelLeftIcon } from '@lucide/vue'
import { RouterView } from 'vue-router'

import OrganisationSelector from '@/components/application/OrganisationSelector.vue'
import SidebarNav from '@/components/application/SidebarNav.vue'
import UserMenu from '@/components/application/UserMenu.vue'
import { Button } from '@/components/ui/button'
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetTrigger } from '@/components/ui/sheet'
import { useUiStore } from '@/stores/ui'

/**
 * Protected application shell (Scope §6.3, blueprint §14, §16).
 *
 * Desktop: a collapsible sidebar (collapsed state lives in Pinia and persists
 * across reloads, acceptance §5.5) plus a header holding the organisation
 * selector and the user menu. Mobile: the same navigation moves into a sheet
 * opened from the header. The shell owns no server data; the selector and the
 * user menu pull identity from `useMeQuery` (blueprint §14 boundary).
 */
const ui = useUiStore()

const mobileNavOpen = ref(false)

function closeMobileNav(): void {
  mobileNavOpen.value = false
}
</script>

<template>
  <Sheet v-model:open="mobileNavOpen">
    <div class="flex min-h-screen">
      <!-- Desktop sidebar -->
      <aside
        data-testid="sidebar"
        class="bg-background sticky top-0 hidden h-screen shrink-0 flex-col gap-4 border-r p-3 transition-[width] duration-200 ease-in-out md:flex"
        :class="ui.sidebarCollapsed ? 'w-16' : 'w-64'"
      >
        <div
          class="flex h-10 shrink-0 items-center px-2"
          :class="ui.sidebarCollapsed && 'justify-center px-0'"
        >
          <span
            v-if="ui.sidebarCollapsed"
            class="bg-primary text-primary-foreground size-6 rounded-md"
          />
          <RouterLink v-else to="/" class="text-sm font-semibold tracking-tight">
            app-template
          </RouterLink>
        </div>
        <SidebarNav :collapsed="ui.sidebarCollapsed" />
      </aside>

      <!-- Content column -->
      <div class="flex min-w-0 flex-1 flex-col">
        <header
          class="bg-background/95 supports-backdrop-filter:backdrop-blur sticky top-0 z-40 flex h-14 items-center gap-2 border-b px-3 md:px-4"
        >
          <!-- Mobile: open the nav sheet -->
          <SheetTrigger as-child>
            <Button
              variant="ghost"
              size="icon"
              class="md:hidden"
              aria-label="Open navigation"
              data-testid="mobile-nav-trigger"
            >
              <MenuIcon />
            </Button>
          </SheetTrigger>

          <!-- Desktop: collapse the sidebar -->
          <Button
            variant="ghost"
            size="icon"
            class="hidden md:inline-flex"
            :aria-expanded="!ui.sidebarCollapsed"
            :aria-label="ui.sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'"
            data-testid="sidebar-toggle"
            @click="ui.toggleSidebar()"
          >
            <PanelLeftIcon />
          </Button>

          <div class="ml-auto flex items-center gap-2">
            <OrganisationSelector />
            <UserMenu />
          </div>
        </header>

        <main class="mx-auto w-full max-w-5xl flex-1 px-4 py-6 md:px-6">
          <RouterView />
        </main>
      </div>
    </div>

    <!-- Mobile navigation sheet -->
    <SheetContent side="left" class="w-72">
      <SheetHeader class="px-2 pt-2">
        <SheetTitle class="text-sm">app-template</SheetTitle>
      </SheetHeader>
      <div class="px-2">
        <SidebarNav @navigate="closeMobileNav" />
      </div>
    </SheetContent>
  </Sheet>
</template>
