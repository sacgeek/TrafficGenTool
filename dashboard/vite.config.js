import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev server proxies /api and /ws to the FastAPI backend so the React
// dev server and the Python server can run side-by-side without CORS issues.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target:    'http://localhost:8000',
        changeOrigin: true,
      },
      '/ws': {
        target:    'ws://localhost:8000',
        ws:        true,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir:    'dist',
    emptyOutDir: true,
  },
})
