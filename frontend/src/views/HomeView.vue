<script setup lang="ts">
import { useHealthQuery } from '@/queries/health'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

const { data, isPending, isError, error, refetch } = useHealthQuery()
</script>

<template>
  <div class="space-y-6">
    <div>
      <h1 class="text-2xl font-semibold">Welcome to app-template</h1>
      <p class="mt-1 text-sm text-muted-foreground">
        A reusable full-stack starter. This page verifies the generated client round-trip against
        the backend health endpoint.
      </p>
    </div>

    <Card>
      <CardHeader>
        <CardTitle>API health</CardTitle>
        <CardDescription>
          Fetched via the generated OpenAPI client through TanStack Vue Query.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <p v-if="isPending" class="text-sm text-muted-foreground">Checking backend…</p>
        <p v-else-if="isError" class="text-sm text-destructive">
          Backend unreachable: {{ error instanceof Error ? error.message : 'unknown error' }}
        </p>
        <p v-else class="text-sm">
          <span class="font-medium">Status:</span>
          <span
            class="ml-1 inline-flex rounded-full bg-primary px-2 py-0.5 text-xs font-medium text-primary-foreground"
          >
            {{ data?.status }}
          </span>
        </p>
      </CardContent>
    </Card>

    <Button variant="outline" size="sm" :disabled="isPending" @click="refetch()">
      Re-check health
    </Button>
  </div>
</template>
