import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';
import { readFileSync, existsSync } from 'fs';
import { homedir } from 'os';
import { join } from 'path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const certDir = join(homedir(), '.office-addin-dev-certs');
const certKey = join(certDir, 'localhost.key');
const certCrt = join(certDir, 'localhost.crt');
const devCerts = existsSync(certKey) && existsSync(certCrt)
  ? { key: readFileSync(certKey), cert: readFileSync(certCrt) }
  : undefined;

export default defineConfig({
  plugins: [react()],
  base: process.env.VITE_BASE_PATH || '/excel-plugin/',
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
      'dompurify',
    ],
  },
  server: {
    port: 3001,
    strictPort: true,
    ...(devCerts ? { https: devCerts } : {}),
    proxy: {
      '/api': {
        target: 'http://localhost:3000',
        changeOrigin: true,
        secure: false,
        rewrite: (path: string) => {
          if (/\/api\/v1\/projects\/[^/]+\/agent/.test(path)) {
            return '/agent' + path;
          }
          return path;
        },
      },
      '/health': {
        target: 'http://localhost:8001',
        changeOrigin: true,
        secure: false,
      },
    },
  },
  preview: {
    port: 3443,
    strictPort: true,
    ...(devCerts ? { https: devCerts } : {}),
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      input: {
        main: resolve(__dirname, 'index.html'),
        functions: resolve(__dirname, 'functions.html'),
      },
    },
  },
});
