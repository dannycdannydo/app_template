import { createClient } from '@workos-inc/authkit-js'

/**
 * The only module that imports the WorkOS browser SDK. Everything else in the
 * app reaches auth through these four functions, so the SDK stays swappable
 * behind an adapter (blueprint §8: WorkOS owns login and session management,
 * the app never handles identity fields).
 *
 * The SDK is initialized lazily. `createClient` also runs the SDK
 * initialization, which detects an authorization `code` in the callback URL,
 * exchanges it for a session and fires `onRedirectCallback`.
 */
type WorkOSClient = Awaited<ReturnType<typeof createClient>>

const clientId = import.meta.env.VITE_WORKOS_CLIENT_ID
const redirectUri =
  import.meta.env.VITE_WORKOS_REDIRECT_URI ||
  new URL('/auth/callback', window.location.origin).toString()

let clientPromise: Promise<WorkOSClient> | null = null
let exchangedAccessToken: string | null = null

function getClient(): Promise<WorkOSClient> {
  if (!clientId) {
    throw new Error('VITE_WORKOS_CLIENT_ID is not configured. See .env.example.')
  }
  clientPromise ??= createClient(clientId, {
    redirectUri,
    onRedirectCallback: ({ accessToken }) => {
      exchangedAccessToken = accessToken
    },
  })
  return clientPromise
}

/**
 * Starts the login flow: builds the WorkOS AuthKit authorization URL with the
 * configured client id and callback URL, then redirects the browser to it.
 */
export async function startLogin(options: { returnTo?: string } = {}): Promise<void> {
  const client = await getClient()
  const url = await client.getSignInUrl(
    options.returnTo ? { state: { returnTo: options.returnTo } } : {},
  )
  window.location.assign(url)
}

/**
 * Completes the authorization-code flow on the callback route and returns the
 * session access token. Throws when the flow failed or no session exists.
 */
export async function completeLogin(): Promise<string> {
  const client = await getClient()
  if (exchangedAccessToken) {
    const token = exchangedAccessToken
    exchangedAccessToken = null
    return token
  }
  return client.getAccessToken()
}

/**
 * Ends the WorkOS session (local cleanup plus server-side revocation). Safe to
 * call without an active session; navigation is left to the caller.
 */
export async function signOut(): Promise<void> {
  const client = await getClient()
  await client.signOut({ navigate: false })
}

/**
 * Returns the current session access token, or null when the user is not
 * signed in (or WorkOS is not configured). Used to restore the session on app
 * boot; never throws.
 */
export async function getSession(): Promise<string | null> {
  try {
    const client = await getClient()
    return await client.getAccessToken()
  } catch {
    return null
  }
}
