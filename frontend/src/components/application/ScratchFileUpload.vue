<script setup lang="ts">
import {
  CheckCircle2Icon,
  FileUpIcon,
  LoaderCircleIcon,
  RefreshCwIcon,
  TriangleAlertIcon,
} from '@lucide/vue'
import { computed, ref } from 'vue'

import { Button } from '@/components/ui/button'
import { formatBytes } from '@/lib/format'
import { showApiErrorToast } from '@/lib/toast'
import { useScratchUploadMutation } from '@/queries/ai'

/**
 * Transient uploader for the AI test screen (v0.8 Scope §2.2/§6.5).
 *
 * The same direct-upload journey as the files module — intent → signed PUT →
 * complete — but targeting the organisation-scoped ``ai/scratch/`` namespace,
 * where the AI layer classifies the source as transient and routes a >5 MB
 * PDF through the provider-upload mode. There is no durable file record and
 * no processing job: the mutation resolves with the storage reference the
 * parent sends to the ask endpoint.
 */
const props = withDefaults(
  defineProps<{
    accept?: string
  }>(),
  {
    accept: undefined,
  },
)

const emit = defineEmits<{
  (e: 'uploaded', storageReference: string): void
}>()

type Phase = 'idle' | 'requesting-url' | 'uploading' | 'completing' | 'done' | 'error'

const fileInput = ref<HTMLInputElement | null>(null)
const selectedFile = ref<File | null>(null)
const phase = ref<Phase>('idle')
const progress = ref(0)
const errorMessage = ref<string | null>(null)

const uploadMutation = useScratchUploadMutation({
  onProgress: (next) => {
    if (next.total > 0) {
      progress.value = Math.round((next.loaded / next.total) * 100)
    }
    if (phase.value === 'requesting-url') {
      phase.value = 'uploading'
    }
  },
  onSuccess: (reference) => {
    progress.value = 100
    phase.value = 'done'
    emit('uploaded', reference)
  },
})

const busy = computed(() => uploadMutation.isPending.value)

const phaseLabel = computed(() => {
  switch (phase.value) {
    case 'requesting-url':
      return 'Preparing upload…'
    case 'uploading':
      return 'Uploading…'
    case 'completing':
      return 'Verifying upload…'
    case 'done':
      return 'Upload complete'
    default:
      return ''
  }
})

function onFilePicked(event: Event): void {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  selectedFile.value = file
  progress.value = 0
  errorMessage.value = null
  phase.value = 'idle'
}

function clearSelection(): void {
  selectedFile.value = null
  progress.value = 0
  errorMessage.value = null
  phase.value = 'idle'
  if (fileInput.value) {
    fileInput.value.value = ''
  }
}

async function startUpload(): Promise<void> {
  const file = selectedFile.value
  if (!file) return
  phase.value = 'requesting-url'
  errorMessage.value = null
  try {
    await uploadMutation.mutateAsync(file)
  } catch (error) {
    phase.value = 'error'
    errorMessage.value = error instanceof Error ? error.message : 'Upload failed'
    showApiErrorToast(error, { title: 'Could not upload to the AI scratch area' })
  }
}

const pickerId = 'scratch-file-upload-input'
</script>

<template>
  <div data-testid="scratch-file-upload" class="space-y-4">
    <template v-if="!selectedFile">
      <label
        :for="pickerId"
        class="hover:border-ring/50 focus-within:ring-ring/50 flex cursor-pointer flex-col items-center justify-center gap-2 rounded-xl border border-dashed px-6 py-8 text-center outline-none transition-colors focus-within:ring-3"
        data-testid="scratch-file-upload-picker"
      >
        <FileUpIcon class="text-muted-foreground size-8" aria-hidden="true" />
        <span class="text-sm font-medium">Choose a PDF to upload</span>
        <span class="text-muted-foreground text-xs">
          Stored as a transient scratch object; deleted by the AI retention sweep.
        </span>
        <input
          :id="pickerId"
          ref="fileInput"
          type="file"
          :accept="props.accept"
          class="sr-only"
          data-testid="scratch-file-upload-input"
          @change="onFilePicked"
        />
      </label>
    </template>

    <template v-else>
      <div class="bg-muted/50 flex items-center justify-between gap-3 rounded-xl border px-4 py-3">
        <div class="min-w-0">
          <p class="truncate text-sm font-medium" data-testid="scratch-file-upload-name">
            {{ selectedFile.name }}
          </p>
          <p class="text-muted-foreground text-xs">{{ formatBytes(selectedFile.size) }}</p>
        </div>
        <div class="flex shrink-0 items-center gap-2">
          <template v-if="phase === 'done'">
            <span class="text-muted-foreground flex items-center gap-1.5 text-sm">
              <CheckCircle2Icon class="size-4" aria-hidden="true" />
              Done
            </span>
            <Button
              variant="outline"
              size="sm"
              data-testid="scratch-file-upload-clear"
              @click="clearSelection"
            >
              Upload another
            </Button>
          </template>
          <Button
            v-else-if="phase !== 'error'"
            variant="outline"
            size="sm"
            :disabled="busy"
            data-testid="scratch-file-upload-clear"
            @click="clearSelection"
          >
            Cancel
          </Button>
        </div>
      </div>

      <div
        v-if="phase !== 'idle' && phase !== 'error' && phase !== 'done'"
        class="space-y-2"
        data-testid="scratch-file-upload-progress"
      >
        <div class="flex items-center justify-between text-xs">
          <span class="text-muted-foreground flex items-center gap-1.5">
            <LoaderCircleIcon v-if="busy" class="size-3.5 animate-spin" aria-hidden="true" />
            {{ phaseLabel }}
          </span>
          <span class="text-muted-foreground tabular-nums">{{ progress }}%</span>
        </div>
        <div
          class="bg-muted h-2 w-full overflow-hidden rounded-full"
          role="progressbar"
          :aria-valuenow="progress"
          aria-valuemin="0"
          aria-valuemax="100"
        >
          <div
            class="bg-primary h-full rounded-full transition-[width] duration-300"
            :style="{ width: `${progress}%` }"
          />
        </div>
      </div>

      <p
        v-if="phase === 'error'"
        class="text-destructive text-sm"
        data-testid="scratch-file-upload-error"
        role="alert"
      >
        <span class="flex items-center gap-1.5">
          <TriangleAlertIcon class="size-4 shrink-0" aria-hidden="true" />
          {{ errorMessage }}
        </span>
      </p>

      <div v-if="phase === 'idle' || phase === 'error'" class="flex items-center gap-2">
        <Button
          data-testid="scratch-file-upload-submit"
          :disabled="busy"
          @click="startUpload"
        >
          <RefreshCwIcon v-if="phase === 'error'" class="size-4" />
          <FileUpIcon v-else class="size-4" />
          {{ phase === 'error' ? 'Try again' : 'Upload' }}
        </Button>
      </div>
    </template>
  </div>
</template>
