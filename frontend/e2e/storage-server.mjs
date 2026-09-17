import { createServer } from 'node:http'

/**
 * Minimal storage-origin server for the AI browser journey (plan P9).
 *
 * A real server (rather than a Playwright route fulfilment, which bypasses the
 * browser's CORS check) is required to prove that the browser enforces the
 * storage origin's CORS policy. `/allowed/**` grants the app origin the
 * preflight and PUT methods; `/denied/**` answers without any CORS header so
 * the browser refuses the cross-origin upload.
 */
const PORT = Number(process.env.E2E_STORAGE_PORT ?? 4180)
const ALLOWED_ORIGIN = process.env.E2E_APP_ORIGIN ?? 'http://localhost:4173'

const corsHeaders = {
  'access-control-allow-origin': ALLOWED_ORIGIN,
  'access-control-allow-methods': 'PUT, GET, HEAD, OPTIONS',
  'access-control-allow-headers': 'content-type',
  'access-control-expose-headers': 'ETag',
}

const server = createServer((request, response) => {
  if (request.url === '/health') {
    response.writeHead(200)
    response.end('ok')
    return
  }
  const denied = (request.url ?? '').startsWith('/denied/')
  const headers = denied ? {} : corsHeaders
  if (request.method === 'OPTIONS') {
    response.writeHead(204, headers)
    response.end()
    return
  }
  if (request.method === 'PUT') {
    request.resume()
    response.writeHead(200, headers)
    response.end()
    return
  }
  response.writeHead(404, headers)
  response.end()
})

server.listen(PORT, '127.0.0.1')
