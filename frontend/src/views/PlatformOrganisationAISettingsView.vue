<script setup lang="ts">
import { ArrowLeftIcon, LoaderCircleIcon } from '@lucide/vue'
import { computed, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { showApiErrorToast, showSuccessToast } from '@/lib/toast'
import { usePlatformAISettingsQuery, useUpdatePlatformAISettingsMutation } from '@/queries/platform'

/**
 * Organisation AI settings admin page (v0.7 Scope §6.5, v0.8 Scope §6.2).
 *
 * The platform-admin editing surface for one organisation's AI policy: enable
 * AI, choose which providers and transfer modes the organisation may use and
 * cap the large-file ceiling. The backend stays the enforcement point — the
 * admin centre only manages the settings row, and `AIService` enforces it at
 * request time. Updates carry the optimistic-concurrency `version` from the
 * GET; a stale save is rejected with a conflict and the form refreshes.
 */

const props = defineProps<{ organisationId: string }>()

const router = useRouter()

const {
  data: settings,
  isPending,
  isError,
  refetch,
} = usePlatformAISettingsQuery(() => props.organisationId)

const KNOWN_PROVIDERS: { id: string; label: string }[] = [
  { id: 'fake', label: 'fake (deterministic test adapter)' },
  { id: 'openai', label: 'OpenAI' },
  { id: 'anthropic', label: 'Anthropic' },
  { id: 'deepseek', label: 'DeepSeek' },
  { id: 'azure_openai', label: 'Azure OpenAI' },
  { id: 'vertex', label: 'Vertex AI' },
  { id: 'local', label: 'Local OpenAI-compatible' },
]

const TRANSFER_MODES: { id: string; label: string; hint: string }[] = [
  { id: 'inline', label: 'inline', hint: 'files up to 5 MB embedded in the request (always on)' },
  {
    id: 'provider_upload',
    label: 'provider_upload',
    hint: 'transient files uploaded to the provider',
  },
  {
    id: 'managed_signed_url',
    label: 'managed_signed_url',
    hint: 'retained files via a short-lived signed URL',
  },
  {
    id: 'storage_reference',
    label: 'storage_reference',
    hint: 'large files staged through the Vertex GCS bucket',
  },
]

interface FormState {
  version: number
  enabled: boolean
  allowedProviderIds: string[]
  allowedModelIds: string
  providerOverride: string
  modelOverride: string
  // `type="number"` inputs coerce v-model to numbers and the API serializes
  // the Decimal budget as a JSON number, so these fields may hold either.
  monthlyBudget: string | number
  retentionPolicyDays: string | number
  allowedTransferModes: string[]
  maxLargeAttachmentBytes: string | number
}

function emptyForm(version: number): FormState {
  return {
    version,
    enabled: false,
    allowedProviderIds: [],
    allowedModelIds: '',
    providerOverride: '',
    modelOverride: '',
    monthlyBudget: '',
    retentionPolicyDays: '',
    allowedTransferModes: ['inline'],
    maxLargeAttachmentBytes: '50000000',
  }
}

const form = reactive<FormState>(emptyForm(1))

watch(
  () => settings.value,
  (value) => {
    if (!value) return
    form.version = value.version
    form.enabled = value.enabled
    form.allowedProviderIds = [...value.allowed_provider_ids]
    form.allowedModelIds = value.allowed_model_ids.join(', ')
    form.providerOverride = value.provider_override ?? ''
    form.modelOverride = value.model_override ?? ''
    form.monthlyBudget = value.monthly_budget ?? ''
    form.retentionPolicyDays = value.retention_policy_days?.toString() ?? ''
    form.allowedTransferModes = [...value.allowed_transfer_modes]
    form.maxLargeAttachmentBytes = value.max_large_attachment_bytes.toString()
  },
)

const versionError = ref(false)

const updateMutation = useUpdatePlatformAISettingsMutation({
  onSuccess: (updated) => {
    form.version = updated.version
    showSuccessToast('AI settings updated')
  },
})

function toggleListItem(list: string[], id: string): string[] {
  return list.includes(id) ? list.filter((item) => item !== id) : [...list, id]
}

function toggleProvider(id: string): void {
  form.allowedProviderIds = toggleListItem(form.allowedProviderIds, id)
}

function toggleTransferMode(id: string): void {
  // ``inline`` is the default-deny baseline and can never be switched off:
  // the backend rejects a payload without it (v0.8 Scope §2.2).
  if (id === 'inline') return
  form.allowedTransferModes = toggleListItem(form.allowedTransferModes, id)
}

const parsedMaxBytes = computed(() => {
  const value = Number.parseInt(String(form.maxLargeAttachmentBytes), 10)
  return Number.isFinite(value) ? value : NaN
})

const maxBytesValid = computed(
  () => parsedMaxBytes.value >= 1 && parsedMaxBytes.value <= 50_000_000,
)

const canSave = computed(() => maxBytesValid.value && form.allowedTransferModes.includes('inline'))

async function save(): Promise<void> {
  if (!canSave.value) return
  versionError.value = false
  const budget = String(form.monthlyBudget ?? '').trim()
  const retention = String(form.retentionPolicyDays ?? '').trim()
  try {
    await updateMutation.mutateAsync({
      organisationId: props.organisationId,
      payload: {
        version: form.version,
        enabled: form.enabled,
        allowed_provider_ids: form.allowedProviderIds,
        allowed_model_ids: form.allowedModelIds
          .split(',')
          .map((item) => item.trim())
          .filter(Boolean),
        provider_override: form.providerOverride.trim() || null,
        model_override: form.modelOverride.trim() || null,
        monthly_budget: budget ? budget : null,
        retention_policy_days: retention ? Number(retention) : null,
        allowed_transfer_modes: form.allowedTransferModes,
        max_large_attachment_bytes: parsedMaxBytes.value,
      },
    })
  } catch (error) {
    showApiErrorToast(error, { title: 'Could not update AI settings' })
    const message =
      error instanceof Error
        ? error.message
        : String((error as { message?: unknown } | null)?.message ?? '')
    if (message.toLowerCase().includes('conflict')) {
      versionError.value = true
      void refetch()
    }
  }
}
</script>

<template>
  <div class="space-y-6">
    <div class="flex items-center gap-3">
      <Button
        variant="outline"
        size="sm"
        data-testid="ai-settings-back"
        @click="router.push({ name: 'platform-organisation-detail', params: { organisationId } })"
      >
        <ArrowLeftIcon class="size-4" aria-hidden="true" />
        Back
      </Button>
      <div>
        <h1 class="text-2xl font-semibold">AI settings</h1>
        <p class="text-muted-foreground mt-1 text-sm">
          The organisation's intelligence-layer policy: enablement, allowed providers and transfer
          modes. Enforced by the AI service at request time.
        </p>
      </div>
    </div>

    <Card v-if="isError">
      <CardHeader>
        <CardTitle>Could not load AI settings</CardTitle>
        <CardDescription>Refresh the page to try again.</CardDescription>
      </CardHeader>
    </Card>

    <template v-else-if="!isPending && settings">
      <Card>
        <CardHeader>
          <CardTitle>General</CardTitle>
          <CardDescription>
            When AI is disabled the organisation receives a 503 for every intelligence request.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <label class="flex items-start gap-3 rounded-xl border p-4">
            <input
              v-model="form.enabled"
              type="checkbox"
              class="mt-1 size-4 accent-current"
              data-testid="ai-settings-enabled"
            />
            <span>
              <span class="block text-sm font-medium">Enable AI for this organisation</span>
              <span class="text-muted-foreground text-xs">
                Also set an allowed provider list below, or routing finds nothing to dispatch
                through.
              </span>
            </span>
          </label>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Allowed providers</CardTitle>
          <CardDescription>
            Empty means no provider is allowed (default deny). Select at least the one this
            deployment actually configures.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div class="grid gap-2 sm:grid-cols-2">
            <label
              v-for="provider in KNOWN_PROVIDERS"
              :key="provider.id"
              class="flex items-center gap-2 rounded-lg border px-3 py-2 text-sm"
            >
              <input
                type="checkbox"
                class="size-4 accent-current"
                :checked="form.allowedProviderIds.includes(provider.id)"
                :data-testid="`ai-settings-provider-${provider.id}`"
                @change="toggleProvider(provider.id)"
              />
              {{ provider.label }}
            </label>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Transfer modes</CardTitle>
          <CardDescription>
            How documents are carried to the provider. <code>inline</code> is always on; the rest
            require matching deployment configuration.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div class="grid gap-2">
            <label
              v-for="mode in TRANSFER_MODES"
              :key="mode.id"
              class="flex items-start gap-2 rounded-lg border px-3 py-2 text-sm"
            >
              <input
                type="checkbox"
                class="mt-0.5 size-4 accent-current"
                :checked="form.allowedTransferModes.includes(mode.id)"
                :disabled="mode.id === 'inline'"
                :data-testid="`ai-settings-mode-${mode.id}`"
                @change="toggleTransferMode(mode.id)"
              />
              <span>
                <span class="block font-medium">{{ mode.label }}</span>
                <span class="text-muted-foreground text-xs">{{ mode.hint }}</span>
              </span>
            </label>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Limits</CardTitle>
          <CardDescription>Ceilings that can only tighten the template defaults.</CardDescription>
        </CardHeader>
        <CardContent class="grid max-w-md gap-4">
          <div class="flex flex-col gap-2">
            <Label for="ai-settings-max-bytes">Max large attachment bytes</Label>
            <Input
              id="ai-settings-max-bytes"
              v-model="form.maxLargeAttachmentBytes"
              type="number"
              min="1"
              max="50000000"
              data-testid="ai-settings-max-bytes"
            />
            <p v-if="!maxBytesValid" class="text-destructive text-xs">
              Must be between 1 and 50,000,000.
            </p>
          </div>
          <div class="flex flex-col gap-2">
            <Label for="ai-settings-budget">Monthly budget (USD, empty = no budget)</Label>
            <Input
              id="ai-settings-budget"
              v-model="form.monthlyBudget"
              type="number"
              min="0"
              step="0.000001"
              data-testid="ai-settings-budget"
            />
          </div>
          <div class="flex flex-col gap-2">
            <Label for="ai-settings-retention">Retention policy days (empty = keep forever)</Label>
            <Input
              id="ai-settings-retention"
              v-model="form.retentionPolicyDays"
              type="number"
              min="1"
              max="3650"
              data-testid="ai-settings-retention"
            />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Advanced routing</CardTitle>
          <CardDescription>
            Optional allowlists and overrides. Empty lists mean "no restriction from this knob".
          </CardDescription>
        </CardHeader>
        <CardContent class="grid max-w-md gap-4">
          <div class="flex flex-col gap-2">
            <Label for="ai-settings-model-ids">
              Allowed model ids (comma-separated registry ids)
            </Label>
            <Input
              id="ai-settings-model-ids"
              v-model="form.allowedModelIds"
              placeholder="vertex.gemini-2.0-flash"
              data-testid="ai-settings-model-ids"
            />
          </div>
          <div class="flex flex-col gap-2">
            <Label for="ai-settings-provider-override">Provider override</Label>
            <Input
              id="ai-settings-provider-override"
              v-model="form.providerOverride"
              placeholder="vertex"
              data-testid="ai-settings-provider-override"
            />
          </div>
          <div class="flex flex-col gap-2">
            <Label for="ai-settings-model-override">Model override</Label>
            <Input
              id="ai-settings-model-override"
              v-model="form.modelOverride"
              placeholder="vertex.gemini-2.0-flash"
              data-testid="ai-settings-model-override"
            />
          </div>
        </CardContent>
      </Card>

      <div class="flex items-center gap-3">
        <Button
          data-testid="ai-settings-save"
          :disabled="!canSave || updateMutation.isPending.value"
          @click="save"
        >
          <LoaderCircleIcon v-if="updateMutation.isPending.value" class="animate-spin" />
          Save AI settings
        </Button>
        <p v-if="versionError" class="text-destructive text-sm" data-testid="ai-settings-conflict">
          The settings changed elsewhere. Reloaded the latest version — review and save again.
        </p>
      </div>
    </template>
  </div>
</template>
