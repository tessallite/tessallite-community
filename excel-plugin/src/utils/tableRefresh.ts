/**
 * Task 2: Refresh tracked tables — re-execute Tessallite-inserted tables
 * on the active sheet using their stored semantic queries.
 *
 * Fail-closed: tables whose stored projectId/modelId do not match the active
 * session are SKIPPED (never silently re-pointed). Tables whose returned
 * column set drifts from the stored headers are also skipped (schema changed).
 * Pivoted (cross-tab) tables are skipped because the pivot layout is not
 * reconstructible from the stored semanticQuery alone.
 * Agent-chat tables (source: 'agent') are skipped with an honest reason.
 */

import {
  getTableMetadata,
  isMetadataFetchFailure,
  setTableMetadataWithinLock,
  removeTableMetadataWithinLock,
  unionTableRangeAddress,
  expandRangeRows,
  blockKeysForAddress,
  rangeRowColBounds,
  withTableLocksKeys,
  tableRangeKey,
  LockAcquireTimeoutError,
  type TableMetadata,
  invalidateMetadataCache,
} from './workbookMetadata';
import { getModelContext } from './storage';
import { executeQuery, type PluginExecuteParams } from '../api/queryRouter';
import type { SemanticQuery, ExecuteResponse } from '../types/tessallite';
import { strings, templates } from '../i18n/strings';
import {
  isProvenanceFooter,
  restampProvenanceFooter,
  PROVENANCE_FOOTER_ROWS,
  FOOTER_FONT_ITALIC,
  FOOTER_FONT_SIZE,
  FOOTER_FONT_COLOR,
  BODY_FONT_SIZE,
  BODY_FONT_COLOR,
} from './provenanceFooter';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface RefreshableTable {
  name: string;
  rangeAddress: string;
  metadata: Partial<TableMetadata>;
}

export interface RefreshResult {
  refreshed: string[];
  skipped: { name: string; reason: string }[];
  // Bug-7397 R6: the table's DATA was refreshed successfully, but a non-fatal
  // issue occurred (e.g. the provenance write-back failed). Reported honestly
  // rather than swallowed, so the UI can warn and the user knows a later
  // refresh may not work.
  warnings: { name: string; reason: string }[];
}

/**
 * Bug-7397 R12-5: one flattened row per table the user needs to know about,
 * ready for the details surface. Pure and exported so the mapping from
 * skipped/warnings to a displayable list is testable without rendering, and so
 * the producer (refreshTables) and the consumer (RefreshDetailsPanel) share one
 * shape instead of the UI re-deriving it.
 */
export interface RefreshDetailRow {
  name: string;
  reason: string;
  kind: 'skipped' | 'warning';
}

/**
 * Outcome of one in-place table rewrite. `out-of-bounds` and
 * `growth-zone-occupied` both mean NOTHING was mutated -- they are separated
 * because the user's remedy differs (refresh again vs clear the cells below).
 */
type RewriteOutcome = 'written' | 'out-of-bounds' | 'growth-zone-occupied';

/**
 * Bug-7397 R12 review round 4, finding 2: an `Excel.run` batch is NOT
 * transactional. If `context.sync()` rejects after the resize/values/clear
 * operations were already applied, the sheet HAS changed. Reporting that as an
 * ordinary skip ("was not changed at all, refresh again") is a false statement
 * about the user's data and invites a retry against a half-rewritten table.
 * `rewriteTableBody` therefore reports whether it had queued any mutation when
 * it failed, and the caller downgrades to a warning naming the table.
 */
export class PartialRewriteError extends Error {
  constructor(readonly cause: unknown) {
    super('The table rewrite failed after some changes had already been applied');
    this.name = 'PartialRewriteError';
  }
}

export function buildRefreshDetailRows(result: RefreshResult): RefreshDetailRow[] {
  return [
    ...result.skipped.map(s => ({ name: s.name, reason: s.reason, kind: 'skipped' as const })),
    ...result.warnings.map(w => ({ name: w.name, reason: w.reason, kind: 'warning' as const })),
  ];
}

// ---------------------------------------------------------------------------
// Pure logic: list refreshable tables (testable without Excel)
// ---------------------------------------------------------------------------

/**
 * From a list of table entries (name + range + metadata), filter to those that
 * have enough provenance to be refreshable: semanticQuery + projectId + modelId.
 */
export function filterRefreshableTables(
  tables: { name: string; rangeAddress: string; metadata: Partial<TableMetadata> }[],
): RefreshableTable[] {
  return tables.filter(
    t => t.metadata.semanticQuery && t.metadata.projectId && t.metadata.modelId,
  ) as RefreshableTable[];
}

