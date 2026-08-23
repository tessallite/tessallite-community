/**
 * Bug-7397 R12 — the block-lock CONTRACT, extended to every first-party cell
 * writer, plus the acquisition-deadline semantics.
 *
 * R6-R11 proved the block-lock MECHANISM (overlapping rectangles necessarily
 * share a key). The external gate then reproduced, live, that the mechanism was
 * only applied by two producers: a formula insert completed and overwrote a cell
 * WHILE a covering refresh lock was held, because `insertFormula` opened its own
 * Excel.run with no lock at all. These tests drive the REAL hook methods through
 * the real lock so that gap cannot reopen -- and pin the deadlock-freedom and
 * timeout properties the lock relies on.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { renderHook } from '@testing-library/react';
import { useExcel } from '../hooks/useExcel';
import {
  blockKeysForAddress,
  blockKeysForRect,
  cellBlockKeys,
  withTableLocksKeys,
  LockAcquireTimeoutError,
  invalidateMetadataCache,
} from '../utils/workbookMetadata';
import { resolveWriteTarget, withPinnedCellWrite } from '../utils/lockedCellWrite';

// ---------------------------------------------------------------------------
// A minimal fake host that records WHERE each cell write landed.
// ---------------------------------------------------------------------------

let currentSelection: { address: string; rowIndex: number; columnIndex: number };
let writeCoords: { row: number; col: number }[];

function makeRange(row: number, col: number) {
  return {
    rowIndex: row,
    columnIndex: col,
    address: `Sheet1!R${row}C${col}`,
    get values() { return [['', '', '', '', '', '']]; }, // empty -> no overwrite prompt
    set values(_v: unknown) { writeCoords.push({ row, col }); },
    get formulas() { return [['']]; },
    set formulas(_v: unknown) { writeCoords.push({ row, col }); },
    numberFormat: [],
    format: { font: {}, fill: {}, autofitColumns: () => {} },
    conditionalFormats: { add: () => ({ iconSetOrNullObject: {} }) },
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

function stubHost() {
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
          formulas: [['']],
          load: () => {},
        }),
        names: { items: [], load: () => {}, add: () => ({ comment: '' }), getItemOrNullObject: () => ({ delete: () => {} }) },
      },
      sync: async () => {},
    }),
    ConditionalFormatType: { iconSet: 'iconSet' },
    IconSet: { threeTrafficLights1: 'threeTrafficLights1' },
    ConditionalFormatIconRuleType: { number: 'number' },
    ConditionalIconCriterionOperator: { greaterThanOrEqual: 'gte' },
  });
}

/** Drain the microtask queue so a queued lock acquisition can settle. */
async function drain(times = 60): Promise<void> {
  for (let i = 0; i < times; i++) await Promise.resolve();
}

beforeEach(() => {
  currentSelection = { address: 'Sheet1!K6', rowIndex: 5, columnIndex: 10 };
  writeCoords = [];
  invalidateMetadataCache();
  stubHost();
});

afterEach(() => {
  vi.stubGlobal('Excel', undefined);
});

// ---------------------------------------------------------------------------
// HIGH: every first-party cell writer participates in the lock.
// ---------------------------------------------------------------------------

