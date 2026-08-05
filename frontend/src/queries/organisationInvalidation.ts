import { watch } from 'vue'
import type { Pinia } from 'pinia'

import { useOrganisationStore } from '@/stores/organisation'

import { queryClient } from './queryClient'

/**
 * Refetch everything scoped to an organisation when the selected organisation
 * changes (v0.3 Scope §6.4, blueprint §14 client-state boundary).
 *
 * Organisation switching is client state in Pinia; the server data that lives
 * under the new organisation belongs to TanStack Vue Query. Every org-scoped
 * query key starts with `['organisations', <orgId>]` (see
 * `src/queries/records.ts`), so one predicate invalidates the whole subtree:
 * active queries refetch immediately and cached entries from other
 * organisations are marked stale.
 *
 * Called once from the app bootstrap (main.ts), after Pinia is installed.
 * Returns a stop handle for test cleanup.
 */
export function installOrganisationSwitchInvalidation(pinia?: Pinia): () => void {
  const organisation = useOrganisationStore(pinia)

  const stop = watch(
    () => organisation.selectedOrganisationId,
    () => {
      void queryClient.invalidateQueries({
        predicate: (query) => query.queryKey[0] === 'organisations',
      })
    },
  )

  return stop
}