/**
 * Validate a single table's metadata against the active model context.
 * Returns null if valid, or a skip reason string if mismatched.
 */
export function validateTableContext(
  metadata: Partial<TableMetadata>,
  activeProjectId: string,
  activeModelId: string,
): string | null {
  if (metadata.projectId !== activeProjectId) {
    return strings.tableRefresh.projectMismatchSkip;
  }
  if (metadata.modelId !== activeModelId) {
    return strings.tableRefresh.modelMismatchSkip;
  }
  return null;
}

/**
 * Parse stored column headers from their persisted JSON string format.
 * The producer (useExcel.ts) stores headers as JSON.stringify(string[]).
 * Returns null if parsing fails (corrupted metadata).
 */
export function parseStoredColumnHeaders(raw: string): string[] | null {
  try {
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed) && parsed.every(h => typeof h === 'string')) {
      return parsed;
    }
    return null;
  } catch {
    return null;
  }
}

/**
 * Compare returned display headers against stored display headers.
 * Both arrays must be the same type (display names).
 * Returns null if they match, or a reason string on drift.
 */
export function validateColumnHeaders(
  returnedHeaders: string[],
  storedHeaders: string[],
): string | null {
  if (storedHeaders.length !== returnedHeaders.length) {
    return templates.tableRefresh.columnCountChanged();
  }
  for (let i = 0; i < storedHeaders.length; i++) {
    if (storedHeaders[i] !== returnedHeaders[i]) {
      return templates.tableRefresh.columnRenamed(storedHeaders[i], returnedHeaders[i]);
    }
  }
  return null;
}

/**
 * F3: Detect agent-chat provenance objects that are not executable queries.
 * Agent inserts store { source: 'agent', conversation_id, message_id }.
 */
export function isAgentSourcedQuery(parsed: unknown): boolean {
  if (typeof parsed !== 'object' || parsed === null) return false;
  const obj = parsed as Record<string, unknown>;
  return obj.source === 'agent' || !Array.isArray(obj.measures);
}

/**
 * Build display headers from an ExecuteResponse using its annotation titles.
 * Falls back to technical keys when no annotation is available.
 */
function buildDisplayHeaders(
  data: Record<string, unknown>[],
  annotation?: ExecuteResponse['annotation'],
): string[] {
  if (!data || data.length === 0) return [];
  const technicalKeys = Object.keys(data[0]);
  if (!annotation) return technicalKeys;

  const titles: Record<string, string> = {};
  if (annotation.measures) {
    for (const [k, m] of Object.entries(annotation.measures)) titles[k] = m.title;
  }
  if (annotation.dimensions) {
    for (const [k, d] of Object.entries(annotation.dimensions)) titles[k] = d.title;
  }
  return technicalKeys.map(k => titles[k] ?? k);
}

// ---------------------------------------------------------------------------
// Excel-dependent: enumerate tables on active sheet
// ---------------------------------------------------------------------------

/**
 * Enumerate Excel tables on the active sheet (or workbook) and read their
 * Tessallite metadata. Returns raw entries for filterRefreshableTables.
 */
export async function listTrackedTables(
  scope: 'activeSheet' | 'workbook',
): Promise<{ name: string; rangeAddress: string; metadata: Partial<TableMetadata> }[]> {
  const results: { name: string; rangeAddress: string; metadata: Partial<TableMetadata> }[] = [];

  if (typeof Excel === 'undefined') return results;

  await Excel.run(async (context) => {
    const tables = scope === 'activeSheet'
      ? context.workbook.worksheets.getActiveWorksheet().tables
      : context.workbook.tables;
    tables.load('items/name');
    await context.sync();

    // Load ranges for all tables
    const ranges = tables.items.map(t => {
      const range = t.getRange();
      range.load('address');
      return range;
    });
    await context.sync();

    for (let i = 0; i < tables.items.length; i++) {
      const address = ranges[i].address;
      const metadata = await getTableMetadata(address);
      results.push({ name: tables.items[i].name, rangeAddress: address, metadata });
    }
  });

  return results;
}

/**
 * Bug-7397 R6: enumerate table identities (name + range address) ONLY, without
 * reading provenance. The authoritative metadata read must happen INSIDE the
 * per-table lock (see refreshTables), so this cheap pass just tells the loop
 * which tables exist and where. Reading metadata here (as listTrackedTables
 * does) would take a snapshot OUTSIDE the lock that a concurrent insert could
 * supersede before we ever acquire it -- the exact stale-read gap the lock
 * closes.
 */
