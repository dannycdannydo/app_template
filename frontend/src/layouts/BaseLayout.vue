<script setup lang="ts">
import { RouterLink, RouterView } from 'vue-router'
</script>

<template>
  <div class="flex min-h-screen flex-col">
    <header class="border-b bg-background">
      <div class="mx-auto flex h-14 w-full max-w-5xl items-center justify-between px-4">
        <RouterLink to="/" class="font-semibold">app-template</RouterLink>
        <nav class="flex items-center gap-4 text-sm">
          <RouterLink to="/" class="text-muted-foreground hover:text-foreground">Home</RouterLink>
          <RouterLink to="/about" class="text-muted-foreground hover:text-foreground"
            >About</RouterLink
          >
        </nav>
      </div>
    </header>
    <main class="mx-auto w-full max-w-5xl flex-1 px-4 py-8">
      <RouterView />
    </main>
  </div>
</template>
