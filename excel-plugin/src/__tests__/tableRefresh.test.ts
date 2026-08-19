/**
 * Task 2: Table refresh — pure logic tests for metadata filtering,
 * model-mismatch skip, column-drift skip, agent-source detection, and
 * header parsing. Fixtures use the REAL producer format (JSON.stringify([...])).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  filterRefreshableTables,
  validateTableContext,
  validateColumnHeaders,
  parseStoredColumnHeaders,
  isAgentSourcedQuery,
  refreshTables,
} from '../utils/tableRefresh';
import { invalidateMetadataCache } from '../utils/workbookMetadata';
import { strings } from '../i18n/strings';

describe('filterRefreshableTables', () => {
  it('returns only tables with semanticQuery + projectId + modelId', () => {
    const tables = [
      { name: 'Table1', rangeAddress: 'Sheet1!A1:D10', metadata: { semanticQuery: '{}', projectId: 'p1', modelId: 'm1' } },
      { name: 'Table2', rangeAddress: 'Sheet1!A12:D20', metadata: { projectId: 'p1', modelId: 'm1' } },
      { name: 'Table3', rangeAddress: 'Sheet1!A22:D30', metadata: { semanticQuery: '{}', projectId: 'p1' } },
      { name: 'Table4', rangeAddress: 'Sheet1!A32:D40', metadata: {} },
    ];
    const result = filterRefreshableTables(tables);
    expect(result).toHaveLength(1);
    expect(result[0].name).toBe('Table1');
  });

  it('returns empty array when no tables have metadata', () => {
    const tables = [
      { name: 'Table1', rangeAddress: 'Sheet1!A1:D10', metadata: {} },
    ];
    expect(filterRefreshableTables(tables)).toHaveLength(0);
  });
});

describe('validateTableContext', () => {
  it('returns null when project and model match', () => {
    const result = validateTableContext(
      { projectId: 'proj-1', modelId: 'model-1' },
      'proj-1',
      'model-1',
    );
    expect(result).toBeNull();
  });

  it('returns reason when project mismatches', () => {
    const result = validateTableContext(
      { projectId: 'proj-DIFFERENT', modelId: 'model-1' },
      'proj-1',
      'model-1',
    );
    expect(result).toContain('different project');
  });

  it('returns reason when model mismatches', () => {
    const result = validateTableContext(
      { projectId: 'proj-1', modelId: 'model-DIFFERENT' },
      'proj-1',
      'model-1',
    );
    expect(result).toContain('different model');
  });
});

describe('parseStoredColumnHeaders', () => {
  it('parses valid JSON array of strings (the real producer format)', () => {
    const stored = JSON.stringify(['Region', 'Revenue', 'Cost']);
    const result = parseStoredColumnHeaders(stored);
    expect(result).toEqual(['Region', 'Revenue', 'Cost']);
  });

  it('returns null for corrupted data', () => {
    expect(parseStoredColumnHeaders('not-json')).toBeNull();
  });

  it('returns null for non-array JSON', () => {
    expect(parseStoredColumnHeaders('"just a string"')).toBeNull();
  });

  it('returns null for array of non-strings', () => {
    expect(parseStoredColumnHeaders('[1, 2, 3]')).toBeNull();
  });

  it('handles headers with commas and quotes (would break split-based parse)', () => {
    const stored = JSON.stringify(['Region, Area', 'Revenue "Gross"', 'Cost']);
    const result = parseStoredColumnHeaders(stored);
    expect(result).toEqual(['Region, Area', 'Revenue "Gross"', 'Cost']);
  });
});

describe('validateColumnHeaders', () => {
  it('returns null when display headers match exactly', () => {
    const result = validateColumnHeaders(
      ['Region', 'Revenue', 'Cost'],
      ['Region', 'Revenue', 'Cost'],
    );
    expect(result).toBeNull();
  });

  it('returns reason when column count differs', () => {
    const result = validateColumnHeaders(
      ['Region', 'Revenue'],
      ['Region', 'Revenue', 'Cost'],
    );
    expect(result).toContain('Column count changed');
  });

  it('returns reason when a column name drifts', () => {
    const result = validateColumnHeaders(
      ['Region', 'Total Revenue', 'Cost'],
      ['Region', 'Revenue', 'Cost'],
    );
    expect(result).toContain('Revenue');
    expect(result).toContain('Total Revenue');
    expect(result).toContain('schema drift');
  });

  it('catches the real producer/consumer scenario (F1 regression guard)', () => {
    // The producer stores headers as JSON.stringify(string[]), the consumer
    // must JSON.parse before comparing. This test would have caught the
    // original bug (split-based parse on a JSON string).
    const storedRaw = JSON.stringify(['Country', 'Base Amount']);
    const parsed = parseStoredColumnHeaders(storedRaw);
    expect(parsed).not.toBeNull();
    const result = validateColumnHeaders(['Country', 'Base Amount'], parsed!);
    expect(result).toBeNull();
  });
});

describe('isAgentSourcedQuery', () => {
  it('detects agent-chat provenance objects', () => {
    const agentQuery = { source: 'agent', conversation_id: 'conv-1', message_id: 'msg-1' };
    expect(isAgentSourcedQuery(agentQuery)).toBe(true);
  });

  it('detects objects without a measures array as non-executable', () => {
    const incomplete = { project_id: 'p1' };
    expect(isAgentSourcedQuery(incomplete)).toBe(true);
  });

  it('passes valid semantic queries through', () => {
    const validQuery = { measures: ['revenue'], dimensions: ['region'] };
    expect(isAgentSourcedQuery(validQuery)).toBe(false);
  });

  it('handles null/undefined gracefully', () => {
    expect(isAgentSourcedQuery(null)).toBe(false);
    expect(isAgentSourcedQuery(undefined)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Bug-8424: a table whose provenance could not be READ is reported, not dropped.
//
// Driven through the real refreshTables -> refreshTable -> getTableMetadata
// path. The host answers the table ENUMERATION and fails the metadata read,
// which is exactly the transient shape the defect describes.
// ---------------------------------------------------------------------------
describe('Bug-8424 — a failed metadata read produces an honest skip reason', () => {
  beforeEach(() => {
    invalidateMetadataCache();
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    const mem = new Map<string, string>([
      ['tessallite_project_id', 'proj-1'],
      ['tessallite_model_id', 'model-1'],
    ]);
    vi.stubGlobal('OfficeRuntime', {
      storage: {
        getItem: async (k: string) => mem.get(k) ?? null,
        setItem: async (k: string, v: string) => { mem.set(k, v); },
        removeItem: async (k: string) => { mem.delete(k); },
      },
    });
    vi.stubGlobal('Excel', {
      run: async (cb: (ctx: unknown) => Promise<unknown>) => {
        const range = { address: 'Sheet1!A1:D10', load: () => {} };
        const sheet = {
          tables: { items: [{ name: 'Table1', getRange: () => range }], load: () => {} },
        };
        const context = {
          workbook: {
            worksheets: { getActiveWorksheet: () => sheet, getItem: () => sheet },
            tables: sheet.tables,
            // getTableMetadata is the only caller that reads workbook.names.
            get names(): never { throw new Error('transient host error'); },
          },
          sync: async () => {},
        };
        return cb(context);
      },
    });
  });

  afterEach(() => {
    vi.stubGlobal('Excel', undefined);
    vi.restoreAllMocks();
  });

  it('skips with a stated reason instead of vanishing from the result', async () => {
    const result = await refreshTables('activeSheet');
    // Pre-fix: `skipped` was empty and the table simply disappeared from the
    // count, so the user saw "0 of 1 refreshed" with nothing explaining it.
    expect(result.refreshed).toEqual([]);
    expect(result.skipped).toEqual([
      { name: 'Table1', reason: strings.tableRefresh.metadataFetchFailedSkip },
    ]);
  });

  it('the reason is a real user-facing string, not a bare code', () => {
    expect(strings.tableRefresh.metadataFetchFailedSkip).toMatch(/could not read/i);
    expect(strings.tableRefresh.metadataFetchFailedSkip).not.toMatch(/_/);
  });
});