export async function listTrackedTableRanges(
  scope: 'activeSheet' | 'workbook',
): Promise<{ name: string; rangeAddress: string }[]> {
  const results: { name: string; rangeAddress: string }[] = [];

  if (typeof Excel === 'undefined') return results;

  await Excel.run(async (context) => {
    const tables = scope === 'activeSheet'
      ? context.workbook.worksheets.getActiveWorksheet().tables
      : context.workbook.tables;
    tables.load('items/name');
    await context.sync();

    const ranges = tables.items.map(t => {
      const range = t.getRange();
      range.load('address');
      return range;
    });
    await context.sync();

    for (let i = 0; i < tables.items.length; i++) {
      results.push({ name: tables.items[i].name, rangeAddress: ranges[i].address });
    }
  });

  return results;
}

// ---------------------------------------------------------------------------
// Core refresh logic
// ---------------------------------------------------------------------------

/**
 * Bug-7397 R9: refresh ONE table under SPATIAL BLOCK LOCKS.
 *
 * A refresh only learns its intended (post-growth) extent AFTER executeQuery
 * returns, so the sequence is: read metadata + execute the query UNLOCKED
 * (nothing is written yet) -> compute the union of the current and intended
 * extent -> acquire the block locks covering that union -> RE-READ and
 * re-validate metadata under the lock -> rewrite -> write back. The blocks are
 * the exclusion mechanism (any insert/refresh touching these cells shares a
 * block key by construction); the under-lock re-read is only a consistency
 * check that the provenance we executed against was not superseded by a
 * concurrent insert during the unlocked phase -- if it was, we skip (the
 * concurrent write stands), we never write a stale query's result over it.
 */
