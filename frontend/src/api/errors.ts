import type { components } from './generated/openapi'

/**
 * Error envelope types come straight from the generated OpenAPI client
 * (blueprint §15: never hand-write duplicate API interfaces). The backend
 * always answers non-2xx with this shape (blueprint §13).
 */
export type ApiErrorEnvelope = components['schemas']['ErrorResponse']
export type ApiErrorDetail = components['schemas']['ErrorDetail']

/**
 * Typed client error carrying the standard API error envelope (blueprint §13).
 *
 * `client.ts` normalizes every non-2xx response into one of these so query
 * composables, toasts and forms all consume the same shape: a stable `code`,
 * a human `message`, optional field-level `details` and the backend
 * `request_id` for support correlation.
 */
export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly details: ApiErrorDetail[] | null
  readonly requestId: string

  constructor(status: number, envelope: ApiErrorEnvelope) {
    super(envelope.message)
    this.name = 'ApiError'
    this.status = status
    this.code = envelope.code
    this.details = envelope.details ?? null
    this.requestId = envelope.request_id
  }
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError
}

/** Codes used when the body is missing or not the standard envelope. */
const FALLBACK_CODES: Record<number, string> = {
  400: 'bad_request',
  401: 'unauthorized',
  403: 'permission_denied',
  404: 'not_found',
  409: 'conflict',
  422: 'validation_error',
  429: 'rate_limit_exceeded',
  500: 'internal_error',
  502: 'external_service_error',
  503: 'service_unavailable',
}

function isErrorEnvelope(body: unknown): body is ApiErrorEnvelope {
  if (typeof body !== 'object' || body === null) return false
  const candidate = body as Record<string, unknown>
  return typeof candidate.code === 'string' && typeof candidate.message === 'string'
}

/**
 * Parse a non-2xx `Response` into a typed `ApiError`.
 *
 * Accepts the standard envelope first; falls back to a status-derived code and
 * message when the body is missing, not JSON, or not the envelope (e.g. an
 * HTML error page from a proxy).
 */
export async function normalizeErrorResponse(response: Response): Promise<ApiError> {
  const status = response.status

  let body: unknown = null
  try {
    body = await response.json()
  } catch {
    // Body is empty or not JSON; fall back below.
  }

  if (isErrorEnvelope(body)) {
    return new ApiError(status, body)
  }

  const fallbackCode = FALLBACK_CODES[status] ?? `http_${status}`
  return new ApiError(status, {
    code: fallbackCode,
    message: `Request failed with status ${status}.`,
    details: null,
    request_id: '',
  })
}
