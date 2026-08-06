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
let exchangedReturnTo: string | null = null

export interface CompletedLogin {
  accessToken: string
  returnTo: string | null
}

function safeLocalReturnTo(value: unknown): string | null {
  if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//')) return null
  try {
    return new URL(value, window.location.origin).origin === window.location.origin ? value : null
  } catch {
    return null
  }
}

function clearStaleLocalSessionHint(): void {
  // AuthKit's browser SDK uses this cookie as a refresh hint. In localhost
  // development it stores the actual refresh token in localStorage. A prior
  // interrupted logout can leave only the hint, which makes SDK initialization
  // post an invalid refresh request with no token on every page load.
  const isLocalDevelopment =
    window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1'
  const refreshTokenKey = `workos:refresh-token:${clientId}`
  if (!isLocalDevelopment || localStorage.getItem(refreshTokenKey)) return

  document.cookie = 'workos-has-session=; Max-Age=0; Path=/; SameSite=Lax'
}

function getClient(): Promise<WorkOSClient> {
  if (!clientId) {
    throw new Error('VITE_WORKOS_CLIENT_ID is not configured. See .env.example.')
  }
  clearStaleLocalSessionHint()
  clientPromise ??= createClient(clientId, {
    redirectUri,
    onRedirectCallback: ({ accessToken, state }) => {
      exchangedAccessToken = accessToken
      exchangedReturnTo = safeLocalReturnTo(state?.returnTo)
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
export async function completeLogin(): Promise<CompletedLogin> {
  const client = await getClient()
  if (exchangedAccessToken) {
    const completedLogin = {
      accessToken: exchangedAccessToken,
      returnTo: exchangedReturnTo,
    }
    exchangedAccessToken = null
    exchangedReturnTo = null
    return completedLogin
  }
  return { accessToken: await client.getAccessToken(), returnTo: null }
}

/**
 * Ends the WorkOS session through a top-level WorkOS logout navigation.
 *
 * WorkOS owns its session cookie. A background cross-origin request cannot
 * reliably clear that cookie in browsers with third-party-cookie protections,
 * so normal and invalid-session logout both navigate at the top level.
 */
export async function signOut(): Promise<boolean> {
  try {
    const client = await getClient()
    client.signOut({
      // This is an already-registered AuthKit logout URI; keeping it queryless
      // avoids a dashboard configuration dependency for local forks.
      returnTo: new URL('/login', window.location.origin).toString(),
    })
    return true
  } catch {
    return false
  }
}

/**
 * End a rejected session through a top-level WorkOS logout navigation.
 *
 * A background logout request can clear this application's refresh token while
 * the WorkOS session cookie survives as a third-party cookie. On the next app
 * load, AuthKit then sees the cookie and attempts a refresh without a token.
 * A browser navigation lets WorkOS clear its cookie before returning to the
 * application's configured invalid-session logout URI.
 */
export async function signOutForInvalidSession(): Promise<boolean> {
  try {
    const client = await getClient()
    client.signOut()
    return true
  } catch {
    return false
  }
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
