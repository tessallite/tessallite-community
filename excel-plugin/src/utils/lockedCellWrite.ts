/**
 * Bug-7397 R12-1: THE shared entry point for every first-party cell write that
 * is not the result-table insert.
 *
 * WHY THIS MODULE EXISTS. R6-R11 built and proved a spatial block-lock
 * (workbookMetadata.cellBlockKeys / withTableLocksKeys): lock keys derive from
 * the CELLS an operation touches, so two overlapping rectangles necessarily
 * share a key and are mutually excluded by construction. Two independent
 * adversarial probes confirmed the mechanism. But exclusion only binds
 * PARTICIPANTS, and only two producers participated -- the table insert and the
 * table refresh. Every other first-party cell writer (formula insert, literal
 * insert, named-set expansion, the KPI writers, the scorecard) opened its own
 * `Excel.run` and wrote cells with no lock at all. The external gate reproduced
 * the consequence live: a formula insert completed and overwrote a cell WHILE a
 * covering refresh lock was held -- the same wrong-numbers class as the headline
 * bug, via a different producer.
 *
 * The structural fix is not another guard; it is to make participation the
 * default. Every cell writer routes through `withPinnedCellWrite`, which:
 *
 *   1. resolves its write target from ONE host sample (`resolveWriteTarget`) --
 *      pinning, so the location that gets LOCKED is the location that gets
 *      WRITTEN. Re-reading the selection inside the write would reintroduce the
 *      two-sample TOCTOU R6 already closed for the insert path;
 *   2. derives the block keys covering the exact rectangle it will write
 *      (`blockKeysForRect`) and acquires them (`withTableLocksKeys`) -- reusing
 *      the proven primitives, never reimplementing locking;
 *   3. runs the write against the PINNED sheet/coordinates, so it cannot drift
 *      onto cells outside the blocks it holds.
 *
 * NOT ROUTED THROUGH HERE, deliberately: `insertChart` and `insertLocalPivot`
 * write their data into a worksheet they CREATE in the same operation. A
 * brand-new uniquely-named sheet has no existing content and no other operation
 * can hold blocks on it, so there is nothing to exclude. (`insertChart`'s
 * existing-range branch adds only a chart object; it writes no cells.)
 */

import {
  blockKeysForRect,
  withTableLocksKeys,
  LockAcquireTimeoutError,
  type TargetRect,
} from './workbookMetadata';

/** A write location resolved from ONE host sample and never re-derived. */
export interface PinnedWriteTarget {
  /** Raw (unquoted) sheet name, as `worksheets.getItem()` expects. */
  sheetName: string;
  startRow: number;
  startCol: number;
}

/**
 * Outcome of a locked cell write. `busy` means the blocks covering the target
 * were held past the acquisition deadline: NOTHING was written, and the caller
 * must tell the user so (silence reads as a broken feature).
 */
export type PinnedWriteOutcome<T> =
  | { ok: true; value: T }
  | { ok: false; reason: 'busy' };

/** Everything a locked write body needs; all coordinates are pinned. */
export interface PinnedWriteContext {
  context: Excel.RequestContext;
  sheet: Excel.Worksheet;
  target: PinnedWriteTarget;
}

/**
 * Resolve the pinned write target in ONE host round trip.
 *
 * `targetCell` (an explicit A1 reference from the caller, e.g. the cube-formula
 * wizard) is resolved on the active worksheet exactly as the pre-lock code did;
 * without it the current selection is used. Either way the SHEET NAME is read
 * from the resolved range's own address, so a concurrent `sheets.add().activate()`
 * cannot redirect the later write to a different sheet than the one whose
 * blocks are locked.
 *
 * Returns null when the host read fails or the address carries no sheet
 * qualifier (`getItem('')` would throw) -- the caller then FAILS CLOSED rather
 * than writing to an undetermined location.
 */
export async function resolveWriteTarget(targetCell?: string): Promise<PinnedWriteTarget | null> {
  if (typeof Excel === 'undefined') return null;
  try {
    return await Excel.run(async (context) => {
      const range = targetCell
        ? context.workbook.worksheets.getActiveWorksheet().getRange(targetCell)
        : context.workbook.getSelectedRange();
      range.load(['address', 'rowIndex', 'columnIndex']);
      await context.sync();

      const address = String(range.address || '');
      const bang = address.lastIndexOf('!');
      // Unquote a spaced/punctuated sheet name ('My Sheet'!A1 -> My Sheet) so it
      // matches the raw name worksheets.getItem() expects.
      const sheetName = bang >= 0
        ? address.slice(0, bang).replace(/^'(.*)'$/, '$1').replace(/''/g, "'")
        : '';
      if (!sheetName) return null;
      return { sheetName, startRow: range.rowIndex, startCol: range.columnIndex };
    });
  } catch {
    return null;
  }
}

/**
 * Run `body` holding the block locks covering the `rowCount` x `colCount`
 * rectangle anchored at `target`.
 *
 * The body receives the pinned sheet object and coordinates: it must address
 * cells with `sheet.getRangeByIndexes(target.startRow + dr, target.startCol + dc, ...)`
 * and must NOT re-read the selection or the active worksheet, or the write can
 * land outside the held blocks.
 *
 * `size` must cover EVERY cell the body touches (including any confirm-probe
 * range), because a cell outside the rectangle is a cell outside the lock.
 */
export async function withPinnedCellWrite<T>(
  target: PinnedWriteTarget,
  size: { rowCount: number; colCount: number },
  body: (ctx: PinnedWriteContext) => Promise<T>,
  options?: { timeoutMs?: number },
): Promise<PinnedWriteOutcome<T>> {
  const rect: TargetRect = {
    startRow: target.startRow,
    startCol: target.startCol,
    rowCount: size.rowCount,
    colCount: size.colCount,
  };
  const blockKeys = blockKeysForRect(target.sheetName, rect);
  try {
    const value = await withTableLocksKeys(
      blockKeys,
      () => Excel.run(async (context) => {
        const sheet = context.workbook.worksheets.getItem(target.sheetName);
        return body({ context, sheet, target });
      }),
      options,
    );
    return { ok: true, value };
  } catch (err) {
    if (err instanceof LockAcquireTimeoutError) return { ok: false, reason: 'busy' };
    throw err;
  }
}
