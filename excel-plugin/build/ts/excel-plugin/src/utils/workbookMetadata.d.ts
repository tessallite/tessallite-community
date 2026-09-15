/**
 * Workbook metadata persistence for inserted tables.
 * Stores project, model, query, and plugin metadata on tables
 * using Excel custom properties and hidden named ranges.
 */
/**
 * Bug-7397: Office.js `NamedItem.comment` is capped at 255 characters. Table
 * provenance is stored as `key=value` comments; `semanticQuery` in particular
 * is `JSON.stringify(query)` and easily exceeds the cap for a query with a few
 * measures/dimensions/filters. Over-length assignment is either rejected (the
 * add is wrapped in a swallow-all try) or silently truncated, so a Refresh or
 * Drill that reads `metadata.semanticQuery` (useExcel.ts, tableRefresh.ts —
 * where it is `JSON.parse`d) silently loses ALL provenance for that table.
 *
 * Fix: values whose `key=value` comment would exceed the cap are CHUNKED across
 * additional sequenced named items and reassembled on read. Short values keep
 * the exact single-comment form, so existing workbooks and the read path are
 * unchanged.
 */
export declare const MAX_COMMENT_LENGTH = 255;
/**
 * Split `value` into pieces whose UTF-16 length does not exceed `size`, WITHOUT
 * ever cutting a surrogate pair. Iterating a string yields whole code points, so
 * a non-BMP character (emoji, some CJK) -- length 2 in UTF-16 -- is kept intact.
 * A lone surrogate is unencodable in workbook XML and would fail the comment
 * assignment (losing the whole batch) or be replaced with U+FFFD (corrupting
 * the reassembled JSON).
 */
export declare function splitByCodePoints(value: string, size: number): string[];
/**
 * Encode one metadata entry into the named-item comment(s) needed to persist it
 * without exceeding {@link MAX_COMMENT_LENGTH}. Returns `{ suffix, comment }`
 * pairs; each becomes a named item `<prefix><rangeKey>_<suffix>`. A value that
 * fits stays a single `key=value` comment (backward compatible); an over-long
 * value emits a `<key>__chunks=<N>` head plus N `<key>__cI=<piece>` items.
 *
 * The per-chunk envelope `<key>__cI=` reserves up to 6 index digits, so every
 * emitted comment is <= the cap for any reachable value size (1e6 chunks would
 * be > 200 MB of value; Excel caps sheets long before that).
 */
export declare function encodeMetadataEntry(key: string, value: string): {
    suffix: string;
    comment: string;
}[];
/**
 * Reassemble any chunked values in a raw `key -> value` metadata map (mutates
 * and returns it). For each `<key>__chunks=<N>` head, the `<key>__cI` pieces are
 * concatenated into `<key>` and the head + chunk entries removed. The head is
 * ALWAYS removed so it never leaks to callers. Reassembly happens only when
 * EVERY expected chunk is present: a missing chunk leaves `<key>` absent (an
 * honest "no provenance" skip) rather than collapsing to `''` (which would break
 * `JSON.parse` on the refresh/drill path). Idempotent -- a second call is a
 * no-op because the heads are already gone. Non-chunked keys are untouched.
 */
export declare function reassembleChunkedMetadata(raw: Record<string, string>): Record<string, string>;
export declare function invalidateMetadataCache(): void;
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
    /**
     * Bug-8424: set by {@link getTableMetadata} when the underlying `Excel.run`
     * threw, so a caller can tell "this table genuinely carries no Tessallite
     * provenance" from "we could not read its provenance this time". Synthetic
     * and never persisted — `setTableMetadata` skips every `_`-prefixed key.
     */
    _metadataFetchFailed?: true;
}
/**
 * Bug-8424: did this metadata read FAIL, as opposed to come back genuinely
 * empty? Exported so the producer (`getTableMetadata`) and every consumer
 * agree on one predicate instead of each re-testing the raw key.
 */
export declare function isMetadataFetchFailure(metadata: Partial<TableMetadata>): boolean;
/**
 * F-025-19: Excel named-item names forbid spaces and most punctuation, and a
 * sheet name embedded directly in a name breaks for sheets like "Q1 Report" or
 * names containing the "_" delimiter we split on. Hash the sheet name into a
 * short stable token so the named-item key is always a legal identifier and the
 * slow-path key split is unambiguous. Deterministic (same sheet -> same token).
 */
export declare function hashSheetName(sheetName: string): string;
/**
 * F-025-19: quote a sheet name for use in a range reference when it contains a
 * space or punctuation, per Excel's formula rules ('My Sheet'!A1). A literal
 * single quote inside the name is doubled.
 */
