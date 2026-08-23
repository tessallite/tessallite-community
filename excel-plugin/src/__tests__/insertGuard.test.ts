import { describe, it, expect, beforeEach } from 'vitest';
import {
  parseRangeAddress,
  rangeHasContent,
  rangesOverlap,
  InsertTracker,
  type CellRange,
} from '../utils/insertGuard';

describe('parseRangeAddress', () => {
  it('parses a simple cell reference', () => {
    const r = parseRangeAddress('A1');
    expect(r).toEqual({ sheet: '', rowStart: 0, colStart: 0, rowCount: 1, colCount: 1 });
  });

  it('parses a cell reference with a sheet name', () => {
    const r = parseRangeAddress('Sheet1!B2');
    expect(r).toEqual({ sheet: 'Sheet1', rowStart: 1, colStart: 1, rowCount: 1, colCount: 1 });
  });

  it('parses a range address', () => {
    const r = parseRangeAddress('Sheet1!A1:C10');
    expect(r).toEqual({ sheet: 'Sheet1', rowStart: 0, colStart: 0, rowCount: 10, colCount: 3 });
  });

  it('handles quoted sheet names', () => {
    const r = parseRangeAddress("'My Sheet'!D5");
    expect(r).toEqual({ sheet: 'My Sheet', rowStart: 4, colStart: 3, rowCount: 1, colCount: 1 });
  });

  it('handles absolute references ($A$1)', () => {
    const r = parseRangeAddress('$A$1:$C$10');
    expect(r).toEqual({ sheet: '', rowStart: 0, colStart: 0, rowCount: 10, colCount: 3 });
  });

  it('returns null for invalid input', () => {
    expect(parseRangeAddress('')).toBeNull();
    expect(parseRangeAddress('invalid')).toBeNull();
  });

  it('parses multi-letter columns (AA, AB)', () => {
    const r = parseRangeAddress('AA1');
    expect(r).not.toBeNull();
    expect(r!.colStart).toBe(26); // AA = column 27 (zero-based 26)
  });
});

describe('Bug-8344: rangeHasContent consults BOTH the values and formulas channels', () => {
  it('THE DEFECT: a formula rendering "" is an OCCUPIED cell, not an empty one', () => {
    // `=IF(A1>0,A1,"")` reads back from Office.js as an empty VALUE. The
    // pre-fix probes tested `values` alone, called the cell empty, skipped the
    // overwrite prompt and destroyed the formula.
    expect(rangeHasContent([['']], [['=IF(A1>0,A1,"")']])).toBe(true);
  });

  it('a CUBEVALUE formula in the last cell of a large range is still found', () => {
    const values = Array.from({ length: 5 }, () => Array(3).fill(''));
    const formulas = Array.from({ length: 5 }, () => Array(3).fill(''));
    formulas[4][2] = '=CUBEVALUE("Tessallite","[Measures].[Revenue]")';
    expect(rangeHasContent(values, formulas)).toBe(true);
  });

  it('genuinely empty rectangles stay empty (no false overwrite prompt)', () => {
    expect(rangeHasContent([], [])).toBe(false);
    expect(rangeHasContent(null, null)).toBe(false);
    expect(rangeHasContent(undefined, undefined)).toBe(false);
    expect(rangeHasContent([['']], [['']])).toBe(false);
    expect(rangeHasContent([[null, '', undefined]], [[null, '', null]])).toBe(false);
  });

  it('a plain value in any cell occupies the range', () => {
    expect(rangeHasContent([['hello']], [['']])).toBe(true);
    expect(rangeHasContent([[null, 'x']], [[null, null]])).toBe(true);
  });

  it('a literal 0 is content, not emptiness', () => {
    // A zero is a real number a user typed; overwriting it silently would be
    // the same data loss as overwriting a formula.
    expect(rangeHasContent([[0]], [['']])).toBe(true);
  });

  it('tolerates ragged / partially-loaded grids without missing a cell', () => {
    // Office.js hands back whatever was loaded; the helper must not index past
    // the shorter grid and must still see content in the longer one.
    expect(rangeHasContent([['']], [['', ''], ['', '=A1']])).toBe(true);
    expect(rangeHasContent([[], ['x']], [])).toBe(true);
    expect(rangeHasContent([['a', null], ['', '']], [[null, null], [null, null]])).toBe(true);
  });

  it('a non-string formulas entry is never mistaken for a formula', () => {
    // Defensive: only a non-empty STRING is a formula. A stray number in the
    // formulas grid must not create a phantom overwrite prompt on an empty cell.
    expect(rangeHasContent([['']], [[0]])).toBe(false);
  });
});

