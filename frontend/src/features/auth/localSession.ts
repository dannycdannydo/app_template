import { queryClient } from '@/queries/queryClient'
import { useOrganisationStore } from '@/stores/organisation'
import { useSessionStore } from '@/stores/session'

/**
 * Clear every application-owned piece of authenticated browser state.
 *
 * WorkOS owns its session and refresh cookie; its adapter performs the remote
 * logout. This function owns the complementary local boundary and is safe to
 * call before a top-level navigation or when that navigation cannot start.
 * Server state must not survive for a later user in the same tab.
 */
export function clearLocalSession(): void {
  useSessionStore().clearSession()
  useOrganisationStore().setSelectedOrganisation(null)
  queryClient.clear()
}
