<script setup lang="ts">
import { toTypedSchema } from '@vee-validate/zod'
import { LoaderCircleIcon } from '@lucide/vue'
import { useRouter } from 'vue-router'
import { useForm } from 'vee-validate'
import { z } from 'zod'

import NativeSelect from '@/components/application/NativeSelect.vue'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { FormControl, FormField, FormItem, FormLabel, FormMessage } from '@/components/ui/form'
import { Input } from '@/components/ui/input'
import { ORGANISATION_ROLE_CODES } from '@/lib/permissions'
import { showApiErrorToast, showSuccessToast } from '@/lib/toast'
import { useCreateInvitationMutation } from '@/queries/platform'

/**
 * Platform invite form (Scope §6.5/§6.9, acceptance §5.6).
 *
 * Invites a user into the organisation through the WorkOS Invitation API:
 * email plus the intended organisation role. No membership is created here —
 * the invitee gains it at login-time linking when they accept (Scope §6.5) —
 * so success toasts and returns to the organisation detail where the pending
 * invitation is visible.
 */
const props = defineProps<{ organisationId: string }>()

const router = useRouter()

const inviteFormSchema = toTypedSchema(
  z.object({
    email: z.string().trim().min(1, 'Email is required.').email('Enter a valid email address.'),
    role_code: z.string().min(1, 'Choose a role.'),
  }),
)

interface InviteFormValues {
  email: string
  role_code: string
}

const { handleSubmit, isSubmitting } = useForm<InviteFormValues>({
  validationSchema: inviteFormSchema,
  initialValues: { email: '', role_code: 'member' },
})

const inviteMutation = useCreateInvitationMutation({
  onSuccess: (invitation) => {
    showSuccessToast(`Invitation sent to ${invitation.email}`)
    void router.push({
      name: 'platform-organisation-detail',
      params: { organisationId: props.organisationId },
    })
  },
})

const onSubmit = handleSubmit(async (values) => {
  try {
    await inviteMutation.mutateAsync({ organisationId: props.organisationId, body: values })
  } catch (error) {
    showApiErrorToast(error, { title: 'Could not send invitation' })
  }
})
</script>

<template>
  <div class="space-y-6">
    <div>
      <h1 class="text-2xl font-semibold">Invite user</h1>
      <p class="text-muted-foreground mt-1 text-sm">
        The invitation is delivered by WorkOS; the membership is created when the invitee signs in.
      </p>
    </div>

    <Card>
      <CardHeader>
        <CardTitle>Invitation</CardTitle>
        <CardDescription>Email and the organisation role the invitee will receive.</CardDescription>
      </CardHeader>
      <CardContent>
        <form
          data-testid="platform-invite-form"
          novalidate
          class="grid max-w-md gap-4"
          @submit="onSubmit"
        >
          <FormField v-slot="{ componentField }" name="email">
            <FormItem>
              <FormLabel>Email</FormLabel>
              <FormControl>
                <Input
                  type="email"
                  placeholder="invitee@example.com"
                  :disabled="isSubmitting"
                  v-bind="componentField"
                />
              </FormControl>
              <FormMessage />
            </FormItem>
          </FormField>

          <FormField v-slot="{ componentField }" name="role_code">
            <FormItem>
              <FormLabel>Role</FormLabel>
              <FormControl>
                <!-- `owner` is offered here on purpose: a platform-created
                     organisation starts with no members, so inviting its first
                     owner is the bootstrap path for the owner bundle. The
                     memberships role select in the detail view excludes
                     `owner` for post-hoc assignment to avoid accidental
                     owner grants. -->
                <NativeSelect v-bind="componentField" :disabled="isSubmitting">
                  <option v-for="role in ORGANISATION_ROLE_CODES" :key="role" :value="role">
                    {{ role }}
                  </option>
                </NativeSelect>
              </FormControl>
              <FormMessage />
            </FormItem>
          </FormField>

          <div class="flex justify-end gap-2">
            <Button
              type="button"
              variant="outline"
              :disabled="isSubmitting"
              @click="
                router.push({ name: 'platform-organisation-detail', params: { organisationId } })
              "
            >
              Cancel
            </Button>
            <Button type="submit" :disabled="isSubmitting" data-testid="platform-invite-submit">
              <LoaderCircleIcon v-if="isSubmitting" class="animate-spin" />
              Send invitation
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  </div>
</template>
