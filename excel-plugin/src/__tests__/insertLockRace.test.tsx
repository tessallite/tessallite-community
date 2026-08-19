/**
 * Bug-7397 R6 — the INSERT path's cell-data write is under the per-table lock,
 * and its write target is PINNED to the same host sample the lock key came from.
 *
 * The deep-review found (and reproduced) that the prior R6 cut sampled the lock
 * key and the write location in two separate host round trips, so a selection
 * move (or active-sheet change) between them defeated exclusion. It also found
 * the suite never drove the REAL insert data-write under contention -- only the
 * metadata-only writer. These tests close both gaps end to end through the real
 * useExcel().insertTable -> doInsertAndTag -> insertResultTable path.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { renderHook } from '@testing-library/react';
import { useExcel } from '../hooks/useExcel';
import {
  cellBlockKeys,
  blockKeysForRect,
  blockKeysForAddress,
  withTableLocksKeys,
  invalidateMetadataCache,
  unionTableRangeAddress,
  type TargetRect,
} from '../utils/workbookMetadata';
import { describeTableInsertResult } from '../utils/measureFormulaInsert';

function rectAt(startRow: number, startCol: number, rowCount = 1, colCount = 1): TargetRect {
  return { startRow, startCol, rowCount, colCount };
}

describe('Bug-7397 R9 — spatial block-lock keys (exclusion by construction)', () => {
  it('OVERLAPPING rectangles ALWAYS share at least one block key', () => {
    // Two rectangles that overlap on a cell must intersect in block space.
    const a = blockKeysForAddress('Sheet1!A1:E2');   // rows 0-1, cols 0-4
    const b = blockKeysForAddress('Sheet1!C1:F10');  // rows 0-9, cols 2-5 (overlaps cols C-E)
    expect(a.some(k => b.includes(k))).toBe(true);
  });

  it('DISJOINT far-apart rectangles share NO block key (per-table concurrency preserved)', () => {
    const a = blockKeysForAddress('Sheet1!A1:B2');       // block 0,0
    const b = blockKeysForAddress('Sheet1!A200:B201');   // row 199 -> block 3,0
    expect(a.some(k => b.includes(k))).toBe(false);
  });

  it('a wide insert whose START cell is outside a table but whose BODY overlaps it shares a block (R7-1 by construction)', () => {
    // Insert A1:E2 (start A1 outside C1:F10) still overlaps in cells -> blocks.
    const insert = blockKeysForRect('Sheet1', rectAt(0, 0, 2, 5));
    const table = blockKeysForAddress('Sheet1!C1:F10');
    expect(insert.some(k => table.includes(k))).toBe(true);
  });

  it('different sheets never share a block key', () => {
    const a = blockKeysForAddress('Sheet1!A1:B2');
    const b = blockKeysForAddress('Sheet2!A1:B2');
    expect(a.some(k => b.includes(k))).toBe(false);
  });

  it('R8-4: the footer row is inside the locked block set when it crosses a block boundary (rowCount includes the footer)', () => {
    // Insert 1 data row at row 62 -> header 62, data 63, footer at row 64.
    // rows.length+2 = 3 rows (62-64) crosses the 64-row block boundary, so the
    // footer's block (row 64 -> block br=1) must be included. Mutation:
    // rowCount+1 (rows 62-63) stays in block br=0 and misses the footer block.
    const withFooter = blockKeysForRect('Sheet1', rectAt(62, 0, 3, 2));
    const footerBlock = cellBlockKeys('Sheet1', 64, 0, 64, 0)[0];
    expect(withFooter).toContain(footerBlock);
    const withoutFooter = blockKeysForRect('Sheet1', rectAt(62, 0, 2, 2));
    expect(withoutFooter).not.toContain(footerBlock);
  });

  it('a large table costs ceil(rows/64) x ceil(cols/64) block keys', () => {
    // A1:B130 -> rows 0-129 = 3 row-bands (0-63, 64-127, 128-129); cols 0-1 = 1 band.
    expect(blockKeysForAddress('Sheet1!A1:B130').length).toBe(3);
  });
});

describe('Bug-7397 R8-1 — unionTableRangeAddress (reserve intended growth extent)', () => {
  it('covers the UNION of the current and intended (grown) extent', () => {
    // Current C1:F10 (4 cols, 10 rows). Refresh will write 30 rows (header+29)
    // in the same 4 columns -> intended C1:F30. Union = C1:F30.
    expect(unionTableRangeAddress('Sheet1!C1:F10', 30, 4)).toBe('Sheet1!C1:F30');
  });

  it('keeps the LARGER extent when the refresh shrinks the table', () => {
    // Current A1:D10; intended 3 rows x 4 cols -> A1:D3. Union stays A1:D10.
    expect(unionTableRangeAddress('Sheet1!A1:D10', 3, 4)).toBe('Sheet1!A1:D10');
  });

  it('quotes a spaced sheet name', () => {
    expect(unionTableRangeAddress("'My Sheet'!A1:B2", 5, 2)).toBe("'My Sheet'!A1:B5");
  });
});

describe('Bug-7397 R8-3 — a blocked (fail-closed) insert surfaces a "busy" toast', () => {
  it('describeTableInsertResult returns a warning toast when blocked, but stays silent on a plain no-op', () => {
    // Blocked (fail-closed) -> user-visible warning.
    const blocked = describeTableInsertResult(false, 5, false, true);
    expect(blocked).not.toBeNull();
    expect(blocked!.severity).toBe('warning');
    // Plain no-op (busy guard / decline) -> silent.
    expect(describeTableInsertResult(false, 5, false, false)).toBeNull();
    expect(describeTableInsertResult(false, 5, false)).toBeNull();
  });
});

describe('Bug-7397 R6 — insertTable data write is under the lock and pinned to one sample', () => {
  // Mutable "host" selection. resolveInsertTarget reads it ONCE; the pinned
  // write must not re-read it afterward.
  let currentSelection: { address: string; rowIndex: number; columnIndex: number };
  // Every cell-VALUE assignment records its (row,col). A `.values =` setter
  // fires only on the real data/footer write, never on the overwrite-check read.
  let writeCoords: { row: number; col: number }[];

  function makeRange(row: number, col: number) {
    return {
      rowIndex: row,
      columnIndex: col,
      address: `Sheet1!R${row}C${col}`,
      get values() { return [['']]; },       // overwrite check reads empty -> no prompt
      set values(_v: unknown) { writeCoords.push({ row, col }); },
      formulas: [['']],
      numberFormat: [],
      format: { font: {}, fill: {}, autofitColumns: () => {} },
      load: () => {},
      getCell: () => ({ values: [['']] }),
      getHeaderRowRange: () => ({ format: { font: {} } }),
    };
  }

  function makeSheet() {
    return {
      getRangeByIndexes: (row: number, col: number) => makeRange(row, col),
      getRange: () => makeRange(currentSelection.rowIndex, currentSelection.columnIndex),
      tables: { items: [], load: () => {}, add: () => ({ style: '', getHeaderRowRange: () => ({ format: { font: {} } }) }) },
    };
  }

  beforeEach(() => {
    currentSelection = { address: 'Sheet1!K6', rowIndex: 5, columnIndex: 10 };
    writeCoords = [];
    invalidateMetadataCache();
    const sheet = makeSheet();
    vi.stubGlobal('Excel', {
      run: async (cb: (ctx: unknown) => Promise<unknown>) => cb({
        workbook: {
          worksheets: { getActiveWorksheet: () => sheet, getItem: () => sheet },
          getSelectedRange: () => ({
            get address() { return currentSelection.address; },
            get rowIndex() { return currentSelection.rowIndex; },
            get columnIndex() { return currentSelection.columnIndex; },
            values: [['']],
            load: () => {},
          }),
          names: { items: [], load: () => {}, add: () => ({ comment: '' }), getItemOrNullObject: () => ({ delete: () => {} }) },
        },
        sync: async () => {},
      }),
    });
  });

  afterEach(() => {
    vi.stubGlobal('Excel', undefined);
  });

  it('defers the cell-data write while another op holds an overlapping cell BLOCK, then writes at the PINNED cell (not the moved selection)', async () => {
    // An external op holds the block(s) covering the cells the insert will
    // write (its rect at K6, height rows.length+2). Block keys derive from
    // cells, so the insert shares a key and must wait.
    const heldKeys = blockKeysForRect('Sheet1', rectAt(5, 10, 3, 2));
    let releaseHold: () => void = () => {};
    const held = withTableLocksKeys(heldKeys, () => new Promise<void>((r) => { releaseHold = r; }));

    const { result } = renderHook(() => useExcel(undefined, undefined, 'model-x'));
    const insertPromise = result.current.insertTable(['A', 'B'], [[1, 2]], { useActiveCell: true });

    // Let resolveInsertTarget run (it samples selection = K6) and the insert
    // queue behind the held lock.
    for (let i = 0; i < 50; i++) await Promise.resolve();
    // The data write must NOT have happened yet -- the lock is held.
    expect(writeCoords).toHaveLength(0);

    // Move the selection AFTER the target was resolved. A pinned write must
    // ignore this; a two-sample (unpinned) write would relocate to Z10.
    currentSelection = { address: 'Sheet1!Z10', rowIndex: 9, columnIndex: 25 };

    releaseHold();
    await held;
    await insertPromise;

    // The data write landed at the PINNED cell (row 5, col 10 = K6), never at
    // the moved selection (row 9, col 25 = Z10).
    expect(writeCoords.some(c => c.row === 5 && c.col === 10)).toBe(true);
    expect(writeCoords.some(c => c.row === 9 || c.col === 25)).toBe(false);
  });

  it('R7-1 end to end: a WIDE insert whose rectangle overlaps a tracked table (start cell outside it) shares a block and defers, then writes', async () => {
    // A "refresh" holds the blocks covering a tracked table at C1:F10.
    let release: () => void = () => {};
    const held = withTableLocksKeys(blockKeysForAddress('Sheet1!C1:F10'), () => new Promise<void>((r) => { release = r; }));

    // Insert a 5-column table at A1 (A1:E2) -- start cell A1 is OUTSIDE the
    // table, but the rectangle overlaps its columns C-E (same cell block).
    currentSelection = { address: 'Sheet1!A1', rowIndex: 0, columnIndex: 0 };
    const { result } = renderHook(() => useExcel(undefined, undefined, 'model-x'));
    const insertPromise = result.current.insertTable(['a', 'b', 'c', 'd', 'e'], [[1, 2, 3, 4, 5]], { useActiveCell: true });

    for (let i = 0; i < 50; i++) await Promise.resolve();
    // The insert must be BLOCKED on the shared block -> no data write.
    expect(writeCoords).toHaveLength(0);

    release();
    await held;
    await insertPromise;
    // Once the block frees, the data write lands at the pinned A1 (0,0).
    expect(writeCoords.some(c => c.row === 0 && c.col === 0)).toBe(true);
  });

  it('fails closed (no data write) when the target/lock-key cannot be resolved', async () => {
    // Make the anchor-resolution host read throw. The insert must NOT write
    // blindly to an undetermined location.
    vi.stubGlobal('Excel', {
      run: async () => { throw new Error('host read failed'); },
    });
    const { result } = renderHook(() => useExcel(undefined, undefined, 'model-x'));
    const res = await result.current.insertTable(['A', 'B'], [[1, 2]], { useActiveCell: true });
    expect(res.address).toBeNull();
    expect(writeCoords).toHaveLength(0);
    // R8-3: fail-closed is NOT silent -- it is flagged so the UI can warn.
    expect(res.blocked).toBe(true);
  });

  it('R8-4 end to end: an insert whose footer row shares a block with a tracked table defers behind that table\'s lock', async () => {
    // Insert a 1-row table at A1 -> data A1:B2, footer at row index 2. A tracked
    // table at A3:B8 shares the cell block; the lock rect (rows.length+2) covers
    // the footer row, so the insert serializes against that table's refresh.
    let release: () => void = () => {};
    const held = withTableLocksKeys(blockKeysForAddress('Sheet1!A3:B8'), () => new Promise<void>((r) => { release = r; }));

    currentSelection = { address: 'Sheet1!A1', rowIndex: 0, columnIndex: 0 };
    const { result } = renderHook(() => useExcel(undefined, undefined, 'model-x'));
    const insertPromise = result.current.insertTable(['a', 'b'], [[1, 2]], { useActiveCell: true });

    for (let i = 0; i < 50; i++) await Promise.resolve();
    expect(writeCoords).toHaveLength(0); // blocked on the shared block

    release();
    await held;
    await insertPromise;
    expect(writeCoords.some(c => c.row === 0 && c.col === 0)).toBe(true);
  });
});