export declare function quoteSheetRef(sheetName: string, rangeRef: string): string;
/**
 * Canonical per-table lock key. IDENTICAL to the key `setTableMetadata` writes
 * named items under, so the insert path, the refresh path, and the metadata
 * writer all serialize against each other for the same physical table:
 * sheet-name hashed (space/punctuation-safe, no `_` delimiter) and start cell
 * `$`-stripped + upper-cased ($A$1 / a1 / A1 all collapse to one key).
 */
export declare function tableRangeKey(rangeAddress: string): string;
/**
 * Bug-7397 R12-3: raised when an acquirer gave up WAITING for a cell block.
 * Nothing was written. Callers must surface it as a user-visible "this is
 * taking too long, try again" outcome -- never swallow it into a silent no-op
 * (silence is indistinguishable from a broken feature) and never proceed with
 * the write (that would defeat the exclusion the lock exists to provide).
 */
export declare class LockAcquireTimeoutError extends Error {
    readonly rangeKey: string;
    constructor(rangeKey: string);
}
/**
 * Bug-7397 R12-3: how long an acquirer waits for a held cell block before
 * giving up. Sized for the slowest legitimate holder -- a locked critical
 * section is only Office host round trips (a resize + values write + a
 * named-item batch); the network query already ran unlocked. A wait longer than
 * this means the holder is wedged (a `context.sync()` that never resolved), and
 * a user staring at a frozen button learns nothing, so we fail loudly instead.
 */
export declare const DEFAULT_LOCK_ACQUIRE_TIMEOUT_MS = 20000;
/**
 * Bug-7397 R6/R9: acquire ONE lock by a PRE-COMPUTED key and run `fn` holding
 * it. The key namespace is the spatial CELL BLOCK (see cellBlockKeys) -- every
 * operation touching a set of cells locks the blocks covering them, so
 * operations on overlapping cells share a key by construction. NOT re-entrant:
 * code inside `fn` must never re-acquire the SAME key (or call the self-locking
 * `setTableMetadata` for overlapping cells) -- it would deadlock behind its own
 * unsettled promise. Use `setTableMetadataWithinLock` for a nested metadata
 * write. Callers almost always use `withTableLocksKeys` (a whole block set).
 */
export declare function withTableLockKey<T>(rangeKey: string, fn: () => Promise<T>, deadline?: number): Promise<T>;
/** A written rectangle in 0-indexed row/col terms (the insert's full extent). */
export interface TargetRect {
    startRow: number;
    startCol: number;
    rowCount: number;
    colCount: number;
}
/**
 * Bug-7397 R9: the sorted, de-duplicated block-lock keys covering the cell
 * rectangle [startRow..endRow] x [startCol..endCol] on `sheetName`. Sorting
 * makes multi-block acquisition deadlock-free (every acquirer takes blocks in
 * the same total order).
 */
export declare function cellBlockKeys(sheetName: string, startRow: number, startCol: number, endRow: number, endCol: number): string[];
/** Block-lock keys covering the cells of a range ADDRESS (e.g. 'Sheet1!A1:D10'). */
export declare function blockKeysForAddress(rangeAddress: string): string[];
/** 0-indexed inclusive row/col bounds of a range ADDRESS (normalised so end >= start). */
export declare function rangeRowColBounds(rangeAddress: string): {
    startRow: number;
    startCol: number;
    endRow: number;
    endCol: number;
};
/** Block-lock keys covering a TargetRect (rowCount x colCount from a start cell). */
export declare function blockKeysForRect(sheetName: string, rect: TargetRect): string[];
/**
 * Bug-7397 R7/R9: acquire MULTIPLE locks and run `fn` holding all of them.
 * Keys are sorted internally so every acquirer takes them in the same total
 * order -- that makes multi-key acquisition deadlock-free even when two
 * operations request overlapping-but-different key sets. Nested
 * `withTableLockKey` calls chain each key behind whatever holds it, so `fn`
 * runs only once every key is free.
 */
export declare function withTableLocksKeys<T>(rangeKeys: string[], fn: () => Promise<T>, options?: {
    timeoutMs?: number;
}): Promise<T>;
/**
 * Persist table provenance. Public, self-locking entry point for STANDALONE
 * callers (a metadata write not already inside a held per-table critical
 * section). Callers that already hold the covering block locks (via `withTableLocksKeys`)
 * (the insert path in useExcel.ts, the refresh write-back in tableRefresh.ts)
 * MUST call `setTableMetadataWithinLock` instead to avoid re-entrant deadlock.
 */
