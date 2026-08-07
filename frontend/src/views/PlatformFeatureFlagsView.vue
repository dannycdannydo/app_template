<script setup lang="ts">
import { computed } from 'vue'

import type { components } from '@/api/generated/openapi'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { usePlatformFeatureFlagsQuery } from '@/queries/platform'

type PlatformFeatureFlagItem = components['schemas']['PlatformFeatureFlagItem']

/**
 * Feature-flag catalogue (Scope §6.7, acceptance §5.8).
 *
 * Shows every known feature flag at its catalogue default — no organisation
 * context, because the platform plane administers organisations the caller
 * does not belong to. Per-organisation overrides are managed on the
 * organisation detail screen; this view is the read-only catalogue.
 */
const { data, isPending, isError, error } = usePlatformFeatureFlagsQuery(() => null)

const flags = computed<PlatformFeatureFlagItem[]>(() => data.value?.items ?? [])
</script>

<template>
  <div class="space-y-6">
    <div>
      <h1 class="text-2xl font-semibold">Feature flags</h1>
      <p class="text-muted-foreground mt-1 text-sm">
        The platform-controlled organisation feature catalogue. Overrides are managed per
        organisation.
      </p>
    </div>

    <Card>
      <CardHeader>
        <CardTitle>Catalogue</CardTitle>
        <CardDescription>Defaults applied unless an organisation overrides them.</CardDescription>
      </CardHeader>
      <CardContent>
        <div v-if="isPending" class="text-muted-foreground text-sm">Loading flags…</div>
        <p v-else-if="isError" class="text-destructive text-sm">{{ error?.message }}</p>
        <ul v-else class="divide-border divide-y">
          <li v-for="flag in flags" :key="flag.feature_key" class="py-3">
            <div class="flex items-center justify-between gap-4">
              <div>
                <p class="text-sm font-medium">{{ flag.name }}</p>
                <p class="text-muted-foreground text-xs">{{ flag.description }}</p>
              </div>
              <span
                class="border-border text-muted-foreground shrink-0 rounded-md border px-2 py-0.5 text-xs"
                :data-testid="`feature-flag-default-${flag.feature_key}`"
              >
                {{ flag.default_enabled ? 'enabled' : 'disabled' }} by default
              </span>
            </div>
          </li>
        </ul>
      </CardContent>
    </Card>
  </div>
</template>
