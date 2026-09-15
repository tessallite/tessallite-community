import { defineConfig } from 'vite';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));

const outDir = process.env.VITE_OUT_DIR || 'dist';

export default defineConfig({
  base: '/excel-plugin/',
  build: {
    // Bug-9889. Same override as vite.config.ts. Without it a test-profile build sends
    // the pane to VITE_OUT_DIR and leaves functions.iife.js behind in dist/,
    // so the served test origin has no functions bundle at all: the manifest's
    // <Script> URL 404s and every TESSALLITE function is #NAME? on the host.
    outDir,
    emptyOutDir: false,
    lib: {
      entry: resolve(__dirname, 'src/functions.ts'),
      name: 'TessalliteFunctions',
      formats: ['iife'],
      fileName: () => 'functions.iife.js',
    },
  },
});