async function refreshTable(
  table: { name: string; rangeAddress: string },
  activeCtx: { projectId: string; modelId: string },
  personaId: string | null | undefined,
  result: RefreshResult,
  lockTimeoutMs?: number,
): Promise<void> {
  // ---- Phase A (unlocked): read provenance, validate, execute the query. ----
  const metadata = await getTableMetadata(table.rangeAddress);

  // Bug-8424: a metadata READ FAILURE is not an absent-provenance table. It
  // used to land in the silent branch below and drop the table with no reason,
  // so a transient host error made Refresh report fewer tables than the user
  // has, with no visible cause. Report it like every other degrade path here.
  if (isMetadataFetchFailure(metadata)) {
    result.skipped.push({ name: table.name, reason: strings.tableRefresh.metadataFetchFailedSkip });
    return;
  }

  // Not (or no longer) refreshable. A plain non-Tessallite table is silently
  // ignored; a table that LOST its provenance is reported honestly.
  if (!metadata.projectId && !metadata.modelId && !metadata.semanticQuery) {
    return; // never was a tracked Tessallite table
  }
  if (!metadata.semanticQuery || !metadata.projectId || !metadata.modelId) {
    result.skipped.push({ name: table.name, reason: strings.tableRefresh.metadataModifiedSkip });
    return;
  }

  // Fail-closed: model/project mismatch -> skip.
  const mismatch = validateTableContext(metadata, activeCtx.projectId, activeCtx.modelId);
  if (mismatch) {
    result.skipped.push({ name: table.name, reason: mismatch });
    return;
  }

  // Parse the stored semantic query.
  let parsed: unknown;
  try {
    parsed = JSON.parse(metadata.semanticQuery);
  } catch {
    result.skipped.push({ name: table.name, reason: strings.tableRefresh.corruptedQuerySkip });
    return;
  }

  // F3: Detect agent-chat provenance objects (not executable queries).
  if (isAgentSourcedQuery(parsed)) {
    result.skipped.push({ name: table.name, reason: strings.tableRefresh.agentSourceSkip });
    return;
  }

  const query = parsed as SemanticQuery;

  // Execute via the governed plugin-execute endpoint.
  const params: PluginExecuteParams = {
    projectId: activeCtx.projectId,
    modelId: activeCtx.modelId,
    personaId: personaId || undefined,
  };

  let executeResult: ExecuteResponse;
  try {
    executeResult = await executeQuery(query, params);
  } catch {
    result.skipped.push({ name: table.name, reason: strings.tableRefresh.executionFailedSkip });
    return;
  }

  if (!executeResult.data || executeResult.data.length === 0) {
    result.skipped.push({ name: table.name, reason: strings.tableRefresh.noResultsSkip });
    return;
  }

  // F2: Build display headers using the response annotation.
  const displayHeaders = buildDisplayHeaders(
    executeResult.data as Record<string, unknown>[],
    executeResult.annotation,
  );

  // F1: Validate column drift using properly parsed stored headers. Fail-closed.
  if (!metadata.columnHeaders) {
    result.skipped.push({ name: table.name, reason: strings.tableRefresh.noStoredHeadersSkip });
    return;
  }
  const storedHeaders = parseStoredColumnHeaders(metadata.columnHeaders);
  if (!storedHeaders) {
    result.skipped.push({ name: table.name, reason: strings.tableRefresh.corruptedHeadersSkip });
    return;
  }
  const drift = validateColumnHeaders(displayHeaders, storedHeaders);
  if (drift) {
    if (!metadata.measureColumns && !metadata.dimensionColumns) {
      result.skipped.push({ name: table.name, reason: strings.tableRefresh.pivotedSkip });
    } else {
      result.skipped.push({ name: table.name, reason: drift });
    }
    return;
  }

  // Build rows in the same column order as the display headers.
  const technicalKeys = Object.keys(executeResult.data[0]);
  const rows: (string | number | boolean)[][] =
    (executeResult.data as Record<string, unknown>[]).map(row =>
      technicalKeys.map(k => {
        const val = row[k];
        if (val === null || val === undefined) return '';
        if (typeof val === 'number' || typeof val === 'boolean') return val;
        return String(val);
      }),
    );

  // ---- Phase B: acquire the block locks covering the UNION of the current
  // and intended (post-growth) extent, then re-validate + rewrite + write back. ----
  // Bug-7397 R11-1 (a): re-fetch the LIVE range immediately before computing the
  // union so the declaration starts fresh -- the enumeration snapshot may be
  // stale (a user can auto-expand a table by typing below it, and table N is
  // reached after N-1 tables' worth of round trips). (b) below makes it hold BY
  // CONSTRUCTION even if the live table grows again after this read.
  const liveAddress = (await getActualTableRange(table.name)) || table.rangeAddress;
  const unionAddr = unionTableRangeAddress(liveAddress, rows.length + 1, displayHeaders.length);
  // Bug-7397 R12-2: the PROVENANCE FOOTER is part of this operation's footprint,
  // not an afterthought. It sits one row below the body, so a refresh that grows
  // the table writes result data over the footer's current cell, and a refresh
  // that shrinks it strands the footer below cleared rows. Extending the union
  // by PROVENANCE_FOOTER_ROWS brings BOTH the footer's current position and its
  // post-resize position inside the locked rectangle, the under-lock bounds
  // check, and the single rewrite batch -- so the footer moves atomically with
  // the body instead of being eaten or orphaned.
  const lockedAddr = expandRangeRows(unionAddr, PROVENANCE_FOOTER_ROWS);
  const blockKeys = blockKeysForAddress(lockedAddr);
  const declaredBounds = rangeRowColBounds(lockedAddr);

  try {
    await withTableLocksKeys(blockKeys, async () => {
      // Re-read provenance under the lock. The held blocks cover every cell we
      // will touch, so this is a consistency check (not the exclusion): if a
      // PROVENANCE writer intervened during the unlocked Phase A, the result we
      // computed is for a stale snapshot -> skip; the newer write stands and is
      // never overwritten. Bug-7397 R10-2: compare the TIMESTAMP (every metadata
      // write stamps a fresh one) so a concurrent re-insert of the SAME query
      // with new data is also caught -- and columnHeaders, since Phase A's drift
      // check authorised a POSITIONAL write against the old header order.
      //
      // Bug-7397 R12-4: this MUST bypass the TTL cache. `getTableMetadata` keeps
      // results for METADATA_CACHE_TTL_MS, and Phase A's own read populated that
      // cache for this very address -- so a cached re-read would compare the
      // original snapshot against ITSELF, doing zero host I/O and structurally
      // unable to report a change. It happened to behave correctly only because
      // provenance writes invalidate the entry for their own rangeKey; that
      // coupling is incidental, not a property of this check. Forcing a host
      // read makes the check correct by its own logic, and stops depending on an
      // invalidation that lives in another module.
      //
      // SCOPE, stated honestly: this compares PROVENANCE. It cannot see a writer
      // that changed cells without changing provenance (a formula/literal/KPI
      // write). Those are excluded by the BLOCK LOCKS -- since R12-1 every
      // first-party cell writer acquires the blocks covering the cells it
      // writes, so such a writer cannot be running concurrently here at all.
      const current = await getTableMetadata(table.rangeAddress, { bypassCache: true });
      // Bug-8424: a FAILED re-read has every field undefined, so the comparison
      // below would call it a concurrent modification — a confident, wrong
      // reason for a read that simply did not happen. Say what actually
      // occurred; we still refuse to write, which is the safe outcome either way.
      if (isMetadataFetchFailure(current)) {
        result.skipped.push({ name: table.name, reason: strings.tableRefresh.metadataFetchFailedSkip });
        return;
      }
      if (current.semanticQuery !== metadata.semanticQuery ||
          current.projectId !== metadata.projectId ||
          current.modelId !== metadata.modelId ||
          current.timestamp !== metadata.timestamp ||
          current.columnHeaders !== metadata.columnHeaders) {
        result.skipped.push({ name: table.name, reason: strings.tableRefresh.concurrentModificationSkip });
        return;
      }

      // One timestamp for BOTH the rewritten footer and the persisted
      // provenance, so the date a reader sees in the sheet is the date stored
      // with the table (two `new Date()` calls could straddle a minute
      // boundary and disagree).
      const refreshTimestamp = new Date().toISOString();

      try {
        // Bug-7397 R11-1 (b): rewriteTableBody validates, UNDER THE LOCK, that the
        // live table still fits the declared/locked rectangle before mutating. If
        // the table grew past it since the union was computed, it returns false
        // and writes NOTHING -> skip honestly (never clear/erase unlocked cells).
        const outcome = await rewriteTableBody(table.name, rows, declaredBounds, refreshTimestamp);
        if (outcome === 'out-of-bounds') {
          // Distinct from a provenance change: the table itself grew/moved past
          // the locked rectangle, so we skipped rather than touch unlocked cells.
          result.skipped.push({ name: table.name, reason: strings.tableRefresh.tableResizedSkip });
          return;
        }
        if (outcome === 'growth-zone-occupied') {
          // Bug-8340: the query now returns more rows than the table holds, and
          // the rows it would grow into are not empty. Refuse rather than
          // destroy the user's own content -- the details surface names the
          // table and tells them what to clear.
          result.skipped.push({ name: table.name, reason: strings.tableRefresh.growthZoneOccupiedSkip });
          return;
        }
        // Bug-7397 fix #3: read the table's ACTUAL post-resize range so
        // __table_range reflects the current extent.
        const actualRange = await getActualTableRange(table.name);
        const writeBackAddress = actualRange || table.rangeAddress;

        // F5a: write back the updated timestamp/provenance. setTableMetadataWithinLock
        // (NOT the self-locking setTableMetadata) because we already hold the blocks.
        const wroteBack = await updateTableTimestamp(writeBackAddress, current, refreshTimestamp);
        result.refreshed.push(table.name);
        if (!wroteBack) {
          result.warnings.push({ name: table.name, reason: strings.tableRefresh.provenanceWriteBackFailed });
        }

        // Defensive (Bug-7397 R6): a resize that MOVES the start cell would orphan
        // the old key's provenance. rewriteTableBody keeps the header row fixed so
        // this is currently unreachable, but clean up to guard future changes.
        if (actualRange && wroteBack && tableRangeKey(actualRange) !== tableRangeKey(table.rangeAddress)) {
          await removeTableMetadataWithinLock(table.rangeAddress);
        }
      } catch (err) {
        if (err instanceof PartialRewriteError) {
          // Bug-7397 R12-R4-2: the sheet HAS changed -- calling this a skip
          // ("was not changed at all") would be a false statement about the
          // user's data, and would invite a retry against a half-rewritten
          // table. Surface it as a warning naming the table instead.
          result.warnings.push({ name: table.name, reason: strings.tableRefresh.partialRewriteWarning });
          return;
        }
        result.skipped.push({ name: table.name, reason: strings.tableRefresh.writeFailedSkip });
      }
    }, lockTimeoutMs === undefined ? undefined : { timeoutMs: lockTimeoutMs });
  } catch (err) {
    // Bug-7397 R12-3: the blocks covering this table were held past the
    // acquisition deadline (a wedged Office host call in another operation's
    // critical section). Nothing was read, executed against, or written here --
    // report it as an honest skip with a retry hint rather than hanging the
    // whole refresh silently.
    if (err instanceof LockAcquireTimeoutError) {
      result.skipped.push({ name: table.name, reason: strings.tableRefresh.lockBusySkip });
      return;
    }
    throw err;
  }
}

