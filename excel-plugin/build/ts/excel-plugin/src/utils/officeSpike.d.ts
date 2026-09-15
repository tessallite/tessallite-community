export interface CompatibilityMatrix {
    host: string;
    platform: string;
    insertTable: boolean | string;
    insertChart: boolean | string;
    localPivotTable: boolean | string;
    cubeFormulas: boolean | string;
    createXmlaConnection: boolean | string;
    readSelectedCubeFormula: boolean | string;
    readActiveCell: boolean | string;
    getActiveCellAddress: boolean | string;
    detectWorkbookConnections: boolean | string;
    notes: string;
}
export declare function detectHost(): {
    host: string;
    platform: string;
};
/**
 * Bug-6737 (REOPENED): the table insert has TWO commit points inside a single
 * Excel.run. The first sync commits cell data + formatting; the second sync
 * commits the Excel table object (filter dropdowns, alternating row colors,
 * header styling). If the second sync fails (e.g., the range overlaps an
 * existing table object -- Bug-6736), the data is already visible but the
 * function threw, propagating as "Insert failed" to the caller.
 *
 * Fix: return { address, tableObjectFailed } so the caller knows the data
 * was written even if the table object step failed. The table object is
 * cosmetic (filter dropdowns, alternating rows); the data values are the
 * user's primary concern.
 */
export interface InsertResultTableOutcome {
    address: string | null;
    tableObjectFailed: boolean;
}
/**
 * Bug-7397 R6: a PINNED write target. When supplied, insertResultTable writes
 * to exactly this sheet + start cell and NEVER re-reads the selection or the
 * active worksheet. The caller (doInsertAndTag) resolves this target and the
 * table lock key from ONE host sample, so the location that is locked is the
 * location that is written -- closing the two-sample TOCTOU the deep-review
 * reproduced (a selection move, or a concurrent chart/pivot insert activating
 * a different sheet, between the anchor pre-read and the write).
 */
export interface PinnedInsertTarget {
    sheetName: string;
    startRow: number;
    startCol: number;
}
export declare function insertResultTable(headers: string[], rows: (string | number)[][], formatTokens?: Record<string, string>, useActiveCell?: boolean, 
/**
 * Bug-6735 / R1 Finding 1: when the user confirms the overwrite prompt,
 * the retry must position at the same active cell WITHOUT re-checking
 * for existing data (which would throw OVERWRITE_WARNING again in an
 * infinite loop). `forceOverwrite` = true means "use active cell, skip
 * the data check". Previously the retry passed `useActiveCell=false`,
 * which silently relocated the table to (0,0) / A1.
 */
forceOverwrite?: boolean, pinnedTarget?: PinnedInsertTarget): Promise<InsertResultTableOutcome>;
export declare function readActiveCell(): Promise<{
    address: string;
    value: unknown;
    formula: string;
}>;
export declare function runCompatibilitySpike(): Promise<CompatibilityMatrix>;
