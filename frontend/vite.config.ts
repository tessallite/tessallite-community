import { defineConfig, loadEnv, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";
import { execSync } from "child_process";
import { readFileSync } from "fs";

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
/**
 * libavoid-js instantiates its WebAssembly binary by resolving the path itself
 * (`libavoid.wasm`). Vite emits that binary hashed, so the runtime's own request
 * misses and a SPA server answers it with index.html — the loader then fails with
 * "expected magic word 00 61 73 6d, found 3c 21 64 6f", because `<!do` is HTML.
 *
 * The URL cannot be supplied from source: libavoid-js declares an `exports` map
 * containing only its root entry, so the WASM cannot be imported with `?url` and passed
 * to `AvoidLib.load(path)`.
 *
 * This emits an unhashed copy beside the hashed asset and at the bundle root, since the
 * loader's resolution base is not observable from source. The bundle keeps referencing
 * the hashed asset, so caching is unchanged; the copies exist only to satisfy the loader.
 */
function emitUnhashedLibavoidWasm(): Plugin {
  return {
    name: "emit-unhashed-libavoid-wasm",
    apply: "build",
    generateBundle(_options, bundle) {
      for (const [fileName, output] of Object.entries(bundle)) {
        if (output.type !== "asset") continue;
        const match = /(^|\/)libavoid-[A-Za-z0-9_-]+\.wasm$/.exec(fileName);
        if (!match) continue;
        const beside = fileName.replace(/libavoid-[A-Za-z0-9_-]+\.wasm$/, "libavoid.wasm");
        for (const target of new Set([beside, "libavoid.wasm"])) {
          this.emitFile({ type: "asset", fileName: target, source: output.source });
        }
      }
    },
  };
}

export default defineConfig(({ mode }) => {
  // Load .env.* variables (only those with a `VITE_` prefix are exposed
  // to client code; the dev-server config can read any var freely here).
  const env = loadEnv(mode, process.cwd(), "");
  const devPort = Number(env.VITE_DEV_SERVER_PORT) || 5173;
  const proxyTarget = env.VITE_DEV_PROXY_TARGET || "http://localhost:8001";

  // Dev-only fallbacks for the login about line (see the `define` block
  // below) — never used for a production build, which must keep degrading
  // to "unknown" when the deploy pipeline forgets to inject the real values.
  let devVersion = "";
  let devCommitHash = "";
  if (mode === "development") {
    try {
      devVersion = JSON.parse(readFileSync(path.resolve(__dirname, "package.json"), "utf-8")).version ?? "";
    } catch {
      devVersion = "";
    }
    try {
      devCommitHash = execSync("git rev-parse HEAD", { cwd: __dirname }).toString().trim();
    } catch {
      devCommitHash = "";
    }
  }

  return {
  // Bug-9558: build-time injection for the login about line. The build/CI
  // environment (or a .env file) supplies VITE_TESSALLITE_VERSION,
  // VITE_DEPLOYMENT_TYPE and VITE_BUILD_COMMIT_HASH for a real release
  // (Community/Enterprise/Cloud Edition); `env` above merges process.env with
  // the .env files, process.env winning. A LOCAL DEV SERVER has none of that
  // deploy plumbing, so it always rendered "unknown" with no type/hash at all
  // — every developer's login screen looked identical to a broken build.
  // Fall back to values derived from the repo itself, ONLY when the operator
  // has not explicitly set the var: package.json's version, the current git
  // commit, and a fixed "Dev Stack" label for a dev-mode build.
  define: {
    "import.meta.env.VITE_TESSALLITE_VERSION": JSON.stringify(
      env.VITE_TESSALLITE_VERSION || (mode === "development" ? devVersion : ""),
    ),
    "import.meta.env.VITE_DEPLOYMENT_TYPE": JSON.stringify(
      env.VITE_DEPLOYMENT_TYPE || (mode === "development" ? "Dev Stack" : ""),
    ),
    "import.meta.env.VITE_BUILD_COMMIT_HASH": JSON.stringify(
      env.VITE_BUILD_COMMIT_HASH || (mode === "development" ? devCommitHash : ""),
    ),
  },

  plugins: [
    react(),
    emitUnhashedLibavoidWasm(),
  ],
  worker: {
    // ES modules: the worker uses `import.meta.url` (via libavoid's loader) and
    // a dynamic import, neither of which is available in an iife worker.
    format: "es",
  },
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
