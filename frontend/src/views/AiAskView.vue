<script setup lang="ts">
import { BotIcon, FileQuestionIcon, LoaderCircleIcon, SendIcon } from '@lucide/vue'
import { computed, ref } from 'vue'

import FileUpload from '@/components/application/FileUpload.vue'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { formatDateTime } from '@/lib/format'
import { useFilePermissions } from '@/lib/permissions'
import { showApiErrorToast } from '@/lib/toast'
import { useAskMutation } from '@/queries/ai'
import { useOrganisationStore } from '@/stores/organisation'

/**
 * AI test screen (v0.8 Scope §2.2/§6.4).
 *
 * The minimal intelligence-layer harness: upload a PDF, ask one bounded
 * question about it and read the validated answer. The backend decides the
 * transfer path — inline at or below the 5 MB aggregate threshold, private
 * Vertex GCS staging above it — and this view never names a provider or
 * storage path. The server-generated object key is reconstructed from the
 * resolved membership and the uploaded file id (the same format the files
 * module owns); the backend re-validates ownership on every request.
 *
 * The upload/ask affordances are gated by the documents.upload role bundle
 * (`useFilePermissions`); the backend stays the enforcement point.
 */

const organisation = useOrganisationStore()
const { permissions, mePending } = useFilePermissions()

const storageReference = ref<string | null>(null)
const question = ref('')
const askMutation = useAskMutation()

const canAsk = computed(() => storageReference.value !== null && question.value.trim().length > 0)

function onUploaded(reference: string): void {
  storageReference.value = reference
}

async function submitQuestion(): Promise<void> {
  if (storageReference.value === null || question.value.trim() === '') return
  askMutation.reset()
  try {
    await askMutation.mutateAsync({
      storage_reference: storageReference.value,
      question: question.value.trim(),
    })
  } catch (error) {
    showApiErrorToast(error, { title: 'Could not ask the document' })
  }
}
</script>

<template>
  <div class="space-y-6">
    <div>
      <h1 class="text-2xl font-semibold">AI test</h1>
      <p class="text-muted-foreground mt-1 text-sm">
        Upload a PDF, ask a question about it, and read the answer. Small files travel inline;
        larger ones stage through private GCS before Vertex processes them.
      </p>
    </div>

    <Card v-if="!mePending && permissions.canUpload" data-testid="ai-ask-upload-card">
      <CardHeader>
        <CardTitle class="flex items-center gap-2">
          <FileQuestionIcon class="size-4" aria-hidden="true" />
          1. Upload a document
        </CardTitle>
        <CardDescription>
          The file uploads directly to storage through a signed URL; use it to ask questions.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <FileUpload accept=".pdf,application/pdf" @uploaded="onUploaded" />
      </CardContent>
    </Card>

    <Card data-testid="ai-ask-question-card">
      <CardHeader>
        <CardTitle class="flex items-center gap-2">
          <BotIcon class="size-4" aria-hidden="true" />
          2. Ask a question
        </CardTitle>
        <CardDescription>
          {{
            storageReference === null
              ? 'Upload a document first.'
              : 'The question is answered from the uploaded document alone.'
          }}
        </CardDescription>
      </CardHeader>
      <CardContent class="space-y-4">
        <div v-if="storageReference === null" class="text-muted-foreground text-sm">
          No document uploaded yet.
        </div>
        <template v-else>
          <div class="flex flex-col gap-2">
            <Label for="ai-ask-question">Question</Label>
            <Textarea
              id="ai-ask-question"
              v-model="question"
              data-testid="ai-ask-question-input"
              :maxlength="512"
              placeholder="e.g. What is the renewal term in this lease?"
              rows="3"
            />
            <p class="text-muted-foreground text-xs">{{ question.length }}/512 characters</p>
          </div>
          <Button
            data-testid="ai-ask-submit"
            :disabled="!canAsk || askMutation.isPending.value"
            @click="submitQuestion"
          >
            <LoaderCircleIcon
              v-if="askMutation.isPending.value"
              class="animate-spin"
              aria-hidden="true"
            />
            <SendIcon v-else class="size-4" aria-hidden="true" />
            {{ askMutation.isPending.value ? 'Asking…' : 'Ask' }}
          </Button>
        </template>
      </CardContent>
    </Card>

    <Card v-if="askMutation.isError.value" data-testid="ai-ask-error-card">
      <CardHeader>
        <CardTitle>The question could not be answered</CardTitle>
      </CardHeader>
      <CardContent class="text-muted-foreground text-sm">
        {{ askMutation.error.value?.message ?? 'An unexpected error occurred.' }}
      </CardContent>
    </Card>

    <Card v-if="askMutation.data.value" data-testid="ai-ask-answer-card">
      <CardHeader>
        <CardTitle>Answer</CardTitle>
      </CardHeader>
      <CardContent class="space-y-4">
        <p class="text-sm whitespace-pre-wrap" data-testid="ai-ask-answer">
          {{ askMutation.data.value.output }}
        </p>
        <dl class="text-muted-foreground flex flex-wrap gap-x-6 gap-y-1 text-xs">
          <div class="flex items-center gap-1.5">
            <dt>Model</dt>
            <dd class="text-foreground font-medium">
              {{ askMutation.data.value.routing.model }}
            </dd>
          </div>
          <div class="flex items-center gap-1.5">
            <dt>Provider</dt>
            <dd class="text-foreground font-medium">
              {{ askMutation.data.value.routing.provider }}
            </dd>
          </div>
          <div v-if="askMutation.data.value.routing.region" class="flex items-center gap-1.5">
            <dt>Region</dt>
            <dd class="text-foreground font-medium">
              {{ askMutation.data.value.routing.region }}
            </dd>
          </div>
          <div class="flex items-center gap-1.5">
            <dt>Tokens</dt>
            <dd class="text-foreground font-medium">
              {{ askMutation.data.value.usage.input_tokens }} in /
              {{ askMutation.data.value.usage.output_tokens }} out
            </dd>
          </div>
          <div class="flex items-center gap-1.5">
            <dt>Cost</dt>
            <dd class="text-foreground font-medium">
              {{ askMutation.data.value.cost.amount }} {{ askMutation.data.value.cost.currency }}
            </dd>
          </div>
          <div class="flex items-center gap-1.5">
            <dt>Completed</dt>
            <dd class="text-foreground font-medium">
              {{ formatDateTime(askMutation.data.value.completed_at) }}
            </dd>
          </div>
        </dl>
      </CardContent>
    </Card>
  </div>
</template>
