<script setup lang="ts">
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
</script>

<template>
  <Card>
    <CardHeader>
      <CardTitle>About</CardTitle>
    </CardHeader>
    <CardContent>
      <p class="text-sm text-muted-foreground">
        Frontend foundation for the app-template starter: Vue 3, TypeScript, Tailwind CSS,
        shadcn-vue, Pinia, TanStack Vue Query and a generated OpenAPI client.
      </p>
      <div class="mt-4">
        <Button as-child variant="outline" size="sm">
          <RouterLink to="/">Back home</RouterLink>
        </Button>
      </div>
    </CardContent>
  </Card>
</template>
