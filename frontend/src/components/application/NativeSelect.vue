<script setup lang="ts">
import type { HTMLAttributes } from 'vue'
import { cn } from '@/lib/utils'

/**
 * Native select styled with the design tokens (blueprint §16).
 *
 * shadcn-vue does not ship a vendored `select` primitive in this template yet
 * (only button, input, table, form, …), and the blueprint's rule is to reuse
 * tested primitives rather than build custom focus-management logic. A native
 * `<select>` is the accessible default: keyboard, screen-reader and form
 * semantics come from the browser. Styling mirrors the `Input` component so
 * selects and inputs sit side by side in forms and tables.
 */
const props = defineProps<{
  modelValue?: string
  class?: HTMLAttributes['class']
  disabled?: boolean
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: string): void
}>()

function onChange(event: Event): void {
  const target = event.target as HTMLSelectElement
  emit('update:modelValue', target.value)
}
</script>

<template>
  <select
    :value="modelValue"
    :disabled="disabled"
    data-slot="native-select"
    :class="
      cn(
        'dark:bg-input/30 border-input focus-visible:border-ring focus-visible:ring-ring/50 h-9 rounded-md border bg-transparent px-2.5 py-1 text-base shadow-xs transition-[color,box-shadow] focus-visible:ring-3 md:text-sm w-full min-w-0 outline-none placeholder:text-muted-foreground disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50',
        props.class,
      )
    "
    @change="onChange"
  >
    <slot />
  </select>
</template>
