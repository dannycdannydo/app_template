<script setup lang="ts">
import { toTypedSchema } from '@vee-validate/zod'
import { LoaderCircleIcon } from '@lucide/vue'
import { useRouter } from 'vue-router'
import { useForm } from 'vee-validate'
import { z } from 'zod'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { FormControl, FormField, FormItem, FormLabel, FormMessage } from '@/components/ui/form'
import { Input } from '@/components/ui/input'
import { showApiErrorToast, showSuccessToast } from '@/lib/toast'
import { useCreatePlatformOrganisationMutation } from '@/queries/platform'

/**
 * Platform organisation create form (Scope §6.9).
 *
 * Follows the standard form pattern (RecordForm, v0.3 Scope §6.6): a zod
 * schema mirrors the backend's name validation, field errors surface inline,
 * API failures arrive as toasts through the error envelope, and success
 * navigates to the new organisation's detail. Creation also creates the
 * WorkOS organisation and mapping on the backend (Scope §6.3).
 */
const organisationFormSchema = toTypedSchema(
  z.object({
    name: z
      .string()
      .trim()
      .min(1, 'Name is required.')
      .max(255, 'Name must be 255 characters or fewer.'),
  }),
)

interface OrganisationFormValues {
  name: string
}

const router = useRouter()

const { handleSubmit, isSubmitting } = useForm<OrganisationFormValues>({
  validationSchema: organisationFormSchema,
  initialValues: { name: '' },
})

const createMutation = useCreatePlatformOrganisationMutation({
  onSuccess: (organisation) => {
    showSuccessToast('Organisation created')
    void router.push({
      name: 'platform-organisation-detail',
      params: { organisationId: organisation.id },
    })
  },
})

const onSubmit = handleSubmit(async (values) => {
  try {
    await createMutation.mutateAsync(values)
  } catch (error) {
    showApiErrorToast(error, { title: 'Could not create organisation' })
  }
})
</script>

<template>
  <div class="space-y-6">
    <div>
      <h1 class="text-2xl font-semibold">New organisation</h1>
      <p class="text-muted-foreground mt-1 text-sm">
        Creating an organisation also creates its WorkOS organisation and mapping.
      </p>
    </div>

    <Card>
      <CardHeader>
        <CardTitle>Organisation details</CardTitle>
        <CardDescription>The name matches the backend's organisation schema.</CardDescription>
      </CardHeader>
      <CardContent>
        <form
          data-testid="platform-organisation-form"
          novalidate
          class="grid max-w-md gap-4"
          @submit="onSubmit"
        >
          <FormField v-slot="{ componentField }" name="name">
            <FormItem>
              <FormLabel>Name</FormLabel>
              <FormControl>
                <Input
                  type="text"
                  placeholder="Organisation name"
                  :disabled="isSubmitting"
                  v-bind="componentField"
                />
              </FormControl>
              <FormMessage />
            </FormItem>
          </FormField>

          <div class="flex justify-end gap-2">
            <Button
              type="button"
              variant="outline"
              :disabled="isSubmitting"
              @click="router.push({ name: 'platform-organisations' })"
            >
              Cancel
            </Button>
            <Button
              type="submit"
              :disabled="isSubmitting"
              data-testid="platform-organisation-form-submit"
            >
              <LoaderCircleIcon v-if="isSubmitting" class="animate-spin" />
              Create organisation
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  </div>
</template>