/**
 * Refresh all Tessallite-tracked tables in the given scope.
 * Re-executes each stored semantic query through the governed plugin-execute
 * endpoint under the CURRENT persona.
 */
export async function refreshTables(
  scope: 'activeSheet' | 'workbook',
  personaId?: string | null,
  // Bug-7397 R12-3: how long each table waits for the cell blocks it needs
  // before skipping honestly. Omitted in production (the module default
  // applies); an explicit value lets a caller shorten the wait.
  options?: { lockTimeoutMs?: number },
): Promise<RefreshResult> {
  const result: RefreshResult = { refreshed: [], skipped: [], warnings: [] };

  const activeCtx = await getModelContext();
  if (!activeCtx) {
    // F5b: surface an honest reason when no model is selected instead of
    // returning a silent empty result that the UI reports as "0 refreshed".
    // This branch only reports; it never executes or writes, so reading
    // metadata outside a lock here is safe (the value is not acted on).
    const rawTables = await listTrackedTables(scope);
    const refreshable = filterRefreshableTables(rawTables);
    for (const table of refreshable) {
      result.skipped.push({ name: table.name, reason: strings.tableRefresh.noModelSelected });
    }
    return result;
  }

  // Bug-7397 R9: enumerate table identities only. refreshTable acquires the
  // spatial block locks itself (it can only compute the locked extent after
  // executing the query), so no lock is held here.
  const tables = await listTrackedTableRanges(scope);
  if (tables.length === 0) return result;

  for (const table of tables) {
    // Bug-7397 R10-1: rewriteTableBody no longer shifts cells, so an earlier
    // table's refresh cannot move a later one -- the enumerated address stays
    // valid and no by-name re-resolution workaround is needed.
    await refreshTable(table, activeCtx, personaId, result, options?.lockTimeoutMs);
  }

  // Invalidate metadata cache so subsequent reads pick up updated timestamps.
  invalidateMetadataCache();

  // Refresh PivotTables that may reference the updated backing tables
  if (result.refreshed.length > 0) {
    await refreshPivotTables();
  }

  return result;
}

