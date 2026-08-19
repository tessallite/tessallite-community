/**
 * F-025-03: persona/profile/model transitions must not leave or re-cache stale
 * governed values. `applyContextTransition` is the single awaited operation the
 * pane runs AFTER persisting the new governed scope. It must:
 *   - clear the local caches,
 *   - await a cross-runtime generation bump (so the separate functions runtime
 *     can only ever observe the new generation alongside the already-persisted
 *     new scope), and
 *   - request a full workbook rebuild so already-inserted cells recompute under
 *     the new scope without the user manually invoking anything.
 *
 * The original defect bumped the generation before the scope was persisted and
 * never requested a rebuild on a persona switch, so cells kept the previous
 * persona's values. These tests pin the two guarantees at the boundary.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

const mockStorage: Record<string, string> = {};

function stubOfficeRuntime() {
  (globalThis as Record<string, unknown>).OfficeRuntime = {
    storage: {
      getItem: async (key: string) => mockStorage[key] ?? null,
      setItem: async (key: string, value: string) => { mockStorage[key] = value; },
      removeItem: async (key: string) => { delete mockStorage[key]; },
    },
  };
}

interface RecalcCapture {
  calls: string[];
}

function stubExcel(capture: RecalcCapture) {
  (globalThis as Record<string, unknown>).Excel = {
    CalculationType: { fullRebuild: 'FullRebuild' },
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    run: (fn: (ctx: any) => Promise<void>) => {
      const context = {
        workbook: {
          application: {
            calculate: (type: string) => { capture.calls.push(type); },
          },
        },
        sync: async () => {},
      };
      return fn(context);
    },
  };
}

beforeEach(() => {
  Object.keys(mockStorage).forEach((k) => delete mockStorage[k]);
  stubOfficeRuntime();
  (globalThis as Record<string, unknown>).CustomFunctions = { associate: () => {} };
});

afterEach(() => {
  delete (globalThis as Record<string, unknown>).Excel;
  vi.resetModules();
});

describe('applyContextTransition (F-025-03)', () => {
  it('bumps the cross-runtime cache generation token', async () => {
    stubExcel({ calls: [] });
    const { applyContextTransition } = await import('../functions');
    const { getCacheGeneration } = await import('../utils/storage');

    expect(await getCacheGeneration()).toBeNull();
    await applyContextTransition();
    expect(await getCacheGeneration()).toBeTruthy();
  });

  it('requests a full workbook rebuild so existing cells recompute', async () => {
    const capture: RecalcCapture = { calls: [] };
    stubExcel(capture);
    const { applyContextTransition } = await import('../functions');

    await applyContextTransition();
    // The rebuild must be requested by the transition itself — not left to the
    // user pressing Ctrl+Alt+F9.
    expect(capture.calls).toContain('FullRebuild');
  });

  it('resolves (does not throw) when the Excel host is unavailable', async () => {
    // No Excel global — pane-only / test host.
    const { applyContextTransition } = await import('../functions');
    await expect(applyContextTransition()).resolves.toBeUndefined();
  });

  it('persists the generation bump BEFORE resolving, so a persist-then-transition caller is race-free', async () => {
    // Guarantee: by the time applyContextTransition resolves, the new generation
    // is already in storage. A caller that persists the new persona first and
    // then awaits this cannot leave the functions runtime seeing a new
    // generation with a stale persona.
    stubExcel({ calls: [] });
    mockStorage['tessallite_active_persona'] = 'persona-restricted';
    const { applyContextTransition } = await import('../functions');
    const { getCacheGeneration } = await import('../utils/storage');

    const before = await getCacheGeneration();
    await applyContextTransition();
    const after = await getCacheGeneration();

    expect(after).toBeTruthy();
    expect(after).not.toBe(before);
    // The already-persisted scope is intact alongside the new generation.
    expect(mockStorage['tessallite_active_persona']).toBe('persona-restricted');
  });
});

describe('refreshCustomFunctionValues (F-025-03: awaitable)', () => {
  it('bumps generation and requests a rebuild, and is awaitable', async () => {
    const capture: RecalcCapture = { calls: [] };
    stubExcel(capture);
    const { refreshCustomFunctionValues } = await import('../functions');
    const { getCacheGeneration } = await import('../utils/storage');

    const result = refreshCustomFunctionValues();
    expect(result).toBeInstanceOf(Promise);
    await result;

    expect(await getCacheGeneration()).toBeTruthy();
    expect(capture.calls).toContain('FullRebuild');
  });
});
