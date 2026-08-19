/**
 * Bug-8863 — how many vitest workers this suite may use.
 *
 * Extracted from `vitest.config.ts` so the arithmetic is unit-testable without
 * reading `node:os`. See `workerSizing.test.ts` for the properties it must hold.
 */

/**
 * Vitest 1.6's own default worker count in run (non-watch) mode.
 *
 * Source of truth: `node_modules/vitest/dist/vendor/cli-api.*.js`, `createThreadsPool`:
 *   const threadsCount = ctx.config.watch
 *     ? Math.max(Math.floor(numCpus / 2), 1)
 *     : Math.max(numCpus - 1, 1);
 *   const maxThreads = poolOptions.maxThreads ?? ctx.config.maxWorkers ?? threadsCount;
 *
 * It is `cores - 1`, NOT `cores` — vitest already leaves one core for the
 * orchestrator. Getting this constant wrong is how the first version of this
 * cap ended up *raising* parallelism on every machine except the one it was
 * measured on.
 */
export function vitestDefaultMaxThreads(cores: number): number {
  return Math.max(cores - 1, 1);
}

/**
 * Memory budget per worker, in GiB.
 *
 * A worker in this suite is expensive in MEMORY, not just CPU: each test file
 * builds its own jsdom environment and re-executes the app's module graph.
 * Measured peak was ~2.0 GiB RSS across 8 workers (~250 MB each); the rest of
 * the budget is headroom for the jsdom heap churn that makes an
 * under-provisioned box swap rather than test.
 *
 * The value is calibrated, not derived: 1.75 is the budget that reproduces the
 * worker count actually measured as good (4) on the 8-core / 7.5 GiB box where
 * the non-determinism was diagnosed.
 */
export const WORKER_MEMORY_BUDGET_GIB = 1.75;

/**
 * Worker count for the frontend suite.
 *
 * Only ever REDUCES vitest's default, and only on machines short of memory.
 * Core count was never the binding constraint, so throttling a machine with
 * memory to spare would cost wall clock on evidence never gathered there.
 *
 * Measured on the 8-core / 7.5 GiB box, same code, same suite:
 *   7 workers (vitest default): tests 410-548s, collect 374-494s, env 200-263s,
 *                               slowest single test 16.3s
 *   4 workers (this cap):       tests 294-318s, collect 273-322s, env 148-167s,
 *                               slowest single test 8.7-9.5s
 * Fewer workers did MORE useful work per unit of wall clock and cut the
 * slow-test tail by ~1.7x.
 *
 * KNOWN LIMIT (Bug-8886): `totalBytes` comes from `os.totalmem()`, which reports
 * the HOST's memory, not a cgroup limit. Verified — inside
 * `docker run -m 512m node:22-alpine` it still reported the host's 7.52 GiB. In
 * a memory-capped container the memory term therefore never binds and this
 * returns vitest's default unchanged. That is a fail-open: no worse than the
 * suite's behaviour without this cap, but it does mean the cap cannot be relied
 * on inside a constrained container, where the determinism margin rests on
 * `testTimeout` alone.
 */
export function computeMaxTestThreads(cores: number, totalBytes: number): number {
  const memoryBudgetedWorkers = Math.floor(
    totalBytes / 2 ** 30 / WORKER_MEMORY_BUDGET_GIB,
  );
  return Math.max(
    1,
    Math.min(vitestDefaultMaxThreads(cores), memoryBudgetedWorkers),
  );
}
