import { defineConfig } from 'vite';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  base: '/excel-plugin/',
  build: {
    outDir: 'dist',
    emptyOutDir: false,
    lib: {
      entry: resolve(__dirname, 'src/functions.ts'),
      name: 'TessalliteFunctions',
      formats: ['iife'],
      fileName: () => 'functions.iife.js',
    },
  },
});
