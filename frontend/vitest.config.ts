import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "path";
import { availableParallelism, totalmem } from "node:os";
import { computeMaxTestThreads } from "./src/test/workerSizing";

// Bug-8863 — worker sizing. Rationale, the measured figures, and the known
// cgroup limitation live in `src/test/workerSizing.ts`; the arithmetic is
// pinned by `src/test/workerSizing.test.ts`, whose central property is that
// this cap may only ever REDUCE vitest's own default, never raise it.
//
// An explicit override is available for tuning a specific runner without
// editing this file:
//   TESSALLITE_VITEST_MAX_THREADS=6 npm test
const MAX_TEST_THREADS =
  Number(process.env.TESSALLITE_VITEST_MAX_THREADS) ||
  computeMaxTestThreads(availableParallelism(), totalmem());

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
    poolOptions: {
      // `minThreads: 1` lets tinypool start small and scale up to maxThreads on
      // queued work, rather than pre-spawning the full pool. Vitest's own
      // run-mode default for BOTH bounds is `cores - 1`, so this lowers the warm
      // floor as well as the ceiling. It was part of every measured run behind
      // the determinism evidence and showed no cost, but unlike maxThreads it is
      // not pinned by `workerSizing.test.ts` — see Bug-8891.
      threads: { minThreads: 1, maxThreads: MAX_TEST_THREADS },
    },
    // Bug-8863 — calibrated, not guessed.
    //
    // The panel suites contain multi-step user-journey tests that do real React
    // work: opening a dialog, walking a wizard, driving MUI selects. In
    // isolation the slowest of them take 1-6s. Under full-suite parallel load
    // the SAME tests take 2.2x-4.4x longer (measured: 4.56s -> 10.22s,
    // 3.69s -> 16.26s). The previous 10000 ceiling sat inside that variance
    // band, so whether a run was green depended on machine load rather than on
    // the code — two runs of identical code produced different failure sets.
    //
    // Nothing hangs: every one of those tests completes. Three of them ran
    // 12.4s/15.7s/16.3s and PASSED in the same run, because they carried
    // hand-added per-test overrides; the tests that flaked were simply the ones
    // nobody had bumped yet. No unhandled rejection, no hanging-process report,
    // and a DOM probe confirmed React Testing Library cleanup returns the body
    // to zero nodes between tests.
    //
    // 30000 is ~3.2x the slowest test across the three consecutive full runs
    // that established determinism under this worker sizing (8.70s / 9.47s /
    // 8.89s), and still low enough that a genuinely hung test fails fast. It is
    // a single visible policy replacing eight scattered per-test overrides.
    //
    // The trade is deliberate and logged as Bug-8884: a test that later
    // regresses from 6s to 25s now passes silently. The 10000 ceiling was never
    // a performance gate either — eight per-test overrides had already hollowed
    // it out — so this loses no guard that was actually working, but the gap is
    // real and Bug-8884 proposes the tail-margin check that would close it.
    testTimeout: 30000,
    // The remaining two ceilings sit in the same contention band, so leaving
    // either at its 10000 default would just move the flake rather than clear
    // it. Neither has been observed timing out; both are raised for consistency
    // with the calibrated value above, not on separate measurements.
    hookTimeout: 30000,
    teardownTimeout: 30000,
  },
});
