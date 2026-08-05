import { ref } from 'vue'
import { defineStore } from 'pinia'

/**
 * Selected-organisation state (blueprint §14 client-state boundary, Scope §6.3).
 *
 * Holds only the id of the organisation the user acts within. The memberships
 * the selector lists are server data and live in TanStack Vue Query
 * (`useMeQuery`); this store never caches them. The id is persisted to
 * localStorage and hydrated on store creation, then attached to every API
 * request as the `X-Org-Id` header by the client middleware.
 */
const ORGANISATION_STORAGE_KEY = 'app-template:selected-organisation'

function readSelectedOrganisation(): string | null {
  if (typeof localStorage === 'undefined') return null
  return localStorage.getItem(ORGANISATION_STORAGE_KEY)
}

export const useOrganisationStore = defineStore('organisation', () => {
  const selectedOrganisationId = ref<string | null>(readSelectedOrganisation())

  function persistSelection(): void {
    if (typeof localStorage === 'undefined') return
    if (selectedOrganisationId.value === null) {
      localStorage.removeItem(ORGANISATION_STORAGE_KEY)
    } else {
      localStorage.setItem(ORGANISATION_STORAGE_KEY, selectedOrganisationId.value)
    }
  }

  function setSelectedOrganisation(id: string | null) {
    selectedOrganisationId.value = id
    persistSelection()
  }

  return { selectedOrganisationId, setSelectedOrganisation }
})