// ---------------------------------------------------------------------------
// Excel write helpers
// ---------------------------------------------------------------------------

/**
 * Bug-7397 fix #3: read a table's current range address AFTER a resize
 * (rewriteTableBody can change the row count), so the persisted
 * __table_range reflects the actual post-refresh extent.
 */
async function getActualTableRange(tableName: string): Promise<string | null> {
  if (typeof Excel === 'undefined') return null;
  let address: string | null = null;
  try {
    await Excel.run(async (context) => {
      const table = context.workbook.tables.getItem(tableName);
      const range = table.getRange();
      range.load('address');
      await context.sync();
      address = range.address;
    });
  } catch {
    return null;
  }
  return address;
}

/**
 * Rewrite a table's body IN PLACE. Returns true on success, false if the LIVE
 * table extends past `declared` (the locked union rectangle) -- in which case
 * NOTHING is mutated (the caller skips the table). Bug-7397 R11-1: the declared
 * extent is computed before acquiring the lock; if the live table auto-expanded
 * (e.g. a user typed below it) since then, resizing/clearing to the query result
 * would reach cells OUTSIDE the held blocks AND could erase user data. The
 * under-lock bounds check makes the footprint invariant hold BY CONSTRUCTION
 * (its only failure mode is a safe skip, never "write anyway").
 */
