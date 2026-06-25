import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "path";

export default defineConfig({
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
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    css: false,
    testTimeout: 10000,
  },
});