describe('Bug-7397 R12-1 — first-party cell writers acquire the blocks covering the cells they write', () => {
  it('insertFormula DEFERS while a covering block is held, then writes at the PINNED cell (not the moved selection)', async () => {
    const heldKeys = blockKeysForRect('Sheet1', { startRow: 5, startCol: 10, rowCount: 1, colCount: 1 });
    let release: () => void = () => {};
    const held = withTableLocksKeys(heldKeys, () => new Promise<void>((r) => { release = r; }));

    const { result } = renderHook(() => useExcel());
    const p = result.current.insertFormula('=TESSALLITE.VALUE("m","x")');

    await drain();
    // Pre-fix this write completed immediately, straight through a held lock.
    expect(writeCoords).toHaveLength(0);

    // Move the selection after the target was pinned; the write must ignore it.
    currentSelection = { address: 'Sheet1!Z40', rowIndex: 39, columnIndex: 25 };

    release();
    await held;
    await expect(p).resolves.toBe(true);
    expect(writeCoords.some(c => c.row === 5 && c.col === 10)).toBe(true);
    expect(writeCoords.some(c => c.row === 39 || c.col === 25)).toBe(false);
  });

  it('insertLiteral DEFERS while a covering block is held (the static-insert path is not exempt)', async () => {
    const heldKeys = blockKeysForRect('Sheet1', { startRow: 5, startCol: 10, rowCount: 1, colCount: 1 });
    let release: () => void = () => {};
    const held = withTableLocksKeys(heldKeys, () => new Promise<void>((r) => { release = r; }));

    const { result } = renderHook(() => useExcel());
    const p = result.current.insertLiteral('1234.5');

    await drain();
    expect(writeCoords).toHaveLength(0);

    release();
    await held;
    await expect(p).resolves.toBe(true);
    expect(writeCoords.some(c => c.row === 5 && c.col === 10)).toBe(true);
  });

  it('a single-cell KPI/measure writer DEFERS while a tracked table holding the same block is being refreshed', async () => {
    // A refresh holds Sheet1!A1:D10; the user's selection (B3) is inside it.
    currentSelection = { address: 'Sheet1!B3', rowIndex: 2, columnIndex: 1 };
    let release: () => void = () => {};
    const held = withTableLocksKeys(blockKeysForAddress('Sheet1!A1:D10'), () => new Promise<void>((r) => { release = r; }));

    const { result } = renderHook(() => useExcel());
    const p = result.current.insertMeasureAsFormula('revenue', 'Tessallite');

    await drain();
    expect(writeCoords).toHaveLength(0);

    release();
    await held;
    await p;
    expect(writeCoords.some(c => c.row === 2 && c.col === 1)).toBe(true);
  });

  it('the KPI scorecard locks its FULL rectangle: a block covering only its LOWER rows still defers it', async () => {
    // Scorecard anchored at row 60 with 10 KPIs occupies rows 60..70, crossing
    // the 64-row block boundary. A holder of the row-64+ block (br=1) must
    // exclude it even though its start cell is in block br=0.
    currentSelection = { address: 'Sheet1!A61', rowIndex: 60, columnIndex: 0 };
    const lowerBlock = cellBlockKeys('Sheet1', 70, 0, 70, 0);
    let release: () => void = () => {};
    const held = withTableLocksKeys(lowerBlock, () => new Promise<void>((r) => { release = r; }));

    const kpis = Array.from({ length: 10 }, (_, i) => ({
      id: `k${i}`, name: `kpi${i}`, display_name: null,
      evaluatedValue: i, evaluatedGoal: null, evaluatedStatus: null,
    }));

    const { result } = renderHook(() => useExcel());
    const p = result.current.insertKpiScorecard(kpis as never, 'Tessallite');

    await drain();
    expect(writeCoords).toHaveLength(0);

    release();
    await held;
    await p;
    expect(writeCoords.length).toBeGreaterThan(0);
  });

  it('R12 review finding 1: a composite-KPI status literal is written INSIDE insertKpiStatusOnly\'s own critical section, through the values channel only', async () => {
    // Pre-fix, ReportBuilder called insertKpiStatusOnly (which wrote a CUBE
    // formula), let that critical section RELEASE, then overwrote the same cell
    // from its own raw, unlocked Excel.run -- an unlocked write AND a
    // second-acquisition TOCTOU. The literal now goes in as the cell's only
    // write, inside the one lock the hook already holds.
    const channels: string[] = [];
    const sheet = {
      getRangeByIndexes: () => ({
        get values() { return [['']]; },
        set values(_v: unknown) { channels.push('values'); },
        get formulas() { return [['']]; },
        set formulas(_v: unknown) { channels.push('formulas'); },
        conditionalFormats: { add: () => ({ iconSetOrNullObject: {} }) },
        address: 'Sheet1!K6',
        load: () => {},
      }),
      getRange: () => ({ rowIndex: 5, columnIndex: 10, address: 'Sheet1!K6', load: () => {} }),
    };
    vi.stubGlobal('Excel', {
      run: async (cb: (ctx: unknown) => Promise<unknown>) => cb({
        workbook: {
          worksheets: { getActiveWorksheet: () => sheet, getItem: () => sheet },
          getSelectedRange: () => ({ address: 'Sheet1!K6', rowIndex: 5, columnIndex: 10, load: () => {} }),
        },
        sync: async () => {},
      }),
      ConditionalFormatType: { iconSet: 'iconSet' },
      IconSet: { threeTrafficLights1: 'x' },
      ConditionalFormatIconRuleType: { number: 'number' },
      ConditionalIconCriterionOperator: { greaterThanOrEqual: 'gte' },
    });

    const { result } = renderHook(() => useExcel());
    const addr = await result.current.insertKpiStatusOnly('kpi_margin', 'Tessallite', { statusLiteral: 'on_target' });

    expect(addr).toBe('Sheet1!K6');
    // Exactly ONE cell write, and it is the values channel -- no transient CUBE
    // formula that a second acquisition then has to overwrite, and no formula
    // channel for a source-derived string (Bug-7393).
    expect(channels).toEqual(['values']);
  });

  it('an ordinary (non-composite) KPI status insert still uses the CUBE formula channel', async () => {
    // Guards the other side: the literal override must not silently change
    // behaviour for every other KPI.
    const { result } = renderHook(() => useExcel());
    await result.current.insertKpiStatusOnly('kpi_margin', 'Tessallite');
    expect(writeCoords.some(c => c.row === 5 && c.col === 10)).toBe(true);
  });

  // Promoted from review round 1: the reviewer probed these six writers by hand
  // (none of them had a deferral test) and found them sound. A hand probe that
  // is not in the suite protects nothing, so each one is pinned here.
  const WRITERS: { name: string; run: (hook: ReturnType<typeof useExcel>) => Promise<unknown> }[] = [
    {
      name: 'insertNamedSetAsFormulas',
      run: h => h.insertNamedSetAsFormulas(
        { id: 'ns', name: 'top', display_name: null, expression: '{}', updated_at: undefined }, 'Tessallite', 3,
      ),
    },
    {
      name: 'insertKpiFormulas',
      run: h => h.insertKpiFormulas(
        { id: 'k', name: 'kpi', display_name: null }, 'valueM', 'goalM', 'Tessallite',
      ),
    },
    {
      name: 'insertKpiFullRow',
      run: h => h.insertKpiFullRow(
        { id: 'k', name: 'kpi', display_name: null }, 'valueM', 'goalM', 'Tessallite',
      ),
    },
    { name: 'insertKpiValueOnly', run: h => h.insertKpiValueOnly('revenue', 'Tessallite') },
    { name: 'insertKpiStatusOnly', run: h => h.insertKpiStatusOnly('kpi', 'Tessallite') },
    { name: 'insertKpiValueFormula', run: h => h.insertKpiValueFormula('kpi', 'Tessallite') },
  ];

  it.each(WRITERS)('$name DEFERS while a covering block is held, then writes at the pinned cell', async ({ run }) => {
    const heldKeys = blockKeysForRect('Sheet1', { startRow: 5, startCol: 10, rowCount: 8, colCount: 8 });
    let release: () => void = () => {};
    const held = withTableLocksKeys(heldKeys, () => new Promise<void>((r) => { release = r; }));

    const { result } = renderHook(() => useExcel());
    const p = run(result.current);

    await drain();
    expect(writeCoords).toHaveLength(0);

    release();
    await held;
    await p;
    expect(writeCoords.some(c => c.row === 5 && c.col === 10)).toBe(true);
  });

  it('fails closed (no write) when the target cannot be resolved from the host', async () => {
    vi.stubGlobal('Excel', { run: async () => { throw new Error('host read failed'); } });
    const { result } = renderHook(() => useExcel());
    await expect(result.current.insertFormula('=X()')).rejects.toThrow();
    expect(writeCoords).toHaveLength(0);
  });

  it('resolveWriteTarget refuses an address with no sheet qualifier instead of guessing', async () => {
    currentSelection = { address: 'K6', rowIndex: 5, columnIndex: 10 };
    expect(await resolveWriteTarget()).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// MED 3: acquisition deadline, cancellation and recovery.
// ---------------------------------------------------------------------------

describe('Bug-7397 R12-3 — bounded lock acquisition (a wedged holder fails loudly, never silently)', () => {
  const KEYS = cellBlockKeys('SheetT', 0, 0, 0, 0);

  it('a waiter gives up with LockAcquireTimeoutError, never runs its body, and does NOT run it later when the holder frees the key', async () => {
    let release: () => void = () => {};
    const held = withTableLocksKeys(KEYS, () => new Promise<void>((r) => { release = r; }));

    let waiterRan = false;
    const waiter = withTableLocksKeys(KEYS, async () => { waiterRan = true; }, { timeoutMs: 10 });

    await expect(waiter).rejects.toBeInstanceOf(LockAcquireTimeoutError);
    expect(waiterRan).toBe(false);

    // The wedge clears. The abandoned waiter must STAY abandoned: the user was
    // already told nothing happened, so a surprise write landing now would be
    // worse than the timeout.
    release();
    await held;
    await drain();
    expect(waiterRan).toBe(false);
  });

  it('the key is still usable by a NEW acquirer after a waiter timed out (the promise chain is not permanently broken)', async () => {
    let release: () => void = () => {};
    const held = withTableLocksKeys(KEYS, () => new Promise<void>((r) => { release = r; }));
    await expect(
      withTableLocksKeys(KEYS, async () => 'never', { timeoutMs: 10 }),
    ).rejects.toBeInstanceOf(LockAcquireTimeoutError);

    release();
    await held;

    await expect(withTableLocksKeys(KEYS, async () => 'ok', { timeoutMs: 200 })).resolves.toBe('ok');
  });

  it('the deadline bounds WAITING only -- a critical section that runs longer than the deadline is never interrupted', async () => {
    const value = await withTableLocksKeys(
      cellBlockKeys('SheetT2', 0, 0, 0, 0),
      async () => {
        await new Promise<void>((r) => { setTimeout(r, 40); });
        return 'completed';
      },
      { timeoutMs: 10 },
    );
    // Interrupting the critical section would admit a second writer into cells
    // the first may still be mutating -- exactly the class this lock prevents.
    expect(value).toBe('completed');
  });

  // Promoted from review round 1: the reviewer ran these four attacks by hand
  // and none broke the lock. Hand-run attacks protect nothing once the agent
  // exits, so they are pinned here.
  it('MUTUAL EXCLUSION under load: 40 concurrent acquirers of one key never overlap', async () => {
    const key = cellBlockKeys('SheetStress', 0, 0, 0, 0);
    let inside = 0;
    let maxInside = 0;
    let completed = 0;
    await Promise.all(Array.from({ length: 40 }, () =>
      withTableLocksKeys(key, async () => {
        inside++;
        maxInside = Math.max(maxInside, inside);
        // Yield across a real async boundary -- the whole point is that the
        // chain serializes across `await`, not merely across a sync block.
        await new Promise<void>((r) => { setTimeout(r, 0); });
        inside--;
        completed++;
      }, { timeoutMs: 5000 })));
    expect(maxInside).toBe(1);
    expect(completed).toBe(40);
  });

  it('a timer firing in the same tick as the release runs the body EXACTLY once or not at all -- never both', async () => {
    // The dangerous outcome is a waiter that both rejects to the user AND later
    // runs its body: the user is told nothing happened while a write lands.
    for (let trial = 0; trial < 25; trial++) {
      const key = cellBlockKeys(`SheetRace${trial}`, 0, 0, 0, 0);
      let release: () => void = () => {};
      const held = withTableLocksKeys(key, () => new Promise<void>((r) => { release = r; }));
      let ran = 0;
      const waiter = withTableLocksKeys(key, async () => { ran++; }, { timeoutMs: 5 })
        .then(() => 'ok').catch(() => 'timeout');
      setTimeout(() => release(), 5); // collide the deadline with the release
      const outcome = await waiter;
      await held;
      await drain();
      expect(ran).toBe(outcome === 'ok' ? 1 : 0);
    }
  });

  it('a FREE first key does not leave a WEDGED second key unbounded, and the first key is released promptly', async () => {
    const free = 'multi#b0_0';
    const wedged = 'multi#b0_1';
    let release: () => void = () => {};
    const holder = withTableLocksKeys([wedged], () => new Promise<void>((r) => { release = r; }));

    let bodyRan = false;
    const started = Date.now();
    await expect(
      withTableLocksKeys([free, wedged], async () => { bodyRan = true; }, { timeoutMs: 30 }),
    ).rejects.toBeInstanceOf(LockAcquireTimeoutError);
    expect(bodyRan).toBe(false);

    // The FREE key must not stay held by the abandoned acquirer: a later
    // operation needing only that key proceeds without waiting for the wedge.
    const t0 = Date.now();
    await withTableLocksKeys([free], async () => undefined, { timeoutMs: 500 });
    expect(Date.now() - t0).toBeLessThan(300);
    expect(Date.now() - started).toBeLessThan(2000);

    release();
    await holder;
  });

  it('the deadline spans the WHOLE key set, not each key independently', async () => {
    // Three wedged keys with an 80ms budget must reject in ~80ms, not 240ms.
    const keys = ['span#b0_0', 'span#b0_1', 'span#b0_2'];
    const releases: (() => void)[] = [];
    const holders = keys.map(k => withTableLocksKeys([k], () => new Promise<void>((r) => { releases.push(r); })));

    const t0 = Date.now();
    await expect(
      withTableLocksKeys(keys, async () => undefined, { timeoutMs: 80 }),
    ).rejects.toBeInstanceOf(LockAcquireTimeoutError);
    expect(Date.now() - t0).toBeLessThan(200);

    releases.forEach(r => r());
    await Promise.all(holders);
  });

  it('withPinnedCellWrite reports a busy outcome (not a thrown failure, not a silent no-op) when the blocks stay held', async () => {
    const target = { sheetName: 'SheetT3', startRow: 0, startCol: 0 };
    let release: () => void = () => {};
    const held = withTableLocksKeys(
      blockKeysForRect('SheetT3', { startRow: 0, startCol: 0, rowCount: 1, colCount: 1 }),
      () => new Promise<void>((r) => { release = r; }),
    );

    let bodyRan = false;
    const outcome = await withPinnedCellWrite(target, { rowCount: 1, colCount: 1 }, async () => {
      bodyRan = true;
      return 'written';
    }, { timeoutMs: 10 });

    expect(outcome).toEqual({ ok: false, reason: 'busy' });
    expect(bodyRan).toBe(false);
    release();
    await held;
  });
});

// ---------------------------------------------------------------------------
// LOW #1: the sort in withTableLocksKeys is what makes multi-key acquisition
// deadlock-free. Unpinned before this test.
// ---------------------------------------------------------------------------

describe('Bug-7397 R12-LOW-1 — multi-key acquisition takes keys in one total order (deadlock freedom)', () => {
  it('two operations requesting the SAME two keys in OPPOSITE orders both complete', async () => {
    // Without the `.sort()` in withTableLocksKeys, A takes k1 then waits for k2
    // while B takes k2 then waits for k1 -- a classic hold-and-wait deadlock
    // that leaves both promises pending forever. Mutation: delete the .sort()
    // and this test fails on the 'deadlocked' branch.
    const k1 = 'sortguard#b0_0';
    const k2 = 'sortguard#b0_1';
    const done: string[] = [];

    const a = withTableLocksKeys([k1, k2], async () => { await Promise.resolve(); done.push('a'); }, { timeoutMs: 5000 });
    const b = withTableLocksKeys([k2, k1], async () => { await Promise.resolve(); done.push('b'); }, { timeoutMs: 5000 });

    const outcome = await Promise.race([
      Promise.all([a, b]).then(() => 'completed').catch(() => 'errored'),
      new Promise<string>((r) => { setTimeout(() => r('deadlocked'), 250); }),
    ]);

    expect(outcome).toBe('completed');
    expect(done.sort()).toEqual(['a', 'b']);
  });
});

// ---------------------------------------------------------------------------
// LOW #2: the insert's lock rectangle includes the provenance FOOTER row.
// ---------------------------------------------------------------------------

describe('Bug-7397 R12-LOW-2 — the insert locks the provenance footer row it writes', () => {
  it('an insert whose footer row falls in the NEXT cell block defers behind a holder of that block', async () => {
    // Header at row 62, one data row at 63, footer at row 64 -> the footer is
    // the ONLY part of the insert in block br=1. Mutation: dropping the footer
    // row from the lock rectangle (rows.length + 1 instead of
    // rows.length + 1 + PROVENANCE_FOOTER_ROWS) leaves the insert holding only
    // block br=0, so it proceeds immediately and writes the footer into a cell
    // another operation is mutating.
    currentSelection = { address: 'Sheet1!A63', rowIndex: 62, columnIndex: 0 };
    const footerBlock = cellBlockKeys('Sheet1', 64, 0, 64, 0);
    let release: () => void = () => {};
    const held = withTableLocksKeys(footerBlock, () => new Promise<void>((r) => { release = r; }));

    const { result } = renderHook(() => useExcel());
    const p = result.current.insertTable(['a', 'b'], [[1, 2]], { useActiveCell: true });

    await drain();
    expect(writeCoords).toHaveLength(0);

    release();
    await held;
    await p;
    expect(writeCoords.some(c => c.row === 62 && c.col === 0)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Bug-8345 — a claimed rectangle is a WRITTEN rectangle.
// ---------------------------------------------------------------------------

/**
 * A writer that locks a rectangle AND asks the user to confirm overwriting it
 * has claimed every cell in it. Skipping a write inside that rectangle does not
 * produce a blank cell -- it leaves the previous occupant's content in place,
 * where the surrounding fresh cells make it read as current data. For a KPI that
 * is a wrong number in the user's workbook.
 *
 * The host below starts with a PREVIOUS KPI already on the sheet (label
 * `Old KPI`, value `42`, goal `99`) and confirms the overwrite prompt, which is
 * the exact reachable path the registry describes for Bug-8345.
 */
describe('Bug-8345 — every cell inside the claimed, overwrite-confirmed rectangle is written', () => {
  /** Every write, with the VALUE that landed -- not just the coordinate. */
  let cellWrites: { row: number; col: number; value: unknown }[];
  let confirmedPrompts: string[];

  /** Stale content a previous KPI left behind, keyed `row,col`. */
  const STALE: Record<string, unknown> = {
    '5,10': 'Old KPI', '5,11': 'leftover',
    '6,10': 'Value', '6,11': 42,
    '7,10': 'Goal', '7,11': 99,
    '5,12': 99, '5,13': 1,
  };

  function occupiedRange(row: number, col: number, rows: number, cols: number) {
    return {
      rowIndex: row,
      columnIndex: col,
      address: `Sheet1!R${row}C${col}`,
      get values() {
        return Array.from({ length: rows }, (_, dr) =>
          Array.from({ length: cols }, (_, dc) => STALE[`${row + dr},${col + dc}`] ?? ''));
      },
      set values(v: unknown) {
        const grid = v as unknown[][];
        grid.forEach((r, dr) => r.forEach((cell, dc) => {
          cellWrites.push({ row: row + dr, col: col + dc, value: cell });
        }));
      },
      get formulas() { return [['']]; },
      set formulas(v: unknown) {
        const grid = v as unknown[][];
        grid.forEach((r, dr) => r.forEach((cell, dc) => {
          cellWrites.push({ row: row + dr, col: col + dc, value: cell });
        }));
      },
      numberFormat: [],
      format: { font: {}, fill: {}, autofitColumns: () => {} },
      conditionalFormats: { add: () => ({ iconSetOrNullObject: {} }) },
      load: () => {},
      getCell: () => ({ values: [['']] }),
      getHeaderRowRange: () => ({ format: { font: {} } }),
    };
  }

  beforeEach(() => {
    cellWrites = [];
    confirmedPrompts = [];
    currentSelection = { address: 'Sheet1!K6', rowIndex: 5, columnIndex: 10 };
    invalidateMetadataCache();
    const sheet = {
      // The hook asks for the whole block first (the overwrite probe) and then
      // for each cell; one factory serves both because the rect is a parameter.
      getRangeByIndexes: (row: number, col: number, rows = 1, cols = 1) => occupiedRange(row, col, rows, cols),
      getRange: () => occupiedRange(currentSelection.rowIndex, currentSelection.columnIndex, 1, 1),
      tables: { items: [], load: () => {}, add: () => ({ style: '', getHeaderRowRange: () => ({ format: { font: {} } }) }) },
    };
    vi.stubGlobal('Excel', {
      run: async (cb: (ctx: unknown) => Promise<unknown>) => cb({
        workbook: {
          worksheets: { getActiveWorksheet: () => sheet, getItem: () => sheet },
          getSelectedRange: () => ({
            get address() { return currentSelection.address; },
            get rowIndex() { return currentSelection.rowIndex; },
            get columnIndex() { return currentSelection.columnIndex; },
            values: [['']],
            formulas: [['']],
            load: () => {},
          }),
          names: { items: [], load: () => {}, add: () => ({ comment: '' }), getItemOrNullObject: () => ({ delete: () => {} }) },
        },
        sync: async () => {},
      }),
      ConditionalFormatType: { iconSet: 'iconSet' },
      IconSet: { threeTrafficLights1: 'threeTrafficLights1' },
      ConditionalFormatIconRuleType: { number: 'number' },
      ConditionalIconCriterionOperator: { greaterThanOrEqual: 'gte' },
    });
  });

  const confirmYes = async (msg: string): Promise<boolean> => {
    confirmedPrompts.push(msg);
    return true;
  };

  /** The value that finally landed in a cell, or `undefined` if never written. */
  const finalValue = (row: number, col: number): unknown => {
    const hits = cellWrites.filter(w => w.row === row && w.col === col);
    return hits.length ? hits[hits.length - 1].value : undefined;
  };

  it('insertKpiFormulas BLANKS the Value cell when the composite evaluation is null (does not leave the previous KPI\'s number)', async () => {
    const { result } = renderHook(() => useExcel(confirmYes));
    const addr = await result.current.insertKpiFormulas(
      { id: 'k', name: 'kpi_margin', display_name: 'Margin' },
      null,        // no value measure -- composite/expression KPI
      null,        // no goal measure
      'Tessallite',
      undefined,   // goalLiteral
      null,        // valueLiteral: div-by-zero / no data
      true,        // forceLiteral
    );

    expect(addr).toBeTruthy();
    // The user WAS asked to overwrite -- so every cell in the block was claimed.
    expect(confirmedPrompts).toHaveLength(1);
    // The defect: pre-fix nothing was written here and `42` survived.
    expect(finalValue(6, 11)).toBe('');
    expect(finalValue(6, 11)).not.toBe(42);
    // And the label row's second cell, also inside the claimed 2-column block.
    expect(finalValue(5, 11)).toBe('');
  });

  it('insertKpiFormulas still writes a non-null composite value as the number itself', async () => {
    const { result } = renderHook(() => useExcel(confirmYes));
    await result.current.insertKpiFormulas(
      { id: 'k', name: 'kpi_margin', display_name: 'Margin' },
      null, null, 'Tessallite',
      undefined,
      17.5,
      true,
    );
    expect(finalValue(6, 11)).toBe(17.5);
  });

  it('insertKpiFullRow BLANKS the Goal cell when the KPI has neither a goal measure nor a static target', async () => {
    // Reachable from ReportBuilder: `goalLiteral` is null for every KPI whose
    // target_type is not 'static', and `goalMeasure?.name || null` is null when
    // the KPI has no goal measure. The Goal column is still inside the 1x4
    // rectangle the writer locks and prompts about.
    const { result } = renderHook(() => useExcel(confirmYes));
    const addr = await result.current.insertKpiFullRow(
      { id: 'k', name: 'kpi_margin', display_name: 'Margin' },
      'revenue',   // value measure
      null,        // no goal measure
      'Tessallite',
      null,        // no static target
    );

    expect(addr).toBeTruthy();
    expect(confirmedPrompts).toHaveLength(1);
    // Pre-fix the previous KPI's goal (99) was never overwritten.
    expect(finalValue(5, 12)).toBe('');
    expect(finalValue(5, 12)).not.toBe(99);
  });

  it('insertKpiFullRow still writes a static target as the number itself', async () => {
    const { result } = renderHook(() => useExcel(confirmYes));
    await result.current.insertKpiFullRow(
      { id: 'k', name: 'kpi_margin', display_name: 'Margin' },
      'revenue', null, 'Tessallite',
      250,
    );
    expect(finalValue(5, 12)).toBe(250);
  });
});

// ---------------------------------------------------------------------------
// Bug-8344: the overwrite-confirmation probe must see the FORMULAS channel.
// ---------------------------------------------------------------------------

describe('Bug-8344 — a formula rendering "" occupies the target and MUST raise the overwrite prompt', () => {
  let prompts: string[];
  let confirmAnswer: boolean;
  let wrote: boolean;

  /**
   * A host faithful to Office.js on the one point that matters: `.formulas` is
   * unreadable until `load()` asked for it. A probe that loads `values` alone
   * therefore CANNOT see the formula — which is the defect, not an accident of
   * the fake. The user's cell holds `=IF(B1>0,B1,"")`, so every VALUE reads
   * back as the empty string.
   */
  function formulaOnlyRange(row: number, col: number, rows: number, cols: number) {
    let loadedFormulas = false;
    const grid = <T,>(fill: T) => Array.from({ length: rows }, () => Array.from({ length: cols }, () => fill));
    return {
      rowIndex: row,
      columnIndex: col,
      address: `Sheet1!R${row}C${col}`,
      get values() { return grid(''); },          // formula renders "" -> empty VALUE
      set values(_v: unknown) { wrote = true; },
      get formulas() {
        if (!loadedFormulas) {
          throw new Error(
            'PropertyNotLoaded: formulas was read without load("formulas") — '
            + 'on a real Excel host this throws, which is why a values-only '
            + 'probe cannot see a formula at all.',
          );
        }
        return grid('=IF(B1>0,B1,"")');
      },
      set formulas(_v: unknown) { wrote = true; },
      numberFormat: [],
      format: { font: {}, fill: {}, autofitColumns: () => {} },
      conditionalFormats: { add: () => ({ iconSetOrNullObject: {} }) },
      load: (props?: string | string[]) => {
        const asked = Array.isArray(props) ? props.join(',') : (props ?? '');
        if (asked.includes('formulas')) loadedFormulas = true;
      },
      getCell: () => ({ values: [['']] }),
      getHeaderRowRange: () => ({ format: { font: {} } }),
    };
  }

  beforeEach(() => {
    prompts = [];
    confirmAnswer = false;   // the user DECLINES -> nothing may be written
    wrote = false;
    currentSelection = { address: 'Sheet1!K6', rowIndex: 5, columnIndex: 10 };
    invalidateMetadataCache();
    const sheet = {
      getRangeByIndexes: (row: number, col: number, rows = 1, cols = 1) => formulaOnlyRange(row, col, rows, cols),
      getRange: () => formulaOnlyRange(currentSelection.rowIndex, currentSelection.columnIndex, 1, 1),
      tables: { items: [], load: () => {}, add: () => ({ style: '', getHeaderRowRange: () => ({ format: { font: {} } }) }) },
    };
    vi.stubGlobal('Excel', {
      run: async (cb: (ctx: unknown) => Promise<unknown>) => cb({
        workbook: {
          worksheets: { getActiveWorksheet: () => sheet, getItem: () => sheet },
          getSelectedRange: () => ({
            get address() { return currentSelection.address; },
            get rowIndex() { return currentSelection.rowIndex; },
            get columnIndex() { return currentSelection.columnIndex; },
            values: [['']],
            formulas: [['']],
            load: () => {},
          }),
          names: { items: [], load: () => {}, add: () => ({ comment: '' }), getItemOrNullObject: () => ({ delete: () => {} }) },
        },
        sync: async () => {},
      }),
      ConditionalFormatType: { iconSet: 'iconSet' },
      IconSet: { threeTrafficLights1: 'threeTrafficLights1' },
      ConditionalFormatIconRuleType: { number: 'number' },
      ConditionalIconCriterionOperator: { greaterThanOrEqual: 'gte' },
    });
  });

  const confirm = async (msg: string): Promise<boolean> => {
    prompts.push(msg);
    return confirmAnswer;
  };

  it('insertNamedSetAsFormulas asks before overwriting, and writes nothing when refused', async () => {
    const { result } = renderHook(() => useExcel(confirm));
    const addr = await result.current.insertNamedSetAsFormulas(
      { id: 'ns', name: 'top_products', display_name: 'Top Products', expression: '[Product].[Name].Members' },
      'Tessallite',
      3,
    );
    // Pre-fix: `values` alone read back all-"" -> hasContent false -> no prompt
    // and the formulas were destroyed.
    expect(prompts).toHaveLength(1);
    expect(wrote).toBe(false);
    expect(addr).toBeNull();
  });

  it('insertKpiFormulas asks before overwriting, and writes nothing when refused', async () => {
    const { result } = renderHook(() => useExcel(confirm));
    const addr = await result.current.insertKpiFormulas(
      { id: 'k', name: 'kpi_margin', display_name: 'Margin' },
      'revenue', null, 'Tessallite',
    );
    expect(prompts).toHaveLength(1);
    expect(wrote).toBe(false);
    expect(addr).toBeNull();
  });

  it('insertKpiFullRow asks before overwriting, and writes nothing when refused', async () => {
    const { result } = renderHook(() => useExcel(confirm));
    const addr = await result.current.insertKpiFullRow(
      { id: 'k', name: 'kpi_margin', display_name: 'Margin' },
      'revenue', null, 'Tessallite', 250,
    );
    expect(prompts).toHaveLength(1);
    expect(wrote).toBe(false);
    expect(addr).toBeNull();
  });

  it('insertKpiScorecard asks before overwriting, and writes nothing when refused', async () => {
    const { result } = renderHook(() => useExcel(confirm));
    const addr = await result.current.insertKpiScorecard(
      [{ id: 'k', name: 'kpi_margin', display_name: 'Margin', value: 1, goal: 2, status: 1, trend: 0 }],
      'Tessallite',
    );
    expect(prompts).toHaveLength(1);
    expect(wrote).toBe(false);
    expect(addr).toBeNull();
  });

  it('with consent, the same writers proceed — the guard asks, it does not block', async () => {
    confirmAnswer = true;
    const { result } = renderHook(() => useExcel(confirm));
    const addr = await result.current.insertKpiFullRow(
      { id: 'k', name: 'kpi_margin', display_name: 'Margin' },
      'revenue', null, 'Tessallite', 250,
    );
    expect(prompts).toHaveLength(1);
    expect(wrote).toBe(true);
    expect(addr).toBeTruthy();
  });
});
