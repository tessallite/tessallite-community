import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { existsSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  checkStaleEntities, updateManifestStatuses, trackEntityUsage,
  hashSheetName, quoteSheetRef, getEntityManifest, _resetWorkbookIdCache,
  encodeMetadataEntry, reassembleChunkedMetadata, MAX_COMMENT_LENGTH,
  splitByCodePoints, setTableMetadata, setTableMetadataWithinLock, getTableMetadata,
  isMetadataFetchFailure,
  invalidateMetadataCache, withTableLocksKeys, blockKeysForAddress,
  type EntityManifest, type StaleEntity,
} from '../utils/workbookMetadata';

const mockStorage = new Map<string, string>();

vi.stubGlobal('OfficeRuntime', {
  storage: {
    getItem: vi.fn((key: string) => Promise.resolve(mockStorage.get(key) ?? null)),
    setItem: vi.fn((key: string, value: string) => {
      mockStorage.set(key, value);
      return Promise.resolve();
    }),
  },
});

beforeEach(() => {
  mockStorage.clear();
  _resetWorkbookIdCache();
});

describe('checkStaleEntities', () => {
  it('returns empty when manifest is empty', async () => {
    const result = await checkStaleEntities([
      { id: 'a', type: 'kpi', certification_status: 'certified' },
    ]);
    expect(result).toEqual([]);
  });

  it('detects deprecated entities', async () => {
    const manifest: EntityManifest = {
      version: 1,
      entries: [{
        id: 'kpi-1',
        type: 'kpi',
        displayName: 'Revenue KPI',
        certificationStatus: 'certified',
        insertedAt: '2026-01-01T00:00:00Z',
        cellLocations: ['Sheet1!A1'],
      }],
    };
    mockStorage.set('tessallite_entity_manifest', JSON.stringify(manifest));

    const result = await checkStaleEntities([
      { id: 'kpi-1', type: 'kpi', certification_status: 'deprecated' },
    ]);
    expect(result).toHaveLength(1);
    expect(result[0].reason).toBe('deprecated');
    expect(result[0].entry.displayName).toBe('Revenue KPI');
  });

  it('detects deleted entities', async () => {
    const manifest: EntityManifest = {
      version: 1,
      entries: [{
        id: 'ns-1',
        type: 'named_set',
        displayName: 'Top Products',
        certificationStatus: 'certified',
        insertedAt: '2026-01-01T00:00:00Z',
        cellLocations: ['Sheet1!B2'],
      }],
    };
    mockStorage.set('tessallite_entity_manifest', JSON.stringify(manifest));

    const result = await checkStaleEntities([]);
    expect(result).toHaveLength(1);
    expect(result[0].reason).toBe('deleted');
  });

  it('detects status changes', async () => {
    const manifest: EntityManifest = {
      version: 1,
      entries: [{
        id: 'kpi-2',
        type: 'kpi',
        displayName: 'Cost KPI',
        certificationStatus: 'certified',
        insertedAt: '2026-01-01T00:00:00Z',
        cellLocations: ['Sheet1!C3'],
      }],
    };
    mockStorage.set('tessallite_entity_manifest', JSON.stringify(manifest));

    const result = await checkStaleEntities([
      { id: 'kpi-2', type: 'kpi', certification_status: 'draft' },
    ]);
    expect(result).toHaveLength(1);
    expect(result[0].reason).toBe('status_changed');
  });

  it('returns empty when nothing changed', async () => {
    const manifest: EntityManifest = {
      version: 1,
      entries: [{
        id: 'kpi-3',
        type: 'kpi',
        displayName: 'Profit KPI',
        certificationStatus: 'certified',
        insertedAt: '2026-01-01T00:00:00Z',
        cellLocations: ['Sheet1!D4'],
      }],
    };
    mockStorage.set('tessallite_entity_manifest', JSON.stringify(manifest));

    const result = await checkStaleEntities([
      { id: 'kpi-3', type: 'kpi', certification_status: 'certified' },
    ]);
    expect(result).toEqual([]);
  });

  it('handles mixed stale and current entries', async () => {
    const manifest: EntityManifest = {
      version: 1,
      entries: [
        {
          id: 'kpi-ok', type: 'kpi', displayName: 'OK KPI',
          certificationStatus: 'certified', insertedAt: '2026-01-01T00:00:00Z',
          cellLocations: ['Sheet1!A1'],
        },
        {
          id: 'ns-deprecated', type: 'named_set', displayName: 'Old Set',
          certificationStatus: 'shared', insertedAt: '2026-01-01T00:00:00Z',
          cellLocations: ['Sheet1!B1'],
        },
        {
          id: 'ns-deleted', type: 'named_set', displayName: 'Gone Set',
          certificationStatus: 'certified', insertedAt: '2026-01-01T00:00:00Z',
          cellLocations: ['Sheet1!C1'],
        },
      ],
    };
    mockStorage.set('tessallite_entity_manifest', JSON.stringify(manifest));

    const result = await checkStaleEntities([
      { id: 'kpi-ok', type: 'kpi', certification_status: 'certified' },
      { id: 'ns-deprecated', type: 'named_set', certification_status: 'deprecated' },
    ]);
    expect(result).toHaveLength(2);
    const reasons = result.map((s: StaleEntity) => s.reason).sort();
    expect(reasons).toEqual(['deleted', 'deprecated']);
  });

  it('detects version changes via updated_at', async () => {
    const manifest: EntityManifest = {
      version: 1,
      entries: [{
        id: 'ns-1',
        type: 'named_set',
        displayName: 'My Set',
        certificationStatus: 'certified',
        updatedAt: '2026-01-01T00:00:00Z',
        insertedAt: '2026-01-01T00:00:00Z',
        cellLocations: ['Sheet1!A1'],
      }],
    };
    mockStorage.set('tessallite_entity_manifest', JSON.stringify(manifest));

    const result = await checkStaleEntities([
      { id: 'ns-1', type: 'named_set', certification_status: 'certified', updated_at: '2026-02-01T00:00:00Z' },
    ]);
    expect(result).toHaveLength(1);
    expect(result[0].reason).toBe('version_changed');
  });

  it('does not flag version_changed when updated_at matches', async () => {
    const manifest: EntityManifest = {
      version: 1,
      entries: [{
        id: 'kpi-1',
        type: 'kpi',
        displayName: 'Revenue',
        certificationStatus: 'certified',
        updatedAt: '2026-01-01T12:00:00Z',
        insertedAt: '2026-01-01T00:00:00Z',
        cellLocations: ['Sheet1!B2'],
      }],
    };
    mockStorage.set('tessallite_entity_manifest', JSON.stringify(manifest));

    const result = await checkStaleEntities([
      { id: 'kpi-1', type: 'kpi', certification_status: 'certified', updated_at: '2026-01-01T12:00:00Z' },
    ]);
    expect(result).toEqual([]);
  });

  it('skips version check when manifest entry has no updatedAt', async () => {
    const manifest: EntityManifest = {
      version: 1,
      entries: [{
        id: 'ns-old',
        type: 'named_set',
        displayName: 'Legacy Set',
        certificationStatus: 'certified',
        insertedAt: '2026-01-01T00:00:00Z',
        cellLocations: ['Sheet1!A1'],
      }],
    };
    mockStorage.set('tessallite_entity_manifest', JSON.stringify(manifest));

    const result = await checkStaleEntities([
      { id: 'ns-old', type: 'named_set', certification_status: 'certified', updated_at: '2026-05-01T00:00:00Z' },
    ]);
    expect(result).toEqual([]);
  });
});

describe('updateManifestStatuses', () => {
  it('updates certificationStatus for all entries', async () => {
    const manifest: EntityManifest = {
      version: 1,
      entries: [{
        id: 'kpi-1', type: 'kpi', displayName: 'Revenue',
        certificationStatus: 'draft', insertedAt: '2026-01-01T00:00:00Z',
        cellLocations: ['Sheet1!A1'],
      }],
    };
    mockStorage.set('tessallite_entity_manifest', JSON.stringify(manifest));

    await updateManifestStatuses([
      { id: 'kpi-1', type: 'kpi', certification_status: 'certified', updated_at: '2026-02-01T00:00:00Z' },
    ]);

    const updated: EntityManifest = JSON.parse(mockStorage.get('tessallite_entity_manifest')!);
    expect(updated.entries[0].certificationStatus).toBe('certified');
    expect(updated.entries[0].updatedAt).toBe('2026-02-01T00:00:00Z');
  });

  it('preserves stale updatedAt when entry key is in skipUpdatedAtKeys', async () => {
    const manifest: EntityManifest = {
      version: 1,
      entries: [{
        id: 'ns-1', type: 'named_set', displayName: 'My Set',
        certificationStatus: 'certified', updatedAt: '2026-01-01T00:00:00Z',
        insertedAt: '2026-01-01T00:00:00Z', cellLocations: ['Sheet1!A1'],
      }],
    };
    mockStorage.set('tessallite_entity_manifest', JSON.stringify(manifest));

    const skipKeys = new Set(['named_set:ns-1']);
    await updateManifestStatuses(
      [{ id: 'ns-1', type: 'named_set', certification_status: 'certified', updated_at: '2026-03-01T00:00:00Z' }],
      skipKeys,
    );

    const updated: EntityManifest = JSON.parse(mockStorage.get('tessallite_entity_manifest')!);
    expect(updated.entries[0].updatedAt).toBe('2026-01-01T00:00:00Z');
    expect(updated.entries[0].certificationStatus).toBe('certified');
  });

  it('preserves stale updatedAt even when reason is status_changed (combined status+version change)', async () => {
    const manifest: EntityManifest = {
      version: 1,
      entries: [{
        id: 'kpi-cert', type: 'kpi', displayName: 'Certified KPI',
        certificationStatus: 'certified', updatedAt: '2026-01-01T00:00:00Z',
        insertedAt: '2026-01-01T00:00:00Z', cellLocations: ['Sheet1!A1'],
      }],
    };
    mockStorage.set('tessallite_entity_manifest', JSON.stringify(manifest));

    const entities = [
      { id: 'kpi-cert', type: 'kpi' as const, certification_status: 'draft', updated_at: '2026-03-01T00:00:00Z' },
    ];
    const stale = await checkStaleEntities(entities);
    expect(stale).toHaveLength(1);
    expect(stale[0].reason).toBe('status_changed');

    const skipKeys = new Set<string>();
    for (const s of stale) {
      const key = `${s.entry.type}:${s.entry.id}`;
      const current = entities.find(e => e.id === s.entry.id && e.type === s.entry.type);
      if (s.entry.updatedAt && current?.updated_at && s.entry.updatedAt !== current.updated_at) {
        skipKeys.add(key);
      }
    }
    await updateManifestStatuses(entities, skipKeys);

    const updated: EntityManifest = JSON.parse(mockStorage.get('tessallite_entity_manifest')!);
    expect(updated.entries[0].certificationStatus).toBe('draft');
    expect(updated.entries[0].updatedAt).toBe('2026-01-01T00:00:00Z');

    const secondCheck = await checkStaleEntities(entities);
    expect(secondCheck).toHaveLength(1);
  });

  it('version_changed entry stays stale across check-then-update cycle', async () => {
    const manifest: EntityManifest = {
      version: 1,
      entries: [{
        id: 'kpi-2', type: 'kpi', displayName: 'Cost',
        certificationStatus: 'certified', updatedAt: '2026-01-01T00:00:00Z',
        insertedAt: '2026-01-01T00:00:00Z', cellLocations: ['Sheet1!B2'],
      }],
    };
    mockStorage.set('tessallite_entity_manifest', JSON.stringify(manifest));

    const entities = [
      { id: 'kpi-2', type: 'kpi' as const, certification_status: 'certified', updated_at: '2026-04-01T00:00:00Z' },
    ];
    const stale = await checkStaleEntities(entities);
    expect(stale).toHaveLength(1);
    expect(stale[0].reason).toBe('version_changed');

    const skipKeys = new Set(
      stale.filter(s => s.reason === 'version_changed').map(s => `${s.entry.type}:${s.entry.id}`),
    );
    await updateManifestStatuses(entities, skipKeys);

    const secondCheck = await checkStaleEntities(entities);
    expect(secondCheck).toHaveLength(1);
    expect(secondCheck[0].reason).toBe('version_changed');
  });
});

describe('trackEntityUsage', () => {
  it('creates a new manifest entry with updatedAt', async () => {
    await trackEntityUsage('kpi', 'kpi-new', 'New KPI', 'Sheet1!C3', 'certified', '2026-05-01T00:00:00Z');

    const manifest: EntityManifest = JSON.parse(mockStorage.get('tessallite_entity_manifest')!);
    expect(manifest.entries).toHaveLength(1);
    expect(manifest.entries[0].id).toBe('kpi-new');
    expect(manifest.entries[0].updatedAt).toBe('2026-05-01T00:00:00Z');
    expect(manifest.entries[0].cellLocations).toEqual(['Sheet1!C3']);
  });

  it('appends cell location to existing entry', async () => {
    await trackEntityUsage('named_set', 'ns-1', 'Set A', 'Sheet1!A1', 'certified', '2026-01-01T00:00:00Z');
    await trackEntityUsage('named_set', 'ns-1', 'Set A', 'Sheet1!D4', 'certified', '2026-01-01T00:00:00Z');

    const manifest: EntityManifest = JSON.parse(mockStorage.get('tessallite_entity_manifest')!);
    expect(manifest.entries).toHaveLength(1);
    expect(manifest.entries[0].cellLocations).toEqual(['Sheet1!A1', 'Sheet1!D4']);
  });

  it('updates updatedAt on existing entry', async () => {
    await trackEntityUsage('kpi', 'kpi-1', 'Rev', 'Sheet1!A1', 'certified', '2026-01-01T00:00:00Z');
    await trackEntityUsage('kpi', 'kpi-1', 'Rev', 'Sheet1!A1', 'certified', '2026-03-01T00:00:00Z');

    const manifest: EntityManifest = JSON.parse(mockStorage.get('tessallite_entity_manifest')!);
    expect(manifest.entries[0].updatedAt).toBe('2026-03-01T00:00:00Z');
  });
});

