import { describe, expect, it } from 'vitest'

import { ApiError, isApiError, normalizeErrorResponse } from '@/api/errors'

describe('normalizeErrorResponse', () => {
  it('parses the standard error envelope into a typed ApiError', async () => {
    const response = new Response(
      JSON.stringify({
        code: 'validation_error',
        message: 'The request contains invalid data.',
        details: [{ field: 'name', message: 'Value must not be empty.' }],
        request_id: 'req-1',
      }),
      { status: 422, headers: { 'content-type': 'application/json' } },
    )

    const error = await normalizeErrorResponse(response)

    expect(error).toBeInstanceOf(ApiError)
    expect(error.status).toBe(422)
    expect(error.code).toBe('validation_error')
    expect(error.message).toBe('The request contains invalid data.')
    expect(error.details).toEqual([{ field: 'name', message: 'Value must not be empty.' }])
    expect(error.requestId).toBe('req-1')
  })

  it('falls back to a status-derived code when the body is not the envelope', async () => {
    const response = new Response('<html>Bad Gateway</html>', {
      status: 502,
      headers: { 'content-type': 'text/html' },
    })

    const error = await normalizeErrorResponse(response)

    expect(error).toBeInstanceOf(ApiError)
    expect(error.status).toBe(502)
    expect(error.code).toBe('external_service_error')
    expect(error.details).toBeNull()
    expect(error.requestId).toBe('')
  })

  it('falls back to a generic message when the body is empty', async () => {
    const response = new Response(null, { status: 500 })

    const error = await normalizeErrorResponse(response)

    expect(error.status).toBe(500)
    expect(error.code).toBe('internal_error')
    expect(error.message).toContain('500')
  })
})

describe('isApiError', () => {
  it('narrows ApiError instances and rejects other errors', () => {
    const apiError = new ApiError(404, {
      code: 'not_found',
      message: 'Missing.',
      details: null,
      request_id: 'req-2',
    })

    expect(isApiError(apiError)).toBe(true)
    expect(isApiError(new Error('plain'))).toBe(false)
    expect(isApiError(null)).toBe(false)
  })
})
