<script setup lang="ts">
import { toTypedSchema } from '@vee-validate/zod'
import { LoaderCircleIcon } from '@lucide/vue'
import { toValue, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useForm } from 'vee-validate'
import { z } from 'zod'

import { Button } from '@/components/ui/button'
import { FormControl, FormField, FormItem, FormLabel, FormMessage } from '@/components/ui/form'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { showApiErrorToast, showSuccessToast } from '@/lib/toast'
import { useCreateRecordMutation, useUpdateRecordMutation } from '@/queries/records'
import type { components } from '@/api/generated/openapi'

type RecordDetail = components['schemas']['RecordDetail']

/**
 * Standard create/edit form for records (Scope §6.6, blueprint §16, §13).
 *
 * The reusable form pattern every edit screen follows: a zod schema mirrors
 * the backend's validation (title 1..255, body <= 100,000, matching the
 * `RecordCreate` schema), field errors surface inline through the vendored
 * form primitives, API failures arrive as toasts via the error envelope, and
 * success navigates back to the list.
 *
 * The form is data-driven: create mode posts a `RecordCreate`, edit mode
 * patches `recordId` with the `initialValues` loaded by the parent (e.g. via
 * `useRecordQuery`). Navigation to the list route happens when the parent
 * passes `listRouteName`; without it the form only emits `created`/`updated`.
 */
export interface RecordFormValues {
  title: string
  body: string
}

const recordFormSchema = toTypedSchema(
  z.object({
    title: z
      .string()
      .trim()
      .min(1, 'Title is required.')
      .max(255, 'Title must be 255 characters or fewer.'),
    body: z.string().max(100_000, 'Body must be 100,000 characters or fewer.'),
  }),
)

const props = withDefaults(
  defineProps<{
    mode: 'create' | 'edit'
    recordId?: string
    initialValues?: RecordFormValues
    listRouteName?: string
  }>(),
  {
    recordId: undefined,
    initialValues: undefined,
    listRouteName: undefined,
  },
)

const emit = defineEmits<{
  (e: 'created', record: RecordDetail): void
  (e: 'updated', record: RecordDetail): void
  (e: 'cancel'): void
}>()

const router = useRouter()

const { handleSubmit, isSubmitting, resetForm } = useForm<RecordFormValues>({
  validationSchema: recordFormSchema,
  initialValues: toValue(props.initialValues) ?? { title: '', body: '' },
})

// Edit mode loads values asynchronously (parent's detail query); adopt them
// once they arrive instead of showing stale or empty fields.
watch(
  () => props.initialValues,
  (values) => {
    if (values) resetForm({ values })
  },
)

function navigateToList(): void {
  if (props.listRouteName) {
    void router.push({ name: props.listRouteName })
  }
}

function onCancel(): void {
  emit('cancel')
  navigateToList()
}

const createMutation = useCreateRecordMutation({
  onSuccess: (record) => {
    showSuccessToast('Record created')
    emit('created', record)
    navigateToList()
  },
})

const updateMutation = useUpdateRecordMutation({
  onSuccess: (record) => {
    showSuccessToast('Record updated')
    emit('updated', record)
    navigateToList()
  },
})

const onSubmit = handleSubmit(async (values) => {
  try {
    if (props.mode === 'create') {
      await createMutation.mutateAsync(values)
    } else {
      if (!props.recordId) {
        throw new Error('Cannot edit a record without an id')
      }
      await updateMutation.mutateAsync({ recordId: props.recordId, payload: values })
    }
  } catch (error) {
    showApiErrorToast(error, {
      title: props.mode === 'create' ? 'Could not create record' : 'Could not update record',
    })
  }
})
</script>

<template>
  <form data-testid="record-form" novalidate class="grid gap-4" @submit="onSubmit">
    <FormField v-slot="{ componentField }" name="title">
      <FormItem>
        <FormLabel>Title</FormLabel>
        <FormControl>
          <Input
            type="text"
            placeholder="Record title"
            :disabled="isSubmitting"
            v-bind="componentField"
          />
        </FormControl>
        <FormMessage />
      </FormItem>
    </FormField>

    <FormField v-slot="{ componentField }" name="body">
      <FormItem>
        <FormLabel>Body</FormLabel>
        <FormControl>
          <Textarea
            placeholder="Notes (optional)"
            :disabled="isSubmitting"
            v-bind="componentField"
          />
        </FormControl>
        <FormMessage />
      </FormItem>
    </FormField>

    <div class="flex justify-end gap-2">
      <Button type="button" variant="outline" :disabled="isSubmitting" @click="onCancel">
        Cancel
      </Button>
      <Button type="submit" :disabled="isSubmitting" data-testid="record-form-submit">
        <LoaderCircleIcon v-if="isSubmitting" class="animate-spin" />
        {{ mode === 'create' ? 'Create record' : 'Save changes' }}
      </Button>
    </div>
  </form>
</template>
