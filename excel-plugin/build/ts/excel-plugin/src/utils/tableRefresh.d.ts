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
import { type TableMetadata } from './workbookMetadata';
export interface RefreshableTable {
    name: string;
    rangeAddress: string;
    metadata: Partial<TableMetadata>;
}
export interface RefreshResult {
    refreshed: string[];
    skipped: {
        name: string;
        reason: string;
    }[];
    warnings: {
        name: string;
        reason: string;
    }[];
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
 * Bug-7397 R12 review round 4, finding 2: an `Excel.run` batch is NOT
 * transactional. If `context.sync()` rejects after the resize/values/clear
 * operations were already applied, the sheet HAS changed. Reporting that as an
 * ordinary skip ("was not changed at all, refresh again") is a false statement
 * about the user's data and invites a retry against a half-rewritten table.
 * `rewriteTableBody` therefore reports whether it had queued any mutation when
 * it failed, and the caller downgrades to a warning naming the table.
 */
export declare class PartialRewriteError extends Error {
    readonly cause: unknown;
    constructor(cause: unknown);
}
export declare function buildRefreshDetailRows(result: RefreshResult): RefreshDetailRow[];
/**
 * From a list of table entries (name + range + metadata), filter to those that
 * have enough provenance to be refreshable: semanticQuery + projectId + modelId.
 */
export declare function filterRefreshableTables(tables: {
    name: string;
    rangeAddress: string;
    metadata: Partial<TableMetadata>;
}[]): RefreshableTable[];
/**
 * Validate a single table's metadata against the active model context.
 * Returns null if valid, or a skip reason string if mismatched.
 */
export declare function validateTableContext(metadata: Partial<TableMetadata>, activeProjectId: string, activeModelId: string): string | null;
/**
 * Parse stored column headers from their persisted JSON string format.
 * The producer (useExcel.ts) stores headers as JSON.stringify(string[]).
 * Returns null if parsing fails (corrupted metadata).
 */
export declare function parseStoredColumnHeaders(raw: string): string[] | null;
/**
 * Compare returned display headers against stored display headers.
 * Both arrays must be the same type (display names).
 * Returns null if they match, or a reason string on drift.
 */
export declare function validateColumnHeaders(returnedHeaders: string[], storedHeaders: string[]): string | null;
/**
 * F3: Detect agent-chat provenance objects that are not executable queries.
 * Agent inserts store { source: 'agent', conversation_id, message_id }.
 */
export declare function isAgentSourcedQuery(parsed: unknown): boolean;
/**
 * Enumerate Excel tables on the active sheet (or workbook) and read their
 * Tessallite metadata. Returns raw entries for filterRefreshableTables.
 */
export declare function listTrackedTables(scope: 'activeSheet' | 'workbook'): Promise<{
    name: string;
    rangeAddress: string;
    metadata: Partial<TableMetadata>;
}[]>;
/**
 * Bug-7397 R6: enumerate table identities (name + range address) ONLY, without
 * reading provenance. The authoritative metadata read must happen INSIDE the
 * per-table lock (see refreshTables), so this cheap pass just tells the loop
 * which tables exist and where. Reading metadata here (as listTrackedTables
 * does) would take a snapshot OUTSIDE the lock that a concurrent insert could
 * supersede before we ever acquire it -- the exact stale-read gap the lock
 * closes.
 */
export declare function listTrackedTableRanges(scope: 'activeSheet' | 'workbook'): Promise<{
    name: string;
    rangeAddress: string;
}[]>;
/**
 * Refresh all Tessallite-tracked tables in the given scope.
 * Re-executes each stored semantic query through the governed plugin-execute
 * endpoint under the CURRENT persona.
 */
export declare function refreshTables(scope: 'activeSheet' | 'workbook', personaId?: string | null, options?: {
    lockTimeoutMs?: number;
}): Promise<RefreshResult>;