describe('rangesOverlap', () => {
  const rangeA: CellRange = { sheet: 'Sheet1', rowStart: 0, colStart: 0, rowCount: 5, colCount: 3 };
  const rangeB: CellRange = { sheet: 'Sheet1', rowStart: 3, colStart: 2, rowCount: 5, colCount: 3 };
  const rangeC: CellRange = { sheet: 'Sheet1', rowStart: 10, colStart: 0, rowCount: 5, colCount: 3 };
  const rangeD: CellRange = { sheet: 'Sheet2', rowStart: 0, colStart: 0, rowCount: 5, colCount: 3 };

  it('detects overlapping ranges', () => {
    expect(rangesOverlap(rangeA, rangeB)).toBe(true);
  });

  it('detects non-overlapping ranges', () => {
    expect(rangesOverlap(rangeA, rangeC)).toBe(false);
  });

  it('different sheets never overlap', () => {
    expect(rangesOverlap(rangeA, rangeD)).toBe(false);
  });

  it('exact same range overlaps', () => {
    expect(rangesOverlap(rangeA, rangeA)).toBe(true);
  });

  it('adjacent ranges do not overlap', () => {
    const adj: CellRange = { sheet: 'Sheet1', rowStart: 5, colStart: 0, rowCount: 5, colCount: 3 };
    expect(rangesOverlap(rangeA, adj)).toBe(false);
  });
});

describe('InsertTracker', () => {
  let tracker: InsertTracker;

  beforeEach(() => {
    tracker = new InsertTracker();
  });

  describe('tryStart / complete', () => {
    it('allows the first submission', () => {
      expect(tracker.tryStart('table:report1')).toBe(true);
    });

    it('rejects a duplicate submission', () => {
      tracker.tryStart('table:report1');
      expect(tracker.tryStart('table:report1')).toBe(false);
    });

    it('allows resubmission after completion', () => {
      tracker.tryStart('table:report1');
      tracker.complete('table:report1');
      expect(tracker.tryStart('table:report1')).toBe(true);
    });
  });

  describe('anchor collision detection', () => {
    it('detects collision with in-flight range', () => {
      tracker.tryStart('op1');
      tracker.registerInFlight('op1', {
        sheet: 'Sheet1', rowStart: 0, colStart: 0, rowCount: 10, colCount: 3,
      });

      const proposed: CellRange = {
        sheet: 'Sheet1', rowStart: 5, colStart: 0, rowCount: 5, colCount: 3,
      };
      expect(tracker.checkCollision(proposed)).not.toBeNull();
    });

    it('detects collision with recently-written range', () => {
      tracker.tryStart('op1');
      tracker.registerInFlight('op1', {
        sheet: 'Sheet1', rowStart: 0, colStart: 0, rowCount: 10, colCount: 3,
      });
      tracker.commitRange('op1');
      tracker.complete('op1');

      const proposed: CellRange = {
        sheet: 'Sheet1', rowStart: 5, colStart: 0, rowCount: 5, colCount: 3,
      };
      expect(tracker.checkCollision(proposed)).not.toBeNull();
    });

    it('no collision on different sheet', () => {
      tracker.tryStart('op1');
      tracker.registerInFlight('op1', {
        sheet: 'Sheet1', rowStart: 0, colStart: 0, rowCount: 10, colCount: 3,
      });

      const proposed: CellRange = {
        sheet: 'Sheet2', rowStart: 0, colStart: 0, rowCount: 5, colCount: 3,
      };
      expect(tracker.checkCollision(proposed)).toBeNull();
    });
  });

  describe('findSafeAnchor', () => {
    it('returns null when no collision', () => {
      const proposed: CellRange = {
        sheet: 'Sheet1', rowStart: 0, colStart: 0, rowCount: 5, colCount: 3,
      };
      expect(tracker.findSafeAnchor(proposed)).toBeNull();
    });

    it('offsets below the last collision', () => {
      tracker.tryStart('op1');
      tracker.registerInFlight('op1', {
        sheet: 'Sheet1', rowStart: 0, colStart: 0, rowCount: 10, colCount: 3,
      });

      const proposed: CellRange = {
        sheet: 'Sheet1', rowStart: 5, colStart: 0, rowCount: 5, colCount: 3,
      };
      const safe = tracker.findSafeAnchor(proposed);
      expect(safe).not.toBeNull();
      // Should be below the in-flight range (row 10) + 1 gap = row 11.
      expect(safe!.rowStart).toBe(11);
      expect(safe!.colStart).toBe(0);
      expect(safe!.rowCount).toBe(5);
      expect(safe!.colCount).toBe(3);
    });
  });

  describe('isAnyActive', () => {
    it('returns false when no operations are active', () => {
      expect(tracker.isAnyActive).toBe(false);
    });

    it('returns true when an operation is in flight', () => {
      tracker.tryStart('op1');
      expect(tracker.isAnyActive).toBe(true);
    });
  });

  describe('clear', () => {
    it('resets all tracking state', () => {
      tracker.tryStart('op1');
      tracker.registerInFlight('op1', {
        sheet: 'Sheet1', rowStart: 0, colStart: 0, rowCount: 10, colCount: 3,
      });
      tracker.clear();
      expect(tracker.isAnyActive).toBe(false);
      expect(tracker.checkCollision({
        sheet: 'Sheet1', rowStart: 0, colStart: 0, rowCount: 5, colCount: 3,
      })).toBeNull();
    });
  });

  describe('operationKey', () => {
    it('builds a deterministic key', () => {
      expect(InsertTracker.operationKey('table', 'report1')).toBe('table:report1');
      expect(InsertTracker.operationKey('chart', 'c1')).toBe('chart:c1');
    });
  });
});
