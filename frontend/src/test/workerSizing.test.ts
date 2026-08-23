import { describe, it, expect } from "vitest";
import {
  computeMaxTestThreads,
  vitestDefaultMaxThreads,
  WORKER_MEMORY_BUDGET_GIB,
} from "./workerSizing";

const GIB = 2 ** 30;

/**
 * Bug-8863 — the frontend worker cap must only ever REDUCE parallelism.
 *
 * The first version of this cap was written against the belief that vitest's
 * default `maxThreads` is `availableParallelism()`. It is `cores - 1` in run
 * mode, so that version silently RAISED the worker count on every machine
 * shape except the 8-core box it was measured on — including the CI runner —
 * while its inline documentation claimed it changed nothing there. These tests
 * fail against that expression and pass against the corrected one.
 */
describe("frontend vitest worker cap (Bug-8863)", () => {
  it("matches vitest 1.6 run-mode default arithmetic", () => {
    // Pinned against node_modules/vitest/.../cli-api.*.js createThreadsPool:
    //   Math.max(numCpus - 1, 1)
    expect(vitestDefaultMaxThreads(1)).toBe(1);
    expect(vitestDefaultMaxThreads(2)).toBe(1);
    expect(vitestDefaultMaxThreads(4)).toBe(3);
    expect(vitestDefaultMaxThreads(8)).toBe(7);
    expect(vitestDefaultMaxThreads(16)).toBe(15);
  });

  it.each([
    [1, 2],
    [2, 7],
    [2, 8],
    [4, 16],
    [8, 7.5],
    [16, 32],
    [64, 64],
  ])(
    "never exceeds the vitest default on %i cores / %s GiB",
    (cores, gib) => {
      expect(computeMaxTestThreads(cores, gib * GIB)).toBeLessThanOrEqual(
        vitestDefaultMaxThreads(cores),
      );
    },
  );

  it("leaves a well-provisioned machine at vitest's default", () => {
    // Memory is not the binding constraint here, so the cap must not engage.
    expect(computeMaxTestThreads(4, 16 * GIB)).toBe(vitestDefaultMaxThreads(4));
    expect(computeMaxTestThreads(16, 32 * GIB)).toBe(vitestDefaultMaxThreads(16));
  });

  it("throttles the memory-starved box the flake was measured on", () => {
    // 8 cores / 7.52 GiB: the configuration proven deterministic across three
    // consecutive full runs (slowest test 8.7-9.5s against a 30s ceiling).
    expect(computeMaxTestThreads(8, 7.52 * GIB)).toBe(4);
  });

  it("throttles hard when memory is scarce relative to cores", () => {
    expect(computeMaxTestThreads(8, 2 * GIB)).toBe(1);
  });

  it("never returns less than one worker", () => {
    expect(computeMaxTestThreads(1, 0.5 * GIB)).toBeGreaterThanOrEqual(1);
    expect(computeMaxTestThreads(1, 0)).toBeGreaterThanOrEqual(1);
  });

  it("keeps the budget constant positive so the memory term cannot invert", () => {
    expect(WORKER_MEMORY_BUDGET_GIB).toBeGreaterThan(0);
  });
});
