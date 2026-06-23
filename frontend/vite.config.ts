import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

/**
 * Bundle splitting strategy (Phase G1 of the known-issues plan).
 *
 * Before splitting the production build was a single 1045 KB JS
 * chunk — every route, every panel, every vendor in one file.
 * Target for G1 is a much smaller initial chunk (under 300 KB of
 * application code + separate vendor chunks the browser can cache
 * across deploys). We achieve this with two complementary changes:
 *
 * 1. `manualChunks` below splits the vendor code into stable
 *    cache-friendly groups. Each group is large enough to be
 *    worth a separate HTTP request and small enough that a
 *    change in one vendor doesn't invalidate the others.
 *
 * 2. Route-level and panel-level `React.lazy()` in the page
 *    components themselves loads heavy panels on demand.
 *    `AggregatesPanel`, `DiagnosticsPanel`, `ModelHealthPanel`,
 *    `LineagePanel`, and friends are only pulled in when the
 *    user actually clicks them open. See `App.tsx` and
 *    `pages/ModelBuilder.tsx`.
 */
export default defineConfig(({ mode }) => {
  // Load .env.* variables (only those with a `VITE_` prefix are exposed
  // to client code; the dev-server config can read any var freely here).
  const env = loadEnv(mode, process.cwd(), "");
  const devPort = Number(env.VITE_DEV_SERVER_PORT) || 5173;
  const proxyTarget = env.VITE_DEV_PROXY_TARGET || "http://localhost:8001";

  return {
  plugins: [react()],
  resolve: {
    alias: {
      "@tessallite/shared-ui": path.resolve(__dirname, "../shared-ui/src"),
    },
    dedupe: [
      "react",
      "react-dom",
      "@mui/material",
      "@mui/icons-material",
      "@tanstack/react-query",
      "zustand",
      "echarts",
      "dompurify",
      "react-markdown",
      "remark-gfm",
    ],
  },
  server: {
    port: devPort,
    proxy: {
      "/api": {
        target: proxyTarget,
        changeOrigin: true,
      },
      "/scheduler": {
        target: env.VITE_SCHEDULER_URL || "http://localhost:8004",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/scheduler/, ""),
      },
      "/optimizer": {
        target: env.VITE_OPTIMIZER_URL || "http://localhost:8003",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/optimizer/, ""),
      },
      "/query-router": {
        target: env.VITE_QUERY_ROUTER_URL || "http://localhost:8002",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/query-router/, ""),
      },
      "/agent": {
        target: env.VITE_AGENT_SERVICE_URL || "http://localhost:8005",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/agent/, ""),
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    // Raise the per-chunk warning limit slightly so a single
    // vendor bundle crossing 500 KB doesn't spam CI output. The
    // split plan keeps every chunk under 700 KB.
    chunkSizeWarningLimit: 700,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes("node_modules")) {
            return undefined;
          }
          // MUI icons dominate the bundle (hundreds of SVG
          // components). Isolate them so the core MUI chunk stays
          // small and only pages that actually render an icon
          // pull the icon chunk.
          if (id.includes("@mui/icons-material")) {
            return "mui-icons";
          }
          // MUI core + emotion styling engine travel together —
          // emotion is imported transitively from almost every
          // MUI component.
          if (
            id.includes("@mui/material") ||
            id.includes("@mui/system") ||
            id.includes("@mui/base") ||
            id.includes("@mui/utils") ||
            id.includes("@mui/private-theming") ||
            id.includes("@mui/styled-engine") ||
            id.includes("@emotion/")
          ) {
            return "mui-core";
          }
          // ReactFlow + dagre — only needed by the Canvas and the
          // Lineage graph. Splitting this out means pages that
          // don't render a graph (Login, SystemAdmin, Explorer
          // index) don't download it.
          if (id.includes("reactflow") || id.includes("@dagrejs/dagre")) {
            return "reactflow-vendor";
          }
          if (
            id.includes("react-router") ||
            id.includes("@remix-run/router")
          ) {
            return "react-router";
          }
          if (id.includes("@tanstack/react-query")) {
            return "react-query";
          }
          if (
            id.includes("/react/") ||
            id.includes("/react-dom/") ||
            id.includes("/scheduler/")
          ) {
            return "react-vendor";
          }
          // Everything else (axios, zustand, fontsource, etc.)
          // falls into a small "vendor" chunk. Keeps individual
          // libraries grouped rather than tangled into the app
          // chunk.
          return "vendor";
        },
      },
    },
  },
  };
});
