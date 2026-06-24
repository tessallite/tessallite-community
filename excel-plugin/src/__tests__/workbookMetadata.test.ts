import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  checkStaleEntities, updateManifestStatuses, trackEntityUsage,
  hashSheetName, quoteSheetRef, getEntityManifest, _resetWorkbookIdCache,
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
