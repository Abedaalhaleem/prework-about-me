import { defineConfig } from 'vitest/config';

// Tests cover pure logic in src/lib in the node environment (no DOM, no
// WebGL, no network). One smoke test (src/App.smoke.test.tsx) opts into jsdom
// per file to mount the whole app against a fake WebSocket.
export default defineConfig({
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
    reporters: ['default'],
  },
});
