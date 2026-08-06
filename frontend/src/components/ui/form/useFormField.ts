import { FieldContextKey } from 'vee-validate'
import { computed, inject } from 'vue'

import { FORM_ITEM_INJECTION_KEY } from './injectionKeys'

/**
 * Shared field context for the vendored shadcn-vue form primitives (v0.3
 * Scope §6.6, blueprint §16). Must be used inside a `<FormField>` (vee-validate
 * `Field`): it reads vee-validate's injected field context and the parent
 * `FormItem`'s generated id, and derives the `for`/`aria-describedby`/`id`
 * wiring the field label, control, description and message share. This is the
 * single place that turns vee-validate state into accessible form markup.
 */
export function useFormField() {
  const fieldContext = inject(FieldContextKey)
  const fieldItemContext = inject(FORM_ITEM_INJECTION_KEY)

  if (!fieldContext) throw new Error('useFormField should be used within <FormField>')

  const { name, errorMessage: error, meta } = fieldContext
  const id = fieldItemContext

  const fieldState = {
    valid: computed(() => meta.valid),
    isDirty: computed(() => meta.dirty),
    isTouched: computed(() => meta.touched),
    error,
  }

  return {
    id,
    name,
    formItemId: `${id}-form-item`,
    formDescriptionId: `${id}-form-item-description`,
    formMessageId: `${id}-form-item-message`,
    ...fieldState,
  }
}
