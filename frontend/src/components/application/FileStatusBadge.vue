<script setup lang="ts">
import { computed } from 'vue'

import { cn } from '@/lib/utils'
import { fileStatusMeta } from '@/lib/files'

/**
 * Reusable file-status badge (Scope §6.6, blueprint §16).
 *
 * Maps the backend's `FileStatus` values (blueprint §17 lifecycle) to a
 * semantic-token tone + human label via `FILE_STATUS_META`
 * (`src/lib/files.ts`), so the table and any future file views render one
 * consistent status affordance. `data-testid` carries the raw status value
 * so tests assert on state, not presentation.
 */
const props = defineProps<{ status: string }>()

const meta = computed(() => fileStatusMeta(props.status))
</script>

<template>
  <span
    :data-testid="`file-status-${props.status}`"
    :class="
      cn('inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium', meta.className)
    "
  >
    {{ meta.label }}
  </span>
</template>
