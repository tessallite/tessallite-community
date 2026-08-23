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
export function parseRangeAddress(address: string): CellRange | null {
  // Split on '!' — sheet name is everything before, cell ref is after.
  const bangIdx = address.lastIndexOf('!');
  let sheet = '';
  let cellRef = address;
  if (bangIdx >= 0) {
    sheet = address.slice(0, bangIdx).replace(/^'|'$/g, '');
    cellRef = address.slice(bangIdx + 1);
  }

  // Parse cell references like "A1", "A1:C10", "$A$1:$C$10".
  const parts = cellRef.split(':');
  const startCell = parseCellRef(parts[0]);
  if (!startCell) return null;

  if (parts.length === 1) {
    return { sheet, rowStart: startCell.row, colStart: startCell.col, rowCount: 1, colCount: 1 };
  }

  const endCell = parseCellRef(parts[1]);
  if (!endCell) return null;

  return {
    sheet,
    rowStart: Math.min(startCell.row, endCell.row),
    colStart: Math.min(startCell.col, endCell.col),
    rowCount: Math.abs(endCell.row - startCell.row) + 1,
    colCount: Math.abs(endCell.col - startCell.col) + 1,
  };
}

/** Parse a single cell reference like "A1", "$B$2" into {row, col}. */
function parseCellRef(ref: string): { row: number; col: number } | null {
  const stripped = ref.replace(/\$/g, '');
  const match = /^([A-Z]+)(\d+)$/i.exec(stripped);
  if (!match) return null;
  const col = columnLetterToIndex(match[1]);
  const row = parseInt(match[2], 10) - 1; // zero-based
  return { row, col };
}

/** Convert a column letter (A, B, ..., Z, AA, AB, ...) to a zero-based index. */
function columnLetterToIndex(letters: string): number {
  let index = 0;
  for (let i = 0; i < letters.length; i++) {
    index = index * 26 + (letters.charCodeAt(i) & 0x1f);
  }
  return index - 1;
}

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
export function rangeHasContent(
  values: (unknown[] | null)[] | null | undefined,
  formulas: (unknown[] | null)[] | null | undefined,
): boolean {
  if (!values && !formulas) return false;
  const rowCount = Math.max(values?.length ?? 0, formulas?.length ?? 0);
  for (let r = 0; r < rowCount; r++) {
    const valueRow = values?.[r] ?? [];
    const formulaRow = formulas?.[r] ?? [];
    const colCount = Math.max(valueRow.length, formulaRow.length);
    for (let c = 0; c < colCount; c++) {
      const cellValue = valueRow[c];
      if (cellValue !== null && cellValue !== undefined && cellValue !== '') return true;
      const cellFormula = formulaRow[c];
      if (typeof cellFormula === 'string' && cellFormula !== '') return true;
    }
  }
  return false;
}

/**
 * Detect whether two rectangular ranges overlap (same sheet, any cell in common).
 */
export function rangesOverlap(a: CellRange, b: CellRange): boolean {
  // Different sheets never overlap.
  if (a.sheet.toLowerCase() !== b.sheet.toLowerCase()) return false;

  // Check axis-aligned rectangle overlap.
  const aRowEnd = a.rowStart + a.rowCount - 1;
  const bRowEnd = b.rowStart + b.rowCount - 1;
  const aColEnd = a.colStart + a.colCount - 1;
  const bColEnd = b.colStart + b.colCount - 1;

  return !(aRowEnd < b.rowStart || bRowEnd < a.rowStart ||
           aColEnd < b.colStart || bColEnd < a.colStart);
}

/** Tracks in-flight and recently-written ranges for anchor safety. */
export class InsertTracker {
  /** Currently in-flight insert ranges (being written right now). */
  private inFlight = new Map<string, CellRange>();
  /** Recently-written ranges (kept for collision detection until cleared). */
  private written: CellRange[] = [];
  /** In-flight operation keys for duplicate rejection. */
  private activeOps = new Set<string>();

  /**
   * Build an operation key from the insert type and identity.
   * Used to reject duplicate submissions of the same insert.
   */
  static operationKey(type: string, id: string): string {
    return `${type}:${id}`;
  }

  /**
   * Try to start an insert operation. Returns false if the operation is
   * already in flight (duplicate submission).
   */
  tryStart(opKey: string): boolean {
    if (this.activeOps.has(opKey)) return false;
    this.activeOps.add(opKey);
    return true;
  }

  /** Mark an insert operation as complete (success or failure). */
  complete(opKey: string): void {
    this.activeOps.delete(opKey);
  }

  /** Register an in-flight range (before write begins). */
  registerInFlight(opKey: string, range: CellRange): void {
    this.inFlight.set(opKey, range);
  }

  /** Move a range from in-flight to written (after write completes). */
  commitRange(opKey: string): void {
    const range = this.inFlight.get(opKey);
    if (range) {
      this.written.push(range);
      this.inFlight.delete(opKey);
    }
  }

  /** Remove an in-flight range without committing (on failure). */
  cancelInFlight(opKey: string): void {
    this.inFlight.delete(opKey);
  }

  /**
   * Check whether a proposed anchor range collides with any in-flight or
   * recently-written range. Returns the first colliding range, or null.
   */
  checkCollision(proposed: CellRange): CellRange | null {
    for (const [, range] of this.inFlight) {
      if (rangesOverlap(proposed, range)) return range;
    }
    for (const range of this.written) {
      if (rangesOverlap(proposed, range)) return range;
    }
    return null;
  }

  /**
   * Find a safe anchor by offsetting below the last collision. Returns the
   * proposed range shifted below the last written/in-flight range on the
   * same sheet, or null if no collision exists (the original is safe).
   */
  findSafeAnchor(proposed: CellRange): CellRange | null {
    let maxRowEnd = -1;
    let hasCollision = false;

    for (const [, range] of this.inFlight) {
      if (rangesOverlap(proposed, range)) {
        hasCollision = true;
        maxRowEnd = Math.max(maxRowEnd, range.rowStart + range.rowCount);
      }
    }
    for (const range of this.written) {
      if (rangesOverlap(proposed, range)) {
        hasCollision = true;
        maxRowEnd = Math.max(maxRowEnd, range.rowStart + range.rowCount);
      }
    }

    if (!hasCollision) return null;

    // Offset the proposed range below the last collision, with a 1-row gap.
    return {
      ...proposed,
      rowStart: maxRowEnd + 1,
    };
  }

  /** Whether any operation is currently in flight. */
  get isAnyActive(): boolean {
    return this.activeOps.size > 0;
  }

  /** Whether a specific operation is in flight. */
  isActive(opKey: string): boolean {
    return this.activeOps.has(opKey);
  }

  /** Clear all tracked ranges (e.g., on sheet change or session reset). */
  clear(): void {
    this.inFlight.clear();
    this.written = [];
    this.activeOps.clear();
  }
}
