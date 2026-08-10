<script setup lang="ts">
import { BellIcon, CheckIcon, InboxIcon } from '@lucide/vue'
import { computed, ref } from 'vue'

import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { formatDateTime } from '@/lib/format'
import { showApiErrorToast } from '@/lib/toast'
import {
  useMarkNotificationReadMutation,
  useMarkAllNotificationsReadMutation,
  useNotificationsQuery,
  useUnreadNotificationsCountQuery,
} from '@/queries/notifications'

/**
 * Notification bell in the shell header (Scope §6.5, blueprint §16, §14).
 *
 * The bell shows the unread badge fed by the org-scoped unread-count query
 * (polled so background deliveries — file completions, test notifications —
 * surface without a reload) and a dropdown of the caller's most recent
 * notifications with an inline mark-read action. The dropdown reads through
 * the same query composables as the notifications view; components never
 * touch the HTTP client directly.
 */
const { data: unreadData } = useUnreadNotificationsCountQuery()
const { data: listData, isPending } = useNotificationsQuery({ page: 1, pageSize: 100 })

const markingReadIds = ref<Set<string>>(new Set())

const { mutate: markRead } = useMarkNotificationReadMutation({
  onError: (error) => {
    showApiErrorToast(error, { title: 'Could not mark notification as read' })
  },
  onSettled: (notificationId) => {
    const nextMarkingReadIds = new Set(markingReadIds.value)
    nextMarkingReadIds.delete(notificationId)
    markingReadIds.value = nextMarkingReadIds
  },
})

const { mutate: markAllRead, isPending: markAllReadPending } = useMarkAllNotificationsReadMutation({
  onError: (error) => {
    showApiErrorToast(error, { title: 'Could not mark notifications as read' })
  },
})

const open = ref(false)

const unreadCount = computed(() => unreadData.value?.unread_count ?? 0)

const recentNotifications = computed(() => listData.value?.items ?? [])

const hasUnread = computed(() => unreadCount.value > 0)

function handleMarkRead(notificationId: string): void {
  if (markingReadIds.value.has(notificationId)) return
  markingReadIds.value = new Set(markingReadIds.value).add(notificationId)
  markRead(notificationId)
}

function isMarkingRead(notificationId: string): boolean {
  return markingReadIds.value.has(notificationId)
}

function handleMarkAllRead(): void {
  if (markAllReadPending.value) return
  markAllRead()
}
</script>

<template>
  <DropdownMenu v-model:open="open">
    <DropdownMenuTrigger as-child>
      <Button
        variant="ghost"
        size="icon"
        class="relative"
        aria-label="Notifications"
        data-testid="notification-bell-trigger"
      >
        <BellIcon />
        <span
          v-if="hasUnread"
          class="bg-primary text-primary-foreground absolute -top-0.5 -right-0.5 flex h-4 min-w-4 items-center justify-center rounded-full px-1 text-[10px] font-semibold tabular-nums"
          data-testid="notification-bell-badge"
        >
          {{ unreadCount > 99 ? '99+' : unreadCount }}
        </span>
      </Button>
    </DropdownMenuTrigger>

    <DropdownMenuContent align="end" class="w-80" data-testid="notification-bell-content">
      <DropdownMenuLabel class="font-normal">
        <div class="flex items-center justify-between">
          <span class="text-foreground text-sm font-medium">Notifications</span>
          <Button
            v-if="hasUnread"
            variant="ghost"
            size="sm"
            class="text-muted-foreground hover:text-foreground h-auto px-1 text-xs"
            :disabled="markAllReadPending"
            data-testid="notification-bell-mark-all-read"
            @click="handleMarkAllRead"
          >
            Mark all as read
          </Button>
        </div>
      </DropdownMenuLabel>
      <DropdownMenuSeparator />

      <div v-if="isPending" class="text-muted-foreground px-2 py-6 text-center text-sm">
        Loading…
      </div>
      <div
        v-else-if="recentNotifications.length === 0"
        class="text-muted-foreground flex flex-col items-center gap-2 px-2 py-6 text-center text-sm"
        data-testid="notification-bell-empty"
      >
        <InboxIcon class="size-6" aria-hidden="true" />
        <span>No notifications yet.</span>
      </div>
      <div v-else class="max-h-96 overflow-y-auto" data-testid="notification-bell-list">
        <div
          v-for="notification in recentNotifications"
          :key="notification.id"
          class="hover:bg-accent/50 flex flex-col items-start gap-1 rounded-md px-2 py-1.5"
          :data-unread="notification.read_at === null ? 'true' : 'false'"
          :data-testid="`notification-bell-item-${notification.id}`"
        >
          <div class="flex w-full items-start justify-between gap-2">
            <span
              class="text-foreground text-sm"
              :class="notification.read_at === null && 'font-medium'"
              :data-testid="`notification-bell-title-${notification.id}`"
            >
              {{ notification.title }}
            </span>
            <Button
              v-if="notification.read_at === null"
              variant="ghost"
              size="icon-sm"
              class="text-muted-foreground shrink-0"
              :aria-label="`Mark ${notification.title} as read`"
              :disabled="isMarkingRead(notification.id) || markAllReadPending"
              :data-testid="`notification-bell-mark-read-${notification.id}`"
              @click="handleMarkRead(notification.id)"
            >
              <CheckIcon />
            </Button>
          </div>
          <p class="text-muted-foreground line-clamp-2 text-xs">{{ notification.body }}</p>
          <span class="text-muted-foreground/70 text-xs">
            {{ formatDateTime(notification.created_at) }}
          </span>
        </div>
      </div>
    </DropdownMenuContent>
  </DropdownMenu>
</template>