// F-025-19: named-item keys must be legal Excel identifiers and sheet refs
// must be quoted when they contain spaces/punctuation.
describe('F-025-19 — sheet-name sanitisation', () => {
  it('hashSheetName yields a legal identifier (letter-led, no spaces/punctuation)', () => {
    for (const name of ['Q1 Report', 'It_s_a_Sheet', "O'Brien", 'Sheet 1', '財務']) {
      const token = hashSheetName(name);
      expect(token).toMatch(/^s[a-z0-9]+$/);
      // no underscores in the token so the slow-path key split stays unambiguous
      expect(token.includes('_')).toBe(false);
    }
  });

  it('hashSheetName is deterministic and distinguishes different sheets', () => {
    expect(hashSheetName('Q1 Report')).toBe(hashSheetName('Q1 Report'));
    expect(hashSheetName('Q1 Report')).not.toBe(hashSheetName('Q2 Report'));
  });

  it('quoteSheetRef leaves a simple sheet name unquoted', () => {
    expect(quoteSheetRef('Sheet1', 'A1:D10')).toBe('Sheet1!A1:D10');
  });

  it('quoteSheetRef quotes a sheet name containing a space', () => {
    expect(quoteSheetRef('Q1 Report', 'A1:D10')).toBe("'Q1 Report'!A1:D10");
  });

  it('quoteSheetRef doubles an embedded single quote', () => {
    expect(quoteSheetRef("O'Brien", 'A1')).toBe("'O''Brien'!A1");
  });
});

