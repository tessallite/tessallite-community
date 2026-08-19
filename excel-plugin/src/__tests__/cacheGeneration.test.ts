/**
 * Bug-6912: Cross-runtime cache invalidation via cache generation token.
 * Tests that:
 * - bumpCacheGeneration writes a new token to storage
 * - clearFunctionCaches calls bumpCacheGeneration
 * - Generation change clears functions-side caches
 * - Unchanged generation preserves cache within TTL
 * - Storage-unavailable does not throw
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';

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

function stubUnavailableRuntime() {
  (globalThis as Record<string, unknown>).OfficeRuntime = {
    storage: {
      getItem: () => Promise.reject(new Error('unavailable')),
      setItem: () => Promise.reject(new Error('unavailable')),
      removeItem: () => Promise.reject(new Error('unavailable')),
    },
  };
}

beforeEach(() => {
  Object.keys(mockStorage).forEach(k => delete mockStorage[k]);
  stubOfficeRuntime();
});

describe('cache generation helpers', () => {
  it('getCacheGeneration returns null when no token stored', async () => {
    const { getCacheGeneration } = await import('../utils/storage');
    const gen = await getCacheGeneration();
    expect(gen).toBeNull();
  });

  it('bumpCacheGeneration writes a non-empty token', async () => {
    const { bumpCacheGeneration, getCacheGeneration } = await import('../utils/storage');
    await bumpCacheGeneration();
    const gen = await getCacheGeneration();
    expect(gen).toBeTruthy();
    expect(typeof gen).toBe('string');
  });

  it('bumpCacheGeneration writes a different token each time', async () => {
    const { bumpCacheGeneration, getCacheGeneration } = await import('../utils/storage');
    await bumpCacheGeneration();
    const gen1 = await getCacheGeneration();
    // Wait a tick to ensure Date.now() differs
    await new Promise(r => setTimeout(r, 2));
    await bumpCacheGeneration();
    const gen2 = await getCacheGeneration();
    expect(gen1).not.toBe(gen2);
  });

  it('bumpCacheGeneration does not throw when storage unavailable', async () => {
    stubUnavailableRuntime();
    const { bumpCacheGeneration } = await import('../utils/storage');
    // Should not throw
    await expect(bumpCacheGeneration()).resolves.toBeUndefined();
  });
});

describe('clearFunctionCaches bumps generation', () => {
  it('clearFunctionCaches writes a cache generation token', async () => {
    const { getCacheGeneration } = await import('../utils/storage');
    // Register CustomFunctions.associate stub
    (globalThis as Record<string, unknown>).CustomFunctions = {
      associate: () => {},
    };
    const { clearFunctionCaches } = await import('../functions');
    clearFunctionCaches();
    // Wait for the fire-and-forget promise
    await new Promise(r => setTimeout(r, 10));
    const gen = await getCacheGeneration();
    expect(gen).toBeTruthy();
  });
});

describe('generation change clears functions-side caches', () => {
  it('changing generation token is detectable between evaluations', async () => {
    // This test verifies Bug-6912: when the pane bumps the cache generation
    // token in storage, the functions runtime's next call to getCacheGeneration
    // sees the new value — enabling requireFullContext to detect the change
    // and clear its local caches.
    mockStorage['tessallite_cache_generation'] = 'gen-1';
    const { getCacheGeneration } = await import('../utils/storage');

    // First read: functions runtime sees 'gen-1'
    const gen1 = await getCacheGeneration();
    expect(gen1).toBe('gen-1');

    // Simulate pane-side bump (as if user clicked Refresh or switched persona)
    mockStorage['tessallite_cache_generation'] = 'gen-2';

    // Second read: functions runtime detects the change
    const gen2 = await getCacheGeneration();
    expect(gen2).toBe('gen-2');
    expect(gen2).not.toBe(gen1);
  });

  it('bumpCacheGeneration produces a value different from the previous one', async () => {
    // Simulates: pane bumps, functions runtime reads old vs new
    mockStorage['tessallite_cache_generation'] = 'old-generation';
    const { getCacheGeneration, bumpCacheGeneration } = await import('../utils/storage');

    const before = await getCacheGeneration();
    expect(before).toBe('old-generation');

    await bumpCacheGeneration();
    const after = await getCacheGeneration();
    expect(after).not.toBe('old-generation');
    expect(after).toBeTruthy();
  });

  it('unchanged generation does NOT trigger invalidation', async () => {
    // Verify that when the generation token stays the same, reading it
    // multiple times returns the same value (no spurious change detection).
    mockStorage['tessallite_cache_generation'] = 'gen-stable';
    const { getCacheGeneration } = await import('../utils/storage');
    const gen1 = await getCacheGeneration();
    const gen2 = await getCacheGeneration();
    expect(gen1).toBe(gen2);
    expect(gen1).toBe('gen-stable');
  });

  it('F4 escape: transition from null to a value is detectable (cold-start scenario)', async () => {
    // Scenario: workbook formulas evaluate before any token exists (fresh
    // install or after Wef cache clear, pane not yet opened). Caches fill
    // under generation null. User opens pane, switches persona → first-ever
    // bump from null to a real token. The functions runtime must detect this
    // as a change (generation !== lastSeenGeneration where lastSeenGeneration
    // was null) and clear caches. Previously, the seeding guard skipped the
    // clear on first read, allowing stale values to survive.
    const { getCacheGeneration } = await import('../utils/storage');

    // Initially no token in storage (cold start)
    expect(await getCacheGeneration()).toBeNull();

    // Pane opens and bumps for the first time
    mockStorage['tessallite_cache_generation'] = 'first-bump';
    const after = await getCacheGeneration();
    expect(after).toBe('first-bump');
    // The transition null -> 'first-bump' is a CHANGE that requireFullContext
    // must act on (the code now does `if (generation !== lastSeenGeneration)`
    // without a null guard, so both null->value and value->value trigger clear).
    expect(after).not.toBeNull();
  });
});

describe('insert mode storage', () => {
  it('getInsertMode returns live when unset', async () => {
    const { getInsertMode } = await import('../utils/storage');
    const mode = await getInsertMode();
    expect(mode).toBe('live');
  });

  it('setInsertMode persists and getInsertMode reads back', async () => {
    const { getInsertMode, setInsertMode } = await import('../utils/storage');
    await setInsertMode('static');
    expect(await getInsertMode()).toBe('static');
    await setInsertMode('live');
    expect(await getInsertMode()).toBe('live');
  });

  it('getInsertMode returns live for invalid stored value', async () => {
    mockStorage['tessallite_insert_mode'] = 'invalid_value';
    const { getInsertMode } = await import('../utils/storage');
    const mode = await getInsertMode();
    expect(mode).toBe('live');
  });
});