async function rewriteTableBody(
  tableName: string,
  rows: (string | number | boolean)[][],
  declared: { startRow: number; startCol: number; endRow: number; endCol: number },
  footerTimestamp: string,
): Promise<RewriteOutcome> {
  if (typeof Excel === 'undefined') return 'written';

  let withinBounds = true;
  let occupiedGrowthZone = false;
  // Bug-7397 R12-R4-2: set the instant the first mutating operation is QUEUED.
  // If the batch's sync then fails, the sheet may already be changed.
  let mutationsQueued = false;
  try {
    await Excel.run(async (context) => {
    const table = context.workbook.tables.getItem(tableName);
    const headerRange = table.getHeaderRowRange();
    headerRange.load('rowIndex, columnIndex, columnCount');
    const bodyRange = table.getDataBodyRange();
    bodyRange.load('rowCount');
    const sheet = table.getRange().worksheet;
    await context.sync();

    const colCount = headerRange.columnCount;
    const startRow = headerRange.rowIndex;
    const startCol = headerRange.columnIndex;
    const oldBodyRows = bodyRange.rowCount;
    const newBodyRows = rows.length;

    // Bug-7397 R12-2: the provenance footer's rows, before and after the resize.
    const oldFooterRow = startRow + 1 + oldBodyRows;
    const newFooterRow = startRow + 1 + newBodyRows;

    // Bug-7397 R11-1 (b): verify the mutation stays within the DECLARED (locked)
    // rectangle BEFORE touching anything. The touched extent spans the header,
    // max(old, new) body rows (a shrink still clears the old rows) AND the
    // footer row at whichever of its two positions is lower -- which is exactly
    // max(oldFooterRow, newFooterRow). If the live table grew past what was
    // declared/locked, abort without mutating.
    const usedEndRow = Math.max(oldFooterRow, newFooterRow);
    const usedEndCol = startCol + colCount - 1;
    if (startRow < declared.startRow || startCol < declared.startCol ||
        usedEndRow > declared.endRow || usedEndCol > declared.endCol) {
      withinBounds = false;
      return; // NO mutation
    }

    // Bug-7397 R12-2: read the CURRENT footer cell before mutating. Only a
    // footer we can positively identify as ours is moved/restamped: anything
    // else below the table (a user's note, a total, or nothing) is left alone
    // rather than overwritten with a manufactured footer.
    //
    // Bug-8340 (R12 review finding 3): the SAME read also has to answer "is the
    // ground we are about to grow into free?". A refresh that returns more rows
    // resizes over whatever occupies the rows below the old table, and until now
    // destroyed the user's own notes/subtotals silently while reporting a clean
    // success. The probe covers every row this rewrite newly CLAIMS -- from the
    // row after the old body through the new footer row -- so a growth into
    // occupied cells can be refused instead of overwriting them.
    const claimEndRow = Math.max(oldFooterRow, newFooterRow);
    const probe = sheet.getRangeByIndexes(
      oldFooterRow, startCol, claimEndRow - oldFooterRow + 1, colCount,
    );
    // Bug-7397 R12 review round 2, finding 2: load FORMULAS as well as values.
    // A user formula currently evaluating to "" (e.g. `=IF(A1>0,A1,"")`) reads
    // back as an empty VALUE, so a values-only probe would call the row empty
    // and destroy the formula -- the exact silent-loss class Bug-8340 closes,
    // surviving through the one input shape the probe could not see.
    // Bug-8344 correction: this comment previously claimed the insert-side
    // overwrite-confirm probes "have always loaded both channels". They had
    // not — all four loaded `values` only, and the false assurance is part of
    // why the gap survived here for so long. They now do, through the shared
    // `rangeHasContent` helper in `insertGuard.ts`. This probe keeps its own
    // loop because row 0 carries a footer exemption the shared helper has no
    // concept of.
    probe.load('values,formulas');
    // Bug-7397 R12-2: the vacated footer row becomes a data row on growth, so
    // its grey/italic styling must be reverted to whatever the BODY uses. Read
    // that from the last surviving body row rather than assuming a workbook
    // default (review round 2, finding 5) -- an explicit black would not invert
    // over a dark table style the way Excel's automatic colour does.
    const bodyStyleRef = oldBodyRows > 0
      ? sheet.getRangeByIndexes(oldFooterRow - 1, startCol, 1, 1)
      : null;
    bodyStyleRef?.load('format/font/italic, format/font/size, format/font/color');
    await context.sync();
    const probeValues: unknown[][] = (probe.values as unknown[][]) ?? [];
    const probeFormulas: unknown[][] = (probe.formulas as unknown[][]) ?? [];
    const probedFooter: unknown = probeValues[0]?.[0];
    const existingFooter: string | null = isProvenanceFooter(probedFooter) ? probedFooter : null;

    if (newBodyRows > oldBodyRows) {
      // Growth. Row 0 of the probe is the old footer row: it is ours to reuse
      // when it holds our footer, and part of the claim otherwise. Every other
      // probed row is ground we did not previously occupy.
      const occupied = probeValues.some((line, rowOffset) =>
        (line ?? []).some((cell, colOffset) => {
          if (rowOffset === 0 && colOffset === 0 && existingFooter !== null) return false;
          if (cell !== null && cell !== undefined && cell !== '') return true;
          // A formula that currently renders empty still owns the cell.
          const formula = probeFormulas[rowOffset]?.[colOffset];
          return typeof formula === 'string' && formula !== '';
        }),
      );
      if (occupied) {
        occupiedGrowthZone = true;
        return; // NO mutation -- the caller skips honestly with a reason.
      }
    }

    // Bug-7397 R10-1: rewrite IN PLACE (no delete/shift). resize() only
    // redefines the table boundary; it does not insert or shift worksheet rows.
    const newTableRange = sheet.getRangeByIndexes(startRow, startCol, 1 + newBodyRows, colCount);
    mutationsQueued = true; // from here on, a sync failure can leave the sheet changed
    table.resize(newTableRange);
    if (newBodyRows > 0) {
      const newBody = sheet.getRangeByIndexes(startRow + 1, startCol, newBodyRows, colCount);
      newBody.values = rows;
    }
    if (oldBodyRows > newBodyRows) {
      // Clear the contents of rows the (now smaller) table no longer covers --
      // within the declared/locked rectangle (asserted above).
      const excess = sheet.getRangeByIndexes(startRow + 1 + newBodyRows, startCol, oldBodyRows - newBodyRows, colCount);
      excess.clear(Excel.ClearApplyTo.contents);
    }

    // Bug-7397 R12-2: move + restamp the footer in the SAME batch as the resize.
    // On GROWTH the old footer cell is inside the new body and is overwritten by
    // result data, so only the new position needs writing. On SHRINK the old
    // position lies one row below the cleared excess block, so it needs an
    // explicit clear before the footer is written at its new (higher) row.
    // Queued after the excess clear, because on a shrink the new footer row IS
    // the first excess row.
    if (existingFooter !== null) {
      if (oldFooterRow > newFooterRow) {
        // SHRINK: the old footer row leaves the table entirely. Clear
        // ClearApplyTo.all, not .contents -- Bug-7397 R12 review finding 4:
        // clearing contents alone leaves an empty grey/italic/9pt row sitting
        // below the table forever.
        sheet.getRangeByIndexes(oldFooterRow, startCol, 1, colCount).clear(Excel.ClearApplyTo.all);
      } else if (oldFooterRow < newFooterRow) {
        // GROWTH: the old footer row becomes an ordinary DATA row. Its
        // grey/italic/9pt styling is not cleared by writing values over it, so
        // without this the table accumulates one grey italic row per growth
        // refresh. Only the three properties the footer writer sets are
        // reverted -- a blanket format clear would also drop the row's number
        // format, leaving one row of raw unformatted numbers. The target
        // values are read from the row's own neighbours (the last surviving
        // body row) so the reverted row matches its table, whatever font or
        // table style the workbook uses; the constants are only the fallback
        // when there is no body row to copy from.
        const refFont = bodyStyleRef?.format?.font;
        const vacated = sheet.getRangeByIndexes(oldFooterRow, startCol, 1, colCount);
        vacated.format.font.italic = refFont?.italic ?? false;
        vacated.format.font.size = refFont?.size ?? BODY_FONT_SIZE;
        vacated.format.font.color = refFont?.color ?? BODY_FONT_COLOR;
      }
      const footerRange = sheet.getRangeByIndexes(newFooterRow, startCol, 1, colCount);
      footerRange.getCell(0, 0).values = [[restampProvenanceFooter(existingFooter, footerTimestamp)]];
      footerRange.format.font.italic = FOOTER_FONT_ITALIC;
      footerRange.format.font.size = FOOTER_FONT_SIZE;
      footerRange.format.font.color = FOOTER_FONT_COLOR;
    }

    await context.sync();
    });
  } catch (err) {
    // Bug-7397 R12-R4-2: distinguish "nothing was touched" from "the batch
    // failed after the resize/values were already applied".
    if (mutationsQueued) throw new PartialRewriteError(err);
    throw err;
  }
  if (!withinBounds) return 'out-of-bounds';
  if (occupiedGrowthZone) return 'growth-zone-occupied';
  return 'written';
}

