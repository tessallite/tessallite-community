/**
 * Bug-6740: In-flight guard and anchor safety for all insert paths.
 *
 * Prevents duplicate submissions and overlapping writes. Every insert path
 * (tables, scorecards, charts, formula inserts) uses this module.
 *
 * Pure and side-effect-free (no Excel API calls) so the guard logic is
 * unit-testable without mounting the full hook tree.
 */
/** Represents a rectangular range in a worksheet. */
export interface CellRange {
    /** Sheet name (without quotes). */
    sheet: string;
    /** Zero-based row index of the top-left corner. */
    rowStart: number;
    /** Zero-based column index of the top-left corner. */
    colStart: number;
    /** Number of rows in the range. */
    rowCount: number;
    /** Number of columns in the range. */
    colCount: number;
}
/**
 * Parse an Excel-style range address (e.g., "Sheet1!A1:C10" or "'Sheet 1'!B2")
 * into a CellRange. Returns null if the address cannot be parsed.
 */
export declare function parseRangeAddress(address: string): CellRange | null;
/**
 * Bug-8344: the ONE occupancy answer for "is this rectangle free to overwrite?".
 *
 * Office.js reports a cell holding `=IF(B1>0,B1,"")` as an EMPTY VALUE, so a
 * probe that reads only the `values` channel calls the cell empty, skips the
 * overwrite confirmation, and destroys the user's formula silently. A cell is
 * occupied when EITHER channel holds anything, so both are consulted here and
 * every overwrite-confirm site calls this instead of writing its own `.some()`
 * — which is how the same class survived at four insert sites after being
 * fixed in `tableRefresh.ts`'s growth-zone probe.
 *
 * Callers MUST `load('values,formulas')`; a range loaded with `values` alone
 * makes the formulas channel unreadable on a real host.
 *
 * Pure (no Excel API calls) so it is unit-testable.
 *
 * @param values   the range's `values` grid, or null when not loaded
 * @param formulas the range's `formulas` grid, or null when not loaded
 */
export declare function rangeHasContent(values: (unknown[] | null)[] | null | undefined, formulas: (unknown[] | null)[] | null | undefined): boolean;
/**
 * Detect whether two rectangular ranges overlap (same sheet, any cell in common).
 */
export declare function rangesOverlap(a: CellRange, b: CellRange): boolean;
/** Tracks in-flight and recently-written ranges for anchor safety. */
export declare class InsertTracker {
    /** Currently in-flight insert ranges (being written right now). */
    private inFlight;
    /** Recently-written ranges (kept for collision detection until cleared). */
    private written;
    /** In-flight operation keys for duplicate rejection. */
    private activeOps;
    /**
     * Build an operation key from the insert type and identity.
     * Used to reject duplicate submissions of the same insert.
     */
    static operationKey(type: string, id: string): string;
    /**
     * Try to start an insert operation. Returns false if the operation is
     * already in flight (duplicate submission).
     */
    tryStart(opKey: string): boolean;
    /** Mark an insert operation as complete (success or failure). */
    complete(opKey: string): void;
    /** Register an in-flight range (before write begins). */
    registerInFlight(opKey: string, range: CellRange): void;
    /** Move a range from in-flight to written (after write completes). */
    commitRange(opKey: string): void;
    /** Remove an in-flight range without committing (on failure). */
    cancelInFlight(opKey: string): void;
    /**
     * Check whether a proposed anchor range collides with any in-flight or
     * recently-written range. Returns the first colliding range, or null.
     */
    checkCollision(proposed: CellRange): CellRange | null;
    /**
     * Find a safe anchor by offsetting below the last collision. Returns the
     * proposed range shifted below the last written/in-flight range on the
     * same sheet, or null if no collision exists (the original is safe).
     */
    findSafeAnchor(proposed: CellRange): CellRange | null;
    /** Whether any operation is currently in flight. */
    get isAnyActive(): boolean;
    /** Whether a specific operation is in flight. */
    isActive(opKey: string): boolean;
    /** Clear all tracked ranges (e.g., on sheet change or session reset). */
    clear(): void;
}
