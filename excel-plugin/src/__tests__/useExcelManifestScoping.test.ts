/**
 * Bug-6363 / F-025-16 — hook-level manifest scoping.
 *
 * The staleness scoping fix lives in workbookMetadata (entries are compared only
 * against the model + workbook they were inserted into). But the useExcel insert
 * hooks used to call trackEntityUsage WITHOUT a modelId, so entity-formula and
 * scorecard inserts wrote UNSCOPED manifest entries. Unscoped entries are treated
 * as legacy/global and get flagged "Deleted from source" the moment a different
 * model is loaded.
 *
 * This guards the wiring at the boundary that failed: an entity inserted through
 * the hook while model A is active must be scoped to model A, so loading model B
 * never reports it as deleted.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { renderHook } from '@testing-library/react';
import { useExcel } from '../hooks/useExcel';
import {
  getEntityManifest,
  checkStaleEntities,
  _resetWorkbookIdCache,
} from '../utils/workbookMetadata';

// Minimal Excel range stub: empty target (so the overwrite-confirm is skipped),
// a stable address, and no-op formula/value/format setters.
function makeRange(): Record<string, unknown> {
  return {
    rowIndex: 0,
    columnIndex: 0,
    address: 'Sheet1!A1',
    values: [['']],
    formulas: [['']],
    format: { font: { bold: false }, fill: { color: '' } },
    load: () => {},
  };
}

function makeContext() {
  // Bug-7397 R12-1: the hook now resolves a PINNED target from one host sample
  // and writes through `worksheets.getItem(<pinned sheet>)`, so the stub must
  // expose getItem as well as getActiveWorksheet.
  const sheet = { getRangeByIndexes: () => makeRange(), getRange: () => makeRange() };
  return {
    workbook: {
      worksheets: { getActiveWorksheet: () => sheet, getItem: () => sheet },
      getSelectedRange: () => makeRange(),
      names: { items: [], load: () => {}, add: () => ({ comment: '' }), getItemOrNullObject: () => ({ delete: () => {} }) },
    },
    sync: async () => {},
  };
}

describe('Bug-6363 — useExcel scopes inserted manifest entries to the active model', () => {
  const storage = new Map<string, string>();
  const settingsStore = new Map<string, unknown>();

  beforeEach(() => {
    storage.clear();
    settingsStore.clear();
    _resetWorkbookIdCache();

    vi.stubGlobal('Excel', {
      run: async (cb: (ctx: unknown) => Promise<unknown>) => cb(makeContext()),
    });
    vi.stubGlobal('OfficeRuntime', {
      storage: {
        getItem: async (k: string) => storage.get(k) ?? null,
        setItem: async (k: string, v: string) => { storage.set(k, v); },
        removeItem: async (k: string) => { storage.delete(k); },
      },
    });
    vi.stubGlobal('Office', {
      context: {
        document: {
          settings: {
            get: (k: string) => settingsStore.get(k) ?? null,
            set: (k: string, v: unknown) => { settingsStore.set(k, v); },
            saveAsync: (cb: () => void) => cb(),
          },
        },
      },
    });
  });

  it('tags a named-set insert with the hook-supplied model id', async () => {
    const { result } = renderHook(() => useExcel(undefined, undefined, 'model-A'));

    await result.current.insertNamedSetAsFormulas(
      { id: 'ns-1', name: 'top_products', display_name: 'Top Products', expression: '{...}', updated_at: '2026-01-01' },
      'Tessallite',
      3,
    );

    const manifest = await getEntityManifest();
    const entry = manifest.entries.find(e => e.id === 'ns-1');
    expect(entry?.modelId).toBe('model-A');
    expect(entry?.workbookId).toBeTruthy();
  });

  it('does not report a hook-inserted entity as deleted when a different model is loaded', async () => {
    const { result } = renderHook(() => useExcel(undefined, undefined, 'model-A'));

    await result.current.insertNamedSetAsFormulas(
      { id: 'ns-1', name: 'top_products', display_name: 'Top Products', expression: '{...}', updated_at: '2026-01-01' },
      'Tessallite',
      3,
    );

    // Model B is now loaded with its own entities; the model-A named set must NOT
    // surface as "deleted" — that was the spurious warning the fix removes.
    const stale = await checkStaleEntities(
      [{ id: 'kpi-b', type: 'kpi', certification_status: 'certified' }],
      'model-B',
    );
    expect(stale).toEqual([]);
  });
});