// F-025-16: the manifest is machine-global; staleness must be scoped to the
// active workbook + model so switching models / workbooks does not flag
// healthy entities as "Deleted from server".
describe('F-025-16 — workbook + model scoping', () => {
  const settingsStore = new Map<string, unknown>();
  beforeEach(() => {
    settingsStore.clear();
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

  it('does not flag a different model\'s entity as deleted', async () => {
    // KPI inserted from model A.
    await trackEntityUsage('kpi', 'kpi-A', 'Revenue', 'Sheet1!A1', 'certified', '2026-01-01', 'model-A');
    // Now the loaded model is model B with its own (different) entities.
    const stale = await checkStaleEntities(
      [{ id: 'kpi-B', type: 'kpi', certification_status: 'certified' }],
      'model-B',
    );
    // The model-A entry must NOT appear as "deleted" — it belongs to model A.
    expect(stale).toEqual([]);
  });

  it('still flags a genuinely deleted entity within the same model', async () => {
    await trackEntityUsage('kpi', 'kpi-A', 'Revenue', 'Sheet1!A1', 'certified', '2026-01-01', 'model-A');
    const stale = await checkStaleEntities(
      [{ id: 'kpi-other', type: 'kpi', certification_status: 'certified' }],
      'model-A',
    );
    expect(stale).toHaveLength(1);
    expect(stale[0].reason).toBe('deleted');
    expect(stale[0].entry.id).toBe('kpi-A');
  });

  it('tags new manifest entries with the workbook id and model id', async () => {
    await trackEntityUsage('kpi', 'kpi-A', 'Revenue', 'Sheet1!A1', 'certified', '2026-01-01', 'model-A');
    const manifest = await getEntityManifest();
    const entry = manifest.entries.find(e => e.id === 'kpi-A');
    expect(entry?.modelId).toBe('model-A');
    expect(entry?.workbookId).toBeTruthy();
  });
});

describe('Bug-7397: metadata comment length (chunking round-trip)', () => {
  // Mirror how getTableMetadata reads named-item comments into a raw map.
  function readRaw(entries: { suffix: string; comment: string }[]): Record<string, string> {
    const raw: Record<string, string> = {};
    for (const { comment } of entries) {
      const eqIdx = comment.indexOf('=');
      if (eqIdx > 0) raw[comment.slice(0, eqIdx)] = comment.slice(eqIdx + 1);
    }
    return raw;
  }

  it('keeps a short value as a single unchunked comment (backward compatible)', () => {
    const entries = encodeMetadataEntry('projectId', 'proj-123');
    expect(entries).toEqual([{ suffix: 'projectId', comment: 'projectId=proj-123' }]);
  });

  it('chunks an over-length value into a name-keyed head + chunks, all within the cap', () => {
    const big = JSON.stringify({ q: 'x'.repeat(1200), filters: Array.from({ length: 20 }, (_, i) => i) });
    expect(`semanticQuery=${big}`.length).toBeGreaterThan(MAX_COMMENT_LENGTH);
    const entries = encodeMetadataEntry('semanticQuery', big);
    // Head item is keyed by NAME (`<key>__chunks`), not a value sentinel, and
    // there is no plain `semanticQuery` item in the chunked form.
    expect(entries[0].suffix).toBe('semanticQuery__chunks');
    expect(entries[0].comment).toBe(`semanticQuery__chunks=${entries.length - 1}`);
    expect(entries.some((e) => e.suffix === 'semanticQuery')).toBe(false);
    for (const { comment } of entries) {
      expect(comment.length).toBeLessThanOrEqual(MAX_COMMENT_LENGTH);
    }
  });

  it('round-trips an over-length value through encode -> read -> reassemble', () => {
    const big = JSON.stringify({ q: 'y'.repeat(2000), m: ['revenue', 'orders'], d: ['region', 'month'] });
    const raw = readRaw(encodeMetadataEntry('semanticQuery', big));
    // Before reassembly the raw map carries the name-keyed head + chunk keys,
    // and NO plain `semanticQuery` yet.
    expect(raw['semanticQuery']).toBeUndefined();
    expect(raw['semanticQuery__chunks']).toBeTruthy();
    expect(raw['semanticQuery__c0']).toBeTruthy();
    reassembleChunkedMetadata(raw);
    expect(raw['semanticQuery']).toBe(big);
    // Head + chunk keys are cleaned up; JSON.parse (refresh/drill) succeeds.
    expect(raw['semanticQuery__chunks']).toBeUndefined();
    expect(raw['semanticQuery__c0']).toBeUndefined();
    expect(JSON.parse(raw['semanticQuery'])).toHaveProperty('q');
  });

  it('splitByCodePoints never emits a lone surrogate, regardless of boundary position', () => {
    // Directly exercise the splitter with a SMALL size and an emoji straddling
    // every possible boundary offset (envelope-independent, so a future size
    // change cannot silently de-align this guard). A naive value.slice() would
    // cut the pair at an odd offset and emit a lone surrogate.
    for (let prefix = 0; prefix <= 6; prefix++) {
      const value = 'a'.repeat(prefix) + '\u{1F600}' + 'b'.repeat(6);
      const chunks = splitByCodePoints(value, 5);
      for (const chunk of chunks) {
        expect(() => encodeURIComponent(chunk)).not.toThrow(); // throws on lone surrogate
      }
      expect(chunks.join('')).toBe(value);
    }
  });

  it('encodeMetadataEntry keeps a boundary-straddling emoji intact end to end', () => {
    // Build a value long enough to chunk, with an emoji placed at an ODD offset
    // relative to the internal chunk size so a naive slice would split it.
    const size = MAX_COMMENT_LENGTH - ('semanticQuery'.length + 3 + 6 + 1); // mirror encode envelope
    const value = 'a'.repeat(size - 1) + '\u{1F600}' + 'b'.repeat(size);
    const entries = encodeMetadataEntry('semanticQuery', value);
    for (const { comment } of entries) {
      expect(() => encodeURIComponent(comment)).not.toThrow();
    }
    const raw = readRaw(entries);
    reassembleChunkedMetadata(raw);
    expect(raw['semanticQuery']).toBe(value);
  });

  it('drops orphan chunk keys (does NOT leak or persist) when a chunk is missing', () => {
    const big = JSON.stringify({ q: 'z'.repeat(1500) });
    const entries = encodeMetadataEntry('semanticQuery', big);
    // Drop one chunk to simulate a partially-deleted named-item set.
    const damaged = entries.filter((e) => e.suffix !== 'semanticQuery__c1');
    const raw = readRaw(damaged);
    reassembleChunkedMetadata(raw);
    // Must NOT collapse to '' (would break JSON.parse); stays absent.
    expect('semanticQuery' in raw).toBe(false);
    // And the surviving orphan chunk keys must be removed so they neither leak
    // to callers nor get re-persisted by the refresh path (permanent loss).
    expect(Object.keys(raw).some((k) => k.startsWith('semanticQuery__c'))).toBe(false);
    expect(Object.keys(raw).some((k) => k.endsWith('__chunks'))).toBe(false);
  });

  it('drops orphan chunk keys when the __chunks HEAD is missing (symmetric case)', () => {
    const big = JSON.stringify({ q: 'w'.repeat(1500) });
    const entries = encodeMetadataEntry('semanticQuery', big);
    // Simulate the head named-item being lost but the chunk items surviving.
    const damaged = entries.filter((e) => e.suffix !== 'semanticQuery__chunks');
    const raw = readRaw(damaged);
    expect(Object.keys(raw).some((k) => k.startsWith('semanticQuery__c'))).toBe(true);
    reassembleChunkedMetadata(raw);
    // No orphan chunk key survives to leak / be re-persisted; base stays absent.
    expect('semanticQuery' in raw).toBe(false);
    expect(Object.keys(raw).some((k) => /__c\d+$/.test(k))).toBe(false);
  });

  it('drops orphan chunk keys when the __chunks head parses non-numeric', () => {
    const raw = { semanticQuery__chunks: 'not-a-number', semanticQuery__c0: 'x', projectId: 'p1' };
    reassembleChunkedMetadata(raw);
    expect('semanticQuery' in raw).toBe(false);
    expect(Object.keys(raw).some((k) => /__c\d+$/.test(k))).toBe(false);
    expect(raw['projectId']).toBe('p1'); // unrelated keys untouched
  });

  it('leaves non-chunked keys untouched and is idempotent', () => {
    const raw = { projectId: 'p1', modelId: 'm1', semanticQuery: '{"q":1}' };
    reassembleChunkedMetadata(raw);
    reassembleChunkedMetadata(raw); // idempotent
    expect(raw).toEqual({ projectId: 'p1', modelId: 'm1', semanticQuery: '{"q":1}' });
  });

  it('Bug-7397 race: a newer plain value from a concurrent writer wins over stale chunk debris (root-cause regression guard)', () => {
    // Concrete two-writer interleaving from the deep-review finding: writer A
    // persists a long query as `semanticQuery__chunks=N` + `semanticQuery__cI`
    // chunks. setTableMetadata snapshots existing names at its FIRST
    // context.sync() and only deletes/adds at a SECOND context.sync(); writer
    // B's snapshot predates A's adds, so B's delete pass misses A's names and
    // B then adds a plain, NEWER `semanticQuery=<value>` item for the same
    // table. Both now coexist in the raw map getTableMetadata reads back.
    const staleBig = JSON.stringify({ q: 'stale'.repeat(200) });
    const staleEntries = encodeMetadataEntry('semanticQuery', staleBig);
    expect(staleEntries.length).toBeGreaterThan(1); // sanity: it actually chunked

    const raw: Record<string, string> = {};
    for (const { comment } of staleEntries) {
      const eqIdx = comment.indexOf('=');
      raw[comment.slice(0, eqIdx)] = comment.slice(eqIdx + 1);
    }
    // Writer B's newer, short, unchunked value for the SAME base key.
    const newerValue = '{"q":"new-and-correct"}';
    raw['semanticQuery'] = newerValue;

    reassembleChunkedMetadata(raw);

    // The newer plain value must survive -- a revert to unconditional
    // reassembly would silently overwrite it with the stale (older) chunked
    // value, causing Refresh to re-execute the wrong query with no error.
    expect(raw['semanticQuery']).toBe(newerValue);
    // The now-orphaned chunk/head debris from writer A must not leak or be
    // re-persisted by the refresh path.
    expect(Object.keys(raw).some((k) => /__c\d+$/.test(k))).toBe(false);
    expect(Object.keys(raw).some((k) => k.endsWith('__chunks'))).toBe(false);
  });

  it('treats __chunks=0 as an invalid head (skips reassembly, leaves the key absent) instead of collapsing to an empty string', () => {
    const raw: Record<string, string> = { semanticQuery__chunks: '0', projectId: 'p1' };
    reassembleChunkedMetadata(raw);
    // Must NOT collapse to '' -- that breaks JSON.parse on the refresh/drill
    // path, contradicting the module's own documented invariant.
    expect('semanticQuery' in raw).toBe(false);
    expect(raw['projectId']).toBe('p1'); // unrelated keys untouched
  });

  it('an incomplete chunk set with a corrupt/huge parsed count is cleaned up WITHOUT scanning the full parsed count (hang-vector guard)', () => {
    // A Proxy counts every delete issued against the raw map. The redundant
    // `for (let i = 0; i < count; i++) delete raw[...]` loop that used to run
    // in the incomplete-chunk branch would issue one delete per unit of the
    // PARSED (possibly corrupt/huge) count, regardless of how many chunk items
    // actually exist. The orphan sweep it was replaced by only ever touches
    // keys that are ACTUALLY present, so the delete count stays tiny no matter
    // how large the malformed count value is.
    let deleteCount = 0;
    const target: Record<string, string> = {
      semanticQuery__chunks: '1000000', // corrupt/huge parsed count
      semanticQuery__c0: 'only-chunk-actually-present',
      projectId: 'p1',
    };
    const raw = new Proxy(target, {
      deleteProperty(obj, prop) {
        deleteCount++;
        return Reflect.deleteProperty(obj, prop as string);
      },
    }) as Record<string, string>;

    reassembleChunkedMetadata(raw);

    // Orphans are still fully cleaned up...
    expect('semanticQuery' in raw).toBe(false);
    expect(Object.keys(raw).some((k) => /__c\d+$/.test(k))).toBe(false);
    expect(raw['projectId']).toBe('p1');
    // ...but bounded by what's actually present (head + the one real chunk),
    // never by the corrupt parsed count.
    expect(deleteCount).toBeLessThan(10);
  });

  it('round-trips several over-length fields together without cross-contamination', () => {
    const q = JSON.stringify({ q: 'a'.repeat(900) });
    const cols = JSON.stringify(Array.from({ length: 60 }, (_, i) => `column_header_${i}`));
    const raw = readRaw([
      ...encodeMetadataEntry('semanticQuery', q),
      ...encodeMetadataEntry('columnHeaders', cols),
      { suffix: 'projectId', comment: 'projectId=p1' },
    ]);
    reassembleChunkedMetadata(raw);
    expect(raw['semanticQuery']).toBe(q);
    expect(raw['columnHeaders']).toBe(cols);
    expect(raw['projectId']).toBe('p1');
  });
});

describe('Bug-7397: setTableMetadata -> getTableMetadata wiring (real Excel path)', () => {
  // A stateful in-memory names collection shared across set/get so the encode
  // (write) and reassemble (read) paths are exercised through the real
  // functions, not a re-implemented parse loop.
  interface FakeNamedItem { name: string; comment: string; visible?: boolean }
  let items: FakeNamedItem[];

  function makeContext() {
    return {
      workbook: {
        names: {
          items,
          load: () => {},
          add: (name: string) => {
            const item: FakeNamedItem = { name, comment: '', visible: true };
            items.push(item);
            return item;
          },
          getItemOrNullObject: (name: string) => ({
            delete: () => { items = items.filter((i) => i.name !== name); },
          }),
        },
      },
      sync: async () => {},
    };
  }

  beforeEach(() => {
    items = [];
    invalidateMetadataCache();
    vi.stubGlobal('Excel', {
      run: async (cb: (ctx: unknown) => Promise<unknown>) => cb(makeContext()),
    });
  });

  afterEach(() => {
    vi.stubGlobal('Excel', undefined);
  });

  it('persists and reads back an over-length semanticQuery with no chunk leakage', async () => {
    const big = JSON.stringify({ measures: ['revenue'], filters: Array.from({ length: 40 }, (_, i) => `region_${i}`), note: 'q'.repeat(1500) });
    expect(`semanticQuery=${big}`.length).toBeGreaterThan(MAX_COMMENT_LENGTH);

    await setTableMetadata('Sheet1!A1:D10', {
      projectId: 'proj-1',
      modelId: 'model-1',
      semanticQuery: big,
      pluginVersion: '1.0.0',
      timestamp: '2026-07-27T00:00:00Z',
    });

    // Provenance items are hidden (Name Manager clutter guard).
    expect(items.length).toBeGreaterThan(1);
    expect(items.every((i) => i.visible === false)).toBe(true);

    const meta = await getTableMetadata('Sheet1!A1:D10');
    expect(meta.semanticQuery).toBe(big);
    expect(JSON.parse(meta.semanticQuery!)).toHaveProperty('note');
    expect(meta.projectId).toBe('proj-1');
    // No chunk/head keys leak into the returned metadata.
    expect(Object.keys(meta).some((k) => k.includes('__c') || k.endsWith('__chunks'))).toBe(false);
  });

  it('reassembles a chunked value via the SLOW path (cell inside the table, not the start cell)', async () => {
    const big = JSON.stringify({ measures: ['revenue'], note: 'q'.repeat(1500) });
    await setTableMetadata('Sheet1!A1:D10', {
      projectId: 'proj-3',
      modelId: 'model-3',
      semanticQuery: big,
      pluginVersion: '1.0.0',
      timestamp: '2026-07-27T00:00:00Z',
    });
    invalidateMetadataCache();
    // C5 is INSIDE A1:D10 but is not the start cell -> range-containment slow path.
    const meta = await getTableMetadata('Sheet1!C5');
    expect(meta.semanticQuery).toBe(big);
    expect(JSON.parse(meta.semanticQuery!)).toHaveProperty('note');
    expect(Object.keys(meta).some((k) => k.includes('__c') || k.endsWith('__chunks'))).toBe(false);
  });

  it('reads back a short (unchunked) value unchanged', async () => {
    await setTableMetadata('Sheet1!A1:B2', {
      projectId: 'proj-2',
      modelId: 'model-2',
      semanticQuery: '{"measures":["orders"]}',
      pluginVersion: '1.0.0',
      timestamp: '2026-07-27T00:00:00Z',
    });
    invalidateMetadataCache();
    const meta = await getTableMetadata('Sheet1!A1:B2');
    expect(meta.semanticQuery).toBe('{"measures":["orders"]}');
    expect(meta.modelId).toBe('model-2');
  });
});

describe('Bug-7397 F-1/F-2: two-writer race through the real setTableMetadata pipeline (write-lock regression guard)', () => {
  // Unlike the simple shared-array mock above (fine when only one Excel.run is
  // ever in flight at a time), THIS mock must faithfully separate "what a
  // context can see" from "what is actually committed", or two concurrently
  // running writers would accidentally observe each other's uncommitted state
  // through JS object aliasing -- masking the exact race this suite exists to
  // prove. Each context gets its OWN local pending add/delete queue; its
  // FIRST sync() snapshots `serverItems` (mirrors Office.js: loaded
  // properties reflect host state as of that sync, not any later host
  // state); its SECOND sync() is the only point queued deletes/adds are
  // applied to the shared `serverItems`, becoming visible to OTHER contexts
  // (mirrors Office.js: writes only take effect once THAT context syncs).
  // `add()` throws SYNCHRONOUSLY on a name that already exists server-side or
  // in this context's own pending adds. Bug-7397 F-4 (deep-review finding):
  // this is a SIMPLIFICATION, not a claim about real Office.js -- the real
  // API queues `add()` as a batched command and only reports a duplicate-name
  // failure when that batch's `context.sync()` executes, which would reject
  // the WHOLE batch (losing every entry queued in it, not just the colliding
  // one) rather than letting the production code's per-add try/catch skip
  // just that one entry. That per-add try/catch is therefore closer to
  // decorative than a real per-entry recovery for a genuine duplicate-name
  // race in production. This mock's simplification does not change the
  // verdict of the tests below: because real Office.js loses the WHOLE batch
  // rather than one entry on a collision, the real failure mode this mock
  // stands in for is at least as bad as what it demonstrates, so the
  // mutation-provable assertions in this describe block hold a fortiori.
  interface FakeNamedItem { name: string; comment: string; visible?: boolean }
  let serverItems: FakeNamedItem[];
  // Bug-7397 F-1 round-3 follow-up: lets ONE specific upcoming Excel.run call
  // (consumed on first use, then cleared) have its FIRST sync() take its
  // snapshot immediately (so it reflects genuinely current data) but stall
  // the CALLER's continuation until the test releases it -- modelling a read
  // whose own processing/host round trip is slow enough that another writer
  // fully commits and invalidates while the read is still in flight.
  let gateNextContextFirstSync: Promise<void> | null = null;

  function makeIsolatedContext() {
    const gate = gateNextContextFirstSync;
    gateNextContextFirstSync = null;
    let snapshot: FakeNamedItem[] = [];
    const pendingDeletes = new Set<string>();
    const pendingAdds: FakeNamedItem[] = [];
    let hasSyncedOnce = false;
    return {
      workbook: {
        names: {
          get items() { return snapshot; },
          load: () => {},
          add: (name: string) => {
            // A name already scheduled for deletion in THIS same batch is not
            // a real collision -- the production code always queues its
            // delete()s before its add()s (see setTableMetadata), and Excel
            // applies queued operations in order within one sync, so the
            // delete frees the name before the add claims it.
            const existsOnServer = serverItems.some((i) => i.name === name) &&
              !pendingDeletes.has(name);
            const exists = existsOnServer || pendingAdds.some((i) => i.name === name);
            if (exists) throw new Error(`Name '${name}' already exists on the workbook.`);
            const item: FakeNamedItem = { name, comment: '', visible: true };
            pendingAdds.push(item);
            return item;
          },
          getItemOrNullObject: (name: string) => ({
            delete: () => { pendingDeletes.add(name); },
          }),
        },
      },
      sync: async () => {
        if (!hasSyncedOnce) {
          snapshot = serverItems.map((i) => ({ ...i })); // capture NOW -- genuinely current data
          hasSyncedOnce = true;
          if (gate) await gate; // stall the CALLER, not the snapshot's accuracy
          return;
        }
        serverItems = [
          ...serverItems.filter((i) => !pendingDeletes.has(i.name)),
          ...pendingAdds,
        ];
        pendingDeletes.clear();
        pendingAdds.length = 0;
      },
    };
  }

  beforeEach(() => {
    serverItems = [];
    gateNextContextFirstSync = null;
    invalidateMetadataCache();
    vi.stubGlobal('Excel', {
      run: async (cb: (ctx: unknown) => Promise<unknown>) => cb(makeIsolatedContext()),
    });
  });

  afterEach(() => {
    vi.stubGlobal('Excel', undefined);
  });

  function readRawFromServer(): Record<string, string> {
    const raw: Record<string, string> = {};
    for (const item of serverItems) {
      if (item.name.endsWith('__table_range')) continue;
      const eqIdx = item.comment.indexOf('=');
      if (eqIdx > 0) raw[item.comment.slice(0, eqIdx)] = item.comment.slice(eqIdx + 1);
    }
    return raw;
  }

  it('two concurrent writers for the SAME table (plain writer last): the write lock delivers a CLEAN win, not just a coincidentally-right value', async () => {
    // Bug-7397 F-3 (deep-review finding): the `semanticQuery` assertion alone
    // is NOT a write-lock guard for THIS ordering -- round-1's "plain value
    // wins" reassembler heuristic already happens to return the right
    // `semanticQuery` here even WITHOUT the lock (it is the mirrored ordering,
    // tested below, where that heuristic is provably wrong). What the lock
    // adds for THIS ordering is a genuinely CLEAN write: without it, writer
    // 2's read pre-dates writer 1's commit, so writer 2 never deletes writer
    // 1's already-committed `pluginVersion`/`timestamp`/`__table_range` names
    // -- writer 2's attempts to re-add those exact names collide and are
    // silently dropped, leaving writer 1's STALE `timestamp` in place even
    // though writer 2 (the later writer) supplied a newer one. The `timestamp`
    // assertion below is what actually distinguishes "lock" from "no lock" in
    // this ordering.
    const staleBig = 'A'.repeat(2000); // chunks (~9 pieces)
    const newerShort = '{"q":"new-and-correct"}'; // stays a single plain item
    // Neither call is awaited before the other starts -- a real race, not a
    // hand-sequenced one.
    const p1 = setTableMetadata('Sheet1!A1:D10', {
      semanticQuery: staleBig, pluginVersion: '1.0.0', timestamp: '2026-07-27T00:00:00Z',
    });
    const p2 = setTableMetadata('Sheet1!A1:D10', {
      semanticQuery: newerShort, pluginVersion: '1.0.0', timestamp: '2026-07-27T00:00:01Z',
    });
    await Promise.all([p1, p2]);

    const raw = readRawFromServer();
    reassembleChunkedMetadata(raw);
    expect(raw['semanticQuery']).toBe(newerShort);
    expect(raw['timestamp']).toBe('2026-07-27T00:00:01Z'); // writer 2's own timestamp, not writer 1's stale one
    expect(Object.keys(raw).some((k) => /__c\d+$/.test(k) || k.endsWith('__chunks'))).toBe(false);
  });

  it('Bug-7397 F-1 mutation-proof: two concurrent writers, CHUNKED writer submitted LAST -- must win, not the plain FIRST writer', async () => {
    // This is the mirrored interleaving from the deep-review finding: without
    // the write lock, reassembleChunkedMetadata's "plain value wins" rule
    // would keep the FIRST writer's plain value even though the SECOND
    // (chunked) writer ran later -- silently returning the wrong, stale
    // query. The write lock must force writer 1 to fully complete (including
    // deleting its own plain name) before writer 2's read even happens, so
    // writer 2 always wins regardless of value shape.
    const firstPlain = '{"q":"first-and-stale"}';
    const secondBig = 'B'.repeat(2000); // chunks (~9 pieces)
    const p1 = setTableMetadata('Sheet1!A1:D10', {
      semanticQuery: firstPlain, pluginVersion: '1.0.0', timestamp: '2026-07-27T00:00:00Z',
    });
    const p2 = setTableMetadata('Sheet1!A1:D10', {
      semanticQuery: secondBig, pluginVersion: '1.0.0', timestamp: '2026-07-27T00:00:01Z',
    });
    await Promise.all([p1, p2]);

    const raw = readRawFromServer();
    reassembleChunkedMetadata(raw);
    // Reverting the write lock (calling Excel.run directly, without
    // withRangeWriteLock) reproduces the exact race: writer 2's read would
    // land before writer 1 commits, so writer 2 never deletes writer 1's
    // plain name; both coexist, and the reassembler's plain-wins rule then
    // returns `firstPlain` here instead of `secondBig`.
    expect(raw['semanticQuery']).toBe(secondBig);
    expect(raw['semanticQuery']).not.toBe(firstPlain);
  });

  it('Bug-7397 F-2 mutation-proof: two concurrent CHUNKED writers for the SAME table never mix into a corrupted value', async () => {
    // Both writers chunk. Without the write lock, writer 2's chunk names
    // collide with writer 1's already-committed same-named items (Excel
    // named items must be unique) -- writer 2's adds are silently dropped by
    // the per-add try/catch, and the table is left with writer 1's STALE
    // value even though writer 2 ran later, with zero error surfaced.
    const olderLonger = 'A'.repeat(2000); // more chunks than newerShorter
    const newerShorter = 'B'.repeat(600); // fewer chunks -- every one of its
    // names collides with an already-occupied index from the older writer.
    const p1 = setTableMetadata('Sheet1!A1:D10', {
      semanticQuery: olderLonger, pluginVersion: '1.0.0', timestamp: '2026-07-27T00:00:00Z',
    });
    const p2 = setTableMetadata('Sheet1!A1:D10', {
      semanticQuery: newerShorter, pluginVersion: '1.0.0', timestamp: '2026-07-27T00:00:01Z',
    });
    await Promise.all([p1, p2]);

    const raw = readRawFromServer();
    reassembleChunkedMetadata(raw);
    // The write lock forces writer 1 to fully complete (and be superseded --
    // its stale names deleted) before writer 2 starts, so writer 2's value is
    // whole and uncorrupted, never a splice of both writers' chunks.
    expect(raw['semanticQuery']).toBe(newerShorter);
    expect(raw['semanticQuery']).not.toBe(olderLonger);
  });

  it('Bug-7397 F-1 mutation-proof: a read landing between two serialized writes does not poison the cache with a value the later write can no longer clear', async () => {
    // Round-2 deep-review finding: the write lock DEFERS writer 2 behind
    // writer 1 for the same table -- widening the real-time window in which a
    // getTableMetadata read can land AFTER writer 1 commits but BEFORE writer
    // 2 does. If the cache were invalidated up front (at the moment
    // setTableMetadata is CALLED, before either write even runs) rather than
    // after the write actually SETTLES, that read would populate the cache
    // with writer 1's now-superseded value, and nothing would be left to
    // clear it once writer 2 finally commits -- Refresh/Drill would then
    // silently use the stale value for up to METADATA_CACHE_TTL_MS.
    const p1 = setTableMetadata('Sheet1!A1:D10', {
      semanticQuery: '{"q":"FIRST"}', pluginVersion: '1.0.0', timestamp: '2026-07-27T00:00:00Z',
    });
    const p2 = setTableMetadata('Sheet1!A1:D10', {
      semanticQuery: '{"q":"SECOND-and-final"}', pluginVersion: '1.0.0', timestamp: '2026-07-27T00:00:01Z',
    });
    await p1; // writer 1 has committed; writer 2 is still queued behind the lock
    const midRead = await getTableMetadata('Sheet1!A1:D10'); // populates the TTL cache
    expect(midRead.semanticQuery).toBe('{"q":"FIRST"}'); // correct AT THIS MOMENT -- writer 2 has not committed yet
    await p2; // writer 2 now commits its final value

    const afterRead = await getTableMetadata('Sheet1!A1:D10');
    // Must reflect writer 2's commit -- NOT the mid-write value this read's
    // own cache entry was populated with a moment earlier.
    expect(afterRead.semanticQuery).toBe('{"q":"SECOND-and-final"}');
  });

  it('Bug-7397 F-2 mutation-proof: a slow-path (mid-table-cell) cache entry is invalidated by a write to the table, even though the write used a different address string', async () => {
    // getTableMetadata caches under whatever literal address the CALLER
    // queried -- including a mid-table cell resolved via the range-
    // containment slow path (e.g. cellContext.ts reading the selected cell).
    // setTableMetadata only ever knows the ONE address it itself was called
    // with (the table's start cell), so invalidating by that exact string
    // alone can never reach a slow-path entry cached under a DIFFERENT
    // address for the very same table.
    await setTableMetadata('Sheet1!A1:D10', {
      semanticQuery: '{"q":"V1"}', pluginVersion: '1.0.0', timestamp: '2026-07-27T00:00:00Z',
    });
    // C5 is inside A1:D10 but is not the start cell -> slow (containment)
    // path; caches under the literal 'Sheet1!C5' string.
    const midTableRead = await getTableMetadata('Sheet1!C5');
    expect(midTableRead.semanticQuery).toBe('{"q":"V1"}');

    await setTableMetadata('Sheet1!A1:D10', {
      semanticQuery: '{"q":"V2"}', pluginVersion: '1.0.0', timestamp: '2026-07-27T00:00:01Z',
    });

    // The mid-table-cell cache entry must be invalidated too -- it is the
    // SAME table, just addressed differently by the two calls.
    const midTableReadAfter = await getTableMetadata('Sheet1!C5');
    expect(midTableReadAfter.semanticQuery).toBe('{"q":"V2"}');
    const startCellRead = await getTableMetadata('Sheet1!A1:D10');
    expect(startCellRead.semanticQuery).toBe('{"q":"V2"}');
  });

  it('Bug-7397 R6: a read that STARTS before a write but RESOLVES after it must not POISON the cache (admission guard preserved)', async () => {
    // R6 no longer relies on read-time staleness DETECTION (rounds 1-5's
    // approach, rejected 3x). Correctness now comes from the per-table lock:
    // a refresh reads metadata INSIDE the table's lock, so no write can commit
    // mid-read. This test pins the remaining cache-correctness property for
    // reads NOT under the lock (e.g. cellContext reading a cell during a
    // selection change): a read whose own host round trip resolves AFTER a
    // concurrent write must NOT be admitted into the cache as a stale entry
    // that a later reader would serve. (The epoch admission guard in
    // cacheMetadata enforces this; the returned value of the stalled read
    // itself is irrelevant because nothing acts on an unlocked read.)
    await setTableMetadata('Sheet1!A1:D10', {
      semanticQuery: '{"q":"V1"}', pluginVersion: '1.0.0', timestamp: '2026-07-27T00:00:00Z',
    });

    let releaseRead: () => void = () => {};
    gateNextContextFirstSync = new Promise<void>((resolve) => { releaseRead = resolve; });
    const stalledRead = getTableMetadata('Sheet1!A1:D10');

    await setTableMetadata('Sheet1!A1:D10', {
      semanticQuery: '{"q":"V2-committed-while-read-in-flight"}', pluginVersion: '1.0.0', timestamp: '2026-07-27T00:00:01Z',
    });

    releaseRead();
    await stalledRead; // this stalled read must NOT be cached (it is stale by construction)

    // A subsequent read must reflect V2, proving the stalled read did not
    // poison the cache with the superseded V1 value.
    const freshRead = await getTableMetadata('Sheet1!A1:D10');
    expect(freshRead.semanticQuery).toBe('{"q":"V2-committed-while-read-in-flight"}');
  });

  it('Bug-7397 R9: block locks serialize OVERLAPPING cells and run DISJOINT cells concurrently (mutation: bypassing the lock interleaves)', async () => {
    // The primary correctness mechanism. Two critical sections over overlapping
    // cells must not overlap in time; two over disjoint cells must not block.
    const events: string[] = [];
    const makeSection = (label: string, delayMs: number) => async () => {
      events.push(`${label}:enter`);
      await new Promise((r) => setTimeout(r, delayMs));
      events.push(`${label}:exit`);
    };

    // OVERLAPPING cells (A1:D10 vs $A$1, same block): B must wait for A.
    const a = withTableLocksKeys(blockKeysForAddress('Sheet1!A1:D10'), makeSection('A', 20));
    const b = withTableLocksKeys(blockKeysForAddress('Sheet1!$A$1'), makeSection('B', 0)); // $-stripped -> same block as A
    await Promise.all([a, b]);
    expect(events).toEqual(['A:enter', 'A:exit', 'B:enter', 'B:exit']);

    // DISJOINT cells (different sheet -> different block hash): they overlap.
    events.length = 0;
    const d = withTableLocksKeys(blockKeysForAddress('Sheet1!A1:D10'), makeSection('D', 30));
    const c = withTableLocksKeys(blockKeysForAddress('Sheet2!A1:D10'), makeSection('C', 0));
    await Promise.all([c, d]);
    expect(events.indexOf('C:exit')).toBeLessThan(events.indexOf('D:exit'));
    expect(events.indexOf('C:enter')).toBeLessThan(events.indexOf('D:exit'));
  });

  it('Bug-7397 R9: a nested setTableMetadataWithinLock does NOT deadlock, and a concurrent self-locking write WAITS for the held blocks', async () => {
    // The insert/refresh paths hold the blocks and call setTableMetadataWithinLock
    // (no re-acquire). A standalone setTableMetadata (self-locking on the same
    // cells' blocks) attempted while the section is held must WAIT, not interleave.
    const order: string[] = [];
    let releaseSection: () => void = () => {};
    const sectionHeld = new Promise<void>((r) => { releaseSection = r; });

    const held = withTableLocksKeys(blockKeysForAddress('Sheet1!A1:D10'), async () => {
      order.push('section:start');
      // Nested within-lock write must NOT deadlock behind our own held lock.
      await setTableMetadataWithinLock('Sheet1!A1:D10', {
        semanticQuery: '{"q":"from-section"}', pluginVersion: '1.0.0', timestamp: '2026-07-27T00:00:00Z',
      });
      order.push('section:wrote');
      await sectionHeld; // keep the section open until the test releases it
      order.push('section:end');
    });

    // Give the section a tick to start and reach its nested write.
    await Promise.resolve();
    const concurrent = setTableMetadata('Sheet1!A1:D10', {
      semanticQuery: '{"q":"from-concurrent"}', pluginVersion: '1.0.0', timestamp: '2026-07-27T00:00:01Z',
    }).then(() => order.push('concurrent:wrote'));

    releaseSection();
    await Promise.all([held, concurrent]);
    // The concurrent self-locking write completes only AFTER the held section
    // ends -- it waited for the lock rather than interleaving.
    expect(order).toEqual(['section:start', 'section:wrote', 'section:end', 'concurrent:wrote']);
    // Last committed writer wins cleanly (the concurrent one), with no torn state.
    const raw = readRawFromServer();
    reassembleChunkedMetadata(raw);
    expect(raw['semanticQuery']).toBe('{"q":"from-concurrent"}');
  });
});

describe('Bug-7397 F-5 — import-boundary guard: the custom-functions runtime must never reach workbookMetadata', () => {
  // The Excel custom-functions runtime (functions.ts, bundled as an isolated
  // WWAHost IIFE by vite.config.functions.ts) is a SEPARATE JS realm from the
  // task pane. The per-table write lock's whole premise (see
  // withRangeWriteLock's module comment in workbookMetadata.ts) is that every
  // setTableMetadata caller lives in ONE realm sharing ONE `_rangeWriteLocks`
  // Map instance. If functions.ts ever imported (even transitively)
  // workbookMetadata.ts, it would get its OWN separate module instance (and
  // therefore its own separate lock map) in its own realm -- silently making
  // the lock a no-op for a cross-realm race, with no test failing to say so.
  // This statically walks functions.ts's relative-import graph and pins the
  // invariant the fix depends on, mirroring the static build-config guard
  // pattern already used by viteDedupe.test.ts in this suite.
  const pluginSrcRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
  const functionsEntry = join(pluginSrcRoot, 'functions.ts');
  const workbookMetadataFile = join(pluginSrcRoot, 'utils', 'workbookMetadata.ts');

  // Bug-7397 F-5 round-3 follow-up (deep-review finding): resolve BOTH
  // relative specifiers (./foo, ../foo) AND the `@/*` -> `src/*` alias
  // configured in tsconfig.json and vite.config.ts -- an import written as
  // `from '@/utils/workbookMetadata'` is just as real a runtime dependency as
  // a relative one, and the guard exists precisely to catch future drift, so
  // a specifier form it cannot see would defeat it silently.
  function resolveModuleSpecifier(fromFile: string, spec: string): string | null {
    let base: string;
    if (spec.startsWith('.')) {
      base = resolve(dirname(fromFile), spec);
    } else if (spec.startsWith('@/')) {
      base = resolve(pluginSrcRoot, spec.slice(2));
    } else {
      return null; // package import (react, etc.) -- out of scope
    }
    const candidates = [base, `${base}.ts`, `${base}.tsx`, join(base, 'index.ts'), join(base, 'index.tsx')];
    for (const candidate of candidates) {
      if (existsSync(candidate) && statSync(candidate).isFile()) return candidate;
    }
    return null;
  }

  function collectImportSpecs(filePath: string): string[] {
    const text = readFileSync(filePath, 'utf8');
    const specs: string[] = [];
    const patterns = [
      /\bimport\s+(?:type\s+)?(?:[\w*{}\s,]+from\s+)?["']([^"']+)["']/g,
      // Bug-7397 R6: ONE lenient dynamic-import pattern replacing the two
      // brittle `import(...)` forms. It captures the FIRST string literal
      // (any of ' " `) after `import(`, tolerating: whitespace before the
      // paren, a leading block comment, and ANY trailing content before the
      // close (a trailing comma, an inline comment, or `.then(...)` chaining)
      // -- all forms the previous `\s*\)`-anchored pattern silently missed, so
      // a real cross-realm dependency could slip past the guard.
      /\bimport\s*\(\s*(?:\/\*[^]*?\*\/\s*)?["'`]([^"'`]+)["'`]/g,
      // Re-exports, including the star-with-namespace form (`export * as ns
      // from '...'`) that a bare `\*` (without the optional `as ns`) misses.
      /\bexport\s+(?:\*(?:\s+as\s+\w+)?|\{[^}]*\})\s+from\s+["']([^"']+)["']/g,
      // Bug-7397 fix #6: CommonJS require() and TS import = require()
      /\brequire\(\s*["']([^"']+)["']\s*\)/g,
      /\bimport\s+\w+\s*=\s*require\(\s*["']([^"']+)["']\s*\)/g,
    ];
    for (const re of patterns) {
      let m: RegExpExecArray | null;
      while ((m = re.exec(text)) !== null) specs.push(m[1]);
    }
    return specs;
  }

  it('functions.ts exists at the expected entry path', () => {
    expect(existsSync(functionsEntry)).toBe(true);
  });

  it('functions.ts never transitively imports workbookMetadata.ts', () => {
    const visited = new Set<string>();
    const cameFrom: Record<string, string> = {};
    const queue = [functionsEntry];
    let hit: string | null = null;
    // Bug-7397 fix #6: track local specifiers that could not be resolved --
    // a silent skip here would let a new import form bypass the guard.
    const unresolvedLocal: { spec: string; from: string }[] = [];
    while (queue.length > 0) {
      const current = queue.shift()!;
      if (visited.has(current)) continue;
      visited.add(current);
      if (current === workbookMetadataFile) { hit = current; break; }
      for (const spec of collectImportSpecs(current)) {
        const resolved = resolveModuleSpecifier(current, spec);
        if (resolved && !visited.has(resolved)) {
          cameFrom[resolved] = current;
          queue.push(resolved);
        } else if (resolved === null && (spec.startsWith('.') || spec.startsWith('@/'))) {
          // A local specifier the resolver could not map to a file -- the
          // guard has a gap for this import form. Fail loud.
          unresolvedLocal.push({ spec, from: current });
        }
      }
    }
    if (unresolvedLocal.length > 0) {
      throw new Error(
        'Unresolved local specifiers in functions.ts import graph (guard gap):\n' +
        unresolvedLocal.map(u => `  ${u.spec} (from ${u.from})`).join('\n'),
      );
    }
    if (hit) {
      const chain: string[] = [hit];
      let cursor = hit;
      while (cameFrom[cursor]) {
        cursor = cameFrom[cursor];
        chain.unshift(cursor);
      }
      throw new Error(`functions.ts transitively imports workbookMetadata.ts via:\n${chain.join('\n  -> ')}`);
    }
    expect(hit).toBeNull();
  });
});

// ---- Bug-7397 round-5 fix tests (one per finding, mutation-provable) ----

describe('Bug-7397 fix #1 — table-key canonicalization ($-stripping)', () => {
  it('hashSheetName is deterministic and preserves case (case-insensitive hashing reverted to avoid orphaning existing workbooks)', () => {
    expect(hashSheetName('Sheet1')).toBe(hashSheetName('Sheet1'));
    // Case-sensitive by design: Office returns consistent casing, and
    // lowercasing would orphan every named item written by prior builds.
    expect(hashSheetName('Sheet1')).not.toBe(hashSheetName('sheet1'));
    expect(hashSheetName('Sheet1')).not.toBe(hashSheetName('Sheet2'));
  });

  it('$A$1 and A1 address forms produce the same metadata key (mutation: removing the $ strip makes this fail)', async () => {
    interface FakeNamedItem { name: string; comment: string; visible?: boolean }
    let items: FakeNamedItem[] = [];
    invalidateMetadataCache();
    vi.stubGlobal('Excel', {
      run: async (cb: (ctx: unknown) => Promise<unknown>) => cb({
        workbook: {
          names: {
            items,
            load: () => {},
            add: (name: string) => {
              const item: FakeNamedItem = { name, comment: '', visible: true };
              items.push(item);
              return item;
            },
            getItemOrNullObject: (name: string) => ({
              delete: () => { items = items.filter(i => i.name !== name); },
            }),
          },
        },
        sync: async () => {},
      }),
    });

    await setTableMetadata('Sheet1!$A$1:$D$10', {
      semanticQuery: '{"q":"via-absolute"}', pluginVersion: '1.0.0', timestamp: '2026-07-28T00:00:00Z',
    });
    invalidateMetadataCache();
    // Read via non-absolute form -- must find the same metadata
    const meta = await getTableMetadata('Sheet1!A1:D10');
    expect(meta.semanticQuery).toBe('{"q":"via-absolute"}');

    vi.stubGlobal('Excel', undefined);
  });
});

describe('Bug-7397 fix #2 — strict chunk-count parsing', () => {
  it('rejects parseInt-partial-parse values like "2abc" (mutation: removing the regex makes this fail)', () => {
    const raw: Record<string, string> = {
      semanticQuery__chunks: '2abc',
      semanticQuery__c0: 'part0',
      semanticQuery__c1: 'part1',
      projectId: 'p1',
    };
    reassembleChunkedMetadata(raw);
    // Must NOT reassemble: '2abc' is not a valid integer.
    expect('semanticQuery' in raw).toBe(false);
    expect(raw['projectId']).toBe('p1');
  });

  it('rejects scientific notation like "1e9" (mutation: using Number() without regex makes this fail)', () => {
    const raw: Record<string, string> = {
      semanticQuery__chunks: '1e9',
      semanticQuery__c0: 'data',
      projectId: 'p1',
    };
    reassembleChunkedMetadata(raw);
    expect('semanticQuery' in raw).toBe(false);
    expect(raw['projectId']).toBe('p1');
  });
});

describe('Bug-7397 fix #5 — comment length budget guard', () => {
  it('a very long key returns unchunked rather than silently over-length chunks (mutation: restoring Math.max(1,...) makes this fail)', () => {
    const longKey = 'k'.repeat(250);
    const value = 'v'.repeat(100);
    const entries = encodeMetadataEntry(longKey, value);
    // The budget for this key is 255 - (250 + 3 + 6 + 1) = -5 < 1.
    // Must return a single unchunked entry (Office.js rejects loudly)
    // rather than N chunks each exceeding 255.
    expect(entries).toHaveLength(1);
    expect(entries[0].suffix).toBe(longKey);
  });
});

describe('Bug-7397 fix #7 — synthetic _tableStart not persisted', () => {
  it('_tableStart does not appear in persisted named items (mutation: removing the _ prefix skip makes this fail)', async () => {
    // _tableStart is an enumerable property (required: cellContext.ts reads
    // it on every selection-change, and the cache spreads it via {…data}).
    // Persistence is prevented by setTableMetadata's key.startsWith('_')
    // skip, NOT by enumerability. Reverting that skip would persist
    // _tableStart as a real named item, which this test catches.
    interface FakeNamedItem { name: string; comment: string; visible?: boolean }
    let items: FakeNamedItem[] = [];
    invalidateMetadataCache();
    vi.stubGlobal('Excel', {
      run: async (cb: (ctx: unknown) => Promise<unknown>) => cb({
        workbook: {
          names: {
            items,
            load: () => {},
            add: (name: string) => {
              const item: FakeNamedItem = { name, comment: '', visible: true };
              items.push(item);
              return item;
            },
            getItemOrNullObject: (name: string) => ({
              delete: () => { items = items.filter(i => i.name !== name); },
            }),
          },
        },
        sync: async () => {},
      }),
    });

    // Write metadata that includes _tableStart in the spread
    const metaWithSynthetic = {
      semanticQuery: '{"q":"test"}', pluginVersion: '1.0.0',
      timestamp: '2026-07-28T00:00:00Z', _tableStart: 'A1',
    };
    await setTableMetadata('Sheet1!A1:D10', metaWithSynthetic as any);
    // No named item should have '_tableStart' in its comment key
    const tableStartItems = items.filter(i => i.comment.startsWith('_tableStart='));
    expect(tableStartItems).toHaveLength(0);

    // _tableStart IS accessible on the read result and survives cache
    invalidateMetadataCache();
    const meta = await getTableMetadata('Sheet1!A1:D10');
    expect((meta as Record<string, unknown>)._tableStart).toBe('A1');
    // Second read (cache hit) must also have _tableStart
    const cached = await getTableMetadata('Sheet1!A1:D10');
    expect((cached as Record<string, unknown>)._tableStart).toBe('A1');

    vi.stubGlobal('Excel', undefined);
  });
});

describe('Bug-7397 fix #8 — expired cache eviction', () => {
  it('an expired cache entry triggers a fresh Excel.run (mutation: removing eviction makes the run count stay at 1)', async () => {
    // The eviction fix deletes the expired entry from the cache so a
    // subsequent getTableMetadata must hit Excel.run again. Without
    // eviction the expired entry is skipped (not served) but also not
    // deleted, and the re-read still happens -- so verifying the DATA is
    // correct does not distinguish "evicted" from "skipped". Instead, we
    // count Excel.run calls: a cache hit avoids Excel.run; a cache miss
    // (after eviction) triggers one.
    interface FakeNamedItem { name: string; comment: string; visible?: boolean }
    let items: FakeNamedItem[] = [];
    let excelRunCount = 0;
    invalidateMetadataCache();
    vi.stubGlobal('Excel', {
      run: async (cb: (ctx: unknown) => Promise<unknown>) => {
        excelRunCount++;
        return cb({
          workbook: {
            names: {
              items,
              load: () => {},
              add: (name: string) => {
                const item: FakeNamedItem = { name, comment: '', visible: true };
                items.push(item);
                return item;
              },
              getItemOrNullObject: (name: string) => ({
                delete: () => { items = items.filter(i => i.name !== name); },
              }),
            },
          },
          sync: async () => {},
        });
      },
    });

    await setTableMetadata('Sheet1!A1:B2', {
      semanticQuery: '{"q":"cached"}', pluginVersion: '1.0.0', timestamp: '2026-07-28T00:00:00Z',
    });
    excelRunCount = 0; // reset after setTableMetadata's own Excel.run

    // First read: cache miss → Excel.run
    await getTableMetadata('Sheet1!A1:B2');
    expect(excelRunCount).toBe(1);

    // Second read within TTL: cache hit → no Excel.run
    await getTableMetadata('Sheet1!A1:B2');
    expect(excelRunCount).toBe(1); // unchanged

    // Simulate TTL expiration
    const realNow = Date.now();
    vi.spyOn(Date, 'now').mockReturnValue(realNow + 60_000); // 60s > 30s TTL

    // Third read: expired entry evicted → must trigger Excel.run
    await getTableMetadata('Sheet1!A1:B2');
    expect(excelRunCount).toBe(2); // increased

    vi.restoreAllMocks();
    vi.stubGlobal('Excel', undefined);
  });
});

describe('Bug-7397 R9 — no detector stamps leak onto read results', () => {
  it('a read result carries NO internal lock/detector stamps (block locking derives keys from cells, not from a stamped signal)', async () => {
    // R9 removed the entire detector class (version map, epoch, per-table
    // stamps). A read must expose only real metadata + the _tableStart helper
    // cellContext relies on -- never a _rangeKey / _readEpoch / _readTableVersion
    // signal a consumer could (wrongly) act on.
    interface FakeNamedItem { name: string; comment: string; visible?: boolean }
    let items: FakeNamedItem[] = [];
    invalidateMetadataCache();
    vi.stubGlobal('Excel', {
      run: async (cb: (ctx: unknown) => Promise<unknown>) => cb({
        workbook: {
          names: {
            items,
            load: () => {},
            add: (name: string) => {
              const item: FakeNamedItem = { name, comment: '', visible: true };
              items.push(item);
              return item;
            },
            getItemOrNullObject: (name: string) => ({
              delete: () => { items = items.filter(i => i.name !== name); },
            }),
          },
        },
        sync: async () => {},
      }),
    });

    await setTableMetadata('Sheet1!A1:D10', {
      semanticQuery: '{"q":"test"}', pluginVersion: '1.0.0', timestamp: '2026-07-28T00:00:00Z',
    });

    // Fresh read (cache miss) and cached read (cache hit) both carry no stamps.
    const fresh = await getTableMetadata('Sheet1!A1:D10') as Record<string, unknown>;
    const cached = await getTableMetadata('Sheet1!A1:D10') as Record<string, unknown>;
    for (const r of [fresh, cached]) {
      expect(r._rangeKey).toBeUndefined();
      expect(r._readEpoch).toBeUndefined();
      expect(r._readTableVersion).toBeUndefined();
    }
    expect(fresh.semanticQuery).toBe('{"q":"test"}');

    vi.stubGlobal('Excel', undefined);
  });
});

// The fake worksheet's body font, which the growth path copies onto the row the
// provenance footer vacates (so the reverted row matches its own table rather
// than a hardcoded default).
const BODY_FONT_SIZE_REF = 12;
const BODY_FONT_COLOR_REF = '#101010';

describe('Bug-7397 R6 — refreshTables per-table lock integration (real refreshTables path)', () => {
  // Drives the REAL refreshTables with mocked Excel, getModelContext, and
  // executeQuery. Proves the R6 EXCLUSION contract (not detection):
  // (1) A concurrent same-table write fired during the refresh's executeQuery
  //     is SERIALIZED behind the refresh (it does not interleave); the refresh
  //     acts on the consistent pre-write snapshot it read INSIDE its lock, and
  //     the later writer wins cleanly -- cells and provenance never describe
  //     different queries (the wrong-numbers class the 3 gates rejected).
  // (2) A two-table refresh writes back BOTH tables (the backstop never
  //     false-fires on a table's own or another table's write-back).
  //
  // Mutation-provable: reverting the metadata read to OUTSIDE the lock (round-5
  // shape) lets test (1)'s concurrent write land between read and execute, so
  // the executed query becomes the stale one -- the capturedQueries assertion
  // then fails.

  interface FakeNamedItem { name: string; comment: string; visible?: boolean }
  let serverItems: FakeNamedItem[];
  let mockTables: { name: string; rangeAddress: string }[];
  let mockQueryResult: Record<string, unknown>[];
  let onExecuteQuery: (() => void) | null;
  let capturedQueries: unknown[];
  let failMetadataWrite: boolean;
  // Bug-7397 R12-R4-2: fail the REWRITE batch's sync AFTER its mutations were
  // queued, reproducing Office.run's non-transactional behaviour.
  let failRewriteSync: boolean;
  // Bug-7397 R12 R5: fail the REWRITE batch's FIRST sync, BEFORE any mutating
  // operation is queued. Without this knob the mock can only fail POST-mutation,
  // so `mutationsQueued` was proven in one direction only -- and a mutant that
  // set it unconditionally survived the whole suite.
  let failRewriteSyncBeforeMutation: boolean;
  let sawRewriteLoad: boolean;
  // Bug-7397 R10-1: record whether rewriteTableBody used resize-in-place vs a
  // shifting body delete.
  let usedResize: boolean;
  let usedShiftDelete: boolean;
  // Bug-7397 R11-1: the LIVE body row count rewriteTableBody reads under the
  // lock (can be made larger than the enumerated extent to simulate drift).
  let liveBodyRowCount: number;
  // Bug-7397 R12-2: worksheet cell contents, keyed "row,col". Seeded by the
  // footer tests and written by the rewrite so assertions can locate the footer.
  let sheetCells: Map<string, unknown>;
  // Bug-7397 R12 round-2 finding 2: formulas, keyed "row,col" like sheetCells.
  let sheetFormulas: Map<string, unknown>;
  // Bug-7397 R12 review finding 4: clear kind + font-format writes per row.
  let clearOps: { row: number; rowCount: number; applyTo: string }[];
  let fontOps: { row: number; prop: string; value: unknown }[];

  beforeEach(async () => {
    serverItems = [];
    mockTables = [];
    mockQueryResult = [{ region: 'North', revenue: 100 }];
    onExecuteQuery = null;
    capturedQueries = [];
    failMetadataWrite = false;
    failRewriteSync = false;
    failRewriteSyncBeforeMutation = false;
    sawRewriteLoad = false;
    usedResize = false;
    usedShiftDelete = false;
    liveBodyRowCount = 1;
    sheetCells = new Map();
    sheetFormulas = new Map();
    clearOps = [];
    fontOps = [];
    invalidateMetadataCache();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.stubGlobal('Excel', undefined);
  });

  async function setupRefreshMocks() {
    vi.stubGlobal('Excel', {
      run: async (cb: (ctx: unknown) => Promise<unknown>) => {
        // Per-run flag: did THIS Excel.run add a named item? Only the metadata
        // write-back does. Bug-7397 R6: a simulated write-back failure surfaces
        // at sync() (the real Office.js batch-failure path), NOT synchronously
        // at add() -- setTableMetadataWithinLock's per-add try/catch would
        // swallow a synchronous add throw, so failing at sync faithfully
        // reproduces the batch-rejection-after-queued-deletes loss mode.
        let didAddName = false;
        return cb({
          workbook: {
            names: {
              get items() { return serverItems.map(i => ({ ...i })); },
              load: () => {},
              add: (name: string) => {
                didAddName = true;
                const item: FakeNamedItem = { name, comment: '', visible: true };
                serverItems.push(item);
                return item;
              },
              getItemOrNullObject: (name: string) => ({
                delete: () => { serverItems = serverItems.filter(i => i.name !== name); },
              }),
            },
            worksheets: {
              getActiveWorksheet: () => ({
                tables: {
                  get items() { return mockTables.map(t => ({ name: t.name, getRange: () => ({ address: t.rangeAddress, load: () => {} }) })); },
                  load: () => {},
                },
              }),
            },
            tables: {
              getItem: (name: string) => {
                // Bug-7397 R12-2: the rewrite now also probes the provenance
                // footer cell (load -> read values) and, when it finds one,
                // clears/rewrites/formats it. `sheetCells` records every cell
                // written so a test can assert WHERE the footer landed, and
                // `sheetCells` is pre-seeded by tests that want a footer to
                // exist. Keyed "row,col".
                const rewriteSheet = {
                  getRangeByIndexes: (row: number, col: number, rowCount = 1, colCount = 1) => ({
                    get values() {
                      const out: unknown[][] = [];
                      for (let r = 0; r < rowCount; r++) {
                        const line: unknown[] = [];
                        for (let c = 0; c < colCount; c++) line.push(sheetCells.get(`${row + r},${col + c}`) ?? '');
                        out.push(line);
                      }
                      return out;
                    },
                    // Bug-7397 R12 round-2 finding 2: the growth probe reads the
                    // FORMULAS channel too, so a formula rendering as '' is not
                    // mistaken for an empty cell.
                    get formulas() {
                      const out: unknown[][] = [];
                      for (let r = 0; r < rowCount; r++) {
                        const line: unknown[] = [];
                        for (let c = 0; c < colCount; c++) line.push(sheetFormulas.get(`${row + r},${col + c}`) ?? '');
                        out.push(line);
                      }
                      return out;
                    },
                    set values(v: unknown[][]) {
                      v.forEach((line, r) => line.forEach((cell, c) => sheetCells.set(`${row + r},${col + c}`, cell)));
                    },
                    load: () => {},
                    // Bug-7397 R12 review finding 4: record WHICH clear kind was
                    // used and WHICH font properties were set, per row -- the
                    // footer's grey/italic styling must be reverted, not just
                    // its contents cleared.
                    clear: (applyTo?: string) => {
                      clearOps.push({ row, rowCount, applyTo: applyTo ?? 'contents' });
                      for (let r = 0; r < rowCount; r++) {
                        for (let c = 0; c < colCount; c++) sheetCells.delete(`${row + r},${col + c}`);
                      }
                    },
                    getCell: (dr: number, dc: number) => ({
                      set values(v: unknown[][]) { sheetCells.set(`${row + dr},${col + dc}`, v[0][0]); },
                    }),
                    format: {
                      // Readable AND writable: the growth path READS the last
                      // body row's font to revert the vacated footer row to the
                      // table's real styling, and WRITES the result.
                      font: {
                        get italic() { return false; },
                        set italic(v: unknown) { fontOps.push({ row, prop: 'italic', value: v }); },
                        get size() { return BODY_FONT_SIZE_REF; },
                        set size(v: unknown) { fontOps.push({ row, prop: 'size', value: v }); },
                        get color() { return BODY_FONT_COLOR_REF; },
                        set color(v: unknown) { fontOps.push({ row, prop: 'color', value: v }); },
                      },
                    },
                  }),
                };
                // Derive the table's real header position/width from its address
                // (e.g. 'Sheet1!D1:E2' -> startRow 0, startCol 3, columnCount 2),
                // so the R11-1 bounds check sees the table where it actually is.
                const addr = mockTables.find(t => t.name === name)?.rangeAddress ?? '';
                const rangePart = addr.includes('!') ? addr.split('!')[1] : addr;
                const [startCellRef, endCellRef] = rangePart.split(':');
                const cellRc = (ref: string) => {
                  const m = /^([A-Z]+)(\d+)$/.exec(ref || 'A1');
                  const letters = m ? m[1] : 'A';
                  let col = 0;
                  for (const ch of letters) col = col * 26 + (ch.charCodeAt(0) - 64);
                  return { col: col - 1, row: m ? parseInt(m[2], 10) - 1 : 0 };
                };
                const s = cellRc(startCellRef);
                const e = cellRc(endCellRef || startCellRef);
                const colCount = Math.abs(e.col - s.col) + 1;
                return {
                  getRange: () => ({ address: addr, load: () => {}, worksheet: rewriteSheet }),
                  getHeaderRowRange: () => { sawRewriteLoad = true; return { rowIndex: s.row, columnIndex: s.col, columnCount: colCount, load: () => {} }; },
                  // A shifting body delete would set usedShiftDelete; the R10-1
                  // in-place rewrite never calls it.
                  getDataBodyRange: () => ({ rowCount: liveBodyRowCount, columnCount: colCount, load: () => {}, delete: () => { usedShiftDelete = true; } }),
                  resize: () => { usedResize = true; },
                  rows: { add: () => { usedShiftDelete = true; } },
                };
              },
            },
            pivotTables: { items: [], load: () => {} },
          },
          sync: async () => {
            if (failMetadataWrite && didAddName) throw new Error('simulated batch sync failure on metadata write-back');
            if (failRewriteSync && usedResize) throw new Error('simulated batch sync failure AFTER the resize/values already applied');
            if (failRewriteSyncBeforeMutation && sawRewriteLoad && !usedResize) throw new Error('simulated batch sync failure BEFORE any mutation was queued');
          },
        });
      },
      ClearApplyTo: { contents: 'contents', all: 'all' },
    });

    const storage = await import('../utils/storage');
    vi.spyOn(storage, 'getModelContext').mockResolvedValue({
      projectId: 'proj-1',
      modelId: 'model-1',
    } as any);

    const queryRouter = await import('../api/queryRouter');
    vi.spyOn(queryRouter, 'executeQuery').mockImplementation(async (query: unknown) => {
      capturedQueries.push(query);
      if (onExecuteQuery) onExecuteQuery();
      return {
        data: mockQueryResult,
        annotation: { measures: { revenue: { title: 'Revenue' } }, dimensions: { region: { title: 'Region' } } },
      } as any;
    });
  }

  it('R9: a concurrent same-table insert that commits during the refresh\'s (unlocked) execute phase causes the refresh to SKIP under the block lock, never overwriting the newer data with a stale result', async () => {
    await setupRefreshMocks();
    const { refreshTables } = await import('../utils/tableRefresh');

    mockTables = [{ name: 'Table1', rangeAddress: 'Sheet1!A1:B2' }];
    await setTableMetadata('Sheet1!A1:B2', {
      semanticQuery: '{"measures":["revenue"],"dimensions":["region"]}',
      columnHeaders: '["Region","Revenue"]',
      projectId: 'proj-1',
      modelId: 'model-1',
      pluginVersion: '1.0.0',
      timestamp: '2026-01-01T00:00:00Z',
    });

    // During the refresh's UNLOCKED execute phase, a separate insert commits a
    // NEW query (V2) for the same cells. It acquires the block first (fired
    // before the refresh acquires), so the refresh WAITS for it, then re-reads
    // provenance under the block, sees V2 != the V1 it executed, and SKIPS --
    // it never writes the stale V1 result over the newer V2 data.
    let concurrentWrite: Promise<void> | null = null;
    onExecuteQuery = () => {
      concurrentWrite = setTableMetadata('Sheet1!A1:B2', {
        semanticQuery: '{"measures":["orders"],"dimensions":["region"]}',
        columnHeaders: '["Region","Orders"]',
        projectId: 'proj-1',
        modelId: 'model-1',
        pluginVersion: '1.0.0',
        timestamp: '2026-07-28T00:00:00Z',
      });
    };

    const result = await refreshTables('activeSheet');
    await concurrentWrite;

    // The refresh executed V1 (revenue) but detected the supersession and
    // skipped -- Table1 is SKIPPED, not refreshed.
    expect(result.skipped.some(s => s.name === 'Table1')).toBe(true);
    expect(result.refreshed).not.toContain('Table1');
    expect(capturedQueries).toHaveLength(1);
    expect((capturedQueries[0] as { measures: string[] }).measures).toEqual(['revenue']);

    // The concurrent insert's V2 stands (never overwritten by the stale refresh).
    invalidateMetadataCache();
    const finalMeta = await getTableMetadata('Sheet1!A1:B2');
    expect(finalMeta.semanticQuery).toBe('{"measures":["orders"],"dimensions":["region"]}');
  });

  it('a concurrent insert to a spatially DISTANT table does NOT block during the refresh\'s (slow, unlocked) execute phase', async () => {
    await setupRefreshMocks();
    const { refreshTables } = await import('../utils/tableRefresh');

    mockTables = [{ name: 'Table1', rangeAddress: 'Sheet1!A1:B2' }];
    await setTableMetadata('Sheet1!A1:B2', {
      semanticQuery: '{"measures":["revenue"],"dimensions":["region"]}',
      columnHeaders: '["Region","Revenue"]',
      projectId: 'proj-1',
      modelId: 'model-1',
      pluginVersion: '1.0.0',
      timestamp: '2026-01-01T00:00:00Z',
    });

    const order: string[] = [];
    // Hold the refresh open at its (UNLOCKED) executeQuery until released, and
    // during that hold fire an insert to a spatially distant table
    // (Sheet1!A200 -> a different cell block than Table1's A1:B2). It must
    // complete without waiting for the refresh.
    let releaseRefresh: () => void = () => {};
    const refreshHeld = new Promise<void>((r) => { releaseRefresh = r; });
    let otherTableWrite: Promise<void> | null = null;
    onExecuteQuery = () => {
      otherTableWrite = setTableMetadata('Sheet1!A200:B201', {
        semanticQuery: '{"measures":["cost"],"dimensions":["region"]}',
        pluginVersion: '1.0.0', timestamp: '2026-07-28T00:00:00Z',
      }).then(() => { order.push('other-table:done'); });
    };

    // Patch executeQuery to await the hold so the refresh genuinely stays in
    // its (unlocked) execute phase while the distant insert runs.
    const queryRouter = await import('../api/queryRouter');
    vi.spyOn(queryRouter, 'executeQuery').mockImplementation(async (query: unknown) => {
      capturedQueries.push(query);
      if (onExecuteQuery) onExecuteQuery();
      await refreshHeld; // refresh stays in its execute phase until released
      order.push('refresh:executed');
      return {
        data: mockQueryResult,
        annotation: { measures: { revenue: { title: 'Revenue' } }, dimensions: { region: { title: 'Region' } } },
      } as any;
    });

    const refreshPromise = refreshTables('activeSheet');
    // Spin the microtask queue until the refresh has entered executeQuery and
    // fired the other-table write. (No real timers are involved, so this
    // settles quickly.)
    for (let i = 0; i < 100 && !otherTableWrite; i++) await Promise.resolve();
    // The distant write must resolve even though the refresh is STILL in its
    // execute phase (releaseRefresh not called). If the refresh serialized all
    // inserts (a global lock, or holding blocks during execute), awaiting this
    // would hang until release and the test would time out.
    await otherTableWrite;
    expect(order).toContain('other-table:done');
    expect(order).not.toContain('refresh:executed');

    releaseRefresh();
    const result = await refreshPromise;
    expect(order).toEqual(['other-table:done', 'refresh:executed']);
    expect(result.refreshed).toContain('Table1');
  });

  it('Bug-7397 R6 MEDIUM: a failed provenance write-back is surfaced as a warning, not silently reported as a clean success', async () => {
    await setupRefreshMocks();
    const { refreshTables } = await import('../utils/tableRefresh');

    mockTables = [{ name: 'Table1', rangeAddress: 'Sheet1!A1:B2' }];
    await setTableMetadata('Sheet1!A1:B2', {
      semanticQuery: '{"measures":["revenue"],"dimensions":["region"]}',
      columnHeaders: '["Region","Revenue"]',
      projectId: 'proj-1',
      modelId: 'model-1',
      pluginVersion: '1.0.0',
      timestamp: '2026-01-01T00:00:00Z',
    });

    // The initial write is done; now make the write-back (the only remaining
    // names.add) fail. The data still refreshes, but the provenance can't be
    // persisted.
    failMetadataWrite = true;
    const result = await refreshTables('activeSheet');

    // Data refreshed...
    expect(result.refreshed).toContain('Table1');
    // ...but the write-back failure is surfaced honestly (mutation: swallowing
    // the failure in updateTableTimestamp makes warnings empty).
    expect(result.warnings.some(w => w.name === 'Table1')).toBe(true);
    expect(result.skipped).toHaveLength(0);
  });

  it('two-table refresh: both tables get their metadata write-back (no false-positive from self-writes)', async () => {
    await setupRefreshMocks();
    const { refreshTables } = await import('../utils/tableRefresh');

    mockTables = [
      { name: 'Table1', rangeAddress: 'Sheet1!A1:B2' },
      { name: 'Table2', rangeAddress: 'Sheet1!D1:E2' },
    ];
    await setTableMetadata('Sheet1!A1:B2', {
      semanticQuery: '{"measures":["revenue"],"dimensions":["region"]}',
      columnHeaders: '["Region","Revenue"]',
      projectId: 'proj-1',
      modelId: 'model-1',
      pluginVersion: '1.0.0',
      timestamp: '2026-01-01T00:00:00Z',
    });
    await setTableMetadata('Sheet1!D1:E2', {
      semanticQuery: '{"measures":["orders"],"dimensions":["region"]}',
      columnHeaders: '["Region","Revenue"]',
      projectId: 'proj-1',
      modelId: 'model-1',
      pluginVersion: '1.0.0',
      timestamp: '2026-01-01T00:00:00Z',
    });

    const result = await refreshTables('activeSheet');

    // Both tables must be refreshed
    expect(result.refreshed).toContain('Table1');
    expect(result.refreshed).toContain('Table2');
    expect(result.skipped).toHaveLength(0);
  });

  it('R9: a growing refresh (more rows than the table currently has) refreshes cleanly under block locks', async () => {
    await setupRefreshMocks();
    const { refreshTables } = await import('../utils/tableRefresh');

    mockTables = [{ name: 'Table1', rangeAddress: 'Sheet1!A1:B2' }];
    // 20 result rows -> the table grows well beyond its current 2-row extent.
    // Block locks cover the UNION of current and intended extent, so the growth
    // zone is excluded by construction (no reserve/publish step).
    mockQueryResult = Array.from({ length: 20 }, (_, i) => ({ region: `R${i}`, revenue: i }));
    await setTableMetadata('Sheet1!A1:B2', {
      semanticQuery: '{"measures":["revenue"],"dimensions":["region"]}',
      columnHeaders: '["Region","Revenue"]',
      projectId: 'proj-1',
      modelId: 'model-1',
      pluginVersion: '1.0.0',
      timestamp: '2026-01-01T00:00:00Z',
    });

    const result = await refreshTables('activeSheet');
    expect(result.refreshed).toContain('Table1');
    expect(result.skipped).toHaveLength(0);
  });

  it('R10-2: a concurrent re-insert of the SAME query (new timestamp/data) during the execute phase still causes a SKIP', async () => {
    await setupRefreshMocks();
    const { refreshTables } = await import('../utils/tableRefresh');

    mockTables = [{ name: 'Table1', rangeAddress: 'Sheet1!A1:B2' }];
    await setTableMetadata('Sheet1!A1:B2', {
      semanticQuery: '{"measures":["revenue"],"dimensions":["region"]}',
      columnHeaders: '["Region","Revenue"]',
      projectId: 'proj-1',
      modelId: 'model-1',
      pluginVersion: '1.0.0',
      timestamp: '2026-01-01T00:00:00Z',
    });

    // The concurrent insert re-inserts the SAME query but with NEW data and a
    // fresh timestamp -- semanticQuery/projectId/modelId are unchanged, so ONLY
    // the timestamp comparison catches it. Mutation: dropping the timestamp
    // check makes the refresh overwrite the newer data (refreshed, not skipped).
    let concurrentWrite: Promise<void> | null = null;
    onExecuteQuery = () => {
      concurrentWrite = setTableMetadata('Sheet1!A1:B2', {
        semanticQuery: '{"measures":["revenue"],"dimensions":["region"]}',
        columnHeaders: '["Region","Revenue"]',
        projectId: 'proj-1',
        modelId: 'model-1',
        pluginVersion: '1.0.0',
        timestamp: '2026-07-28T09:99:99Z', // FRESH timestamp (a newer insert)
      });
    };

    const result = await refreshTables('activeSheet');
    await concurrentWrite;
    expect(result.skipped.some(s => s.name === 'Table1')).toBe(true);
    expect(result.refreshed).not.toContain('Table1');
  });

  // -------------------------------------------------------------------------
  // Promoted from review rounds 1 and 3: both reviewers traced the footer move
  // and the growth-zone probe at a table whose origin is NOT row 0 / column 0
  // (every committed test used A1, where an off-by-one in `startRow`/`startCol`
  // is invisible because both are zero). Those traces were run by hand and
  // never landed; they are the only coverage of the origin-offset arithmetic.
  // -------------------------------------------------------------------------

  async function seedOffsetTable(bodyRows: number, resultRows: number) {
    await setupRefreshMocks();
    // Header at row 4 (D5), columns 3..4 -- so a dropped `+ startRow` or
    // `+ startCol` term lands the footer, the probe or the body in the wrong
    // place instead of silently coinciding with 0.
    const endRow = 5 + bodyRows;
    mockTables = [{ name: 'Table1', rangeAddress: `Sheet1!D5:E${endRow}` }];
    liveBodyRowCount = bodyRows;
    mockQueryResult = Array.from({ length: resultRows }, (_, i) => ({ region: `R${i}`, revenue: i }));
    await setTableMetadata(`Sheet1!D5:E${endRow}`, {
      semanticQuery: '{"measures":["revenue"],"dimensions":["region"]}',
      columnHeaders: '["Region","Revenue"]',
      projectId: 'proj-1',
      modelId: 'model-1',
      pluginVersion: '1.0.0',
      timestamp: '2026-01-01T00:00:00Z',
    });
    // Footer one row below the body: header row 4 + bodyRows + 1.
    sheetCells.set(`${5 + bodyRows},3`, FOOTER_TEXT);
  }

  it('R12-2 at a NON-ZERO origin: a growing refresh moves the footer by the table\'s own offset, not from row 0', async () => {
    // D5:E15 -> header row 4, body rows 5..14, footer row 15. 25 result rows
    // -> body rows 5..29, footer row 30.
    await seedOffsetTable(10, 25);
    const { refreshTables } = await import('../utils/tableRefresh');

    const result = await refreshTables('activeSheet');
    expect(result.refreshed).toContain('Table1');
    // Old footer cell is now data...
    expect(sheetCells.get('15,3')).toBe('R10');
    // ...and the footer is at row 30, column 3 (D), restamped.
    expect(String(sheetCells.get('30,3'))).toContain('Source: Tessallite');
    expect(String(sheetCells.get('30,3'))).not.toContain('2026-01-01 00:00 UTC');
    // The vacated row's styling is reverted at the RIGHT row.
    expect(fontOps).toContainEqual({ row: 15, prop: 'italic', value: false });
    expect(fontOps).toContainEqual({ row: 30, prop: 'italic', value: true });
  });

  it('R12-2 at a NON-ZERO origin: a shrinking refresh moves the footer up and clears the old row completely', async () => {
    await seedOffsetTable(10, 3);
    const { refreshTables } = await import('../utils/tableRefresh');

    const result = await refreshTables('activeSheet');
    expect(result.refreshed).toContain('Table1');
    // Footer directly under the 3-row body: header 4 + 3 + 1 = row 8.
    expect(String(sheetCells.get('8,3'))).toContain('Source: Tessallite');
    expect(sheetCells.get('15,3')).toBeUndefined();
    expect(clearOps).toContainEqual({ row: 15, rowCount: 1, applyTo: 'all' });
  });

  it('Bug-8340 at a NON-ZERO origin: the growth probe covers the table\'s own columns and rows, not column A / row 0', async () => {
    await seedOffsetTable(10, 25);
    const { refreshTables } = await import('../utils/tableRefresh');
    const { strings } = await import('../i18n/strings');

    // A user cell inside the growth zone, in the table's SECOND column (E) --
    // a probe anchored at column 0, or one column too narrow, would miss it.
    sheetCells.set('20,4', 'my note');

    const result = await refreshTables('activeSheet');
    expect(result.skipped).toEqual([{ name: 'Table1', reason: strings.tableRefresh.growthZoneOccupiedSkip }]);
    expect(sheetCells.get('20,4')).toBe('my note');
    expect(usedResize).toBe(false);
  });

  it('Bug-8340 at a NON-ZERO origin: a cell just ABOVE the growth zone (inside the old body) does NOT block the refresh', async () => {
    // Row 14 is the last OLD body row -- our own data, not newly claimed ground.
    // A probe starting one row too early would false-positive on every refresh.
    await seedOffsetTable(10, 25);
    sheetCells.set('14,3', 'old body value');
    const { refreshTables } = await import('../utils/tableRefresh');

    const result = await refreshTables('activeSheet');
    expect(result.refreshed).toContain('Table1');
    expect(result.skipped).toHaveLength(0);
  });

  it('R12-R4-2: a batch sync failure AFTER the resize/values applied is NOT reported as an untouched Skip (Office.run is not transactional)', async () => {
    await seedOffsetTable(10, 3);
    const { refreshTables } = await import('../utils/tableRefresh');
    const { strings } = await import('../i18n/strings');
    failRewriteSync = true;

    const result = await refreshTables('activeSheet');

    // The sheet DID change: the resize applied and the new body landed.
    expect(usedResize).toBe(true);
    expect(sheetCells.get('5,3')).toBe('R0');
    // So the table must NOT carry a reason that means "not changed at all".
    expect(result.skipped.some(s => s.reason === strings.tableRefresh.writeFailedSkip)).toBe(false);
    // It must be surfaced as a warning naming the table the user has to check.
    expect(result.warnings).toEqual([
      { name: 'Table1', reason: strings.tableRefresh.partialRewriteWarning },
    ]);
  });

  it('R12-R4-2 (negative direction): a rewrite sync failure BEFORE any mutation is queued is an untouched SKIP, never a partial-rewrite warning', async () => {
    // The positive direction (post-mutation failure -> warning) is pinned above.
    // This is the other half, and it is the one that matters for false alarms:
    // if `mutationsQueued` ever drifts upward -- or a new mutating op is queued
    // above it -- every ORDINARY write failure would tell the user an UNTOUCHED
    // table "may hold a mix of old and new rows", and file it under "Refreshed
    // with warnings" for a table that was never refreshed. Round 5 proved a
    // mutant setting the flag unconditionally survived the entire suite.
    await seedOffsetTable(10, 3);
    const { refreshTables } = await import('../utils/tableRefresh');
    const { strings } = await import('../i18n/strings');
    failRewriteSyncBeforeMutation = true;

    const result = await refreshTables('activeSheet');

    expect(usedResize).toBe(false);                 // nothing was queued
    expect(sheetCells.get('5,3')).toBeUndefined();  // no body row landed
    expect(result.warnings).toEqual([]);            // NOT a partial-rewrite warning
    expect(result.skipped).toEqual([
      { name: 'Table1', reason: strings.tableRefresh.writeFailedSkip },
    ]);
    // ...and the two reasons stay distinguishable to a reader.
    expect(strings.tableRefresh.writeFailedSkip).toContain('left unchanged');
    expect(strings.tableRefresh.partialRewriteWarning).not.toContain('left unchanged');
  });

  it('R11-1: when the LIVE table grew past the declared (locked) extent, the refresh SKIPS and mutates NOTHING (no clear past held blocks, no data loss)', async () => {
    await setupRefreshMocks();
    const { refreshTables } = await import('../utils/tableRefresh');

    // Enumerated as a small 2-row table, but the live body has grown to 200 rows
    // (e.g. the user typed below it, auto-expanding it). The query returns 20.
    mockTables = [{ name: 'Table1', rangeAddress: 'Sheet1!A1:B3' }];
    liveBodyRowCount = 200;
    mockQueryResult = Array.from({ length: 20 }, (_, i) => ({ region: `R${i}`, revenue: i }));
    await setTableMetadata('Sheet1!A1:B3', {
      semanticQuery: '{"measures":["revenue"],"dimensions":["region"]}',
      columnHeaders: '["Region","Revenue"]',
      projectId: 'proj-1',
      modelId: 'model-1',
      pluginVersion: '1.0.0',
      timestamp: '2026-01-01T00:00:00Z',
    });

    const result = await refreshTables('activeSheet');
    // The live extent (200 rows) exceeds the declared/locked rectangle, so the
    // refresh must SKIP without mutating -- never resize/clear the 180 rows
    // outside the held blocks. Mutation: dropping the R11-1 bounds check lets it
    // resize+clear (usedResize true) and reports Table1 refreshed.
    expect(result.skipped.some(s => s.name === 'Table1')).toBe(true);
    expect(result.refreshed).not.toContain('Table1');
    expect(usedResize).toBe(false);
    expect(usedShiftDelete).toBe(false);
  });

  // -------------------------------------------------------------------------
  // Bug-7397 R12-2: the provenance footer is part of the refresh's locked and
  // rewritten region -- a growing refresh must not EAT it, a shrinking refresh
  // must not ORPHAN it.
  // -------------------------------------------------------------------------

  const FOOTER_TEXT = 'Source: Tessallite | Model: Sales | 2026-01-01 00:00 UTC';

  async function seedFooterTable(bodyRows: number, resultRows: number) {
    await setupRefreshMocks();
    mockTables = [{ name: 'Table1', rangeAddress: `Sheet1!A1:B${bodyRows + 1}` }];
    liveBodyRowCount = bodyRows;
    mockQueryResult = Array.from({ length: resultRows }, (_, i) => ({ region: `R${i}`, revenue: i }));
    await setTableMetadata(`Sheet1!A1:B${bodyRows + 1}`, {
      semanticQuery: '{"measures":["revenue"],"dimensions":["region"]}',
      columnHeaders: '["Region","Revenue"]',
      projectId: 'proj-1',
      modelId: 'model-1',
      pluginVersion: '1.0.0',
      timestamp: '2026-01-01T00:00:00Z',
    });
    // The footer sits one row below the body: header row 0 + bodyRows.
    sheetCells.set(`${1 + bodyRows},0`, FOOTER_TEXT);
  }

  it('R12-2: a GROWING refresh moves the provenance footer to its new row instead of overwriting it with result data', async () => {
    // Reproduces the gate finding: A1:B11 with its footer at A12 grows to 200
    // rows. Pre-fix the new body overwrote A12 and NO new footer was written
    // (A202 stayed null) -- reported as a clean "refreshed" success.
    await seedFooterTable(10, 200);
    const { refreshTables } = await import('../utils/tableRefresh');

    const result = await refreshTables('activeSheet');
    expect(result.refreshed).toContain('Table1');

    // The footer's OLD cell is now ordinary result data...
    expect(sheetCells.get('11,0')).toBe('R10');
    // ...and the footer itself moved to the row below the new body (row 201),
    // keeping its descriptive segments and picking up a fresh timestamp.
    const moved = sheetCells.get('201,0');
    expect(typeof moved).toBe('string');
    expect(String(moved)).toContain('Source: Tessallite');
    expect(String(moved)).toContain('Model: Sales');
    expect(String(moved)).not.toContain('2026-01-01 00:00 UTC');
  });

  it('R12-2: a SHRINKING refresh moves the footer UP and clears its old cell instead of orphaning it below blank rows', async () => {
    await seedFooterTable(10, 2);
    const { refreshTables } = await import('../utils/tableRefresh');

    const result = await refreshTables('activeSheet');
    expect(result.refreshed).toContain('Table1');

    // Footer now sits directly under the 2-row body (row 3)...
    expect(String(sheetCells.get('3,0'))).toContain('Source: Tessallite');
    // ...and its old position is empty, not a stranded duplicate.
    expect(sheetCells.get('11,0')).toBeUndefined();
  });

  it('R12-2: content below the table that is NOT our footer is left untouched (the refresh never manufactures a footer over user content)', async () => {
    await seedFooterTable(10, 2);
    sheetCells.set('11,0', 'Grand total');
    const { refreshTables } = await import('../utils/tableRefresh');

    const result = await refreshTables('activeSheet');
    expect(result.refreshed).toContain('Table1');
    // The user's own cell survives, and no footer is invented at the new row.
    expect(sheetCells.get('11,0')).toBe('Grand total');
    expect(sheetCells.get('3,0')).toBeUndefined();
  });

  it('R12-F4: a GROWING refresh reverts the vacated footer row\'s grey/italic styling (no accumulating grey rows inside the table body)', async () => {
    await seedFooterTable(10, 200);
    const { refreshTables } = await import('../utils/tableRefresh');

    const result = await refreshTables('activeSheet');
    expect(result.refreshed).toContain('Table1');

    // Row 11 held the footer and is now an ordinary data row: its italic/size/
    // colour must be reverted. Mutation: drop the `else if (oldFooterRow <
    // newFooterRow)` branch and one grey italic row is left per growth refresh.
    expect(fontOps).toContainEqual({ row: 11, prop: 'italic', value: false });
    // Round-2 finding 5: the revert copies the TABLE's own body font, so it is
    // right under any workbook font or table style -- not a hardcoded 11pt/black.
    expect(fontOps).toContainEqual({ row: 11, prop: 'size', value: BODY_FONT_SIZE_REF });
    expect(fontOps).toContainEqual({ row: 11, prop: 'color', value: BODY_FONT_COLOR_REF });
    // ...and the NEW footer row carries the footer styling.
    expect(fontOps).toContainEqual({ row: 201, prop: 'italic', value: true });
    expect(fontOps).toContainEqual({ row: 201, prop: 'size', value: 9 });
  });

  it('R12-F4: a SHRINKING refresh clears the vacated footer row COMPLETELY (formats too), not just its contents', async () => {
    await seedFooterTable(10, 2);
    const { refreshTables } = await import('../utils/tableRefresh');

    const result = await refreshTables('activeSheet');
    expect(result.refreshed).toContain('Table1');

    // Mutation: revert this clear to ClearApplyTo.contents and an empty
    // grey/italic row is stranded below the table forever.
    expect(clearOps).toContainEqual({ row: 11, rowCount: 1, applyTo: 'all' });
  });

  // -------------------------------------------------------------------------
  // Bug-8340 (R12 review finding 3): a GROWING refresh must never destroy the
  // user's own content to make room for itself.
  // -------------------------------------------------------------------------

  it('Bug-8340: a growing refresh whose growth zone holds the USER\'s content SKIPS honestly and mutates nothing', async () => {
    await seedFooterTable(10, 200);
    const { refreshTables } = await import('../utils/tableRefresh');
    const { strings } = await import('../i18n/strings');

    // The user typed a subtotal two rows below the table -- inside the region
    // the 200-row result would expand over. Pre-fix this was overwritten with
    // result data and reported as a clean "refreshed" success.
    sheetCells.set('13,0', 'Q3 subtotal (mine)');

    const result = await refreshTables('activeSheet');

    expect(result.skipped).toEqual([{ name: 'Table1', reason: strings.tableRefresh.growthZoneOccupiedSkip }]);
    expect(result.refreshed).toHaveLength(0);
    // NOTHING was mutated: the user's cell, the footer and the table body all
    // stand exactly as they were.
    expect(sheetCells.get('13,0')).toBe('Q3 subtotal (mine)');
    expect(sheetCells.get('11,0')).toBe(FOOTER_TEXT);
    expect(usedResize).toBe(false);
    expect(usedShiftDelete).toBe(false);
  });

  it('Bug-8340 (round-2 finding 2): a user FORMULA rendering as "" still counts as occupied (a values-only probe would destroy it)', async () => {
    await seedFooterTable(10, 200);
    const { refreshTables } = await import('../utils/tableRefresh');
    const { strings } = await import('../i18n/strings');

    // `=IF(A1>0,A1,"")` currently evaluates to '' -- invisible to a values-only
    // probe, which would classify the row as empty and overwrite the formula.
    sheetFormulas.set('13,0', '=IF(A1>0,A1,"")');

    const result = await refreshTables('activeSheet');
    expect(result.skipped).toEqual([{ name: 'Table1', reason: strings.tableRefresh.growthZoneOccupiedSkip }]);
    expect(usedResize).toBe(false);
    // The formula stands.
    expect(sheetFormulas.get('13,0')).toBe('=IF(A1>0,A1,"")');
  });

  it('Bug-8340: an EMPTY growth zone still refreshes (the probe does not block ordinary growth)', async () => {
    await seedFooterTable(10, 200);
    const { refreshTables } = await import('../utils/tableRefresh');

    const result = await refreshTables('activeSheet');
    expect(result.refreshed).toContain('Table1');
    expect(result.skipped).toHaveLength(0);
  });

  it('Bug-8340: a SHRINKING refresh is never blocked by content below the table (it claims no new ground)', async () => {
    await seedFooterTable(10, 2);
    const { refreshTables } = await import('../utils/tableRefresh');

    // Content well below the table: a shrink never touches it, so it must not
    // cause a skip (the probe applies to growth only).
    sheetCells.set('30,0', 'unrelated note');
    const result = await refreshTables('activeSheet');
    expect(result.refreshed).toContain('Table1');
    expect(sheetCells.get('30,0')).toBe('unrelated note');
  });

  // -------------------------------------------------------------------------
  // Bug-7397 R12-4: the under-lock re-validation must read the HOST, not the
  // 30s TTL cache its own Phase A read populated.
  // -------------------------------------------------------------------------

  it('R12-4: the under-lock re-validation forces a host read, so a host-side provenance change with no cache invalidation is still caught', async () => {
    await setupRefreshMocks();
    const { refreshTables } = await import('../utils/tableRefresh');

    mockTables = [{ name: 'Table1', rangeAddress: 'Sheet1!A1:B2' }];
    await setTableMetadata('Sheet1!A1:B2', {
      semanticQuery: '{"measures":["revenue"],"dimensions":["region"]}',
      columnHeaders: '["Region","Revenue"]',
      projectId: 'proj-1',
      modelId: 'model-1',
      pluginVersion: '1.0.0',
      timestamp: '2026-01-01T00:00:00Z',
    });

    // During the unlocked execute phase, the WORKBOOK's provenance changes
    // without going through setTableMetadata -- so nothing invalidates the TTL
    // cache entry that Phase A just populated for this exact address. Real
    // sources of this: a user editing/removing the hidden name in Name Manager,
    // a co-authoring peer, or any future writer that forgets to invalidate.
    onExecuteQuery = () => {
      const stamp = serverItems.find(i => i.name.endsWith('_timestamp'));
      if (stamp) stamp.comment = 'timestamp=2026-07-28T00:00:00Z';
    };

    const result = await refreshTables('activeSheet');

    // Mutation proof: drop `{ bypassCache: true }` from the under-lock
    // getTableMetadata call and this re-read is served from the Phase A cache
    // entry -- it compares the snapshot against ITSELF, finds no change, and
    // Table1 is reported refreshed instead of skipped.
    expect(result.skipped.some(s => s.name === 'Table1')).toBe(true);
    expect(result.refreshed).not.toContain('Table1');
    expect(usedResize).toBe(false);
  });

  // -------------------------------------------------------------------------
  // Bug-7397 R12-LOW-3: the columnHeaders term of the under-lock comparison.
  // -------------------------------------------------------------------------

  it('R12-LOW-3: a concurrent write that changes ONLY the stored columnHeaders causes a SKIP (the positional write is no longer authorised)', async () => {
    await setupRefreshMocks();
    const { refreshTables } = await import('../utils/tableRefresh');

    mockTables = [{ name: 'Table1', rangeAddress: 'Sheet1!A1:B2' }];
    const BASE = {
      semanticQuery: '{"measures":["revenue"],"dimensions":["region"]}',
      projectId: 'proj-1',
      modelId: 'model-1',
      pluginVersion: '1.0.0',
      timestamp: '2026-01-01T00:00:00Z',
    };
    await setTableMetadata('Sheet1!A1:B2', { ...BASE, columnHeaders: '["Region","Revenue"]' });

    // Same query, same project/model, SAME timestamp -- only the column layout
    // changed. Phase A authorised a POSITIONAL write against the old header
    // order, so proceeding would write values into the wrong columns.
    // Mutation: delete the `current.columnHeaders !== metadata.columnHeaders`
    // term from the under-lock comparison and Table1 is refreshed, not skipped.
    let concurrentWrite: Promise<void> | null = null;
    onExecuteQuery = () => {
      concurrentWrite = setTableMetadata('Sheet1!A1:B2', { ...BASE, columnHeaders: '["Revenue","Region"]' });
    };

    const result = await refreshTables('activeSheet');
    await concurrentWrite;

    expect(result.skipped.some(s => s.name === 'Table1')).toBe(true);
    expect(result.refreshed).not.toContain('Table1');
    expect(usedResize).toBe(false);
  });

  // -------------------------------------------------------------------------
  // Bug-7397 R12-3: a wedged holder produces an honest, user-visible skip.
  // -------------------------------------------------------------------------

  it('R12-3: when the covering blocks stay held past the deadline the table is SKIPPED with a retry reason, and nothing is mutated', async () => {
    await setupRefreshMocks();
    const { refreshTables } = await import('../utils/tableRefresh');
    const { strings } = await import('../i18n/strings');

    mockTables = [{ name: 'Table1', rangeAddress: 'Sheet1!A1:B2' }];
    await setTableMetadata('Sheet1!A1:B2', {
      semanticQuery: '{"measures":["revenue"],"dimensions":["region"]}',
      columnHeaders: '["Region","Revenue"]',
      projectId: 'proj-1',
      modelId: 'model-1',
      pluginVersion: '1.0.0',
      timestamp: '2026-01-01T00:00:00Z',
    });

    // Simulate an operation wedged inside its critical section (e.g. a
    // context.sync() that never resolves) holding the blocks this table needs.
    let release: () => void = () => {};
    const wedged = withTableLocksKeys(
      blockKeysForAddress('Sheet1!A1:B4'),
      () => new Promise<void>((r) => { release = r; }),
    );

    const result = await refreshTables('activeSheet', null, { lockTimeoutMs: 20 });

    expect(result.skipped).toEqual([{ name: 'Table1', reason: strings.tableRefresh.lockBusySkip }]);
    expect(result.refreshed).toHaveLength(0);
    expect(usedResize).toBe(false);
    expect(usedShiftDelete).toBe(false);

    release();
    await wedged;
  });

  it('R10-1: rewriteTableBody resizes IN PLACE and never uses a shifting body delete/add (footprint == declared rectangle)', async () => {
    await setupRefreshMocks();
    const { refreshTables } = await import('../utils/tableRefresh');

    mockTables = [{ name: 'Table1', rangeAddress: 'Sheet1!A1:B2' }];
    mockQueryResult = Array.from({ length: 8 }, (_, i) => ({ region: `R${i}`, revenue: i }));
    await setTableMetadata('Sheet1!A1:B2', {
      semanticQuery: '{"measures":["revenue"],"dimensions":["region"]}',
      columnHeaders: '["Region","Revenue"]',
      projectId: 'proj-1',
      modelId: 'model-1',
      pluginVersion: '1.0.0',
      timestamp: '2026-01-01T00:00:00Z',
    });

    const result = await refreshTables('activeSheet');
    expect(result.refreshed).toContain('Table1');
    // In-place rewrite: resize used, NO shifting body delete/add (which would
    // mutate cells below the table, outside the locked block footprint).
    // Mutation: restoring bodyRange.delete(up) + rows.add flips usedShiftDelete.
    expect(usedResize).toBe(true);
    expect(usedShiftDelete).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Bug-8424: a metadata READ FAILURE is not an empty table.
// ---------------------------------------------------------------------------

describe('Bug-8424 — getTableMetadata reports a failed read instead of caching {}', () => {
  beforeEach(() => {
    invalidateMetadataCache();
    vi.spyOn(console, 'warn').mockImplementation(() => {});
  });

  afterEach(() => {
    vi.stubGlobal('Excel', undefined);
    vi.restoreAllMocks();
  });

  it('returns the fetch-failure sentinel when the host throws', async () => {
    vi.stubGlobal('Excel', {
      run: async () => { throw new Error('transient host error'); },
    });
    const meta = await getTableMetadata('Sheet1!A1:D10');
    expect(isMetadataFetchFailure(meta)).toBe(true);
    // Pre-fix this was an indistinguishable `{}`.
    expect(Object.keys(meta)).not.toEqual([]);
  });

  it('does NOT cache the failure — the very next read reaches the host again', async () => {
    // The defect: the empty result was cached for the full 30s TTL, so ONE
    // transient error silently disabled refresh for every table in that window.
    let calls = 0;
    vi.stubGlobal('Excel', {
      run: async () => { calls++; throw new Error('transient host error'); },
    });
    await getTableMetadata('Sheet1!A1:D10');
    await getTableMetadata('Sheet1!A1:D10');
    expect(calls).toBe(2);
  });

  it('a genuinely empty table is NOT reported as a failure', async () => {
    vi.stubGlobal('Excel', {
      run: async (cb: (ctx: unknown) => Promise<unknown>) => cb({
        workbook: { names: { items: [], load: () => {} } },
        sync: async () => {},
      }),
    });
    const meta = await getTableMetadata('Sheet1!A1:D10');
    expect(isMetadataFetchFailure(meta)).toBe(false);
  });

  it('a recovered host read replaces the failure with real provenance', async () => {
    let fail = true;
    const items = [
      { name: '__tessallite_' + hashSheetName('Sheet1') + '_A1_projectId', comment: 'projectId=p1' },
    ];
    vi.stubGlobal('Excel', {
      run: async (cb: (ctx: unknown) => Promise<unknown>) => {
        if (fail) throw new Error('transient host error');
        return cb({ workbook: { names: { items, load: () => {} } }, sync: async () => {} });
      },
    });
    expect(isMetadataFetchFailure(await getTableMetadata('Sheet1!A1:D10'))).toBe(true);
    fail = false;
    const meta = await getTableMetadata('Sheet1!A1:D10');
    expect(isMetadataFetchFailure(meta)).toBe(false);
    expect(meta.projectId).toBe('p1');
  });
});
