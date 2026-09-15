/**
 * Pane harness runner (harness (b)).
 *
 * Standalone, NOT a `mergeConfig` of `vitest.config.ts`: mergeConfig
 * CONCATENATES `include`, which would drag the whole offline unit suite into
 * every harness run. The unit suite must stay offline and deterministic; these
 * specs deliberately talk to a real Tessallite server, so `npx vitest run`
 * never picks them up and this config never picks the unit suite up.
 */
import { defineConfig } from 'vitest/config';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __dirname = dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
      '@tessallite/shared-ui': resolve(__dirname, '../shared-ui/src'),
    },
    dedupe: [
      'react',
      'react-dom',
      '@mui/material',
      '@mui/icons-material',
      '@tanstack/react-query',
      'zustand',
      'dompurify',
      'react-markdown',
      'remark-gfm',
      'echarts',
    ],
  },
  test: {
    environment: 'jsdom',
    include: ['tests-harness/**/*.harness.ts', 'tests-harness/**/*.harness.tsx'],
    setupFiles: ['./src/__tests__/setup.ts'],
    globals: true,
    testTimeout: 180_000,
    hookTimeout: 180_000,
  },
});
