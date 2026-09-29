import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The dev server binds to loopback only. The backend (FastAPI) also binds to
// 127.0.0.1 by default; '/api' (including the /api/ws WebSocket) is proxied so
// the browser talks to a single origin and no CORS relaxation is needed.
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8765',
        ws: true,
        changeOrigin: false,
      },
    },
  },
  preview: {
    host: '127.0.0.1',
    port: 4173,
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: true,
    // three.js alone is ~700 kB unminified; the app is local-only so one
    // bundle is acceptable. Raise the warning limit instead of splitting.
    chunkSizeWarningLimit: 1500,
  },
});