/**
 * Write back the refreshed table's provenance (updated timestamp). Returns
 * true on success, false if the write-back failed. Bug-7397 R6: the caller
 * (refreshTable) MUST already hold this table's lock, so this uses
 * setTableMetadataWithinLock (the non-locking writer) to avoid re-entrant
 * deadlock. A failure is RETURNED, not swallowed, so refreshTable can surface
 * an honest warning instead of reporting a clean success.
 */
async function updateTableTimestamp(
  rangeAddress: string,
  existingMetadata: Partial<TableMetadata>,
  // Bug-7397 R12-2: the SAME instant written into the sheet's provenance footer,
  // so the visible date and the stored date can never disagree.
  timestamp: string,
): Promise<boolean> {
  try {
    await setTableMetadataWithinLock(rangeAddress, {
      ...existingMetadata,
      timestamp,
      pluginVersion: existingMetadata.pluginVersion || '1.0',
    } as TableMetadata);
    return true;
  } catch {
    return false;
  }
}

async function refreshPivotTables(): Promise<void> {
  if (typeof Excel === 'undefined') return;

  try {
    await Excel.run(async (context) => {
      const pivots = context.workbook.pivotTables;
      pivots.load('items');
      await context.sync();
      for (const pt of pivots.items) {
        pt.refresh();
      }
      await context.sync();
    });
  } catch {
    // Non-critical: pivot refresh failure is silent.
  }
}
