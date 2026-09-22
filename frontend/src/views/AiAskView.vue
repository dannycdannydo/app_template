<script setup lang="ts">
import { BotIcon, FileQuestionIcon, LoaderCircleIcon, SendIcon } from '@lucide/vue'
import { computed, ref } from 'vue'

import FileUpload from '@/components/application/FileUpload.vue'
import ScratchFileUpload from '@/components/application/ScratchFileUpload.vue'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { formatDateTime } from '@/lib/format'
import { useFilePermissions } from '@/lib/permissions'
import { showApiErrorToast } from '@/lib/toast'
import { isAskAccepted, useAskMutation, useAskResultQuery } from '@/queries/ai'

/**
 * AI test screen (v0.8 Scope §2.2/§6.4/§6.5).
 *
 * The minimal intelligence-layer harness: upload a PDF, ask one bounded
 * question about it and read the validated answer. The upload mode selects
 * which namespace the file lands in and therefore which source lifecycle the
 * AI layer classifies — ``transient`` uploads into ``ai/scratch/`` while
 * ``permanent`` uploads through the files module into ``documents/``. The
 * document question is queued durably, so a large-file transfer never runs in
 * the submitting HTTP request. The backend decides the exact transfer path and
 * this view never names a provider; ownership is re-validated by the worker.
 *
 * The upload/ask affordances are gated by the documents.upload role bundle
 * (`useFilePermissions`); the backend stays the enforcement point.
 */

type StorageMode = 'transient' | 'permanent'

const { permissions, mePending } = useFilePermissions()

const storageMode = ref<StorageMode>('transient')
const storageReference = ref<string | null>(null)
const question = ref('')
const askMutation = useAskMutation()
const askRequestId = ref('')
const askResultQuery = useAskResultQuery(askRequestId)

const answer = computed(() => {
  const submitted = askMutation.data.value
  if (submitted && !isAskAccepted(submitted)) return submitted
  return askResultQuery.data.value?.status === 'succeeded' ? askResultQuery.data.value : undefined
})
const askPending = computed(
  () =>
    askMutation.isPending.value ||
    askResultQuery.data.value?.status === 'queued' ||
    askResultQuery.data.value?.status === 'running',
)
const askFailed = computed(
  () => askMutation.isError.value || askResultQuery.data.value?.status === 'failed',
)

const canAsk = computed(() => storageReference.value !== null && question.value.trim().length > 0)

function onUploaded(reference: string): void {
  storageReference.value = reference
}

async function submitQuestion(): Promise<void> {
  if (storageReference.value === null || question.value.trim() === '') return
  askMutation.reset()
  askRequestId.value = ''
  try {
    const submitted = await askMutation.mutateAsync({
      storage_reference: storageReference.value,
      question: question.value.trim(),
      sync: false,
    })
    if (isAskAccepted(submitted)) askRequestId.value = submitted.request_id
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
        Upload a PDF, ask a question about it, and read the answer. Questions run as durable
        background jobs, including PDFs above the 5 MB inline threshold.
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
      <CardContent class="space-y-4">
        <fieldset class="space-y-2">
          <legend class="sr-only">Storage mode</legend>
          <p class="text-muted-foreground text-xs">How should this file be stored?</p>
          <div class="flex gap-2">
            <label
              class="hover:border-ring/50 focus-within:ring-ring/50 flex-1 cursor-pointer rounded-lg border px-3 py-2 text-sm outline-none focus-within:ring-2"
              :class="storageMode === 'transient' ? 'border-ring bg-muted' : 'bg-transparent'"
            >
              <input
                v-model="storageMode"
                type="radio"
                value="transient"
                class="sr-only"
                data-testid="ai-ask-mode-transient"
              />
              <span class="font-medium">Transient</span>
              <span class="text-muted-foreground block text-xs">
                Scratch storage; expires after use
              </span>
            </label>
            <label
              class="hover:border-ring/50 focus-within:ring-ring/50 flex-1 cursor-pointer rounded-lg border px-3 py-2 text-sm outline-none focus-within:ring-2"
              :class="storageMode === 'permanent' ? 'border-ring bg-muted' : 'bg-transparent'"
            >
              <input
                v-model="storageMode"
                type="radio"
                value="permanent"
                class="sr-only"
                data-testid="ai-ask-mode-permanent"
              />
              <span class="font-medium">Permanent</span>
              <span class="text-muted-foreground block text-xs">
                Retained document; signed URL fetch
              </span>
            </label>
          </div>
        </fieldset>
        <ScratchFileUpload
          v-if="storageMode === 'transient'"
          accept=".pdf,application/pdf"
          @uploaded="onUploaded"
        />
        <FileUpload v-else accept=".pdf,application/pdf" @uploaded="onUploaded" />
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
            :disabled="!canAsk || askPending"
            @click="submitQuestion"
          >
            <LoaderCircleIcon v-if="askPending" class="animate-spin" aria-hidden="true" />
            <SendIcon v-else class="size-4" aria-hidden="true" />
            {{ askPending ? 'Asking…' : 'Ask' }}
          </Button>
        </template>
      </CardContent>
    </Card>

    <Card v-if="askFailed" data-testid="ai-ask-error-card">
      <CardHeader>
        <CardTitle>The question could not be answered</CardTitle>
      </CardHeader>
      <CardContent class="text-muted-foreground text-sm">
        {{
          askMutation.error.value?.message ??
          (askResultQuery.data.value?.error_code
            ? `The background job failed (${askResultQuery.data.value.error_code}).`
            : 'An unexpected error occurred.')
        }}
      </CardContent>
    </Card>

    <Card v-if="answer" data-testid="ai-ask-answer-card">
      <CardHeader>
        <CardTitle>Answer</CardTitle>
      </CardHeader>
      <CardContent class="space-y-4">
        <p class="text-sm whitespace-pre-wrap" data-testid="ai-ask-answer">
          {{ answer.output ?? 'The answer was not retained by the organisation policy.' }}
        </p>
        <dl class="text-muted-foreground flex flex-wrap gap-x-6 gap-y-1 text-xs">
          <div class="flex items-center gap-1.5">
            <dt>Model</dt>
            <dd class="text-foreground font-medium">
              {{ answer.routing?.model }}
            </dd>
          </div>
          <div class="flex items-center gap-1.5">
            <dt>Provider</dt>
            <dd class="text-foreground font-medium">
              {{ answer.routing?.provider }}
            </dd>
          </div>
          <div v-if="answer.routing?.region" class="flex items-center gap-1.5">
            <dt>Region</dt>
            <dd class="text-foreground font-medium">
              {{ answer.routing.region }}
            </dd>
          </div>
          <div class="flex items-center gap-1.5">
            <dt>Tokens</dt>
            <dd class="text-foreground font-medium">
              {{ answer.usage?.input_tokens }} in / {{ answer.usage?.output_tokens }} out
            </dd>
          </div>
          <div class="flex items-center gap-1.5">
            <dt>Cost</dt>
            <dd class="text-foreground font-medium">
              {{ answer.cost?.amount }} {{ answer.cost?.currency }}
            </dd>
          </div>
          <div class="flex items-center gap-1.5">
            <dt>Completed</dt>
            <dd class="text-foreground font-medium">
              {{ answer.completed_at ? formatDateTime(answer.completed_at) : '' }}
            </dd>
          </div>
        </dl>
      </CardContent>
    </Card>
  </div>
</template>
