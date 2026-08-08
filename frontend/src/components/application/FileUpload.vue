<script setup lang="ts">
import {
  CheckCircle2Icon,
  FileUpIcon,
  LoaderCircleIcon,
  RefreshCwIcon,
  TriangleAlertIcon,
} from '@lucide/vue'
import { computed, ref, watch } from 'vue'

import { Button } from '@/components/ui/button'
import { formatBytes } from '@/lib/format'
import { showApiErrorToast } from '@/lib/toast'
import { useUploadFileMutation } from '@/queries/files'
import { useJobQuery } from '@/queries/jobs'

/**
 * Direct-upload component (Scope §6.6, blueprint §17 flow, §14 boundary).
 *
 * Walks the signed-upload journey: file picker → upload intent → direct PUT
 * to the signed URL (XHR progress, `src/lib/upload.ts`) → complete → poll the
 * processing job to `ready`. The API orchestration lives in the query layer
 * (`useUploadFileMutation`); this component only renders state, emits the
 * completed file id and calls `onFileProcessed` when the job settles so the
 * parent can refresh its list.
 *
 * The component is permission-agnostic: the parent decides whether to render
 * it (documents.upload gate via `useFilePermissions`). The backend stays the
 * enforcement point for every step.
 */
const props = withDefaults(
  defineProps<{
    onFileProcessed?: () => void
  }>(),
  {
    onFileProcessed: undefined,
  },
)

const emit = defineEmits<{
  (e: 'uploaded', fileId: string): void
}>()

type Phase =
  'idle' | 'requesting-url' | 'uploading' | 'completing' | 'processing' | 'done' | 'error'

const fileInput = ref<HTMLInputElement | null>(null)
const selectedFile = ref<File | null>(null)
const phase = ref<Phase>('idle')
const progress = ref(0)
const errorMessage = ref<string | null>(null)
const processingJobId = ref<string | null>(null)

const uploadMutation = useUploadFileMutation({
  onProgress: (next) => {
    if (next.total > 0) {
      progress.value = Math.round((next.loaded / next.total) * 100)
    }
    // The first progress event proves the bytes are moving; the phase
    // transition happens here rather than in the mutation so the bar and the
    // label cannot disagree.
    if (phase.value === 'requesting-url') {
      phase.value = 'uploading'
    }
  },
  onSuccess: (result) => {
    progress.value = 100
    processingJobId.value = result.file.processing_job_id ?? null
    phase.value = processingJobId.value === null ? 'done' : 'processing'
    emit('uploaded', result.file.id)
  },
})

const { data: job, isError: jobQueryError } = useJobQuery(() => processingJobId.value ?? '')

const busy = computed(() => uploadMutation.isPending.value || phase.value === 'processing')

const phaseLabel = computed(() => {
  switch (phase.value) {
    case 'requesting-url':
      return 'Preparing upload…'
    case 'uploading':
      return 'Uploading…'
    case 'completing':
      return 'Verifying upload…'
    case 'processing':
      return 'Processing…'
    case 'done':
      return 'Upload complete'
    default:
      return ''
  }
})

watch(job, (current) => {
  if (!current) return
  // The job is the source of truth for progress once processing starts; the
  // worker drives it 0→100 server-side (Scope §6.5).
  progress.value = current.progress
  if (current.status === 'succeeded') {
    phase.value = 'done'
    props.onFileProcessed?.()
  } else if (current.status === 'failed' || current.status === 'cancelled') {
    phase.value = 'error'
    errorMessage.value =
      current.error_message ?? `Job ${current.status === 'failed' ? 'failed' : 'was cancelled'}`
    props.onFileProcessed?.()
  }
})

watch(jobQueryError, (failed) => {
  if (failed) {
    phase.value = 'error'
    errorMessage.value = 'Could not read the processing job'
    props.onFileProcessed?.()
  }
})

function onFilePicked(event: Event): void {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  selectedFile.value = file
  processingJobId.value = null
  progress.value = 0
  errorMessage.value = null
  phase.value = 'idle'
}

function clearSelection(): void {
  selectedFile.value = null
  processingJobId.value = null
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
    showApiErrorToast(error, { title: 'Could not upload file' })
  }
}

const pickerId = 'file-upload-input'
</script>

<template>
  <div data-testid="file-upload" class="space-y-4">
    <template v-if="!selectedFile">
      <label
        :for="pickerId"
        class="hover:border-ring/50 focus-within:ring-ring/50 flex cursor-pointer flex-col items-center justify-center gap-2 rounded-xl border border-dashed px-6 py-8 text-center outline-none transition-colors focus-within:ring-3"
        data-testid="file-upload-picker"
      >
        <FileUpIcon class="text-muted-foreground size-8" aria-hidden="true" />
        <span class="text-sm font-medium">Choose a file to upload</span>
        <span class="text-muted-foreground text-xs">
          The file uploads directly to storage through a signed URL, then a background job processes
          it.
        </span>
        <input
          :id="pickerId"
          ref="fileInput"
          type="file"
          class="sr-only"
          data-testid="file-upload-input"
          @change="onFilePicked"
        />
      </label>
    </template>

    <template v-else>
      <div class="bg-muted/50 flex items-center justify-between gap-3 rounded-xl border px-4 py-3">
        <div class="min-w-0">
          <p class="truncate text-sm font-medium" data-testid="file-upload-name">
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
              data-testid="file-upload-clear"
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
            data-testid="file-upload-clear"
            @click="clearSelection"
          >
            Cancel
          </Button>
        </div>
      </div>

      <div
        v-if="phase !== 'idle' && phase !== 'error' && phase !== 'done'"
        class="space-y-2"
        data-testid="file-upload-progress"
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
        data-testid="file-upload-error"
        role="alert"
      >
        <span class="flex items-center gap-1.5">
          <TriangleAlertIcon class="size-4 shrink-0" aria-hidden="true" />
          {{ errorMessage }}
        </span>
      </p>

      <div v-if="phase === 'idle' || phase === 'error'" class="flex items-center gap-2">
        <Button
          data-testid="file-upload-submit"
          :disabled="uploadMutation.isPending.value"
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
