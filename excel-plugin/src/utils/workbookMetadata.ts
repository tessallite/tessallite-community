/**
 * Workbook metadata persistence for inserted tables.
 * Stores project, model, query, and plugin metadata on tables
 * using Excel custom properties and hidden named ranges.
 */

const METADATA_PREFIX = '__tessallite_';

// F-26: TTL cache for getTableMetadata to avoid repeated Excel.run calls
const _metadataCache = new Map<string, { data: Partial<TableMetadata>; ts: number }>();
const METADATA_CACHE_TTL_MS = 30_000;

export function invalidateMetadataCache(): void {
  _metadataCache.clear();
}

export interface TableMetadata {
  projectId?: string;
  modelId?: string;
  personaId?: string;
  conversationId?: string;
  turnId?: string;
  semanticQuery?: string;
  columnHeaders?: string;
  measureColumns?: string;
  dimensionColumns?: string;
  pluginVersion: string;
  timestamp: string;
}

/**
 * Extract the sheet name and start cell from a range address.
 * E.g. "Sheet1!A1:D10" -> { sheetName: "Sheet1", startCell: "A1", endCell: "D10" }
 */
function parseRangeAddress(rangeAddress: string): { sheetName: string; startCell: string; endCell: string } {
  // F-35: Handle quoted sheet names like "'My Sheet'!A1:D10" or "'It''s a Sheet'!A1:D10"
  const quotedMatch = rangeAddress.match(/^'([^']*(?:''[^']*)*)'!(.+)$/);
  if (quotedMatch) {
    const sheetName = quotedMatch[1].replace(/''/g, "'");
    const rangePart = quotedMatch[2];
    const cells = rangePart.split(':');
    return {
      sheetName,
      startCell: cells[0] || rangePart,
      endCell: cells[1] || cells[0] || rangePart,
    };
  }
  const parts = rangeAddress.split('!');
  if (parts.length === 2) {
    const rangePart = parts[1] || parts[0];
    const cells = rangePart.split(':');
    return {
      sheetName: parts[0],
      startCell: cells[0] || rangePart,
      endCell: cells[1] || cells[0] || rangePart,
    };
  }
  return { sheetName: '', startCell: rangeAddress, endCell: rangeAddress };
}

/**
 * F-025-19: Excel named-item names forbid spaces and most punctuation, and a
 * sheet name embedded directly in a name breaks for sheets like "Q1 Report" or
 * names containing the "_" delimiter we split on. Hash the sheet name into a
 * short stable token so the named-item key is always a legal identifier and the
 * slow-path key split is unambiguous. Deterministic (same sheet -> same token).
 */
export function hashSheetName(sheetName: string): string {
  let h = 5381;
  for (let i = 0; i < sheetName.length; i++) {
    h = ((h << 5) + h + sheetName.charCodeAt(i)) >>> 0;
  }
  return `s${h.toString(36)}`;
}

/**
 * F-025-19: quote a sheet name for use in a range reference when it contains a
 * space or punctuation, per Excel's formula rules ('My Sheet'!A1). A literal
 * single quote inside the name is doubled.
 */
export function quoteSheetRef(sheetName: string, rangeRef: string): string {
  if (/^[A-Za-z_][A-Za-z0-9_.]*$/.test(sheetName)) {
    return `${sheetName}!${rangeRef}`;
  }
  return `'${sheetName.replace(/'/g, "''")}'!${rangeRef}`;
}

/**
 * Parse a cell reference like "A1" into { col: 0, row: 0 } (0-indexed).
 */
function parseCellRef(cell: string): { col: number; row: number } {
  const match = cell.match(/^([A-Z]+)(\d+)$/i);
  if (!match) return { col: 0, row: 0 };
  const colStr = match[1].toUpperCase();
  let col = 0;
  for (let i = 0; i < colStr.length; i++) {
    col = col * 26 + (colStr.charCodeAt(i) - 64);
  }
  return { col: col - 1, row: parseInt(match[2], 10) - 1 };
}

/**
 * Check if a cell address falls within a range address.
 */
function isCellInRange(cellAddress: string, rangeAddress: string): boolean {
  const cellParsed = parseRangeAddress(cellAddress);
  const rangeParsed = parseRangeAddress(rangeAddress);
  if (cellParsed.sheetName !== rangeParsed.sheetName) return false;
  const cell = parseCellRef(cellParsed.startCell);
  const start = parseCellRef(rangeParsed.startCell);
  const end = parseCellRef(rangeParsed.endCell);
  return cell.col >= start.col && cell.col <= end.col &&
    cell.row >= start.row && cell.row <= end.row;
}

export async function setTableMetadata(
  rangeAddress: string,
  metadata: TableMetadata,
): Promise<void> {
  _metadataCache.delete(rangeAddress);
  try {
    await Excel.run(async (context) => {
      const { sheetName, startCell } = parseRangeAddress(rangeAddress);
      // F-025-19: hash the sheet name so the named-item key is always a legal
      // identifier (sheets like "Q1 Report" or names with "_" no longer break
      // the name rules or the slow-path key split).
      const rangeKey = `${hashSheetName(sheetName)}_${startCell}`;

      // Remove old named items for this table range first
      const namedItems = context.workbook.names;
      namedItems.load('items/name');
      await context.sync();

      const staleNames = namedItems.items
        .filter(item => item.name.startsWith(`${METADATA_PREFIX}${rangeKey}_`))
        .map(item => item.name);

      for (const staleName of staleNames) {
        context.workbook.names.getItemOrNullObject(staleName).delete();
      }

      // Write new metadata entries with deterministic keys
      const rangeRef = rangeAddress.split('!')[1] || rangeAddress;
      // F-025-19: quote the sheet name in the range reference when it contains
      // spaces/punctuation, otherwise "My Sheet!A1:D10" is an invalid formula
      // and the named-item add throws (silently losing all provenance).
      const sheetRef = quoteSheetRef(sheetName, rangeRef);
      for (const [key, value] of Object.entries(metadata)) {
        if (value !== undefined && value !== null) {
          const name = `${METADATA_PREFIX}${rangeKey}_${key}`;
          try {
            const namedItem = context.workbook.names.add(name, sheetRef);
            namedItem.comment = `${key}=${String(value)}`;
          } catch {
            // Named item may already exist; skip
          }
        }
      }

      // Store the full table range as a named item for containment lookup
      try {
        const rangeItem = context.workbook.names.add(
          `${METADATA_PREFIX}${rangeKey}__table_range`,
          sheetRef,
        );
        rangeItem.comment = `__table_range=${rangeAddress}`;
      } catch {
        // non-critical
      }

      await context.sync();
    });
  } catch {
    // Metadata is non-critical; fail silently
  }
}

// ---- Entity usage manifest (Phase 5) ----

declare const OfficeRuntime: {
  storage: {
    getItem(key: string): Promise<string | null>;
    setItem(key: string, value: string): Promise<void>;
    removeItem(key: string): Promise<void>;
  };
};

export interface EntityUsageEntry {
  id: string;
  type: 'named_set' | 'kpi';
  displayName: string;
  certificationStatus?: string;
  updatedAt?: string;
  insertedAt: string;
  cellLocations: string[];
  // F-025-16: the workbook and model the entity was inserted into. The manifest
  // lives in OfficeRuntime.storage, which is per-add-in per-MACHINE — shared
  // across every workbook the user opens. Without these scoping keys, inserting
  // a KPI from model A in workbook 1 raised a spurious "Deleted from server"
  // warning the moment the user switched to model B or opened workbook 2.
  // Entries are now only compared against the model they belong to, in the
  // workbook they were inserted into. Legacy entries (written before this fix)
  // have neither key and are treated as belonging to the current scope so they
  // are not orphaned.
  workbookId?: string;
  modelId?: string;
}

// F-025-16: a stable per-workbook identifier persisted in the workbook's own
// settings (Office.context.document.settings), so it travels with the .xlsx
// file and is distinct per workbook. Read once and memoised per session.
let _workbookIdPromise: Promise<string | null> | null = null;
const WORKBOOK_ID_SETTING = 'tessallite_workbook_id';

function generateId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `wb_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
}

export async function getWorkbookId(): Promise<string | null> {
  if (_workbookIdPromise) return _workbookIdPromise;
  _workbookIdPromise = (async () => {
    try {
      if (typeof Office === 'undefined' || !Office.context?.document?.settings) return null;
      const settings = Office.context.document.settings;
      const existing = settings.get(WORKBOOK_ID_SETTING) as string | null;
      if (existing) return existing;
      const fresh = generateId();
      settings.set(WORKBOOK_ID_SETTING, fresh);
      await new Promise<void>((resolve) => {
        settings.saveAsync(() => resolve());
      });
      return fresh;
    } catch {
      return null;
    }
  })();
  return _workbookIdPromise;
}

/** Test-only: reset the memoised workbook id between cases. */
export function _resetWorkbookIdCache(): void {
  _workbookIdPromise = null;
}

/**
 * F-025-16: an entry belongs to the active scope when its workbook AND model
 * match the current context. Legacy entries with no scope keys (written before
 * this fix) are treated as in-scope so they are not falsely orphaned.
 */
function entryInScope(
  entry: EntityUsageEntry,
  workbookId: string | null,
  modelId: string | null,
): boolean {
  if (entry.workbookId && workbookId && entry.workbookId !== workbookId) return false;
  if (entry.modelId && modelId && entry.modelId !== modelId) return false;
  return true;
}

export interface EntityManifest {
  version: 1;
  entries: EntityUsageEntry[];
}

const MANIFEST_XML_KEY = 'tessallite_entity_manifest';

async function readManifestFromStorage(): Promise<EntityManifest> {
  const empty: EntityManifest = { version: 1, entries: [] };
  try {
    const raw = await OfficeRuntime.storage.getItem(MANIFEST_XML_KEY);
    if (!raw) return empty;
    return JSON.parse(raw) as EntityManifest;
  } catch {
    return empty;
  }
}

async function writeManifestToStorage(manifest: EntityManifest): Promise<void> {
  try {
    await OfficeRuntime.storage.setItem(MANIFEST_XML_KEY, JSON.stringify(manifest));
  } catch {
    // non-critical
  }
}

export async function trackEntityUsage(
  type: 'named_set' | 'kpi',
  entityId: string,
  displayName: string,
  cellAddress: string,
  certificationStatus?: string,
  updatedAt?: string,
  modelId?: string,
): Promise<void> {
  // F-025-16: tag the entry with its workbook + model so later staleness checks
  // only fire when the same model is loaded in the same workbook.
  const workbookId = await getWorkbookId();
  const manifest = await readManifestFromStorage();
  const existing = manifest.entries.find(
    e => e.id === entityId && e.type === type &&
      (e.workbookId ?? workbookId ?? null) === (workbookId ?? null) &&
      (e.modelId ?? modelId ?? null) === (modelId ?? null),
  );
  if (existing) {
    if (!existing.cellLocations.includes(cellAddress)) {
      existing.cellLocations.push(cellAddress);
    }
    if (certificationStatus) existing.certificationStatus = certificationStatus;
    if (updatedAt) existing.updatedAt = updatedAt;
    if (workbookId && !existing.workbookId) existing.workbookId = workbookId;
    if (modelId && !existing.modelId) existing.modelId = modelId;
  } else {
    manifest.entries.push({
      id: entityId,
      type,
      displayName,
      certificationStatus,
      updatedAt,
      insertedAt: new Date().toISOString(),
      cellLocations: [cellAddress],
      workbookId: workbookId ?? undefined,
      modelId,
    });
  }
  await writeManifestToStorage(manifest);
}

export async function getEntityManifest(): Promise<EntityManifest> {
  return readManifestFromStorage();
}

export interface StaleEntity {
  entry: EntityUsageEntry;
  currentStatus: string;
  reason: 'deprecated' | 'deleted' | 'status_changed' | 'version_changed';
}

export async function checkStaleEntities(
  currentEntities: { id: string; type: 'named_set' | 'kpi'; certification_status: string; updated_at?: string }[],
  modelId?: string,
): Promise<StaleEntity[]> {
  // F-025-16: only inspect manifest entries that belong to THIS workbook and
  // THIS model. The supplied `currentEntities` are the loaded model's entities,
  // so an entry for a different model/workbook must never be reported "deleted".
  const workbookId = await getWorkbookId();
  const manifest = await readManifestFromStorage();
  const stale: StaleEntity[] = [];

  const lookup = new Map(currentEntities.map(e => [`${e.type}:${e.id}`, e]));

  for (const entry of manifest.entries) {
    if (!entryInScope(entry, workbookId, modelId ?? null)) continue;
    const key = `${entry.type}:${entry.id}`;
    const current = lookup.get(key);

    if (!current) {
      stale.push({ entry, currentStatus: 'deleted', reason: 'deleted' });
    } else if (current.certification_status === 'deprecated' && entry.certificationStatus !== 'deprecated') {
      stale.push({ entry, currentStatus: current.certification_status, reason: 'deprecated' });
    } else if (entry.certificationStatus && current.certification_status !== entry.certificationStatus) {
      stale.push({ entry, currentStatus: current.certification_status, reason: 'status_changed' });
    } else if (entry.updatedAt && current.updated_at && entry.updatedAt !== current.updated_at) {
      stale.push({ entry, currentStatus: current.certification_status, reason: 'version_changed' });
    }
  }

  return stale;
}

export async function updateManifestStatuses(
  currentEntities: { id: string; type: 'named_set' | 'kpi'; certification_status: string; updated_at?: string }[],
  skipUpdatedAtKeys?: Set<string>,
  modelId?: string,
): Promise<void> {
  // F-025-16: only reconcile entries belonging to the active workbook + model.
  const workbookId = await getWorkbookId();
  const manifest = await readManifestFromStorage();
  const lookup = new Map(currentEntities.map(e => [`${e.type}:${e.id}`, e]));

  for (const entry of manifest.entries) {
    if (!entryInScope(entry, workbookId, modelId ?? null)) continue;
    const key = `${entry.type}:${entry.id}`;
    const current = lookup.get(key);
    if (current) {
      entry.certificationStatus = current.certification_status;
      if (current.updated_at && !skipUpdatedAtKeys?.has(key)) {
        entry.updatedAt = current.updated_at;
      }
    }
  }

  await writeManifestToStorage(manifest);
}

export async function removeEntityFromManifest(
  type: 'named_set' | 'kpi',
  entityId: string,
): Promise<void> {
  const manifest = await readManifestFromStorage();
  manifest.entries = manifest.entries.filter(e => !(e.id === entityId && e.type === type));
  await writeManifestToStorage(manifest);
}

export async function getTableMetadata(
  rangeAddress: string,
): Promise<Partial<TableMetadata>> {
  // F-26: Check TTL cache first
  const cached = _metadataCache.get(rangeAddress);
  if (cached && (Date.now() - cached.ts) < METADATA_CACHE_TTL_MS) {
    return { ...cached.data };
  }

  const metadata: Partial<TableMetadata> = {};
  try {
    await Excel.run(async (context) => {
      const { sheetName, startCell } = parseRangeAddress(rangeAddress);
      const namedItems = context.workbook.names;
      namedItems.load('items/name, items/comment');
      await context.sync();

      // First try exact match by start-cell key (fast path). F-025-19: keyed
      // by the hashed sheet name to match setTableMetadata.
      const rangeKey = `${hashSheetName(sheetName)}_${startCell}`;
      const directPrefix = `${METADATA_PREFIX}${rangeKey}_`;
      const directMatch = namedItems.items.find(item => item.name.startsWith(directPrefix));
      if (directMatch) {
        (metadata as Record<string, string>)['_tableStart'] = startCell;
        for (const item of namedItems.items) {
          if (item.name.startsWith(directPrefix) && item.comment && !item.name.endsWith('__table_range')) {
            const eqIdx = item.comment.indexOf('=');
            if (eqIdx > 0) {
              const key = item.comment.slice(0, eqIdx);
              const value = item.comment.slice(eqIdx + 1);
              (metadata as Record<string, string>)[key] = value;
            }
          }
        }
        return;
      }

      // Slow path: search by range containment. F-025-19: keyed by the hashed
      // sheet name; the base-key split below is now unambiguous because the
      // hash token contains no "_" delimiter.
      const sheetPrefix = `${METADATA_PREFIX}${hashSheetName(sheetName)}_`;
      const rangeItems = namedItems.items.filter(
        item => item.name.startsWith(sheetPrefix) && item.name.endsWith('__table_range') && item.comment,
      );

      for (const rangeItem of rangeItems) {
        const commentMatch = rangeItem.comment.match(/^__table_range=(.+)$/);
        if (!commentMatch) continue;
        const tableRange = commentMatch[1];
        if (isCellInRange(rangeAddress, tableRange)) {
          // Found the containing table; extract its base key and start cell
          const rangeItemSuffix = `__table_range`;
          const baseKey = rangeItem.name.slice(METADATA_PREFIX.length, -rangeItemSuffix.length);
          const parts = baseKey.split('_');
          const tableStartCell = parts.length >= 2 ? parts[parts.length - 1] : startCell;
          (metadata as Record<string, string>)['_tableStart'] = tableStartCell;
          const metaPrefix = `${METADATA_PREFIX}${baseKey}_`;
          for (const item of namedItems.items) {
            if (item.name.startsWith(metaPrefix) && item.comment && !item.name.endsWith('__table_range')) {
              const eqIdx = item.comment.indexOf('=');
              if (eqIdx > 0) {
                const key = item.comment.slice(0, eqIdx);
                const value = item.comment.slice(eqIdx + 1);
                (metadata as Record<string, string>)[key] = value;
              }
            }
          }
          return;
        }
      }
    });
  } catch {
    // Metadata is non-critical; fail silently
  }
  _metadataCache.set(rangeAddress, { data: { ...metadata }, ts: Date.now() });
  return metadata;
}
