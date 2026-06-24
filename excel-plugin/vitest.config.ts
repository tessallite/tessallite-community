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
      'zustand',
      'dompurify',
      'react-markdown',
      'remark-gfm',
      'echarts',
    ],
  },
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
    setupFiles: ['./src/__tests__/setup.ts'],
    globals: true,
  },
});
