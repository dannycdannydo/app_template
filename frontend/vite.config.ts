import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import vueDevTools from 'vite-plugin-vue-devtools'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue(), vueDevTools()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      // Backend API during local development. The generated client uses a
      // relative base URL, so all API traffic goes through this proxy.
      '/health': 'http://localhost:8000',
      '/ready': 'http://localhost:8000',
      '/api': 'http://localhost:8000',
    },
  },
})