export declare function setTableMetadata(rangeAddress: string, metadata: TableMetadata): Promise<void>;
/**
 * Bug-7397 R6: the metadata-write core, WITHOUT acquiring the table lock. The
 * caller MUST already hold the covering block locks (via `withTableLocksKeys`). Splitting
 * the lock acquisition out of the write is what lets the insert and refresh
 * paths put the data write AND the metadata write inside ONE critical section
 * (a single held block-lock set) instead of two separate acquisitions with an
 * exploitable gap between them.
 */
export declare function setTableMetadataWithinLock(rangeAddress: string, metadata: TableMetadata): Promise<void>;
/**
 * Bug-7397 R6: delete every named item that stores provenance for the table at
 * `rangeAddress`, WITHOUT acquiring locks (the caller must already
 * hold the covering block locks). Used by the refresh path to clean up orphaned
 * provenance under a stale key when a table's start cell moves. Best-effort:
 * a failure is logged, not thrown, because the caller has already written the
 * authoritative provenance under the new key.
 */
export declare function removeTableMetadataWithinLock(rangeAddress: string): Promise<void>;
/**
 * Bug-7397 R8-1: the range address covering the UNION of a table's current
 * extent (`currentAddress`) and the extent it is ABOUT to grow into
 * (`intendedRows` tall x `intendedCols` wide from its start cell). A refresh
 * publishes this claim BEFORE it mutates cells so a concurrent insert into the
 * growth zone sees the (grown) extent and serializes against the refresh.
 */
export declare function unionTableRangeAddress(currentAddress: string, intendedRows: number, intendedCols: number): string;
/**
 * Bug-7397 R12-2: extend a range address downward by `extraRows`.
 *
 * The refresh path uses this to pull the PROVENANCE FOOTER ROW inside the
 * region it locks and rewrites. The footer sits one row below the table body,
 * so a refresh that grows the table writes result data over the footer's old
 * cell, and a refresh that shrinks it leaves the footer stranded below blank
 * rows. Extending the union by one row covers BOTH the footer's current
 * position (current extent + 1) and its intended position (intended extent + 1)
 * -- max(a,b) + 1 == max(a + 1, b + 1) -- so a single extension is exact, not
 * an approximation.
 */
export declare function expandRangeRows(rangeAddress: string, extraRows: number): string;
export interface EntityUsageEntry {
    id: string;
    type: 'named_set' | 'kpi';
    displayName: string;
    certificationStatus?: string;
    updatedAt?: string;
    insertedAt: string;
    cellLocations: string[];
    workbookId?: string;
    modelId?: string;
}
export declare function getWorkbookId(): Promise<string | null>;
/** Test-only: reset the memoised workbook id between cases. */
export declare function _resetWorkbookIdCache(): void;
export interface EntityManifest {
    version: 1;
    entries: EntityUsageEntry[];
}
export declare function trackEntityUsage(type: 'named_set' | 'kpi', entityId: string, displayName: string, cellAddress: string, certificationStatus?: string, updatedAt?: string, modelId?: string): Promise<void>;
export declare function getEntityManifest(): Promise<EntityManifest>;
export interface StaleEntity {
    entry: EntityUsageEntry;
    currentStatus: string;
    reason: 'deprecated' | 'deleted' | 'status_changed' | 'version_changed';
}
export declare function checkStaleEntities(currentEntities: {
    id: string;
    type: 'named_set' | 'kpi';
    certification_status: string;
    updated_at?: string;
}[], modelId?: string): Promise<StaleEntity[]>;
export declare function updateManifestStatuses(currentEntities: {
    id: string;
    type: 'named_set' | 'kpi';
    certification_status: string;
    updated_at?: string;
}[], skipUpdatedAtKeys?: Set<string>, modelId?: string): Promise<void>;
export declare function removeEntityFromManifest(type: 'named_set' | 'kpi', entityId: string): Promise<void>;
/**
 * Bug-7397 R12-4: options for {@link getTableMetadata}.
 */
export interface GetTableMetadataOptions {
    /**
     * Skip the TTL cache and read the workbook. Required by any caller whose
     * correctness depends on the value reflecting the CURRENT host state rather
     * than a value that was true within the last {@link METADATA_CACHE_TTL_MS}
     * -- specifically the refresh path's under-lock re-validation, which exists
     * to answer "did anything change while I was executing the query?". A cached
     * answer to that question compares the original snapshot against itself and
     * can never say "yes". The stale entry for this address is dropped before the
     * read so a discarded result (see the epoch guard in `cacheMetadata`) cannot
     * leave the pre-read value behind for the next caller.
     */
    bypassCache?: boolean;
}
export declare function getTableMetadata(rangeAddress: string, options?: GetTableMetadataOptions): Promise<Partial<TableMetadata>>;
