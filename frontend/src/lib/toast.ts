import { toast } from 'vue-sonner'

import { isApiError } from '@/api/errors'
import type { ApiError } from '@/api/errors'

/**
 * Toast helpers wired to the standard API error envelope (v0.3 Scope §6.6,
 * blueprint §13).
 *
 * Every API failure reaches the user as one consistent toast: the envelope's
 * human `message`, any field-level `details` and the backend `request_id` for
 * support correlation. Field errors also surface inline in forms; the toast
 * is the summary that survives navigation.
 */
function buildErrorDescription(error: ApiError): string {
  const parts: string[] = [error.message]
  if (error.details?.length) {
    for (const detail of error.details) {
      parts.push(`• ${detail.field}: ${detail.message}`)
    }
  }
  if (error.requestId) {
    parts.push(`Request id: ${error.requestId}`)
  }
  return parts.join('\n')
}

/** Map any thrown error to an error toast; non-envelope errors get a safe generic message. */
export function showApiErrorToast(error: unknown, options?: { title?: string }): void {
  const title = options?.title ?? 'Request failed'
  if (isApiError(error)) {
    toast.error(title, { description: buildErrorDescription(error) })
    return
  }
  const fallback = error instanceof Error ? error.message : 'An unexpected error occurred.'
  toast.error(title, { description: fallback })
}

/** Success feedback for mutations and other completed flows. */
export function showSuccessToast(message: string): void {
  toast.success(message)
}
