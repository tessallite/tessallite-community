/**
 * Bug-7397 R12 — MACHINE-CHECK the cell-write contract, on the typed AST.
 *
 * The block lock only excludes PARTICIPANTS. R6-R11 proved the mechanism; R12
 * extended it to every first-party writer. Enforcing participation is where
 * this lane kept failing, three review rounds running:
 *
 *   round 1  a raw unlocked `Excel.run` cell write sat in ReportBuilder.tsx
 *            while a docstring claimed "every cell writer participates" and
 *            783 tests were green. Guard added.
 *   round 2  a brand-new unlocked writer walked past that guard, because it
 *            auto-sanctioned every line in `useExcel.ts` starting
 *            `sheet.getRangeByIndexes(`. Guard rewritten with a line regex plus
 *            a hand-rolled paren matcher.
 *   round 3  SIX more shapes walked past that: an unlocked `range.clear()`
 *            wiping 10,000 cells (no destructive-op vocabulary), a `.values`
 *            assignment split across physical lines (the scan matched one
 *            trimmed LINE, not a statement), a COMMENT containing
 *            `withPinnedCellWrite(` that manufactured a lock span swallowing
 *            every write beneath it, an allowlisted statement text reused
 *            verbatim in a DIFFERENT function of the same file, a static
 *            `range['values'] =` bracket access, and `table.resize()`.
 *
 * Every one of those is a defect of TEXT MATCHING, not of the property being
 * checked. So this guard no longer matches text. It builds a real TypeScript
 * Program (the compiler is already a devDependency, used by `tsc`) and works on
 * the typed syntax tree:
 *
 *   - a cell mutation is an AST node whose RECEIVER TYPE is an Office.js object:
 *     an assignment to `.values` / `.formulas` / ... (or `['values']`), the
 *     ACQUISITION of a range-mutating member, or a reflective write onto one.
 *     Multi-line, reformatted and bracket-access forms are the same node; a
 *     `Set.add` or a `Map.clear` is excluded by its TYPE, not by a name
 *     heuristic.
 *   - lock scope is the BODY of the function argument passed to
 *     `withPinnedCellWrite` / `withTableLocksKeys`, resolved from the real
 *     CallExpression. A comment or string can no longer open or close a span,
 *     and "inside the lock's callback" is distinct from "anywhere in its
 *     argument list".
 *   - a sanctioned exception is keyed by file + ENCLOSING FUNCTION + statement,
 *     so a justification granted to one call site cannot silently cover an
 *     identical line elsewhere.
 *
 * Type resolution is FAIL-CLOSED: an `any` or unresolvable receiver counts as
 * Office. A guard that skipped what it could not type would be the same
 * "assume it is fine" posture that let three rounds of writers through.
 *
 * R8 -- the FIFTH external gate found two more escapes, and both were the same
 * mistake in different clothes: a rule stated as an open list of accepted
 * SYNTAX rather than a closed judgement about the ACT.
 *
 *   gate 5 F-1  mutation detection fired only on a literal dot-access callee,
 *               so `r['clear']()`, `r.clear.call(r)`, `r.clear.bind(r)`,
 *               `const { clear } = r`, `arr.forEach(r.clear)` and a plain
 *               alias all scanned clean. FIX: stop looking at the invocation.
 *               A mutation begins when a mutating MEMBER is taken off an
 *               Office object; every one of those six shapes performs that one
 *               act, so one rule covers all six -- and the seventh nobody has
 *               thought of yet.
 *   gate 5 F-2  `runsInPlace` treated a callback passed to ANY call as running
 *               synchronously, so a write inside `setTimeout(..., 0)` nested in
 *               a lock callback counted as protected while actually running
 *               after release. FIX: invert it into a CLOSED allowlist of callee
 *               shapes whose timing is knowable (array iteration on a real
 *               array, an awaited `Excel.run`, a joined IIFE, a nested lock).
 *               `setTimeout`, `.then`, `queueMicrotask`,
 *               `requestAnimationFrame`, `addEventListener` and every callee
 *               that does not exist yet now fail closed as "runs later". (R10
 *               extended that judgement to a bare method REFERENCE handed to
 *               the same callees; R8 applied it only to closures.)
 *
 * R9 -- the internal review of R8 found five more, and their lesson is that
 * inverting a rule is not finished until the ACCEPTING side is audited too:
 * a closed allowlist whose entries carry open assumptions is not fail-closed.
 *
 *   R9-1  the array-iteration entry assumed the callback completes. An `async`
 *         callback reaches its first `await` and no further, and `forEach`
 *         never joins the promise -- so `rows.forEach(async r => { await
 *         ctx.sync(); range.values = v; })` deferred past the lock. Accepted
 *         now only when the callback does not suspend, or the iteration is
 *         joined through `await Promise.all(...)`.
 *   R9-2  `innermostFunction` did not treat accessors, constructors or class
 *         static blocks as function boundaries, so a `get late() { ... }`
 *         declared in a lock body had no boundary to judge at all.
 *   R9-3  rule 1 enumerated assignment OPERATORS but not assignment TARGET
 *         forms: `[r.values] = rows`, `({ v: r.values } = o)` and
 *         `for (r.values of rows)` are the same store with the `=` moved.
 *   R9-4  `isArrayLikeReceiver` accepted `any`, leaving the module fail-CLOSED
 *         on the type axis and fail-OPEN on the timing axis. They now agree.
 *   R9-5  the dual of R8's own inversion: keying detection on the ACQUISITION
 *         means the guard asks "was the acquisition locked", which sanctioned a
 *         method reference captured inside the critical section and invoked
 *         after release. `escapesLockBody` closes the two escape routes
 *         (returned out of the callback, or assigned to a binding declared
 *         outside it).
 *
 * R10 -- the second internal review round found six more, and every one was an
 * ACCEPTING side that had not been audited to the depth of the rule it served.
 * No new family appeared, and the fixes add no new syntax cases: they mostly
 * make the sites that were not consulting `runsInPlace` / `isRealLockHelper`
 * consult them.
 *
 *   R10-1  the acquisition rule asked only "was the acquisition inside a lock",
 *          so handing the acquired method straight to `setTimeout` / `.then` /
 *          an event registration was the D-1..D-6 deferral with the closure
 *          removed. `escapesLockBody` now asks `runsInPlace` about the callee.
 *   R10-2  the escaping-acquisition walk followed the parent chain only, so one
 *          intermediate `const h = ...` (or an object/array wrapper around the
 *          returned value) broke it. `aliasEscapes` follows the binding.
 *   R10-3  `runsInPlace` accepted a lock entry point by NAME -- round 4 S-H
 *          reintroduced on the second axis. It now resolves the helper, and
 *          requires the nested lock's own promise to be joined.
 *   R10-4  "returned" was taken as "joined". `rows.map(row => Excel.run(...))`
 *          returns each host batch and joins none of them; the writes land
 *          after release. A return now counts only when what it returns to is
 *          itself joined.
 *   R10-5  instance property initialisers were missing from the function
 *          boundaries R9-2 added (a static initialiser genuinely runs in place
 *          and is deliberately excluded).
 *   R10-6  four documentation claims overstated coverage; corrected below.
 *
 * R11 -- the third review round found the pattern behind all of it, and it is
 * the only conclusion in this header worth remembering. Every fail-closed
 * component in this file had been INVERTED into a closed rule -- `runsInPlace`
 * (R8), `isArrayLikeReceiver` (R9-4), `isMutatingMember` on a cell-owning type
 * (R7). `escapesLockBody` was the one that never was: it enumerated the ways a
 * value ESCAPES and defaulted to "did not" when it ran out of cases. Four of
 * round 3's findings were that single defect, and R10's own carrier list had
 * silently RE-OPENED an escape R9 reported (`g = fallback ?? r.clear.bind(r)`)
 * -- a fix opening a hole, which mutation testing structurally cannot catch,
 * because a mutation proves only that reverting the fix goes red.
 *
 * So R11 inverted it. The value ESCAPES unless it is provably CONSUMED, and
 * consumption is a closed allowlist. That collapsed four findings into one rule
 * and moved the last fail-open component in a file whose entire thesis is
 * fail-closed. Also this round: Office COMMON-API writers
 * (`setSelectedDataAsync`, `Binding.setDataAsync`, ...) were invisible to BOTH
 * assertions -- they own cells but are not `CELL_OWNING_TYPES`, and they open
 * no `Excel.run`; the escape check was wired into rule 3 only, so a
 * destructured or reflected acquisition never reached it; `aliasEscapes`
 * recursed without bound on a cyclic binding graph; and `isRealLockHelper` read
 * "imported, therefore real", which moves round 4 S-H's shadow one module out.
 *
 * R12 -- review round 4 ran the CURRENT detector and the two previous revisions
 * over one fixture corpus side by side, and that differential is the only thing
 * that could have found what it found: R11's own inversion had re-opened four
 * escapes R10 reported. `consumesInPlace` accepted "invoked here" without
 * establishing that "here" was still HELD, so an alias invoked from a
 * `setTimeout` arrow inside the lock read as consumed -- the guard's verdict
 * turning on whether the author wrote `registerUndo(restore)` or
 * `registerUndo(() => restore())`. Held-ness now lives in one function
 * (`heldWithin`) applied on both axes. Also this round: the shorthand
 * destructuring-assignment branch resolved its symbol two different ways and so
 * matched no reference (its pinned fixture had been passing on an unrelated
 * short-circuit); rule 4 never received rule 3's callability requirement, so
 * `const { values } = range` -- an ordinary Office.js READ -- reported as a
 * mutation; a boolean test on an acquisition was not modelled as consumption;
 * and the lock-helper import is now RESOLVED rather than matched by basename.
 *
 * THE STANDING LESSON, because it cost four rounds: mutation testing proves a
 * fix cannot be reverted silently. It cannot prove a fix did not open a hole.
 * Any future change to `escapesLockBody` / `consumesInPlace` / `aliasEscapes` /
 * `runsInPlace` must ALSO be validated by running the previous revision's
 * detector over this file's fixture corpus and confirming nothing that was
 * reported stopped being reported.
 *
 * All three inversions move the fail-closed boundary up one level, which is the
 * same move R7 made on the METHOD-NAME axis (an unclassified method on a
 * cell-owning type is a mutation). What is left after them is not another list
 * of shapes: it is the residue below.
 *
 * KNOWN LIMIT, stated rather than papered over: a static parse cannot prove a
 * lock is HELD at runtime -- that is what the per-writer deferral tests in
 * `lockedCellWrite.test.tsx` do -- nor see a property name computed at runtime
 * (`range[someVar] = ...` where `someVar` has no literal type). The `Excel.run`
 * module allowlist NARROWS that residue -- it is where a host batch may be
 * OPENED -- but stated as a bound it is too generous: a module that merely
 * RECEIVES an Office object can write cells without opening a batch of its own
 * (`utils/excelCharts.ts` takes an `Excel.Worksheet` and is not in
 * EXCEL_RUN_MODULES), and the Office COMMON API writes cells without a batch at
 * all. Both are covered by the MUTATION rules; neither is covered by the
 * `Excel.run` bound. Closing it COMPLETELY needs a runtime-enforced invariant (the
 * RequestContext handed out by the lock helper as the only capability that can
 * mutate cells), which cannot be introduced without a native Excel host to
 * verify it against -- see Bug-8346, the architecture intake filed with this
 * round.
 *
 * Every shape that has actually got past a previous round is pinned below.
 * Most live in the `ESCAPED_BEFORE`, `ROUND_4_ESCAPES`, `R8_INDIRECTION`,
 * `R8_DEFERRED`, `R9_DEFERRED_ITERATION`, `R9_ACCESSORS`, `R9_TARGETS` and
 * `R10_DEFERRED_REFERENCE` tables; the round 5 type-erasure escapes (S-A..S-D)
 * and the R9/R10/R11 escaping-acquisition, aliased-reflection, Common-API and
 * lock-helper-shadow cases are standalone `it` blocks, because each needs its
 * own assertion rather than a shared one. The tables are NOT a complete
 * inventory on their own -- read the whole second `describe`.
 *
 * Read that set as a REGRESSION FLOOR, not a coverage ceiling: it proves no
 * shape that has escaped before can escape again. It says nothing about the
 * next one, and five review rounds running have produced one.
 *
 * A NOTE ON NUMBERING, since two schemes appear here: "R12" in the describe
 * titles is the IMPLEMENTATION phase of Bug-7397 (the `R12-n` markers in the
 * source modules -- currently R12-1..R12-5 plus an `R12-R4-n` sub-series);
 * "R7..R13" in the fixture names are external-gate and review ROUNDS of this
 * lane. They are not the same counter and neither is stale.
 */
import { describe, it, expect } from 'vitest';
import { readdirSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';

const SRC = resolve(dirname(fileURLToPath(import.meta.url)), '..');

/** Windows path -> forward slashes, so host callbacks compare consistently. */
function toPosix(p: string): string {
  return p.split(sep).join('/');
}

/**
 * Calls whose function ARGUMENT's body is a held block-lock critical section.
 * Resolved by SYMBOL, not by name: round 4 opened a fake span with a locally
 * declared function that merely happened to be called `withTableLocksKeys`.
 */
const LOCK_ENTRY_POINTS = new Set(['withPinnedCellWrite', 'withTableLocksKeys']);

/** Properties whose assignment puts data into worksheet cells. */
const DATA_PROPS = new Set([
  'values', 'valuesAsJson', 'formulas', 'formulasLocal', 'formulasR1C1',
  'numberFormat', 'numberFormatLocal', 'hyperlink',
]);

/**
 * Methods that destroy, move or reshape worksheet content. An unlocked
 * `clear()` loses a user's data exactly as an unlocked `values =` does --
 * round 3 wiped 10,000 cells through the gap where this set used to be missing,
 * and round 4 moved 26,000 through `moveTo`.
 *
 * This set is not the last line of defence: on a CELL-OWNING receiver type the
 * detector is FAIL-CLOSED, so a method in NEITHER this set nor
 * READ_ONLY_METHODS is reported. An office-js upgrade that adds a new mutating
 * API therefore fails this test until someone classifies it, instead of
 * shipping silently -- which is what stops the "names someone remembered"
 * failure mode that escaped four review rounds.
 */
const MUTATING_METHODS = new Set([
  'clear', 'copyFrom', 'insert', 'merge', 'unmerge', 'resize', 'delete', 'deleteRows',
  'add', 'moveTo', 'replaceAll', 'autoFill', 'removeDuplicates', 'convertToRange',
  'setDirty', 'flashFill', 'ungroup', 'group', 'addNamedItem',
  'addNamedItemFormulaLocal', 'clearAllConditionalFormats', 'clearFilters',
  'reapplyFilters', 'applyValuesFilter', 'convertDataTypeToText', 'convertToLinkedDataType',
  'deleteRow', 'deleteColumn', 'insertRow', 'insertColumn', 'setCellProperties',
  // Office COMMON API writers (`Office.Document` / `Office.Binding`). These own
  // worksheet cells but are not `CELL_OWNING_TYPES`, so the fail-closed
  // unclassified-method rule never reached them -- and they open no `Excel.run`,
  // so EXCEL_RUN_MODULES did not bound them either. `setSelectedDataAsync` is
  // the canonical "write at the user's selection" call in an Office add-in, and
  // the plugin already uses the Common API for `document.settings`, so a
  // component could write user cells tomorrow with no assertion firing.
  'setSelectedDataAsync', 'setDataAsync', 'addRowsAsync', 'addColumnsAsync',
  'deleteAllDataValuesAsync', 'clearFormatsAsync', 'setFormatsAsync',
]);

/**
 * Office methods on cell-owning types that are READ-ONLY (navigation, loading,
 * lookup).
 *
 * This list does NOT have to be complete, and completeness is deliberately not
 * asserted against the pinned typings: on a cell-owning receiver anything
 * absent from BOTH this set and MUTATING_METHODS is treated as a POSSIBLE
 * mutation, so an office-js upgrade that adds a mutating API fails this test
 * until someone classifies it. Hand-transcribing ~200 declared method names
 * into these two sets would REPLACE that fail-closed default with a table whose
 * every misclassification silently reopens the guard, which is strictly worse.
 * (An earlier revision of this docstring promised a classification assertion
 * "at the end of this file". No such assertion existed; the claim is removed
 * rather than left to read as coverage that was never there.)
 */
const READ_ONLY_METHODS = new Set([
  'load', 'toJSON', 'track', 'untrack', 'getRange', 'getRangeByIndexes', 'getCell',
  'getColumn', 'getRow', 'getColumnsAfter', 'getColumnsBefore', 'getRowsAbove',
  'getRowsBelow', 'getEntireColumn', 'getEntireRow', 'getIntersection',
  'getIntersectionOrNullObject', 'getBoundingRect', 'getOffsetRange', 'getResizedRange',
  'getLastCell', 'getLastColumn', 'getLastRow', 'getUsedRange', 'getUsedRangeOrNullObject',
  'getAbsoluteResizedRange', 'getSurroundingRegion', 'getSpecialCells',
  'getSpecialCellsOrNullObject', 'getVisibleView', 'getCellProperties', 'getDirectPrecedents',
  'getPrecedents', 'getDependents', 'getExtendedRange', 'getMergedAreas',
  'getMergedAreasOrNullObject', 'getSpillingToRange', 'getSpillParent', 'getSpillingToRangeOrNullObject',
  'getSpillParentOrNullObject', 'getTables', 'getPivotTables', 'getImage', 'getDataBodyRange',
  'getHeaderRowRange', 'getTotalRowRange', 'getRangeBetweenHeaderAndTotal', 'getNext',
  'getPrevious', 'getNextOrNullObject', 'getPreviousOrNullObject', 'getItem',
  'getItemOrNullObject', 'getItemAt', 'getCount', 'getFirst', 'getFirstOrNullObject',
  'getActiveCell', 'getActiveWorksheet', 'getSelectedRange', 'getSelectedRanges',
  'getNumberFormatCategories', 'getRangeOrNullObject', 'getDataHierarchy', 'getFilterAxis',
  'find', 'findOrNullObject', 'findAll', 'findAllOrNullObject', 'search', 'searchOrNullObject',
  'select', 'activate', 'calculate', 'copyFromOrNullObject', 'showCard', 'refreshAll',
  'getDirectDependents', 'getCategory', 'getPivotTable', 'getWorksheet',
]);

/** Modules permitted to open an Excel.run at all. */
const EXCEL_RUN_MODULES = new Set([
  'hooks/useExcel.ts',
  'utils/lockedCellWrite.ts',
  'utils/tableRefresh.ts',
  'utils/workbookMetadata.ts',
  'utils/officeSpike.ts',
  'utils/cellContext.ts',
  'functions.ts',
]);

/**
 * Sanctioned Office mutations OUTSIDE a lock callback, keyed
 * `<file>::<enclosing function>::<statement>`. Acceptable reasons only:
 *   (a) the site is the body of a helper whose every caller holds the lock,
 *   (b) it targets a worksheet the same operation just created, which no other
 *       operation can hold blocks on,
 *   (c) it targets a disposable sheet deleted in the same call, or
 *   (d) it mutates no worksheet CELLS at all (a named item, a sheet/table/chart
 *       object).
 * "It is only one cell" and "it is unlikely to collide" are NOT reasons.
 */
const ALLOWED: Record<string, string> = {
  // (a) helper bodies whose every caller holds the lock ----------------------
  'utils/tableRefresh.ts::rewriteTableBody::newBody.values = rows':
    'sole caller refreshTable runs it inside withTableLocksKeys, after an under-lock bounds check',
  'utils/tableRefresh.ts::rewriteTableBody::footerRange.getCell(0, 0).values = [[restampProvenanceFooter(existingFooter, footerTimestamp)]]':
    'footer move, same critical section and bounds check',
  'utils/tableRefresh.ts::rewriteTableBody::table.resize(newTableRange)':
    'in-place table resize, same critical section and bounds check',
  'utils/tableRefresh.ts::rewriteTableBody::excess.clear(Excel.ClearApplyTo.contents)':
    'clears rows a shrinking table no longer covers, inside the declared rectangle',
  'utils/tableRefresh.ts::rewriteTableBody::sheet.getRangeByIndexes(oldFooterRow, startCol, 1, colCount).clear(Excel.ClearApplyTo.all)':
    'clears the footer row a shrinking table vacated, inside the declared rectangle',
  'hooks/useExcel.ts::insertProvenanceFooter::footerRange.getCell(0, 0).values = [[joinProvenanceFooter(parts)]]':
    'sole caller doInsertAndTag runs it inside withTableLocksKeys',
  'utils/officeSpike.ts::insertResultTable::range.values = allData':
    'sole caller doInsertAndTag runs it inside withTableLocksKeys',
  'utils/officeSpike.ts::insertResultTable::dataRange.numberFormat = Array.from({ length: rows.length }, () => [formatToExcelFormat(formatToken)])':
    'same critical section as above',
  'utils/officeSpike.ts::insertResultTable::sheet.tables.add(tableRange, true)':
    'creates the table OBJECT over cells the same critical section just wrote',

  // (b) targets a worksheet the same operation just created ------------------
  'hooks/useExcel.ts::insertChart::dataRange.values = [headers, ...rows]':
    'brand-new uniquely-named "Chart Data" sheet',
  'hooks/useExcel.ts::insertLocalPivot::range.values = [headers, ...rows]':
    'brand-new uniquely-named "Pivot Data" sheet',
  'utils/excelCharts.ts::createChartOnSheet::chartDataRange.values = [chartHeaders, ...chartRows]':
    'sole caller insertChart, always on its own new sheet',
  'hooks/useExcel.ts::insertLocalPivot::table.delete()':
    'rollback of the table this operation created on its own new sheet',
  'hooks/useExcel.ts::insertLocalPivot::pivotSheet.delete()':
    'rollback of a sheet this operation created',
  'hooks/useExcel.ts::insertLocalPivot::dataSheet.delete()':
    'rollback of a sheet this operation created',

  // (c) disposable sheet, deleted in the same call ---------------------------
  'utils/officeSpike.ts::probeInTempSheet::range.values = [[\'TestCol1\', \'TestCol2\'], [\'a\', 1], [\'b\', 2]]':
    'compatibility spike, throwaway hidden sheet deleted in a finally block',
  'utils/officeSpike.ts::probeInTempSheet::sheet.getRangeByIndexes(5, 0, 1, 1).formulas = [[\'=CUBEVALUE("Connection","[Measures].[Test]")\']]':
    'compatibility spike, same throwaway sheet',
  'utils/officeSpike.ts::probeInTempSheet::sheet.tables.add(sheet.getRangeByIndexes(0, 0, 3, 2), true)':
    'compatibility spike, same throwaway sheet',
  'utils/officeSpike.ts::probeInTempSheet::sheet.charts.add(Excel.ChartType.columnClustered, dataRange, Excel.ChartSeriesBy.auto)':
    'compatibility spike, same throwaway sheet',
  'utils/officeSpike.ts::probeInTempSheet::sheet.delete()':
    'removes the throwaway spike sheet',
  'utils/officeSpike.ts::probeInTempSheet::context.workbook.worksheets.add(sheetName)':
    'creates the throwaway spike sheet itself',

  // (d) object-level, not cell content ---------------------------------------
  'utils/officeSpike.ts::insertResultTable::existingTable.convertToRange()':
    'Bug-6736: removes an OVERLAPPING TABLE OBJECT while preserving its cell values; inside the held blocks of doInsertAndTag',
  'hooks/useExcel.ts::createNewSheet::sheets.add(name)':
    'createNewSheet: adds a worksheet; writes no cells',
  'hooks/useExcel.ts::insertChart::sheets.add(name)':
    'insertChart: adds its own "Chart Data" worksheet',
  'hooks/useExcel.ts::insertChart::sheet.tables.add(dataRange, true)':
    'insertChart: table OBJECT over the new sheet it just wrote',
  'hooks/useExcel.ts::insertLocalPivot::sheets.add(dataName)':
    'insertLocalPivot: adds its own "Pivot Data" worksheet',
  'hooks/useExcel.ts::insertLocalPivot::dataSheet.tables.add(range, true)':
    'insertLocalPivot: table OBJECT over the new sheet it just wrote',
  'hooks/useExcel.ts::insertLocalPivot::sheets.add(pivotName)':
    'insertLocalPivot: adds its own pivot worksheet',
  'hooks/useExcel.ts::insertLocalPivot::pivotSheet.pivotTables.add( pivotTableName, qualifiedAddr, pivotRange, )':
    'insertLocalPivot: PivotTable object on the sheet it just created',
  'hooks/useExcel.ts::insertLocalPivot::pivotTable.rowHierarchies.add(hier)':
    'PivotTable field wiring; no cell write',
  'hooks/useExcel.ts::insertLocalPivot::pivotTable.dataHierarchies.add(hier)':
    'PivotTable field wiring; no cell write',
  'hooks/useExcel.ts::insertLocalPivot::pivotTable.columnHierarchies.add(hier)':
    'PivotTable field wiring; no cell write',
  'hooks/useExcel.ts::insertLocalPivot::pivotTable.filterHierarchies.add(hier)':
    'PivotTable field wiring; no cell write',
  'utils/excelCharts.ts::createChartOnSheet::sheet.charts.add(chartType, chartDataRange, Excel.ChartSeriesBy.columns)':
    'chart OBJECT on the sheet insertChart just created',
  'utils/workbookMetadata.ts::setTableMetadataWithinLock::context.workbook.names.getItemOrNullObject(staleName).delete()':
    'deletes a hidden NAMED ITEM (provenance), not cells',
  'utils/workbookMetadata.ts::removeTableMetadataWithinLock::context.workbook.names.getItemOrNullObject(staleName).delete()':
    'deletes a hidden NAMED ITEM (provenance), not cells',
  'utils/workbookMetadata.ts::setTableMetadataWithinLock::context.workbook.names.add(name, sheetRef)':
    'adds a hidden NAMED ITEM (provenance), not cells',
  'utils/workbookMetadata.ts::setTableMetadataWithinLock::context.workbook.names.add( `${METADATA_PREFIX}${rangeKey}__table_range`, sheetRef, )':
    'adds a hidden NAMED ITEM (provenance), not cells',
};

/**
 * How many occurrences of an ALLOWED key were actually audited. Default 1.
 * Raising a number here is a deliberate statement that the ADDITIONAL site was
 * reviewed on its own merits -- an identical statement elsewhere in the same
 * function does not inherit the first one's justification for free.
 */
const ALLOWED_OCCURRENCES: Record<string, number> = {};

// ---------------------------------------------------------------------------
// Typed AST detector
// ---------------------------------------------------------------------------

export interface MutationSite { key: string; file: string; line: number; text: string; fn: string }

/** The lock-callback bodies in `sf`, as AST nodes (not just offsets). */
function lockBodies(sf: ts.SourceFile, checker: ts.TypeChecker): ts.Node[] {
  const bodies: ts.Node[] = [];
  const visit = (node: ts.Node): void => {
    if (ts.isCallExpression(node)) {
      const callee = node.expression;
      const nameNode = ts.isIdentifier(callee) ? callee
        : ts.isPropertyAccessExpression(callee) ? callee.name : null;
      if (nameNode && isLockEntryPoint(checker, nameNode)) {
        for (const arg of node.arguments) {
          if (ts.isArrowFunction(arg) || ts.isFunctionExpression(arg)) bodies.push(arg.body);
        }
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  return bodies;
}

/**
 * The modules that may define a lock entry point.
 *
 * `LOCK_HELPER_FILES` is the real test, matched against the RESOLVED
 * declaration file. `LOCK_HELPER_MODULES` matches the import SPECIFIER and is
 * the fallback for the single-file fixture harnesses, where nothing resolves;
 * it necessarily allows a bare `./workbookMetadata` (the form
 * `lockedCellWrite.ts` and `tableRefresh.ts` really use) and therefore pins only
 * a basename, which is why it is not the primary check.
 */
const LOCK_HELPER_FILES = /(^|\/)src\/utils\/(lockedCellWrite|workbookMetadata)\.tsx?$/;
const LOCK_HELPER_MODULES = /(^|\/)(utils\/)?(lockedCellWrite|workbookMetadata)$/;

/** What an import alias points at, if the program resolved it. */
function aliasTarget(checker: ts.TypeChecker, symbol: ts.Symbol | undefined): ts.Symbol | undefined {
  if (!symbol || !(symbol.flags & ts.SymbolFlags.Alias)) return undefined;
  try {
    return checker.getAliasedSymbol(symbol);
  } catch {
    return undefined; // unresolved in a fixture program
  }
}

/** Files an import alias actually resolves to, if the program resolved it. */
function aliasedDeclarationFiles(checker: ts.TypeChecker, symbol: ts.Symbol | undefined): string[] {
  return (aliasTarget(checker, symbol)?.getDeclarations() ?? [])
    .map(d => toPosix(d.getSourceFile().fileName));
}

/** Is this declared in one of the modules allowed to define a lock helper? */
function declaredInLockModule(decls: readonly ts.Declaration[] | undefined): boolean {
  return (decls ?? []).some(d => LOCK_HELPER_FILES.test(toPosix(d.getSourceFile().fileName)));
}

/**
 * Is this callee a lock entry point?
 *
 * Normally the local NAME is the entry point's own, and `isRealLockHelper`
 * decides whether it is the genuine article. A RENAMED import
 * (`import { withTableLocksKeys as withLocks }`) is the real helper under a
 * different local name: gating on the name alone reported every write inside
 * it -- fail-closed, but a false positive whose only "remedy" would be to
 * un-rename the import, which is not a remedy at all.
 */
function isLockEntryPoint(checker: ts.TypeChecker, nameNode: ts.Identifier): boolean {
  if (LOCK_ENTRY_POINTS.has(nameNode.text)) return isRealLockHelper(checker, nameNode);
  const symbol = checker.getSymbolAtLocation(nameNode);
  const target = aliasTarget(checker, symbol);
  if (!target || !LOCK_ENTRY_POINTS.has(target.getName())) return false;
  return declaredInLockModule(target.getDeclarations());
}

/**
 * Is this identifier the REAL lock helper, or something that merely shares its
 * name? Round 4 opened a fake span with a locally declared
 * `function withTableLocksKeys(k, f) { return f(); }`.
 *
 * "Imported, therefore real" is not enough: it moves the shadow one module out.
 * Nor is "imported by NAME": `import * as L from './notTheLock';
 * L.withTableLocksKeys(...)` and `locks.withTableLocksKeys(...)` resolve
 * STRAIGHT to the target declaration, so no ImportSpecifier appears among the
 * declarations at all -- the whole import branch was skipped and the fallthrough
 * answered "declared in some other file, therefore real". Whichever syntax is
 * used, the question is the same one: does the helper's DECLARATION live in a
 * lock-helper file?
 */
function isRealLockHelper(checker: ts.TypeChecker, nameNode: ts.Identifier): boolean {
  const symbol = checker.getSymbolAtLocation(nameNode);
  const decls = symbol?.getDeclarations();
  if (!decls || decls.length === 0) return true; // unresolved (fixture) -> trust the name
  // Resolved through an import alias: follow it to the declaration.
  const files = aliasedDeclarationFiles(checker, symbol);
  if (files.length > 0) return files.some(f => LOCK_HELPER_FILES.test(f));
  const imported = decls.filter(d => ts.isImportSpecifier(d) || ts.isImportClause(d) || ts.isNamespaceImport(d));
  // An import the program could not resolve (the single-file fixture harnesses)
  // -> fall back to the specifier text, which pins only a basename.
  if (imported.length > 0) return imported.some(d => importsFromLockModule(d));
  // Resolved straight to a DECLARATION: a namespace-import member access, a
  // method on an imported object, or a local shadow. Real only if it is
  // declared in a lock-helper file -- which also makes the helper's own
  // self-call inside `workbookMetadata.ts` open a span, as it should.
  return declaredInLockModule(decls);
}

/** Does this import declaration's module specifier name a lock-helper module? */
function importsFromLockModule(decl: ts.Declaration): boolean {
  for (let n: ts.Node | undefined = decl; n; n = n.parent) {
    if (ts.isImportDeclaration(n) && ts.isStringLiteralLike(n.moduleSpecifier)) {
      return LOCK_HELPER_MODULES.test(n.moduleSpecifier.text.replace(/\.[cm]?[jt]sx?$/, ''));
    }
  }
  return false;
}

/**
 * Array methods that drive their callback to completion before returning. The
 * receiver must really be array-like -- a `.forEach` on some other object
 * proves nothing about when the callback runs.
 */
const IN_PLACE_ARRAY_METHODS = new Set([
  'forEach', 'map', 'filter', 'reduce', 'reduceRight', 'some', 'every', 'flatMap',
  'find', 'findIndex', 'findLast', 'findLastIndex', 'sort',
]);

/** Walk UP through parentheses / casts, returning the outermost wrapper. */
function unwrapUp(node: ts.Node): ts.Node {
  let cur = node;
  for (;;) {
    const p = cur.parent;
    if (p && (ts.isParenthesizedExpression(p) || ts.isAsExpression(p) || ts.isNonNullExpression(p)
        || ts.isTypeAssertionExpression(p) || ts.isSatisfiesExpression(p))) {
      cur = p;
      continue;
    }
    return cur;
  }
}

/**
 * Is this call's completion joined by the enclosing (lock-held) flow? A call
 * whose promise nobody waits on can settle after the lock has released.
 *
 * "Returned" is not on its own proof of that: it only moves the question to the
 * caller. `rows.map(row => Excel.run(...))` returns each batch from the map
 * callback and joins none of them, so the writes land after release -- R9-1's
 * defect reached through a NON-suspending callback, which `suspends()` cannot
 * see. A return therefore counts only when the function it returns FROM is
 * itself joined (or is not a callback at all, in which case its own caller is
 * judged separately by this same scan).
 */
function isAwaitedOrReturned(call: ts.Node): boolean {
  const n = unwrapUp(call);
  const p = n.parent;
  if (!p) return false;
  if (ts.isAwaitExpression(p)) return true;
  const enclosing = ts.isReturnStatement(p) ? innermostFunction(p)
    // A concise arrow body is an implicit return: `() => Excel.run(...)`.
    : (ts.isArrowFunction(p) && p.body === n) ? p : null;
  if (!enclosing) return false;
  const owner = enclosing.parent;
  if (!owner || !ts.isCallExpression(owner) || !owner.arguments.some(a => a === enclosing)) return true;
  return isJoinedThroughPromiseAll(owner);
}

/** Does this expression denote the `Excel` namespace, however it is reached? */
function isExcelNamespace(e: ts.Node): boolean {
  let n: ts.Node = e;
  for (;;) {
    if (ts.isParenthesizedExpression(n) || ts.isAsExpression(n) || ts.isNonNullExpression(n)
        || ts.isTypeAssertionExpression(n)) {
      n = n.expression;
      continue;
    }
    break;
  }
  if (ts.isIdentifier(n)) return n.text === 'Excel';
  // `globalThis.Excel` / `window.Excel` / `self.Excel`.
  return ts.isPropertyAccessExpression(n) && n.name.text === 'Excel';
}

/**
 * Is this receiver really an ARRAY, whose iteration contract is what makes the
 * callback's timing knowable?
 *
 * `any` is rejected. R8's first cut accepted it, on the reasoning that the
 * callee-name check had already rejected the deferring shapes -- which is false
 * for any object that merely OWNS a method called `forEach`, reached through an
 * `any`. That is the D-9 case with the type erased, and it left the module
 * fail-CLOSED on the type axis ("an unknown receiver might be a Range") while
 * fail-OPEN on the timing axis ("an unknown receiver might be an array"). The
 * two now agree: unknown means unproven, and unproven means unlocked.
 */
function isArrayLikeReceiver(checker: ts.TypeChecker, node: ts.Node): boolean {
  const t = checker.getNonNullableType(checker.getTypeAtLocation(node));
  if (t.flags & ts.TypeFlags.Any) return false;
  return checker.isArrayLikeType(t);
}

/**
 * Does this function-like SUSPEND?
 *
 * An `async` (or generator) callback runs only as far as its first `await`; the
 * remainder continues on a later microtask. An iteration method drives such a
 * callback to that first suspension point, NOT to completion, and never joins
 * the promise it returns -- so
 * `rows.forEach(async r => { await ctx.sync(); range.values = v; })` lands its
 * write after the lock released. `await ctx.sync()` inside a per-row loop is
 * ordinary code here, so this is a realistic shape, not a contrived one.
 */
function suspends(fn: ts.Node): boolean {
  if (!ts.isArrowFunction(fn) && !ts.isFunctionExpression(fn) && !ts.isFunctionDeclaration(fn)) return false;
  if ((fn as ts.FunctionExpression).asteriskToken) return true;
  return Boolean(ts.canHaveModifiers(fn)
    && ts.getModifiers(fn)?.some(m => m.kind === ts.SyntaxKind.AsyncKeyword));
}

/** `Promise` combinators that wait for EVERY promise handed to them. */
const PROMISE_JOINERS = new Set(['all', 'allSettled']);

/**
 * Is the result of `call` joined by the enclosing flow -- directly, or through
 * `await Promise.all([...])`? Only combinators that wait for every element
 * count; handing the array to an arbitrary function proves nothing.
 *
 * A SPREAD is required to reach the combinator through an array literal.
 * `Promise.all([...rows.map(f)])` puts each promise into the array and joins
 * them; `Promise.all([rows.map(f)])` puts the ARRAY in as a single non-promise
 * element, which `Promise.all` resolves immediately without waiting for
 * anything inside it -- so that form is NOT joined and must not be accepted.
 */
function isJoinedThroughPromiseAll(call: ts.Node): boolean {
  const n = unwrapUp(call);
  if (isAwaitedOrReturned(n)) return true;
  const p = n.parent;
  if (!p) return false;
  if (ts.isSpreadElement(p) && p.parent) return isJoinedThroughPromiseAll(p.parent);
  if (ts.isCallExpression(p) && p.arguments.some(a => a === n)
      && ts.isPropertyAccessExpression(p.expression)
      && PROMISE_JOINERS.has(p.expression.name.text)
      && ts.isIdentifier(p.expression.expression) && p.expression.expression.text === 'Promise') {
    return isJoinedThroughPromiseAll(p);
  }
  return false;
}

/**
 * Is `fn` a callback that certainly runs WHILE the lock is held, rather than a
 * closure that merely happens to be created there?
 *
 * Round 4 escaped by returning an arrow from inside a lock callback and calling
 * it after the lock released -- lexically inside, dynamically outside. Codex
 * gate 5 escaped by DEFERRING one instead: `setTimeout(() => { write }, 0)`
 * nested in a lock callback scanned as protected, because "passed as an
 * argument to any call" was treated as proof of synchronous execution. It is
 * not: `.then`, `queueMicrotask`, `requestAnimationFrame`, `addEventListener`
 * and every emitter registration have that same shape and run AFTER the lock.
 *
 * R8 inverts the rule. This is now a CLOSED allowlist of callee shapes whose
 * timing is knowable, and everything else -- including every callee that has
 * not been invented yet -- fails closed as "runs later, therefore unlocked".
 * A wrong answer in that direction is a visible test failure, not a silent
 * wrong-numbers escape.
 */
function runsInPlace(fn: ts.Node, checker: ts.TypeChecker): boolean {
  const n = unwrapUp(fn);
  const parent = n.parent;
  if (!parent || !ts.isCallExpression(parent)) return false;

  // Immediately-invoked function expression: runs now, but its PROMISE must be
  // joined or an `async` IIFE still finishes after the lock.
  if (parent.expression === n) return isAwaitedOrReturned(parent);
  if (!parent.arguments.some(a => a === n)) return false;

  const callee = parent.expression;
  const calleeName = ts.isIdentifier(callee) ? callee.text
    : ts.isPropertyAccessExpression(callee) ? callee.name.text : null;
  if (!calleeName) return false;

  // A nested lock entry point runs its callback inside the outer lock too --
  // but only the REAL one, and only when its own promise is joined. Matching
  // the NAME here was round 4 S-H reintroduced on the second axis: any local
  // function or object method called `withPinnedCellWrite` certified its
  // callback as running inside the outer critical section.
  const calleeNameNode = ts.isIdentifier(callee) ? callee
    : ts.isPropertyAccessExpression(callee) ? callee.name : null;
  if (calleeNameNode && isLockEntryPoint(checker, calleeNameNode)) {
    return isAwaitedOrReturned(parent);
  }

  if (ts.isPropertyAccessExpression(callee)) {
    // `Excel.run` drives its callback to completion before its promise settles.
    if (calleeName === 'run' && isExcelNamespace(callee.expression)) return isAwaitedOrReturned(parent);
    if (IN_PLACE_ARRAY_METHODS.has(calleeName) && isArrayLikeReceiver(checker, callee.expression)) {
      // Array iteration drives a SYNCHRONOUS callback to completion. A
      // suspending one only reaches its first `await`, unless the iteration's
      // promises are joined (`await Promise.all(rows.map(async ...))`).
      return !suspends(n) || isJoinedThroughPromiseAll(parent);
    }
  }
  return false;
}

/**
 * The innermost function-like node containing `node`, if any.
 *
 * Accessors, constructors, class static blocks and INSTANCE property
 * initialisers are function boundaries too. While they were missing, a
 * `get late() { range.values = ... }` declared inside a lock callback had NO
 * boundary between it and the lock body, so the walk climbed straight to the
 * callback and called the write locked -- even though the body runs whenever
 * the property is read, which is after release. An instance property
 * initialiser is deferred by the same argument (it runs at `new C()`); a STATIC
 * initialiser runs when the class declaration is evaluated, i.e. in place, so
 * treating it as a boundary would only manufacture a false positive.
 */
function innermostFunction(node: ts.Node): ts.Node | null {
  for (let n: ts.Node | undefined = node.parent; n; n = n.parent) {
    if (ts.isArrowFunction(n) || ts.isFunctionExpression(n) || ts.isFunctionDeclaration(n)
        || ts.isMethodDeclaration(n) || ts.isGetAccessorDeclaration(n) || ts.isSetAccessorDeclaration(n)
        || ts.isConstructorDeclaration(n) || ts.isClassStaticBlockDeclaration(n)
        || (ts.isPropertyDeclaration(n) && Boolean(n.initializer)
            && !ts.getModifiers(n)?.some(m => m.kind === ts.SyntaxKind.StaticKeyword))) {
      return n;
    }
  }
  return null;
}

/**
 * Nearest enclosing FUNCTION name. Deliberately never a variable name: round 4
 * smuggled a table over 10,000 user cells because a mutation in a
 * `const table = ...` initialiser was keyed `::table::`, colliding with an
 * allowlist entry granted to a different function that used the same variable
 * name. Walking past the VariableDeclaration to the function that contains it
 * makes each sanction specific to the site that was actually audited.
 */
function enclosingName(node: ts.Node): string {
  let crossedFunction = false;
  for (let n: ts.Node | undefined = node; n; n = n.parent) {
    if (ts.isFunctionDeclaration(n) && n.name) return n.name.text;
    if (ts.isMethodDeclaration(n) && ts.isIdentifier(n.name)) return n.name.text;
    if (ts.isArrowFunction(n) || ts.isFunctionExpression(n)) crossedFunction = true;
    if (ts.isVariableDeclaration(n) && ts.isIdentifier(n.name)) {
      // A variable name is the FUNCTION's name only once a function boundary has
      // been crossed (`const insertChart = useCallback(async () => { ... })`).
      // Reached without crossing one, the mutation is merely this variable's
      // initialiser (`const table = sheet.tables.add(...)`) -- keep walking, or
      // the key becomes `::table::` and collides with every other function in
      // the file that happens to bind a variable called `table`.
      if (crossedFunction) return n.name.text;
    }
  }
  return '<module>';
}

function normalise(text: string): string {
  return text.replace(/\s+/g, ' ').trim();
}

/**
 * Does `node`'s type come from the Office.js typings? Fail-closed on unknown.
 *
 * Round 4: a type declared locally (`interface Cellish { values: unknown[][] }`)
 * used to be treated as "not Office", so casting a Range through it silenced the
 * guard. A receiver is now excluded ONLY when its type is a plain built-in
 * (declared in a `lib.*.d.ts`) or a non-Office type that carries none of the
 * cell-content properties. Anything that structurally looks like a range --
 * wherever it was declared -- counts.
 */
function isOfficeReceiver(checker: ts.TypeChecker, node: ts.Node): boolean {
  // Judge the expression the casts were applied TO, not the asserted type.
  const bare = underlyingExpression(node);
  if (bare !== node && isOfficeReceiver(checker, bare)) return true;
  // Bug-8690: the same judgement at a DECLARATION site. `const c: Cellish = range`
  // narrows the declared TYPE, which `underlyingExpression` cannot see because a
  // variable declaration is none of the five expression forms it unwraps.
  if (declarationOrigin(checker, node, isOfficeReceiver)) return true;
  // Strip `| undefined` introduced by optional chaining (`map.get(k)?.add(v)`),
  // otherwise the union has no single symbol and fail-closed reports a Set.
  const type = checker.getNonNullableType(checker.getTypeAtLocation(node));
  if (type.flags & ts.TypeFlags.Any) return true;
  // A primitive is never a worksheet (`someString.replace(...)`). These have no
  // symbol, so they must be excluded BEFORE the fail-closed branch below.
  const PRIMITIVE = ts.TypeFlags.StringLike | ts.TypeFlags.NumberLike | ts.TypeFlags.BooleanLike
    | ts.TypeFlags.BigIntLike | ts.TypeFlags.ESSymbolLike | ts.TypeFlags.VoidLike
    | ts.TypeFlags.Null | ts.TypeFlags.Never | ts.TypeFlags.EnumLike;
  if (type.flags & PRIMITIVE) return false;
  const symbol = type.getSymbol() ?? type.aliasSymbol;
  const decls = symbol?.getDeclarations();
  if (!decls || decls.length === 0) return true; // an object we cannot type -> fail closed
  if (decls.some(d => /office-js|office\.d\.ts|custom-functions-runtime/i.test(d.getSourceFile().fileName))) {
    return true;
  }
  // Structurally range-like, wherever it was declared: a local
  // `interface Cellish { values: unknown[][] }` is a Range in disguise. The
  // property must be DATA, not a method -- `Array.prototype.values` and
  // `Map.prototype.values` are callables, and treating them as cell content
  // would flood the report and make the guard useless.
  return hasCellDataProperty(checker, type, node);
}

function hasCellDataProperty(checker: ts.TypeChecker, type: ts.Type, node: ts.Node): boolean {
  return [...DATA_PROPS].some((p) => {
    const prop = type.getProperty(p);
    if (!prop) return false;
    const pt = checker.getTypeOfSymbolAtLocation(prop, node);
    // Must be DATA shaped like a cell grid: not a method (`Array.prototype
    // .values`, `Map.prototype.values`) and not an unrelated object field (a
    // `values` Map on one of our own caches, which would flood the report and
    // make the guard useless).
    if (pt.getCallSignatures().length > 0) return false;
    return Boolean(pt.flags & ts.TypeFlags.Any) || checker.isArrayLikeType(pt);
  }) || hasStringIndexCarryingCellData(checker, type);
}

/**
 * Unwrap type assertions, parentheses and non-null operators.
 *
 * Round 5 erased an Office receiver with `(range as unknown as CellSink).values`
 * where `interface CellSink { values: unknown }` -- narrowing the DECLARED type
 * is not narrowing the OBJECT. Judging the expression the cast was applied TO
 * closes that without widening the structural rule into every local class that
 * happens to own a field called `values`.
 */
function underlyingExpression(node: ts.Node): ts.Node {
  let n = node;
  for (;;) {
    if (ts.isParenthesizedExpression(n) || ts.isAsExpression(n) || ts.isNonNullExpression(n)
        || ts.isTypeAssertionExpression(n) || ts.isSatisfiesExpression(n)) {
      n = n.expression;
      continue;
    }
    return n;
  }
}

/**
 * Bug-8690 — declaration-site narrowing.
 *
 * `underlyingExpression` unwraps EXPRESSION-level narrowing, so
 * `(range as unknown as CellSink).values = rows` is judged on `range`. A
 * variable declaration's own type annotation is structurally none of those
 * forms, so this shape performed the identical unlocked mutation unseen:
 *
 *   const sink: CellSink = officeRange;
 *   sink.values = rows;             // reported nothing before this fix
 *
 * The INITIALISER is the right thing to judge: the declared type is what the
 * author asked the compiler to enforce, but the OBJECT is still whatever the
 * initialiser produced, and it is the object that owns the cells.
 *
 * Two deliberate limits, both proven by control fixtures:
 *
 *  - CONST only. A `let`/`var` can be reassigned, so its initialiser does not
 *    establish what the receiver is at the point of the mutation.
 *  - `any` initialisers do NOT propagate. `range.values` is typed `any` in
 *    office-js, so without this guard `const s: Set<string> = range.values as
 *    unknown as Set<string>` would make every Set and Map in the codebase read
 *    as a worksheet -- the cry-wolf failure mode that makes the guard useless.
 *
 * A well-typed program has no circular const chain (temporal dead zone), but a
 * verification tool must not depend on the code it inspects being well-typed:
 * `const a: T = b; const b: T = a;` is a valid AST and would recurse forever,
 * taking the whole guard down with a stack overflow. `declarationOrigin` keeps
 * an in-flight set so a cycle stops at the repeat instead.
 */
function declarationDeclaration(checker: ts.TypeChecker, node: ts.Node): ts.VariableDeclaration | null {
  if (!ts.isIdentifier(node)) return null;
  const decls = checker.getSymbolAtLocation(node)?.getDeclarations();
  if (!decls) return null;
  for (const d of decls) {
    if (ts.isVariableDeclaration(d) && d.type && d.initializer
        && ts.isVariableDeclarationList(d.parent)
        && Boolean(d.parent.flags & ts.NodeFlags.Const)) {
      return d;
    }
  }
  return null;
}

/** Declarations currently being followed, so a circular chain terminates. */
const declarationOriginInFlight = new Set<ts.VariableDeclaration>();

/**
 * Apply `judge` to the const initialiser behind `node`, when there is one and it
 * is not `any`-typed. Shared by `isOfficeReceiver` and `cellOwningKind` so the
 * two cannot drift apart on the same evasion (Bug-8690).
 */
function declarationOrigin<T>(
  checker: ts.TypeChecker,
  node: ts.Node,
  judge: (checker: ts.TypeChecker, n: ts.Node) => T,
): T | null {
  const decl = declarationDeclaration(checker, node);
  if (!decl || declarationOriginInFlight.has(decl)) return null;
  const init = decl.initializer!;
  const initType = checker.getNonNullableType(checker.getTypeAtLocation(underlyingExpression(init)));
  if (initType.flags & ts.TypeFlags.Any) return null;
  declarationOriginInFlight.add(decl);
  try {
    return judge(checker, init);
  } finally {
    declarationOriginInFlight.delete(decl);
  }
}

/**
 * A type with a string index signature (`Record<string, unknown>`) has no NAMED
 * `values` property, so `getProperty` returns undefined and the receiver used to
 * read as non-Office -- round 5 erased a Range through exactly that. Anything
 * that can carry an arbitrary string key can carry `values`; fail closed.
 */
function hasStringIndexCarryingCellData(checker: ts.TypeChecker, type: ts.Type): boolean {
  return Boolean(checker.getIndexInfoOfType(type, ts.IndexKind.String));
}

/**
 * Office types that OWN worksheet cells. Only these get the fail-closed
 * "unclassified method is a mutation" treatment -- applying it to every Office
 * object would report `context.sync()` and `chart.setPosition()` and drown the
 * real findings.
 */
const CELL_OWNING_TYPES = new Set([
  'Range', 'RangeAreas', 'RangeView', 'Table', 'Worksheet',
  'TableRow', 'TableRowCollection', 'TableColumn', 'TableColumnCollection',
]);

/**
 * How confident are we that this receiver owns worksheet cells?
 *
 *   'resolved'  the TYPE says so -- an office-js Range/Table/Worksheet/row or
 *               column collection, or a locally-declared type that structurally
 *               carries cell data (the round 4 `Cellish` evasion).
 *   'unknown'   the receiver is `any`: it COULD be a range, so it is judged
 *               fail-closed, but nothing about it has actually been established.
 *   null        it is definitely something else.
 *
 * The two positive answers are kept apart because the fail-closed
 * "an unclassified method is a mutation" rule is only meaningful on 'resolved'.
 * Applied to every `any` in the codebase it reports `globalThis.OfficeRuntime`
 * and `JSON.parse(x).serverUrl`, which is noise, and noise is how a real
 * finding gets lost.
 */
function cellOwningKind(checker: ts.TypeChecker, node: ts.Node): 'resolved' | 'unknown' | null {
  const bare = underlyingExpression(node);
  if (bare !== node) {
    const inner = cellOwningKind(checker, bare);
    if (inner === 'resolved') return 'resolved';
  }
  // Bug-8690: declaration-site narrowing, same `any` guard as isOfficeReceiver.
  if (declarationOrigin(checker, node, cellOwningKind) === 'resolved') return 'resolved';
  const type = checker.getNonNullableType(checker.getTypeAtLocation(node));
  const symbol = type.getSymbol() ?? type.aliasSymbol;
  const decls = symbol?.getDeclarations();
  if (decls?.some(d => /office-js|office\.d\.ts/i.test(d.getSourceFile().fileName))
      && symbol && CELL_OWNING_TYPES.has(symbol.getName())) {
    return 'resolved';
  }
  if (type.flags & ts.TypeFlags.Any) return 'unknown';
  if (Boolean(decls) && hasCellDataProperty(checker, type, node)) return 'resolved';
  return bare !== node ? cellOwningKind(checker, bare) : null;
}

function isCellOwningReceiver(checker: ts.TypeChecker, node: ts.Node): boolean {
  return cellOwningKind(checker, node) !== null;
}

/**
 * Reflective spellings of a property write/read on an object. `Reflect.set`
 * invokes the SAME office-js setter as `range.values = rows`; `Reflect.get`
 * hands out the same method object as `range.clear`. The value is the argument
 * index (or indices) that could be the Office receiver.
 */
const REFLECTIVE_ACCESS: Record<string, Record<string, number[]>> = {
  Object: { assign: [0], defineProperty: [0], defineProperties: [0] },
  Reflect: { set: [0], get: [0], defineProperty: [0], apply: [0, 1] },
};

/**
 * The member name this access reads, when it is knowable statically.
 *
 * Covers `r.clear`, `r['clear']` and `r[K]` where `K`'s TYPE is a string
 * literal (`const K = 'clear' as const`) -- the checker knows the name even
 * though the syntax is computed. A genuinely runtime-computed name returns
 * null: that is the documented static limit, bounded by EXCEL_RUN_MODULES.
 */
function staticMemberName(
  checker: ts.TypeChecker,
  node: ts.PropertyAccessExpression | ts.ElementAccessExpression,
): string | null {
  if (ts.isPropertyAccessExpression(node)) return node.name.text;
  const arg = node.argumentExpression;
  if (!arg) return null;
  if (ts.isStringLiteralLike(arg)) return arg.text;
  const t = checker.getTypeAtLocation(arg);
  return t.isStringLiteral() ? t.value : null;
}

/**
 * Would acquiring `member` from `receiver` hand out a worksheet mutation?
 *
 * Identical judgement to the one R7 applied at CALL sites, lifted to the
 * MEMBER ACCESS so that it no longer depends on how the member is later
 * invoked. On a CELL-OWNING type the default is fail-closed (an unclassified
 * method is a mutation until someone classifies it); on every other Office
 * receiver the explicit MUTATING_METHODS set applies, so `context.sync()` and
 * `chart.setPosition()` stay out of the report.
 */
function isMutatingMember(checker: ts.TypeChecker, receiver: ts.Node, member: string): boolean {
  if (isCellOwningReceiver(checker, receiver)) return !READ_ONLY_METHODS.has(member);
  return MUTATING_METHODS.has(member) && isOfficeReceiver(checker, receiver);
}

/**
 * The same judgement for a member taken as a VALUE rather than called on the
 * spot (`const g = r.clear`, `const { clear } = r`, `arr.forEach(r.clear)`).
 *
 * Identical to `isMutatingMember`, minus the fail-closed-on-unclassified branch
 * when the receiver is merely `any`. On a RESOLVED cell-owning type an
 * unclassified member is still reported, so an office-js upgrade that adds a
 * mutating API cannot be aliased past this guard either.
 */
function isMutatingMemberValue(checker: ts.TypeChecker, receiver: ts.Node, member: string): boolean {
  if (!isMutatingMember(checker, receiver, member)) return false;
  return MUTATING_METHODS.has(member) || cellOwningKind(checker, receiver) === 'resolved';
}

/**
 * Is the value this access yields CALLABLE (so it can perform the mutation)?
 *
 * This is what keeps the member rule from reporting `range.address`,
 * `range.format` or `table.rows` -- data members, not mutations. Unresolvable
 * types fail closed, so an untyped receiver is still judged.
 */
function isCallableMember(checker: ts.TypeChecker, access: ts.Node): boolean {
  const t = checker.getNonNullableType(checker.getTypeAtLocation(access));
  if (t.flags & (ts.TypeFlags.Any | ts.TypeFlags.Unknown)) return true;
  return t.getCallSignatures().length > 0;
}

/**
 * Where a member acquisition should be REPORTED, and whether it is invoked on
 * the spot.
 *
 * Reporting the enclosing CallExpression for `r.clear()` keeps the allowlist
 * keys (`<file>::<fn>::<statement>`) stable. `.call` / `.apply` / `.bind`
 * report the outer call for the same reason; every other shape -- an alias, an
 * argument, a returned method reference -- reports the access itself, because
 * there is no call to point at.
 */
function invocationShape(access: ts.Node): { reportNode: ts.Node; invoked: boolean } {
  const n = unwrapUp(access);
  const p = n.parent;
  if (p && ts.isCallExpression(p) && p.expression === n) return { reportNode: p, invoked: true };
  if (p && ts.isPropertyAccessExpression(p) && p.expression === n
      && (p.name.text === 'call' || p.name.text === 'apply' || p.name.text === 'bind')) {
    const q = unwrapUp(p).parent;
    // `.call` / `.apply` perform the mutation here; `.bind` only manufactures a
    // callable, so it is an acquisition whose VALUE can travel -- classifying it
    // as invoked would exempt it from the escaping-acquisition check below.
    if (q && ts.isCallExpression(q)) return { reportNode: q, invoked: p.name.text !== 'bind' };
  }
  return { reportNode: access, invoked: false };
}

/**
 * Is this member access an assignment TARGET reached through a destructuring
 * pattern or a for-of/for-in head (`[r.values] = rows`,
 * `({ v: r.values } = o)`, `for (r.values of rows)`)?
 *
 * Each is `r.values = ...` with the `=` moved out of the access's own
 * expression, so none of them is a BinaryExpression whose LEFT is the access
 * and rule 1 alone does not see them. R7 enumerated assignment OPERATORS
 * (`=`, `||=`, `??=`); this is the same omission on the assignment-TARGET axis.
 *
 * The direct form (`hops === 0`) is deliberately left to rule 1, so the ALLOWED
 * keys keep pointing at the whole assignment statement rather than at a bare
 * member access.
 */
function isDestructuringTarget(node: ts.Node): boolean {
  let n: ts.Node = unwrapUp(node);
  for (let hops = 0; ; hops += 1) {
    const p: ts.Node | undefined = n.parent;
    if (!p) return false;
    if (ts.isArrayLiteralExpression(p) || ts.isObjectLiteralExpression(p)) { n = unwrapUp(p); continue; }
    if (ts.isPropertyAssignment(p) && p.initializer === n) { n = p.parent; continue; }
    if (ts.isSpreadElement(p) || ts.isSpreadAssignment(p)) { n = p.parent; continue; }
    if (ts.isBinaryExpression(p) && p.left === n
        && p.operatorToken.kind >= ts.SyntaxKind.FirstAssignment
        && p.operatorToken.kind <= ts.SyntaxKind.LastAssignment) return hops > 0;
    if ((ts.isForOfStatement(p) || ts.isForInStatement(p)) && p.initializer === n) return true;
    return false;
  }
}

/**
 * Does the VALUE of an acquisition taken inside `body` leave that body, so it
 * can be invoked after the lock releases?
 *
 * The dual of R8's own inversion, and worth stating: moving detection onto the
 * ACQUISITION means the question `record()` asks is "was the acquisition inside
 * a lock", which sanctions a method reference captured in the critical section
 * and called after it. Round 4 S-G caught that when the escaping thing was a
 * CLOSURE; the method-reference form needs this.
 *
 * R11 INVERTED THIS, and the reason is the whole lesson of this lane. R8 and R9
 * had already inverted `runsInPlace`, `isArrayLikeReceiver` and
 * `isMutatingMember` from open denylists into closed, fail-closed rules. This
 * function was the ONE component left that still enumerated the ways a value
 * ESCAPES and defaulted to "did not escape" when it fell off the end of the
 * list. Four separate gaps in review round 3 were all that single defect:
 * `??`/`||`/`,` were absent from the carrier list; `rows.map(() => acq)` runs
 * its callback in place but hands the RESULT out; `let h; h = acq` split the
 * declaration from the assignment; and a destructured or reflected acquisition
 * never reached the check at all. Patching four more hops would have produced a
 * fifth.
 *
 * So the question is now asked the other way round. The value ESCAPES unless it
 * is provably CONSUMED inside the critical section, and consumption is a closed
 * allowlist (`consumesInPlace`): invoked on the spot, discarded as an
 * expression statement, reduced by an operator that cannot yield a function, or
 * handed to a callee proven both to run in place AND to discard its callback's
 * result. Anything else -- including every hop nobody has invented yet -- is an
 * escape. A wrong answer in that direction is a visible test failure with a
 * "restructure" remedy, not a silent wrong-numbers gap.
 *
 * Every consumption verdict is additionally conditioned on the site being in
 * the HELD region (`heldWithin`), not merely inside the body's text. R11's
 * first cut checked that only for the acquisition, so an alias invoked from a
 * `setTimeout` arrow inside the lock read as "consumed" -- see `heldWithin`.
 *
 * Bindings are followed rather than treated as walls: `const h = acq` and
 * `h = acq` both hand off to `aliasEscapes`, which puts every reference to that
 * binding through this same test. `seen` bounds the recursion -- a cyclic
 * binding graph (`var a = b; var b = a`) previously overflowed the stack.
 */
function escapesLockBody(
  access: ts.Node, body: ts.Node, sf: ts.SourceFile, checker: ts.TypeChecker,
  seen: Set<ts.Symbol> = new Set(),
): boolean {
  for (let n: ts.Node = unwrapUp(access); ; n = n.parent) {
    const p: ts.Node | undefined = n.parent;
    if (!p) return true;                                   // off the tree -> unproven
    if (consumesInPlace(p, n, checker)) return false;
    // Handed to a call: the callee now holds it, and only a proven in-place,
    // result-discarding callee keeps it inside the critical section.
    if ((ts.isCallExpression(p) || ts.isNewExpression(p))) {
      return !(p.arguments?.some(a => a === n) && discardsInPlace(p, checker));
    }
    if (ts.isVariableDeclaration(p) && p.initializer === n && ts.isIdentifier(p.name)) {
      return aliasEscapes(p.name, body, sf, checker, seen);
    }
    if (ts.isBinaryExpression(p) && p.right === n
        && p.operatorToken.kind >= ts.SyntaxKind.FirstAssignment
        && p.operatorToken.kind <= ts.SyntaxKind.LastAssignment) {
      if (declaredOutside(p.left, body, sf, checker)) return true;
      // Assigned to a binding declared INSIDE the body -- follow it, exactly as
      // a `const` initialiser is followed. `box.g = acq` follows `box`: if the
      // holder escapes, so does everything hung on it.
      let root: ts.Node = unwrapUp(p.left);
      while (ts.isPropertyAccessExpression(root) || ts.isElementAccessExpression(root)) root = root.expression;
      return ts.isIdentifier(root) ? aliasEscapes(root, body, sf, checker, seen) : true;
    }
    // Reached the lock callback itself: the value is its result.
    if (p.getStart(sf) < body.getStart(sf)) return true;
  }
}

/**
 * CLOSED allowlist of ways a value is consumed where it stands. Absence from
 * this list means "not proven consumed", which reports an escape -- a false
 * positive at worst, never a missed one.
 */
function consumesInPlace(p: ts.Node, n: ts.Node, checker: ts.TypeChecker): boolean {
  // Invoked here: `acq()`, `acq.call(r)` (the latter never reaches this
  // function, being classified as an invocation by `invocationShape`).
  if (ts.isCallExpression(p) && p.expression === n) return true;
  // The value is evaluated and thrown away.
  if (ts.isExpressionStatement(p)) return true;
  // Reduced to something that is not a callable.
  if (ts.isTypeOfExpression(p) || ts.isVoidExpression(p) || ts.isDeleteExpression(p)
      || ts.isPrefixUnaryExpression(p) || ts.isPostfixUnaryExpression(p)
      || ts.isTemplateSpan(p) || ts.isTaggedTemplateExpression(p)) {
    return true;
  }
  if (ts.isBinaryExpression(p) && REDUCING_OPERATORS.has(p.operatorToken.kind)) return true;
  // A CONDITION coerces its operand to a boolean and can never yield the
  // function value, so `const h = r.clear.bind(r); if (h) { h(); }` -- ordinary
  // defensive code -- must not be reported. The value still has to be consumed
  // on every path it actually travels; only the test itself is consumption.
  if ((ts.isIfStatement(p) || ts.isWhileStatement(p) || ts.isDoStatement(p)
       || ts.isSwitchStatement(p)) && p.expression === n) return true;
  if (ts.isForStatement(p) && p.condition === n) return true;
  if (ts.isConditionalExpression(p) && p.condition === n) return true;
  // A property/element access reads OFF the value; what continues is a
  // different value, judged on its own by the acquisition rules.
  if ((ts.isPropertyAccessExpression(p) || ts.isElementAccessExpression(p)) && p.expression === n) {
    return !isCallableMember(checker, p);
  }
  return false;
}

/**
 * Operators that cannot yield their operand. `||`, `&&`, `??` and `,` are
 * deliberately ABSENT: each of them CAN yield the operand unchanged, and
 * treating them as reducing is what let `g = fallback ?? r.clear.bind(r)` read
 * as consumed in R10.
 */
const REDUCING_OPERATORS = new Set<ts.SyntaxKind>([
  ts.SyntaxKind.EqualsEqualsToken, ts.SyntaxKind.EqualsEqualsEqualsToken,
  ts.SyntaxKind.ExclamationEqualsToken, ts.SyntaxKind.ExclamationEqualsEqualsToken,
  ts.SyntaxKind.LessThanToken, ts.SyntaxKind.GreaterThanToken,
  ts.SyntaxKind.LessThanEqualsToken, ts.SyntaxKind.GreaterThanEqualsToken,
  ts.SyntaxKind.InstanceOfKeyword, ts.SyntaxKind.InKeyword,
  ts.SyntaxKind.PlusToken, ts.SyntaxKind.MinusToken, ts.SyntaxKind.AsteriskToken,
  ts.SyntaxKind.SlashToken, ts.SyntaxKind.PercentToken, ts.SyntaxKind.AsteriskAsteriskToken,
]);

/**
 * Callees that both run their callback in place AND discard its result, so a
 * value handed to them cannot leave the critical section through the return
 * path. `map` / `flatMap` / `reduce` / `Excel.run` / a nested lock helper all
 * run in place but RETURN the callback's value -- `runsInPlace` answers a
 * different question and must not be reused here.
 */
const DISCARDING_IN_PLACE_CALLEES = new Set(['forEach']);

function discardsInPlace(call: ts.CallExpression | ts.NewExpression, checker: ts.TypeChecker): boolean {
  const callee = call.expression;
  if (!ts.isPropertyAccessExpression(callee)) return false;
  return DISCARDING_IN_PLACE_CALLEES.has(callee.name.text)
    && isArrayLikeReceiver(checker, callee.expression);
}

/**
 * Does a binding that holds an acquisition inside the lock itself escape?
 *
 * `const h = r.clear.bind(r); return h;` is the same escape as `return
 * r.clear.bind(r)`, and introducing that local is the most ordinary refactor
 * there is. References are matched by SYMBOL and each goes through the same
 * consumption test, so `const h = ...; h();` stays in place with no special
 * case. `seen` stops a cyclic binding graph recursing without bound.
 */
function aliasEscapes(
  name: ts.Identifier, body: ts.Node, sf: ts.SourceFile, checker: ts.TypeChecker,
  seen: Set<ts.Symbol>,
): boolean {
  const sym = valueSymbolOf(checker, name);
  if (!sym) return true; // unresolved -> fail closed
  // Pruning a re-entry is safe, not merely convenient: the OUTERMOST call for a
  // symbol enumerates that symbol's complete reference set over `body`, so a
  // cycle cannot hide an escape that the outer enumeration will not also see.
  if (seen.has(sym)) return false;
  seen.add(sym);
  let escaped = false;
  const visit = (node: ts.Node): void => {
    if (escaped) return;
    if (ts.isIdentifier(node) && node !== name && valueSymbolOf(checker, node) === sym) {
      // A BINDING occurrence (`let h;`, a parameter, a binding element) is
      // where the name is introduced, not a use of the value. Treating it as a
      // reference made every followed binding look like an escape, because the
      // walk from a declaration name reaches the callback with nothing to
      // consume it.
      if (!isBindingOccurrence(node)) {
        // A reference sitting OUTSIDE the held region has already left it,
        // whatever is done with it there -- including being invoked.
        if (!heldWithin(node, body, sf, checker)
            || escapesLockBody(node, body, sf, checker, seen)) {
          escaped = true;
        }
      }
      return;
    }
    ts.forEachChild(node, visit);
  };
  visit(body);
  return escaped;
}

/**
 * The symbol of the VALUE an identifier denotes.
 *
 * A shorthand in a destructuring assignment (`({ clear } = r)`) has TWO symbols
 * at the same position: a synthetic property symbol declared at the shorthand
 * itself, and the binding being assigned to. `getSymbolAtLocation` returns the
 * former, so resolving the two sides of a reference comparison with different
 * calls silently matched nothing -- which is why the D-31 shorthand case only
 * passed when its target happened to be declared outside the lock body and
 * short-circuited before the reference walk ever ran.
 */
function valueSymbolOf(checker: ts.TypeChecker, node: ts.Identifier): ts.Symbol | undefined {
  const p = node.parent;
  if (p && ts.isShorthandPropertyAssignment(p) && p.name === node) {
    return checker.getShorthandAssignmentValueSymbol(p) ?? checker.getSymbolAtLocation(node);
  }
  return checker.getSymbolAtLocation(node);
}

/** Is this identifier the place a name is INTRODUCED, rather than used? */
function isBindingOccurrence(node: ts.Identifier): boolean {
  const p = node.parent;
  return Boolean(p) && (ts.isVariableDeclaration(p) || ts.isParameter(p) || ts.isBindingElement(p)
    || ts.isFunctionDeclaration(p) || ts.isClassDeclaration(p)) && (p as { name?: ts.Node }).name === node;
}

/**
 * Which reflection namespace does this expression denote, if any?
 *
 * Resolved rather than string-matched, for the same reason `isExcelNamespace`
 * is: `const R = Reflect; R.set(range, 'values', rows)` is the same write as
 * `Reflect.set(...)`, and a rule that only recognises the literal identifier is
 * a rule about spelling.
 */
function reflectiveHolder(checker: ts.TypeChecker, node: ts.Node): string | null {
  const bare = underlyingExpression(node);
  if (!ts.isIdentifier(bare)) return null;
  if (bare.text in REFLECTIVE_ACCESS) return bare.text;
  // `Object` resolves through its constructor interface; `Reflect` through the
  // declared namespace symbol.
  const typeName = checker.getTypeAtLocation(bare).getSymbol()?.getName();
  if (typeName === 'ObjectConstructor') return 'Object';
  if (typeName && typeName in REFLECTIVE_ACCESS) return typeName;
  // A local alias: `const R = Reflect`.
  for (const d of checker.getSymbolAtLocation(bare)?.getDeclarations() ?? []) {
    if (ts.isVariableDeclaration(d) && d.initializer) {
      const init = underlyingExpression(d.initializer);
      if (ts.isIdentifier(init) && init.text in REFLECTIVE_ACCESS) return init.text;
    }
  }
  return null;
}

/** Is the root binding of this assignment target declared outside `body`? */
function declaredOutside(
  target: ts.Node, body: ts.Node, sf: ts.SourceFile, checker: ts.TypeChecker,
): boolean {
  let root: ts.Node = unwrapUp(target);
  while (ts.isPropertyAccessExpression(root) || ts.isElementAccessExpression(root)) root = root.expression;
  if (!ts.isIdentifier(root)) return true; // cannot resolve the target -> fail closed
  const decls = checker.getSymbolAtLocation(root)?.getDeclarations();
  if (!decls || decls.length === 0) return true; // unresolved (fixture) -> fail closed
  return decls.some(d => d.getSourceFile().fileName !== sf.fileName
    || d.getStart(sf) < body.getStart(sf) || d.getEnd() > body.getEnd());
}

/**
 * Is `node` inside the HELD region of `body` -- lexically in it, AND with every
 * function boundary between the two proven to run in place?
 *
 * R12: this used to exist only as a closure inside `scanFile`, applied to the
 * ACQUISITION. `consumesInPlace` then accepted "invoked here" as consumption
 * without ever establishing that "here" is still held, so an alias invoked from
 * a `setTimeout` / event-handler arrow inside the lock read as consumed --
 * re-opening the pinned D-17..D-23 deferral family behind one binding hop, and
 * making the guard's verdict depend on whether the author wrote
 * `registerUndo(restore)` or `registerUndo(() => restore())`. Held-ness is one
 * question and must be asked the same way on both axes, so it lives here.
 */
function heldWithin(node: ts.Node, body: ts.Node, sf: ts.SourceFile, checker: ts.TypeChecker): boolean {
  const s = body.getStart(sf);
  const e = body.getEnd();
  const pos = node.getStart(sf);
  if (pos < s || pos >= e) return false;
  for (let fn = innermostFunction(node); fn; fn = innermostFunction(fn)) {
    // The lock callback itself always STARTS before its own body, so this also
    // covers `fn === body.parent`; a separate test for that would be an
    // unreachable branch nothing could pin.
    if (fn.getStart(sf) < s) return true;
    if (!runsInPlace(fn, checker)) return false;   // escaping/deferred closure -> not held
  }
  return true;
}

/** Every Office mutation in `sf` that is NOT inside a lock callback body. */
function scanFile(relPath: string, sf: ts.SourceFile, checker: ts.TypeChecker): MutationSite[] {
  const bodies = lockBodies(sf, checker);
  const holdingBody = (node: ts.Node): ts.Node | null =>
    bodies.find(body => heldWithin(node, body, sf, checker)) ?? null;
  const inLock = (node: ts.Node): boolean => holdingBody(node) !== null;
  const out: MutationSite[] = [];
  const reported = new Set<ts.Node>();

  const record = (node: ts.Node, escapingFrom?: ts.Node | null): void => {
    // `escapingFrom` is set when the site IS inside that lock body but its value
    // leaves it, so being in-lock is not a sanction.
    if (reported.has(node)) return;
    if (!escapingFrom && inLock(node)) return;
    reported.add(node);
    const fn = enclosingName(node);
    const text = normalise(node.getText(sf));
    out.push({
      key: `${relPath}::${fn}::${text}`,
      file: relPath,
      line: sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1,
      text,
      fn,
    });
  };

  const visit = (node: ts.Node): void => {
    // 1. EVERY assignment operator, not just `=`. Round 4 evaded with `||=`.
    if (ts.isBinaryExpression(node)
        && node.operatorToken.kind >= ts.SyntaxKind.FirstAssignment
        && node.operatorToken.kind <= ts.SyntaxKind.LastAssignment) {
      const lhs = node.left;
      // Dot access, or bracket access whose key is statically known (a round 3
      // escape used the literal form; a `const K = 'values' as const` key is
      // the same write with the literal moved one line up).
      if (ts.isPropertyAccessExpression(lhs) || ts.isElementAccessExpression(lhs)) {
        const member = staticMemberName(checker, lhs);
        if (member && DATA_PROPS.has(member) && isOfficeReceiver(checker, lhs.expression)) {
          record(node);
        }
      }
    }

    // 2. Reflective property access on an Office receiver.
    if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression)) {
      const callee = node.expression;
      const holder = reflectiveHolder(checker, callee.expression);
      const argIndices = holder ? REFLECTIVE_ACCESS[holder]?.[callee.name.text] : undefined;
      if (argIndices?.some((i) => {
        const target = node.arguments[i];
        return Boolean(target) && isOfficeReceiver(checker, target);
      })) {
        // `Reflect.get` ACQUIRES a member; the others perform the write here.
        // An acquisition taken inside a lock still has to pass the escape test,
        // exactly as rule 3's does -- this route used to record unconditionally,
        // so `g = Reflect.get(r, 'clear')` inside a lock was suppressed.
        const acq = (holder === 'Reflect' && callee.name.text === 'get')
          ? holdingBody(node) : null;
        record(node, acq && escapesLockBody(node, acq, sf, checker) ? acq : null);
      }
    }

    // 3. MEMBER ACQUISITION -- the structural rule.
    //
    // Codex gate 5: detection used to fire only on a literal dot-access CALLEE
    // (`ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression)`),
    // so every indirection walked through: `r['clear']()`, `r.clear.call(r)`,
    // `r.clear.bind(r)`, `const { clear } = r; clear()`, `arr.forEach(r.clear)`,
    // `const g = r.clear; g()`. Those are six spellings of ONE act: taking a
    // mutating member off an Office object. Enumerating invocation forms is
    // what produced six escapes across this lane's history, so the detector no
    // longer looks at the invocation at all -- it reports the ACQUISITION,
    // whatever is done with it afterwards.
    if (ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node)) {
      const member = staticMemberName(checker, node);

      // 3b. A DATA prop assigned through a destructuring pattern or a for-of
      //     head is the same store as `r.values = ...`, and is not a
      //     BinaryExpression whose left is this access, so rule 1 misses it.
      if (member && DATA_PROPS.has(member) && isDestructuringTarget(node)
          && isOfficeReceiver(checker, node.expression)) {
        record(node);
      }

      // DATA_PROPS are excluded from the acquisition rule on purpose: READING
      // `range.values` is legitimate and constant in this codebase; WRITING it
      // is rule 1 (or 3b).
      if (member && !DATA_PROPS.has(member)) {
        const { reportNode, invoked } = invocationShape(node);
        if (invoked
          ? isMutatingMember(checker, node.expression, member)
          // An acquisition that is not called on the spot must still be a
          // FUNCTION, or the rule would report `range.address` / `table.rows`.
          : (isMutatingMemberValue(checker, node.expression, member) && isCallableMember(checker, node))) {
          // An acquisition taken INSIDE a lock but whose value leaves the
          // critical section can be invoked after release -- the dual of
          // round 4 S-G for method references rather than closures.
          const body = invoked ? null : holdingBody(reportNode);
          record(reportNode, body && escapesLockBody(reportNode, body, sf, checker) ? body : null);
        }
      }
    }

    // 4. Destructuring a mutating member out of an Office object.
    //
    // A destructured member is an ACQUISITION like any other, so it takes the
    // same escape test: `const { clear } = r` inside a lock, with `clear`
    // assigned outward, is the R9 I-17 escape wearing gate 5 I-6's syntax.
    // Both routes used to record unconditionally and were suppressed by inLock.
    if (ts.isVariableDeclaration(node) && node.initializer
        && ts.isObjectBindingPattern(node.name)
        && bindsMutatingMember(checker, node.name, node.initializer)) {
      const body = holdingBody(node);
      const escapes = Boolean(body) && node.name.elements.some(el => ts.isIdentifier(el.name)
        && aliasEscapes(el.name, body as ts.Node, sf, checker, new Set()));
      record(node, escapes ? body : null);
    }
    if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
        && ts.isObjectLiteralExpression(node.left)
        && destructuredNames(node.left).some(m => isMutatingMemberValue(checker, node.right, m)
          && memberValueIsCallable(checker, node.right, m))) {
      const body = holdingBody(node);
      record(node, body && destructuredTargetEscapes(node.left, body, sf, checker) ? body : null);
    }

    ts.forEachChild(node, visit);
  };
  visit(sf);
  return out;
}

/**
 * Property names a binding pattern pulls off its initialiser.
 *
 * The CALLABILITY requirement is the same one rule 3 applies, and rule 4 was
 * missing it: on a resolved cell-owning receiver `isMutatingMember` is
 * `!READ_ONLY_METHODS.has(member)`, and that set holds method names only, so
 * every DATA property qualified. Harmless while rule 4 recorded unconditionally
 * inside a lock; once R11 wired the escape check in, `const { values } = range`
 * -- the idiomatic Office.js read after load/sync -- started reporting as a
 * mutation, and the failure message has no correct remedy for a read. A guard
 * that fires on correct code is the one the next author weakens.
 */
function bindsMutatingMember(
  checker: ts.TypeChecker, pattern: ts.ObjectBindingPattern, initializer: ts.Node,
): boolean {
  return pattern.elements.some((el) => {
    // A REST element captures every remaining member, mutating ones included.
    if (el.dotDotDotToken) {
      return isCellOwningReceiver(checker, initializer) || isOfficeReceiver(checker, initializer);
    }
    const key = el.propertyName ?? el.name;
    const member = ts.isIdentifier(key) ? key.text : ts.isStringLiteralLike(key) ? key.text : null;
    if (!member || !isMutatingMemberValue(checker, initializer, member)) return false;
    return memberValueIsCallable(checker, initializer, member);
  });
}

/** Is `receiver[member]` a callable, so the binding could perform a mutation? */
function memberValueIsCallable(checker: ts.TypeChecker, receiver: ts.Node, member: string): boolean {
  const t = checker.getNonNullableType(checker.getTypeAtLocation(receiver));
  if (t.flags & (ts.TypeFlags.Any | ts.TypeFlags.Unknown)) return true; // fail closed
  const prop = t.getProperty(member);
  if (!prop) return true;                                               // fail closed
  // NON-NULLABLE first, exactly as rule 3's `isCallableMember` does. Read raw,
  // an OPTIONAL member's type is `(() => void) | undefined`, a union reports
  // zero call signatures, and the member reads as data -- so `const {
  // newHostMutator } = r` was silenced while `const g = r.newHostMutator` (the
  // same acquisition through rule 3) still reported. Two functions documented
  // as applying the same judgement must not disagree on it.
  const pt = checker.getNonNullableType(checker.getTypeOfSymbolAtLocation(prop, receiver));
  if (pt.flags & (ts.TypeFlags.Any | ts.TypeFlags.Unknown)) return true;
  return pt.getCallSignatures().length > 0;
}

/**
 * Does a destructuring-ASSIGNMENT target carry its acquisition out of the lock?
 *
 * `({ clear } = r)` binds through a SHORTHAND property, whose own
 * `getSymbolAtLocation` resolves to a synthetic property symbol declared AT the
 * shorthand -- which reads as "declared inside the body" and reports no escape.
 * The real binding is reached through `getShorthandAssignmentValueSymbol`.
 */
function destructuredTargetEscapes(
  target: ts.ObjectLiteralExpression, body: ts.Node, sf: ts.SourceFile, checker: ts.TypeChecker,
): boolean {
  return target.properties.some((prop) => {
    const name = ts.isShorthandPropertyAssignment(prop) ? prop.name
      : (ts.isPropertyAssignment(prop) && ts.isIdentifier(prop.initializer)) ? prop.initializer : null;
    if (!name) return false;
    const sym = valueSymbolOf(checker, name);
    const decls = sym?.getDeclarations();
    // Declared outside the lock body -> the value outlives the critical section.
    if (!decls || decls.some(d => d.getStart(sf) < body.getStart(sf) || d.getEnd() > body.getEnd())) {
      return true;
    }
    return aliasEscapes(name, body, sf, checker, new Set());
  });
}

/** Property names a destructuring ASSIGNMENT target pulls off its source. */
function destructuredNames(target: ts.ObjectLiteralExpression): string[] {
  const names: string[] = [];
  for (const p of target.properties) {
    if (ts.isShorthandPropertyAssignment(p)) names.push(p.name.text);
    else if (ts.isPropertyAssignment(p) && ts.isIdentifier(p.name)) names.push(p.name.text);
    else if (ts.isPropertyAssignment(p) && ts.isStringLiteralLike(p.name)) names.push(p.name.text);
  }
  return names;
}

/**
 * Every REFERENCE to `Excel.run` in `sf`, by line -- not only calls.
 *
 * Round 4 defeated the module allowlist with `const r = Excel.run; await r(...)`
 * and `const { run } = Excel;`. Flagging the reference itself (however it is
 * later invoked) closes both, and closes any future aliasing shape too.
 */
function excelRunLines(sf: ts.SourceFile): number[] {
  const lines: number[] = [];
  const at = (node: ts.Node): number => sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1;

  const visit = (node: ts.Node): void => {
    // `Excel.run` in ANY position: call, alias, argument, property value --
    // and through ANY spelling of the namespace. Round 5 aliased the NAMESPACE
    // (`const host = Excel; host.run(...)`, `globalThis.Excel.run(...)`),
    // which is the bound this module's docstring relies on to contain
    // runtime-computed property writes.
    if (ts.isPropertyAccessExpression(node)
        && node.name.text === 'run'
        && isExcelNamespace(node.expression)) {
      lines.push(at(node));
    }
    // `const host = Excel` -- binding the namespace itself is enough to escape.
    if (ts.isVariableDeclaration(node) && node.initializer
        && ts.isIdentifier(node.name) && isExcelNamespace(node.initializer)) {
      lines.push(at(node));
    }
    // `const { run } = Excel` / `const { run: alias } = Excel`.
    if (ts.isVariableDeclaration(node) && node.initializer
        && ts.isIdentifier(node.initializer) && node.initializer.text === 'Excel'
        && node.name && ts.isObjectBindingPattern(node.name)
        && node.name.elements.some(el => ts.isIdentifier(el.propertyName ?? el.name)
          && (el.propertyName ?? el.name).getText(sf) === 'run')) {
      lines.push(at(node));
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  return lines;
}

/**
 * Every source file the bundler can reach.
 *
 * Round 5 hid an unlocked `Excel.run` cell write in `src/utils/__tests__/` and
 * another in a `.mts` file: the old walker skipped ANY directory literally
 * named `__tests__` at ANY depth, and its extension regex matched only
 * `.ts`/`.tsx`. Both files compile under `tsc` and bundle under vite, so both
 * were live production code the contract never inspected. The file-set is now
 * defined by what is bundlable minus what is unmistakably a test, and
 * `filesUnderContract` is asserted TOTAL against an independent sweep below.
 */
const TEST_FILE = /\.(test|spec)\.[cm]?[jt]sx?$/;
const BUNDLABLE = /\.[cm]?[jt]sx?$/;

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      // Only the ONE top-level suite directory is exempt. A `__tests__` folder
      // nested anywhere else is ordinary bundlable code and IS scanned.
      if (entry === 'node_modules' || full === join(SRC, '__tests__')) continue;
      walk(full, out);
    } else if (BUNDLABLE.test(entry) && !TEST_FILE.test(entry) && !entry.endsWith('.d.ts')) {
      out.push(full);
    }
  }
  return out;
}

/** Test-visible: the exact file-set the contract inspects. */
export function filesUnderContract(): string[] {
  return walk(SRC).map(f => relative(SRC, f).split(sep).join('/'));
}

/** The real project program, built once and shared by every assertion. */
let _program: ts.Program | null = null;
function program(): ts.Program {
  if (_program) return _program;
  const root = resolve(SRC, '..');
  const configPath = join(root, 'tsconfig.json');
  const parsed = ts.parseJsonConfigFileContent(
    ts.readConfigFile(configPath, ts.sys.readFile).config, ts.sys, root,
  );
  _program = ts.createProgram(walk(SRC), { ...parsed.options, noEmit: true });
  return _program;
}

function projectFiles(): { rel: string; sf: ts.SourceFile }[] {
  const p = program();
  return walk(SRC)
    .map(f => ({ rel: relative(SRC, f).split(sep).join('/'), sf: p.getSourceFile(f) }))
    .filter((x): x is { rel: string; sf: ts.SourceFile } => Boolean(x.sf));
}

// ---------------------------------------------------------------------------
// Bug-8690 F1 / F2 — the two POLICY guards.
//
// READ THIS BEFORE CITING THEIR GREEN STATUS. These are policy assertions, not
// defect proofs. They pass against the tree today BY DESIGN: their job is to
// stop a shape from APPEARING, not to demonstrate a defect that exists. The
// campaign rule "a guard must fail on current main before it is adopted" is
// deliberately waived for these two by user decision, on exactly that basis.
// Nobody may later read their green as evidence that a Bug-8690 defect was
// found and fixed.
//
// Because a guard that has never been seen to fire has demonstrated nothing,
// each scanner is a plain function over a SourceFile, and each policy test is
// paired with a MUTATION test that feeds it the forbidden shape and asserts it
// goes red. That pairing is the permanent substitute for fail-first.
//
// SCOPE: the excel-plugin's own bundlable production sources — the same file
// set every other assertion in this contract inspects (`walk(SRC)`, tests and
// .d.ts excluded). Not the monorepo. That is the narrowest scope that still
// closes the route, because both evasions need a receiver that reaches an
// Office object, and every such receiver is constructed inside this package;
// an alias laundered through a helper here is still inside the scanned set.
// ---------------------------------------------------------------------------

/**
 * F1 — bare `any` type annotations.
 *
 * `isMutatingMemberValue` deliberately drops fail-closed treatment when the
 * receiver is `any`, because widening there would flood the report with every
 * `JSON.parse()` result in the package. That exclusion is only safe while no
 * `any`-typed binding exists: with one, the VALUE form
 * (`const g = r.someHostMutator; g()`) is invisible to the detector. `strict`
 * already bans IMPLICIT `any`, so an explicit annotation is the only way in,
 * and this is what closes it. Use `unknown` plus a type guard instead.
 */
function scanBareAnyAnnotations(rel: string, sf: ts.SourceFile): string[] {
  const found: string[] = [];
  const at = (node: ts.Node): number => sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1;
  const visit = (node: ts.Node): void => {
    const annotated = node as { type?: ts.TypeNode };
    const isBareAny = annotated.type?.kind === ts.SyntaxKind.AnyKeyword;
    if (isBareAny) {
      if (ts.isVariableDeclaration(node) || ts.isParameter(node)
          || ts.isPropertyDeclaration(node) || ts.isPropertySignature(node)) {
        found.push(`${rel}:${at(node)}  bare \`any\` type annotation`);
      } else if (ts.isFunctionDeclaration(node) || ts.isMethodDeclaration(node)
          || ts.isArrowFunction(node) || ts.isFunctionExpression(node)
          || ts.isMethodSignature(node)) {
        found.push(`${rel}:${at(node)}  bare \`any\` return type`);
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  return found;
}

/** Reflective members that can write a property on an object they were handed. */
const REFLECTIVE_POLICY: Record<string, Set<string>> = {
  Reflect: new Set(['set', 'get', 'defineProperty', 'apply']),
  Object: new Set(['assign', 'defineProperty', 'defineProperties']),
};

/**
 * F2 — reflective APIs outside the canonical dot-access CALL form.
 *
 * Rule 2 recognises `Reflect.set(range, 'values', rows)` because the callee is
 * a PropertyAccessExpression. Alias it (`const s = Reflect.set; s(...)`) or
 * reach it by string (`Reflect['set'](...)`) and the callee is no longer that
 * shape, so the same worksheet write goes unreported. Rather than chase every
 * aliasing form through the detector — the unbounded chase this issue was split
 * out to stop — the non-canonical forms are simply not allowed to exist.
 */
function scanNonCanonicalReflective(rel: string, sf: ts.SourceFile): string[] {
  const found: string[] = [];
  const at = (node: ts.Node): number => sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1;
  const visit = (node: ts.Node): void => {
    if (ts.isPropertyAccessExpression(node) && ts.isIdentifier(node.expression)) {
      const methods = REFLECTIVE_POLICY[node.expression.text];
      if (methods?.has(node.name.text)) {
        // Canonical == this access IS the callee of its own call expression.
        const outer = unwrapUp(node);
        const parent = outer.parent;
        if (!(parent && ts.isCallExpression(parent) && parent.expression === outer)) {
          found.push(`${rel}:${at(node)}  ${node.getText(sf)} used outside a canonical call`);
        }
      }
    }
    if (ts.isElementAccessExpression(node) && ts.isIdentifier(node.expression)
        && node.argumentExpression && ts.isStringLiteralLike(node.argumentExpression)) {
      const methods = REFLECTIVE_POLICY[node.expression.text];
      if (methods?.has(node.argumentExpression.text)) {
        found.push(`${rel}:${at(node)}  ${node.getText(sf)} -- use the dot-access call form`);
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  return found;
}

/** Parse a synthetic source for the mutation fixtures (syntax only, no checker). */
function parseFixture(rel: string, source: string): ts.SourceFile {
  return ts.createSourceFile(
    rel, source, ts.ScriptTarget.Latest, true,
    rel.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
}

function allUnlocked(): MutationSite[] {
  const checker = program().getTypeChecker();
  return projectFiles().flatMap(({ rel, sf }) => scanFile(rel, sf, checker));
}

/**
 * Scan a SYNTHETIC source (the negative fixtures) with the SAME detector used
 * on the real tree. Fixture receivers are untyped, which the fail-closed rule
 * treats as Office -- exactly as an unannotated Office object in real code
 * would be.
 */
export function scanSource(relPath: string, source: string): MutationSite[] {
  const fileName = resolve(SRC, relPath).replace(/\\/g, '/');
  const sf = ts.createSourceFile(
    fileName, source, ts.ScriptTarget.Latest, true,
    relPath.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
  const same = (name: string): boolean => name.replace(/\\/g, '/') === fileName;
  const host: ts.CompilerHost = {
    getSourceFile: (name) => (same(name) ? sf : undefined),
    writeFile: () => {},
    getDefaultLibFileName: () => 'lib.d.ts',
    useCaseSensitiveFileNames: () => false,
    getCanonicalFileName: (n) => n.replace(/\\/g, '/'),
    getCurrentDirectory: () => SRC,
    getNewLine: () => '\n',
    getDirectories: () => [],
    fileExists: (name) => same(name),
    readFile: (name) => (same(name) ? source : undefined),
  };
  const prog = ts.createProgram([fileName], { noLib: true, noResolve: true, types: [] }, host);
  // Path canonicalisation differs per platform; fall back to the file we parsed
  // rather than dereferencing an undefined SourceFile.
  const parsed = prog.getSourceFile(fileName) ?? prog.getSourceFiles().find(f => !f.isDeclarationFile) ?? sf;
  return scanFile(relPath, parsed, prog.getTypeChecker());
}

/**
 * Scan a fixture inside a REAL program: default lib + the pinned office-js
 * typings.
 *
 * `scanSource` builds with noLib/noResolve, so EVERY receiver in it is
 * unresolved and the fail-closed rule reports it -- which means it structurally
 * CANNOT reproduce a type-erasure evasion. That blind spot is why four rounds
 * of "pinned as negative fixtures" did not prevent a fifth escape: those
 * fixtures prove the SYNTACTIC half of the detector and almost nothing about
 * `isOfficeReceiver` / `isCellOwningReceiver`, where round 5's escapes lived.
 * Type-sensitive fixtures must use THIS harness.
 */
export function scanTypedSource(
  relPath: string, source: string, extraFiles: Record<string, string> = {},
): MutationSite[] {
  const fileName = toPosix(resolve(SRC, relPath));
  const options: ts.CompilerOptions = {
    target: ts.ScriptTarget.ES2020,
    lib: ['lib.es2020.d.ts', 'lib.dom.d.ts'],
    types: ['office-js'],
    noEmit: true,
    strict: true,
  };
  const base = ts.createCompilerHost(options, true);
  const make = (name: string, text: string): ts.SourceFile => ts.createSourceFile(
    name, text, ts.ScriptTarget.Latest, true,
    name.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
  const sf = make(fileName, source);
  // `extraFiles` makes the harness MULTI-FILE, which is what lets a fixture
  // exercise anything that depends on where an import RESOLVES rather than on
  // how it is spelled -- `isRealLockHelper`'s module check could not be pinned
  // at all while every fixture program contained exactly one file.
  const extras = new Map(Object.entries(extraFiles)
    .map(([p, text]) => [toPosix(resolve(SRC, p)), text] as const));
  const extraDirs = new Set<string>();
  for (const p of extras.keys()) {
    for (let d = p.slice(0, p.lastIndexOf('/')); d.includes('/'); d = d.slice(0, d.lastIndexOf('/'))) {
      extraDirs.add(d);
    }
  }
  const host: ts.CompilerHost = {
    ...base,
    getSourceFile: (name, lang, onErr, shouldCreate) => {
      const key = toPosix(name);
      if (key === fileName) return sf;
      const extra = extras.get(key);
      return extra === undefined ? base.getSourceFile(name, lang, onErr, shouldCreate) : make(key, extra);
    },
    fileExists: (name) => toPosix(name) === fileName || extras.has(toPosix(name)) || base.fileExists(name),
    // Bundler resolution probes the containing directory before the file, so a
    // synthetic module in a directory that does not exist on disk resolves to
    // nothing and its alias silently reads as `unknown`.
    directoryExists: (dir) => extraDirs.has(toPosix(dir)) || (base.directoryExists?.(dir) ?? true),
    readFile: (name) => {
      const key = toPosix(name);
      if (key === fileName) return source;
      return extras.get(key) ?? base.readFile(name);
    },
    getCurrentDirectory: () => resolve(SRC, '..'),
  };
  const prog = ts.createProgram([fileName, ...extras.keys()], options, host);
  return scanFile(relPath, prog.getSourceFile(fileName) ?? sf, prog.getTypeChecker());
}

/** Test-visible Excel.run scan over a synthetic source. */
export function scanExcelRun(relPath: string, source: string): number[] {
  return excelRunLines(ts.createSourceFile(
    resolve(SRC, relPath), source, ts.ScriptTarget.Latest, true,
    relPath.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  ));
}

// ---------------------------------------------------------------------------
// The contract
// ---------------------------------------------------------------------------

describe('Bug-7397 R12 — cell-write contract (typed AST structural guard)', () => {
  it('every Office worksheet mutation outside a lock callback is explicitly justified', () => {
    const unsanctioned = allUnlocked().filter(m => !ALLOWED[m.key]);
    expect(
      unsanctioned.map(m => `${m.file}:${m.line}  [${m.fn}]  ${m.text}`),
      'Office worksheet mutation(s) outside the block-lock contract. This is the '
      + 'wrong-numbers class Bug-7397 exists to close: route it through '
      + 'utils/lockedCellWrite.withPinnedCellWrite, or add its '
      + '<file>::<function>::<statement> key to ALLOWED with a reason satisfying '
      + '(a), (b), (c) or (d).\n'
      + 'Two cases below are NOT worksheet writes and have different remedies. '
      + '(i) The receiver is `any` (e.g. a JSON.parse result), so the detector '
      + 'cannot tell it from a Range and fails closed -- annotate or narrow the '
      + 'type at the call site rather than allowlisting it. (ii) The call '
      + 'mutates an Office OBJECT and no cells (an event registration such as '
      + '`sheet.onChanged.add`, a chart or named item) -- that is reason (d), so '
      + 'add an ALLOWED entry saying which object it touches.\n'
      + 'Two more cases are CORRECT code the static rule cannot see through, and '
      + 'for both the remedy is to RESTRUCTURE -- do NOT allowlist them, because '
      + 'an ALLOWED entry is a permanent unconditional sanction that would also '
      + 'cover the day the code stops being correct. (iii) A lock body hoisted '
      + 'into a variable (`const body = async () => {...}; withTableLocksKeys(k, '
      + 'body)`): the same const could be called outside a lock too, so inline '
      + 'the callback at the lock call. (iv) A local helper defined inside a lock '
      + 'body and called from it: inline the helper, or move it outside and call '
      + 'it with the pinned coordinates.',
    ).toEqual([]);
  });

  it('the allowlist has not rotted (every entry still matches a real site)', () => {
    // A stale entry silently widens the contract, so it must fail too.
    const keys = new Set(allUnlocked().map(m => m.key));
    const stale = Object.keys(ALLOWED).filter(k => !keys.has(k));
    expect(stale, 'Allowlist entries no longer matching any code -- delete them.').toEqual([]);
  });

  it('one allowlist entry sanctions exactly the number of occurrences it was granted', () => {
    // The reasons are per-SITE ("brand-new uniquely-named sheet"). Because
    // ALLOWED is a lookup, a SECOND identical statement in the same function --
    // after the variable has been repointed at a user range -- would inherit a
    // justification that no longer holds, at zero cost. Counting occurrences
    // makes a new site show up even when its text matches an existing one.
    const counts = new Map<string, number>();
    for (const m of allUnlocked()) counts.set(m.key, (counts.get(m.key) ?? 0) + 1);
    const over = [...counts.entries()]
      .filter(([k, n]) => ALLOWED[k] && n > (ALLOWED_OCCURRENCES[k] ?? 1))
      .map(([k, n]) => `${k}  (${n} occurrences, ${ALLOWED_OCCURRENCES[k] ?? 1} sanctioned)`);
    expect(
      over,
      'An allowlisted statement now appears more times than were audited. Review the '
      + 'NEW occurrence on its own merits, then raise its ALLOWED_OCCURRENCES count.',
    ).toEqual([]);
  });

  it('EVERY bundlable source file under src/ is inspected (no directory or extension escapes it)', () => {
    // Round 5 put an unlocked Excel.run cell write into `src/utils/__tests__/`
    // and another into a `.mts` file. Neither was scanned: the walker skipped
    // any directory literally named `__tests__` at ANY depth, and matched only
    // `.ts`/`.tsx`. Both compile under tsc and bundle under vite -- they were
    // live code the contract never looked at. This asserts the file-set is
    // TOTAL against an independent sweep, so it can never silently narrow.
    const truth: string[] = [];
    const sweep = (dir: string): void => {
      for (const entry of readdirSync(dir)) {
        const full = join(dir, entry);
        if (statSync(full).isDirectory()) {
          if (entry === 'node_modules') continue;
          sweep(full);
        } else if (BUNDLABLE.test(entry) && !TEST_FILE.test(entry) && !entry.endsWith('.d.ts')) {
          truth.push(relative(SRC, full).split(sep).join('/'));
        }
      }
    };
    sweep(SRC);
    const scanned = new Set(filesUnderContract());
    expect(
      // The one top-level suite directory is the only sanctioned exemption.
      truth.filter(f => !scanned.has(f) && !f.startsWith('__tests__/')),
      'Bundlable source file(s) the cell-write contract never inspects. An unlocked '
      + 'cell writer in one of these is invisible to every assertion in this file.',
    ).toEqual([]);
  });

  it('Excel.run may only be opened by an approved module (bounds where a cell write can be introduced at all)', () => {
    const offenders: string[] = [];
    for (const { rel, sf } of projectFiles()) {
      if (EXCEL_RUN_MODULES.has(rel)) continue;
      for (const line of excelRunLines(sf)) offenders.push(`${rel}:${line}`);
    }
    expect(
      offenders,
      'Excel.run outside the approved modules. Components and new utilities must go '
      + 'through the useExcel hook, whose writers hold the covering cell blocks.',
    ).toEqual([]);
  });

  it('POLICY (Bug-8690 F1): no bare `any` type annotation in plugin production code', () => {
    const violations = projectFiles().flatMap(({ rel, sf }) => scanBareAnyAnnotations(rel, sf));
    expect(violations,
      'Bare `any` type annotation in excel-plugin production code. The cell-write '
      + "contract's value-form detector (isMutatingMemberValue) drops fail-closed "
      + 'treatment for `any` receivers to avoid noise, so an `any`-typed binding '
      + 'reopens the aliased-write route. Narrow to the concrete type, or use '
      + '`unknown` and type-guard at the boundary.',
    ).toEqual([]);
  });

  it('POLICY (Bug-8690 F2): reflective APIs only in the canonical dot-access call form', () => {
    const violations = projectFiles().flatMap(({ rel, sf }) => scanNonCanonicalReflective(rel, sf));
    expect(violations,
      'Reflective API used in a non-canonical form in excel-plugin production code. '
      + "Aliasing (`const s = Reflect.set; s(...)`) and bracket access "
      + "(`Reflect['set'](...)`) both evade the contract's Rule 2, which matches on the "
      + 'call callee. Use `Reflect.set(...)` / `Object.assign(...)` directly.',
    ).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// Bug-8690 F1/F2 — MUTATION PROOF.
//
// The two policy assertions above are green on a clean tree and always will be.
// These fixtures introduce the forbidden shapes and require the same scanners
// to report them, so the guards are demonstrably alive rather than decorative.
// The controls beside each one keep the scanners from being satisfied by
// reporting everything.
// ---------------------------------------------------------------------------

describe('Bug-8690 — the policy guards actually fire on the shapes they forbid', () => {
  it('F1: every bare `any` annotation form is reported', () => {
    const forms: [string, string][] = [
      ['variable', 'export function f() { const r: any = getRange(); r.values = [[1]]; return r; }'],
      ['parameter', 'export function f(r: any) { r.values = [[1]]; }'],
      ['function return', 'export function f(): any { return getRange(); }'],
      ['arrow return', 'export const f = (): any => getRange();'],
      ['class property', 'export class C { r: any = getRange(); }'],
      ['interface member', 'export interface Sink { values: any }'],
      ['method return', 'export class C { m(): any { return getRange(); } }'],
    ];
    for (const [label, source] of forms) {
      expect(
        scanBareAnyAnnotations('utils/fixture.ts', parseFixture('utils/fixture.ts', source)),
        `F1 must report a bare \`any\` in the ${label} position`,
      ).toHaveLength(1);
    }
  });

  it('F1 CONTROL: `unknown`, concrete types and inferred types are not reported', () => {
    const legal = [
      'export function f(r: unknown) { return r; }',
      'export function f(r: Excel.Range) { r.values = [[1]]; }',
      'export function f(): Promise<string[]> { return Promise.resolve([]); }',
      'export const f = (r: Record<string, unknown>) => r;',
      // An `any` inside a TYPE ARGUMENT is not a bare annotation on a binding.
      // Reporting it would be noise, and it cannot make a receiver `any`.
      'export function f(r: Map<string, any>) { return r; }',
    ];
    for (const source of legal) {
      expect(
        scanBareAnyAnnotations('utils/fixture.ts', parseFixture('utils/fixture.ts', source)),
        `F1 must not report: ${source}`,
      ).toEqual([]);
    }
  });

  it('F2: aliased and bracket-accessed reflective writes are reported', () => {
    const forms: [string, string][] = [
      ['Reflect.set aliased to a const', "export function f(r: object, v: unknown[][]) { const s = Reflect.set; s(r, 'values', v); }"],
      ['Reflect bracket access', "export function f(r: object, v: unknown[][]) { Reflect['set'](r, 'values', v); }"],
      ['Object.assign aliased', 'export function f(r: object, v: unknown[][]) { const a = Object.assign; a(r, { values: v }); }'],
      ['Object bracket access', "export function f(r: object, v: unknown[][]) { Object['assign'](r, { values: v }); }"],
      ['reflective member passed as a callback', 'export function f(rs: object[]) { rs.forEach(Object.assign); }'],
      ['defineProperty aliased', "export function f(r: object) { const d = Reflect.defineProperty; d(r, 'values', {}); }"],
    ];
    for (const [label, source] of forms) {
      expect(
        scanNonCanonicalReflective('utils/fixture.ts', parseFixture('utils/fixture.ts', source)),
        `F2 must report ${label}`,
      ).toHaveLength(1);
    }
  });

  it('F2 CONTROL: the canonical call form and unrelated members are not reported', () => {
    const legal = [
      "export function f(r: object, v: unknown[][]) { Reflect.set(r, 'values', v); }",
      'export function f(r: object, v: object) { Object.assign(r, v); }',
      // Casts around the canonical callee are still canonical.
      "export function f(r: object, v: unknown[][]) { (Reflect.set as typeof Reflect.set)(r, 'values', v); }",
      // Members outside the policy set, and same-named members of other objects.
      'export function f(r: object) { return Object.keys(r); }',
      'export function f(m: { set: (k: string) => void }) { const s = m.set; s("k"); }',
    ];
    for (const source of legal) {
      expect(
        scanNonCanonicalReflective('utils/fixture.ts', parseFixture('utils/fixture.ts', source)),
        `F2 must not report: ${source}`,
      ).toEqual([]);
    }
  });

  it('both scanners cover the SAME file set the rest of the contract inspects', () => {
    // A policy that silently stopped scanning would also pass forever. Pin the
    // scope to `filesUnderContract()` so a narrowing is a visible test change.
    const files = filesUnderContract();
    expect(files.length).toBeGreaterThan(20);
    expect(projectFiles().map(f => f.rel).sort()).toEqual([...files].sort());
    expect(files.some(f => f.startsWith('__tests__/'))).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Negative fixtures: every shape that has ACTUALLY escaped a previous round.
// Without these, a future edit could blunt the detector and every assertion
// above would still pass on a clean tree.
// ---------------------------------------------------------------------------

describe('Bug-7397 R12 — the contract guard actually detects violations', () => {
  const ESCAPED_BEFORE: { name: string; source: string }[] = [
    {
      name: 'round 1: a raw Excel.run cell write in a component',
      source: `await Excel.run(async (context) => {\n  const cell = sheet.getRange('A1');\n  cell.values = [['x']];\n});`,
    },
    {
      name: 'round 2: a NEW unlocked writer in the hook that holds every other writer',
      source: `export async function smuggleProbeA() {\n  await Excel.run(async (context) => {\n    sheet.getRangeByIndexes(0, 0, 1, 1).values = [['smuggled']];\n  });\n}`,
    },
    { name: 'round 2: copyFrom instead of a values assignment', source: `range.copyFrom(other, Excel.RangeCopyType.values);` },
    { name: 'round 2: valuesAsJson instead of values', source: `range.valuesAsJson = [[{ type: 'String', basicValue: 'x' }]];` },
    { name: 'round 2: Object.assign onto a range', source: `Object.assign(range, { values: [['x']] });` },
    {
      name: 'round 3 S1: an unlocked clear() wiping 10,000 user cells',
      source: `sheet.getRangeByIndexes(0, 0, 500, 20).clear(Excel.ClearApplyTo.contents);`,
    },
    {
      name: 'round 3 S2: a values assignment split across physical lines',
      source: `range\n  .values\n  = [['smuggled']];`,
    },
    {
      name: 'round 3 S4: a COMMENT containing withPinnedCellWrite( manufacturing a lock span',
      source: `rows.forEach((row, i) => {\n  // TODO: eventually route through withPinnedCellWrite(\n  sheet.getRangeByIndexes(i, 0, 1, row.length).values = [row];\n});`,
    },
    { name: 'round 3 S5: a static bracket access instead of a property', source: `range['values'] = [['x']];` },
    { name: 'round 3 S7: table.resize() reshaping which cells the table owns', source: `table.resize(sheet.getRangeByIndexes(0, 0, 50, 4));` },
    { name: 'a row insert shifting cells below it', source: `table.rows.add(null, [[1, 2]]);` },
    { name: 'a range delete shifting cells up', source: `range.delete(Excel.DeleteShiftDirection.up);` },
  ];

  it.each(ESCAPED_BEFORE)('flags $name', ({ source }) => {
    expect(scanSource('utils/fixture.ts', source).length).toBeGreaterThan(0);
  });

  it('round 3 S6: an allowlisted statement reused in a DIFFERENT function does NOT inherit that allowance', () => {
    // The allowlist grants `insertResultTable::range.values = allData`. The same
    // text in another function must produce a DIFFERENT key, so it stays
    // unsanctioned rather than inheriting someone else's justification.
    const sites = scanSource(
      'utils/officeSpike.ts',
      `export function reviewProbeDupKey(range, allData) {\n  range.values = allData;\n}`,
    );
    expect(sites).toHaveLength(1);
    expect(sites[0].key).toBe('utils/officeSpike.ts::reviewProbeDupKey::range.values = allData');
    expect(ALLOWED[sites[0].key]).toBeUndefined();
  });

  it('does NOT flag a mutation inside a lock callback body (no false positives)', () => {
    const locked = `await withPinnedCellWrite(target, size, async ({ sheet }) => {\n`
      + `  sheet.getRangeByIndexes(0, 0, 1, 1).values = [['ok']];\n`
      + `  sheet.getRangeByIndexes(1, 0, 1, 1).clear();\n`
      + `  return true;\n});`;
    expect(scanSource('hooks/useExcel.ts', locked)).toEqual([]);

    const multi = `return withTableLocksKeys(blockKeys, async () => {\n  range.values = [['ok']];\n});`;
    expect(scanSource('hooks/useExcel.ts', multi)).toEqual([]);
  });

  it('a lock helper NAME appearing in a string or comment does not create a lock span', () => {
    const fake = `const doc = 'call withPinnedCellWrite(target, size, fn) to write';\n`
      + `range.values = [['not actually locked']];`;
    expect(scanSource('hooks/useExcel.ts', fake).length).toBe(1);
  });

  it('a NON-callback argument to a lock helper does not open a span (only the function body counts)', () => {
    const src = `withTableLocksKeys(computeKeys(range.values = [['x']]), fn);`;
    expect(scanSource('hooks/useExcel.ts', src).length).toBe(1);
  });

  it('a plain JS collection is NOT reported (the type check is what keeps the report meaningful)', () => {
    // `Set.add` / `Map.clear` share method names with Office mutations. If this
    // regressed to a name-only match the report would fill with noise and the
    // real violations would be lost in it.
    const src = `const seen = new Set<string>();\nseen.add('x');\n`
      + `const cache = new Map<string, number>();\ncache.clear();\ncache.delete('x');`;
    const sf = ts.createSourceFile(resolve(SRC, 'utils/f.ts'), src, ts.ScriptTarget.Latest, true);
    const host: ts.CompilerHost = {
      ...ts.createCompilerHost({ target: ts.ScriptTarget.Latest, lib: ['lib.es2015.d.ts'] }),
      getSourceFile: (name, lang) => (name === sf.fileName
        ? sf
        : ts.createCompilerHost({ target: ts.ScriptTarget.Latest }).getSourceFile(name, lang)),
    };
    const prog = ts.createProgram([sf.fileName], { target: ts.ScriptTarget.ES2020, lib: ['lib.es2015.d.ts'], types: [] }, host);
    const found = scanFileForTest('utils/f.ts', prog.getSourceFile(sf.fileName)!, prog.getTypeChecker());
    expect(found).toEqual([]);
  });

  it('round 4 S-A: an allowlisted VARIABLE-initialiser statement in a DIFFERENT function does NOT inherit that allowance', () => {
    // ALLOWED grants insertChart's own `sheet.tables.add(dataRange, true)` for
    // the sheet it just created. Keying by the VARIABLE (`table`) let any other
    // function binding the same variable name inherit the sanction -- round 4
    // put a table over 10,000 user cells through exactly that hole.
    const sites = scanSource(
      'hooks/useExcel.ts',
      `export async function smuggleTableOverUserCells(sheet, dataRange) {\n`
      + `  const table = sheet.tables.add(dataRange, true);\n  return table;\n}`,
    );
    expect(sites).toHaveLength(1);
    expect(sites[0].fn).toBe('smuggleTableOverUserCells');
    expect(ALLOWED[sites[0].key]).toBeUndefined();
  });

  const ROUND_4_ESCAPES: { name: string; source: string }[] = [
    { name: 'round 4 S-B: Range.moveTo relocates cells and wipes the destination', source: `range.moveTo(sheet.getRangeByIndexes(0, 30, 1000, 26));` },
    { name: 'round 4 S-C1: Range.replaceAll rewrites cell contents', source: `range.replaceAll('old', 'NEW', { completeMatch: false, matchCase: false });` },
    { name: 'round 4 S-C2: Range.autoFill writes into the destination range', source: `range.autoFill(dest, Excel.AutoFillType.fillDefault);` },
    { name: 'round 4 S-C3: Range.removeDuplicates deletes user rows', source: `range.removeDuplicates([0], true);` },
    { name: 'round 4 S-D1: a logical assignment is still an assignment', source: `range.values ||= [['smuggled']];` },
    { name: 'round 4 S-D2: nullish assignment to values', source: `range.values ??= [['smuggled']];` },
    { name: 'round 4 S-K: hyperlink.textToDisplay overwrites the visible cell text', source: `range.hyperlink = { address: 'http://x', textToDisplay: 'OVERWRITTEN' };` },
  ];

  it.each(ROUND_4_ESCAPES)('flags $name', ({ source }) => {
    expect(scanSource('utils/fixture.ts', source).length).toBeGreaterThan(0);
  });

  it('round 4 S-H: a LOCALLY declared function named like a lock helper does not open a lock span', () => {
    // Round 3 closed the COMMENT form of this. A real shadowing identifier is
    // the same defect: the span must come from the RESOLVED helper, not a name.
    const src = `function withTableLocksKeys(k, f) { return f(); }\n`
      + `withTableLocksKeys(null, async () => { range.values = [['unlocked']]; });`;
    expect(scanSource('utils/fixture.ts', src).length).toBe(1);
  });

  it('round 4 S-G: a closure CREATED inside a lock callback but invoked after release is not locked', () => {
    // Lexically inside, dynamically outside: the arrow is returned from the
    // critical section and called once the lock has already been released.
    const src = `const later = await withTableLocksKeys(['k'], async () => () => {\n`
      + `  sheet.getRangeByIndexes(0, 0, 5, 2).values = [['late']];\n});\nlater();`;
    expect(scanSource('hooks/useExcel.ts', src).length).toBe(1);
  });

  it('round 4 S-I: a locally-declared type that structurally carries cell data is still a Range', () => {
    // Casting an Office Range through `interface Cellish { values: unknown[][] }`
    // used to silence the type check entirely.
    const src = `interface Cellish { values: unknown[][] }\n`
      + `export function disguised(c: Cellish) { c.values = [['x']]; }`;
    expect(scanSource('utils/fixture.ts', src).length).toBe(1);
  });

  // --- round 5: TYPE-ERASURE escapes, on a REAL typed program ----------------
  // These MUST use scanTypedSource. Under scanSource every one of them passes
  // for the WRONG reason (unresolved receiver -> fail-closed), which is exactly
  // how they reached the real tree past four rounds of negative fixtures.
  it('round 5 harness control: a genuine Excel.Range write IS reported, and a Set/Map is NOT', () => {
    expect(scanTypedSource('utils/f.ts',
      `export function f(r: Excel.Range, rows: unknown[][]) { r.values = rows as never; }`)).toHaveLength(1);
    expect(scanTypedSource('utils/f.ts',
      `const s = new Set<string>(); s.add('x');\nconst m = new Map<string, number>(); m.clear(); m.delete('x');`)).toEqual([]);
  });

  it('round 5 S-A: casting an Office range through a narrower local type does not erase it', () => {
    // `(range as unknown as CellSink).values = rows` -- narrowing the DECLARED
    // type is not narrowing the OBJECT.
    expect(scanTypedSource('utils/f.ts',
      `interface CellSink { values: unknown }\n`
      + `export function f(r: Excel.Range, rows: unknown[][]) { (r as unknown as CellSink).values = rows; }`,
    ).length).toBeGreaterThan(0);
  });

  it('round 5 S-B: an index-signature type can carry `values`, so it is treated as a range', () => {
    // `Record<string, unknown>` has no NAMED `values` property, so
    // type.getProperty('values') is undefined and the receiver read as non-Office.
    expect(scanTypedSource('utils/f.ts',
      `function opaque(x: unknown): Record<string, unknown> { return x as Record<string, unknown>; }\n`
      + `export function f(r: Excel.Range, rows: unknown[][]) { opaque(r).values = rows; }`,
    ).length).toBeGreaterThan(0);
  });

  it('round 5 S-C: Reflect.set invokes the same office-js setter as `range.values =`', () => {
    expect(scanTypedSource('utils/f.ts',
      `export function f(r: Excel.Range, rows: unknown[][]) { Reflect.set(r as unknown as object, 'values', rows); }`,
    ).length).toBeGreaterThan(0);
  });

  it('round 5 S-C2: Object.defineProperty onto an Office receiver is not a free pass', () => {
    expect(scanTypedSource('utils/f.ts',
      `export function f(r: Excel.Range, rows: unknown[][]) { Object.defineProperty(r, 'values', { value: rows }); }`,
    ).length).toBeGreaterThan(0);
  });

  it('round 5 S-D: aliasing the Excel NAMESPACE still opens an Excel.run', () => {
    // Round 4 closed aliasing the FUNCTION (`const r = Excel.run`). Aliasing the
    // NAMESPACE was left open -- and with it the bound this module's docstring
    // relies on to contain runtime-computed property writes: a component could
    // open a host batch the allowlist never sees.
    expect(scanExcelRun('components/X.tsx', `const host = Excel;\nawait host.run(async (c) => {});`).length).toBeGreaterThan(0);
    expect(scanExcelRun('components/X.tsx', `await globalThis.Excel.run(async (c) => {});`).length).toBe(1);
    expect(scanExcelRun('components/X.tsx', `await window.Excel.run(async (c) => {});`).length).toBe(1);
    // NOTE: wrapped in an async function on purpose -- a top-level `await (x)`
    // parses as a CALL to an identifier named `await`, which would make this
    // fixture test the parser rather than the guard.
    expect(scanExcelRun('components/X.tsx',
      `async function f() { await (Excel as never).run(async (c) => {}); }`).length).toBe(1);
    // ...and still no false positive on an unrelated .run().
    expect(scanExcelRun('components/X.tsx', `runner.run(async () => {});`).length).toBe(0);
  });

  it('round 4 S-E: an ALIASED or DESTRUCTURED Excel.run is still an Excel.run', () => {
    // `const r = Excel.run` / `const { run } = Excel` bypassed the module
    // allowlist entirely, letting a component open an unlocked host batch.
    expect(scanExcelRun('components/X.tsx', `const r = Excel.run;\nawait r(async (c) => {});`).length).toBe(1);
    expect(scanExcelRun('components/X.tsx', `const { run } = Excel;\nawait run(async (c) => {});`).length).toBe(1);
  });

  it('scanExcelRun finds an Excel.run regardless of formatting, and does not fire on an unrelated .run()', () => {
    expect(scanExcelRun('panels/X.tsx', `await Excel\n  .run(async (c) => {});`).length).toBe(1);
    expect(scanExcelRun('panels/X.tsx', `runner.run(async () => {});`).length).toBe(0);
  });

  // --- R8 / gate 5: INDIRECTION between the object and the mutation ----------
  // Detection used to fire only on a literal dot-access callee, so every one of
  // these reached `[]` -- reproduced on this exact harness before the fix. They
  // are all ONE act (taking a mutating member off an Office object) wearing
  // different syntax, which is why the detector now reports the ACQUISITION.
  const R8_INDIRECTION: { name: string; source: string }[] = [
    { name: 'gate 5 I-1: computed bracket call', source: `export function f(r: Excel.Range) { r['clear'](); }` },
    {
      name: 'gate 5 I-2: a bracket key whose literal moved one line up',
      source: `const K = 'clear' as const;\nexport function f(r: Excel.Range) { r[K](); }`,
    },
    { name: 'gate 5 I-3: Function.prototype.call', source: `export function f(r: Excel.Range) { r.clear.call(r); }` },
    { name: 'gate 5 I-4: Function.prototype.apply', source: `export function f(r: Excel.Range) { r.clear.apply(r, []); }` },
    { name: 'gate 5 I-5: Function.prototype.bind, invoked later', source: `export function f(r: Excel.Range) { const g = r.clear.bind(r); g(); }` },
    { name: 'gate 5 I-6: destructured method', source: `export function f(r: Excel.Range) { const { clear } = r; clear(); }` },
    { name: 'gate 5 I-7: destructuring ASSIGNMENT', source: `export function f(r: Excel.Range) { let clear; ({ clear } = r); clear(); }` },
    { name: 'gate 5 I-8: plain alias, invoked later', source: `export function f(r: Excel.Range) { const g = r.clear; g(); }` },
    { name: 'gate 5 I-9: method reference handed to something else', source: `export function f(r: Excel.Range, a: string[]) { a.forEach(r.clear); }` },
    { name: 'gate 5 I-10: Reflect.get hands out the same method object', source: `export function f(r: Excel.Range) { (Reflect.get(r, 'clear') as () => void)(); }` },
    { name: 'gate 5 I-11: Reflect.apply', source: `export function f(r: Excel.Range) { Reflect.apply(r.clear, r, []); }` },
    {
      name: 'gate 5 I-12: a DATA write through a bracket key declared elsewhere',
      source: `const K = 'values' as const;\nexport function f(r: Excel.Range, rows: unknown[][]) { r[K] = rows as never; }`,
    },
    {
      name: 'gate 5 I-13: an UNCLASSIFIED member of a resolved Range, aliased rather than called',
      // Fail-closed still holds through indirection: a future office-js mutating
      // API cannot be laundered past the guard by taking a reference to it.
      source: `export function f(r: Excel.Range) { const g = (r as never as { newHostMutator(): void }).newHostMutator; g(); }`,
    },
  ];

  it.each(R8_INDIRECTION)('flags $name', ({ source }) => {
    expect(scanTypedSource('utils/f.ts', source).length).toBeGreaterThan(0);
  });

  it('R8 control: reading a NON-mutating member of a real Range is not reported', () => {
    // The indirection rule must not turn every property read into a finding --
    // a report nobody can act on is a report nobody reads.
    expect(scanTypedSource('utils/f.ts',
      `export function f(r: Excel.Range, t: Excel.Table) {\n`
      + `  const a = r.address; const n = r.rowCount; const fmt = r.format; const rows = t.rows;\n`
      + `  const v = r.values; const load = r.load; return [a, n, fmt, rows, v, load];\n}`,
    )).toEqual([]);
    // ...and an `any` object that merely owns similarly-named fields is not a Range.
    expect(scanTypedSource('utils/f.ts',
      `export function f() { const g = globalThis as any; const s = g.OfficeRuntime.storage; return s; }`,
    )).toEqual([]);
  });

  // --- R8 / gate 5: DEFERRED execution inside a lock callback ----------------
  // `runsInPlace` treated "passed as an argument to any call" as proof of
  // synchronous execution. It is not. Each of these is lexically inside
  // `withTableLocksKeys(...)` and dynamically AFTER it releases; each scanned
  // as protected before the fix.
  const R8_DEFERRED: { name: string; call: string }[] = [
    { name: 'gate 5 D-1: setTimeout', call: `setTimeout(() => { range.values = [['late']]; }, 0);` },
    { name: 'gate 5 D-2: a promise continuation', call: `p.then(() => { range.values = [['late']]; });` },
    { name: 'gate 5 D-3: queueMicrotask', call: `queueMicrotask(() => { range.values = [['late']]; });` },
    { name: 'gate 5 D-4: requestAnimationFrame', call: `requestAnimationFrame(() => { range.values = [['late']]; });` },
    { name: 'gate 5 D-5: an event handler registration', call: `el.addEventListener('click', () => { range.values = [['late']]; });` },
    { name: 'gate 5 D-6: an Office event handler registration', call: `sheet.onChanged.add(async () => { range.values = [['late']]; });` },
    { name: 'gate 5 D-7: a callback stored on an object', call: `register({ onDone: () => { range.values = [['late']]; } });` },
    { name: 'gate 5 D-8: an un-awaited async IIFE', call: `(async () => { range.values = [['late']]; })();` },
  ];

  it.each(R8_DEFERRED)('flags $name nested inside a lock callback', ({ call }) => {
    const src = `await withTableLocksKeys(['k'], async () => {\n  ${call}\n});`;
    expect(scanSource('hooks/useExcel.ts', src).length).toBe(1);
  });

  it('gate 5 D-9: a `.forEach` on something that is NOT an array proves nothing about timing', () => {
    // The in-place allowlist is keyed on the ARRAY CONTRACT, not on the method
    // name: `Array.prototype.forEach` is synchronous by specification, an
    // arbitrary object's `forEach` is whatever its author wrote. Without the
    // receiver-type check this fixture scans clean while the write lands after
    // the lock releases -- the D-1 defect with one more indirection.
    const deferred = `class Deferred { forEach(cb: () => void) { setTimeout(cb, 0); } }\n`;
    const body = (recv: string) => `export async function f(d: Deferred, a: string[], r: Excel.Range) {\n`
      + `  await withTableLocksKeys(['k'], async () => {\n`
      + `    ${recv}.forEach(() => { r.values = [['late']] as never; });\n  });\n}`;
    expect(scanTypedSource('hooks/useExcel.ts', deferred + body('d')).length).toBe(1);
    // ...and the genuine array receiver is still not a false positive.
    expect(scanTypedSource('hooks/useExcel.ts', deferred + body('a'))).toEqual([]);
  });

  it('R8 control: callbacks that PROVABLY run inside the lock are still not reported', () => {
    const inPlace = [
      `await Excel.run(async (ctx) => { range.values = [['ok']]; });`,
      `await (async () => { range.values = [['ok']]; })();`,
      `await withTableLocksKeys(['inner'], async () => { range.values = [['ok']]; });`,
    ];
    for (const body of inPlace) {
      expect(scanSource('hooks/useExcel.ts', `await withTableLocksKeys(['k'], async () => {\n  ${body}\n});`),
      ).toEqual([]);
    }
    // Array iteration must be judged on a REAL array type, so these two go
    // through the typed harness; an untyped receiver is no longer proof (D-15).
    for (const iter of [
      `rows.forEach((row, i) => { r.getCell(i, 0).values = [[row]] as never; });`,
      `const out = rows.map((row, i) => { r.getCell(i, 0).values = [[row]] as never; return i; });`,
    ]) {
      expect(scanTypedSource('hooks/useExcel.ts',
        `export async function f(rows: string[], r: Excel.Range) {\n`
        + `  await withTableLocksKeys(['k'], async () => {\n    ${iter}\n  });\n}`)).toEqual([]);
    }
    // The real production shape: `withTableLocksKeys(keys, () => Excel.run(...))`
    // -- a concise arrow body is an implicit return, so the batch is joined.
    expect(scanSource('utils/lockedCellWrite.ts',
      `await withTableLocksKeys(keys, () => Excel.run(async (context) => {\n`
      + `  context.workbook.worksheets.getItem(name).getRangeByIndexes(0, 0, 1, 1).values = [['ok']];\n`
      + `}));`,
    )).toEqual([]);
  });

  // --- R9: DEFERRAL THROUGH A SUSPENDING ITERATION CALLBACK ------------------
  // `Array.prototype.forEach` drives an `async` callback to its FIRST `await`,
  // not to completion, and never joins the promise it returns. Accepting "array
  // iteration on a real array" as timing-knowable is therefore only true for a
  // synchronous callback -- the gate-5 D-1 defect with one more indirection.
  const R9_DEFERRED_ITERATION: { name: string; body: string }[] = [
    {
      name: 'R9 D-10: an async forEach callback continues after its first await',
      body: `rows.forEach(async (row) => { await ctx.sync(); r.values = [[row]] as never; });`,
    },
    {
      name: 'R9 D-11: an async map callback whose promises nobody joins',
      body: `rows.map(async (row) => { await ctx.sync(); r.values = [[row]] as never; });`,
    },
  ];

  it.each(R9_DEFERRED_ITERATION)('flags $name', ({ body }) => {
    const src = `export async function f(rows: string[], r: Excel.Range, ctx: Excel.RequestContext) {\n`
      + `  await withTableLocksKeys(['k'], async () => {\n    ${body}\n  });\n}`;
    expect(scanTypedSource('hooks/useExcel.ts', src).length).toBe(1);
  });

  it('R9 control: a JOINED async iteration, and a synchronous one, stay unreported', () => {
    const lock = (body: string) =>
      `export async function f(rows: string[], r: Excel.Range, ctx: Excel.RequestContext) {\n`
      + `  await withTableLocksKeys(['k'], async () => {\n    ${body}\n  });\n}`;
    const asyncWrite = `async (row) => { await ctx.sync(); r.values = [[row]] as never; }`;
    expect(scanTypedSource('hooks/useExcel.ts',
      lock(`await Promise.all(rows.map(${asyncWrite}));`))).toEqual([]);
    // A SPREAD into the combinator's array joins each promise individually.
    expect(scanTypedSource('hooks/useExcel.ts',
      lock(`await Promise.all([...rows.map(${asyncWrite})]);`))).toEqual([]);
    expect(scanTypedSource('hooks/useExcel.ts',
      lock(`rows.forEach((row) => { r.values = [[row]] as never; });`))).toEqual([]);
  });

  it('R9 D-16: Promise.all over an array containing the ITERATION does not join it', () => {
    // `Promise.all([rows.map(f)])` hands the combinator ONE element which is an
    // array, not a promise. Promise.all resolves a non-promise element
    // immediately, so nothing inside it is waited for and the writes still land
    // after the lock releases -- the D-10 defect wearing a joiner's clothes.
    const src = `export async function f(rows: string[], r: Excel.Range, ctx: Excel.RequestContext) {\n`
      + `  await withTableLocksKeys(['k'], async () => {\n`
      + `    await Promise.all([rows.map(async (row) => { await ctx.sync(); r.values = [[row]] as never; })]);\n`
      + `  });\n}`;
    expect(scanTypedSource('hooks/useExcel.ts', src).length).toBe(1);
  });

  it('R9 D-15: a deferring forEach reached through an `any` binding is not proof of timing', () => {
    // D-9 pinned the TYPED form. One `any` in the path restored the escape,
    // because an untyped receiver used to be accepted as array-like.
    const src = `class Deferred { forEach(cb: () => void) { setTimeout(cb, 0); } }\n`
      + `export async function f(r: Excel.Range) {\n`
      + `  const d: any = new Deferred();\n`
      + `  await withTableLocksKeys(['k'], async () => {\n`
      + `    d.forEach(() => { r.values = [['late']] as never; });\n  });\n}`;
    expect(scanTypedSource('hooks/useExcel.ts', src).length).toBe(1);
  });

  // --- R9: A FUNCTION BOUNDARY THAT IS NOT A FUNCTION DECLARATION ------------
  // An accessor or constructor declared inside a lock callback runs whenever the
  // property is read / the class is instantiated, which is after release.
  const R9_ACCESSORS: { name: string; body: string }[] = [
    { name: 'R9 D-12: a getter declared in the lock, evaluated after release', body: `register({ get late() { range.values = [['late']]; return 1; } });` },
    { name: 'R9 D-13: a setter declared in the lock', body: `register({ set late(v) { range.values = [[v]]; } });` },
    { name: 'R9 D-14: a constructor declared in the lock', body: `class Late { constructor() { range.values = [['late']]; } }\n  hold(Late);` },
  ];

  it.each(R9_ACCESSORS)('flags $name', ({ body }) => {
    expect(scanSource('hooks/useExcel.ts',
      `await withTableLocksKeys(['k'], async () => {\n  ${body}\n});`).length).toBe(1);
  });

  // --- R9: ASSIGNMENT TARGET FORMS ------------------------------------------
  // Round 4 enumerated assignment OPERATORS (`=`, `||=`, `??=`). These are the
  // same store with the `=` moved out of the member access's own expression.
  const R9_TARGETS: { name: string; source: string }[] = [
    { name: 'R9 I-14: array-destructuring assignment into values', source: `export function f(r: Excel.Range, rows: unknown[][]) { [r.values] = [rows as never]; }` },
    { name: 'R9 I-15: object-destructuring assignment into values', source: `export function f(r: Excel.Range, o: { v: unknown }) { ({ v: r.values } = o as never); }` },
  ];

  it.each(R9_TARGETS)('flags $name', ({ source }) => {
    expect(scanTypedSource('utils/f.ts', source).length).toBe(1);
  });

  it('R9 I-16: a for-of head writing straight into cells', () => {
    expect(scanSource('utils/f.ts',
      `export function f(r, xs) { for (r.values of xs) { } }`).length).toBe(1);
  });

  it('R9 control: reading values as a destructuring SOURCE is not a write', () => {
    expect(scanTypedSource('utils/f.ts',
      `export function f(r: Excel.Range) { const [first] = r.values as unknown[][]; return first; }`)).toEqual([]);
  });

  it('R9 I-17: a mutating member CAPTURED in the lock and invoked after release', () => {
    // The dual of R8's own inversion: detection keyed on the ACQUISITION asks
    // "was the acquisition locked", which would sanction a method reference
    // taken in the critical section and called once it has released. Round 4
    // S-G caught the closure form of this; the method-reference form did not.
    const escaping = `export async function f(r: Excel.Range) {\n`
      + `  let g: (() => void) | undefined;\n`
      + `  await withTableLocksKeys(['k'], async () => { g = r.clear.bind(r); });\n`
      + `  g!();\n}`;
    expect(scanTypedSource('hooks/useExcel.ts', escaping).length).toBe(1);
    // ...and the RETURNED form.
    expect(scanTypedSource('hooks/useExcel.ts',
      `export async function f(r: Excel.Range) {\n`
      + `  const g = await withTableLocksKeys(['k'], async () => r.clear.bind(r));\n  g();\n}`,
    ).length).toBe(1);
    // Control: a method reference CONSUMED inside the critical section is fine.
    expect(scanTypedSource('hooks/useExcel.ts',
      `export async function f(r: Excel.Range, rows: string[]) {\n`
      + `  await withTableLocksKeys(['k'], async () => { rows.forEach(r.clear); });\n}`,
    )).toEqual([]);
  });

  it('R9 I-18: an ALIASED reflection namespace is still a reflective write', () => {
    // `REFLECTIVE_ACCESS` used to key on the literal identifier `Reflect` /
    // `Object`, so one `const R = Reflect` restored the round 5 S-C escape.
    expect(scanTypedSource('utils/f.ts',
      `const R = Reflect;\n`
      + `export function f(r: Excel.Range, rows: unknown[][]) { R.set(r as unknown as object, 'values', rows); }`,
    ).length).toBeGreaterThan(0);
    expect(scanTypedSource('utils/f.ts',
      `const O = Object;\n`
      + `export function f(r: Excel.Range, rows: unknown[][]) { O.defineProperty(r, 'values', { value: rows }); }`,
    ).length).toBeGreaterThan(0);
    // ...and an unrelated object with a `set` method is not a false positive.
    expect(scanTypedSource('utils/f.ts',
      `export function f(m: Map<string, number>) { m.set('x', 1); }`)).toEqual([]);
  });

  // --- R10: the ACQUISITION rule's own accepting side --------------------------
  // R8 moved detection onto the acquisition; `record()` then asks only "was the
  // acquisition inside a lock". Handing the acquired member to a callee whose
  // timing is not knowable is the pinned D-1..D-6 deferral with the closure
  // removed, so the same closed allowlist has to be applied here too.
  const R10_DEFERRED_REFERENCE: { name: string; call: string }[] = [
    { name: 'R10 D-17: setTimeout on a bound method', call: `setTimeout(r.clear.bind(r), 0);` },
    { name: 'R10 D-18: setTimeout on a bare method reference', call: `setTimeout(r.clear, 0);` },
    { name: 'R10 D-19: queueMicrotask', call: `queueMicrotask(r.clear.bind(r));` },
    { name: 'R10 D-20: a promise continuation', call: `void p.then(r.clear.bind(r));` },
    { name: 'R10 D-21: an event handler registration', call: `el.addEventListener('click', r.clear.bind(r));` },
    { name: 'R10 D-22: an Office event handler registration', call: `sheet.onChanged.add(r.clear.bind(r) as never);` },
    { name: 'R10 D-23: deferred one level down, inside an in-place iteration', call: `rows.forEach(() => setTimeout(r.clear, 0));` },
    { name: 'R10 D-24: stored onto a holder declared outside the lock', call: `Object.assign(holder, { g: r.clear });` },
  ];

  it.each(R10_DEFERRED_REFERENCE)('flags $name', ({ call }) => {
    const src = `export async function f(r: Excel.Range, rows: string[], p: Promise<void>,\n`
      + `    el: HTMLElement, sheet: Excel.Worksheet, holder: { g?: () => void }) {\n`
      + `  await withTableLocksKeys(['k'], async () => { ${call} });\n}`;
    expect(scanTypedSource('hooks/useExcel.ts', src).length).toBeGreaterThan(0);
  });

  it('R10 I-19: the escaping-acquisition check survives one intermediate binding', () => {
    // R9 I-17 pins the direct form. Inserting `const h = ...` between the two
    // halves of that exact fixture reduced it to zero findings.
    expect(scanTypedSource('hooks/useExcel.ts',
      `export async function f(r: Excel.Range) {\n`
      + `  const g: () => void = await withTableLocksKeys(['k'], async () => { const h = r.clear.bind(r); return h; });\n`
      + `  g();\n}`).length).toBe(1);
    expect(scanTypedSource('hooks/useExcel.ts',
      `export async function f(r: Excel.Range) {\n`
      + `  let g: (() => void) | undefined;\n`
      + `  await withTableLocksKeys(['k'], async () => { const h = r.clear.bind(r); g = h; });\n`
      + `  g!();\n}`).length).toBe(1);
    expect(scanTypedSource('hooks/useExcel.ts',
      `export async function f(r: Excel.Range, undoStack: (() => void)[]) {\n`
      + `  await withTableLocksKeys(['k'], async () => { undoStack.push(r.clear.bind(r)); });\n`
      + `  undoStack[0]();\n}`).length).toBeGreaterThan(0);
    // Control: consumed inside the critical section, so nothing leaves.
    expect(scanTypedSource('hooks/useExcel.ts',
      `export async function f(r: Excel.Range) {\n`
      + `  await withTableLocksKeys(['k'], async () => { const h = r.clear.bind(r); h(); });\n}`)).toEqual([]);
  });

  it('R10 I-20: a concise lock body that RETURNS the acquisition inside a literal', () => {
    // The bindings are annotated on purpose: left unannotated they are `any`
    // (the fixture's lock helper is unresolved) and the fixture passes for the
    // fail-closed reason instead of for the escape it is meant to pin.
    expect(scanTypedSource('hooks/useExcel.ts',
      `export async function f(r: Excel.Range) {\n`
      + `  const o: { g: () => void } = await withTableLocksKeys(['k'], async () => ({ g: r.clear.bind(r) }));\n`
      + `  o.g();\n}`).length).toBeGreaterThan(0);
    expect(scanTypedSource('hooks/useExcel.ts',
      `export async function f(r: Excel.Range) {\n`
      + `  const o: (() => void)[] = await withTableLocksKeys(['k'], async () => [r.clear.bind(r)]);\n`
      + `  o[0]();\n}`).length).toBeGreaterThan(0);
  });

  it('R10 D-25: a lock entry point NAME is not a lock entry point (runsInPlace axis)', () => {
    // Round 4 S-H closed this on the lockBodies axis only. `runsInPlace` still
    // matched the NAME, so anything called `withPinnedCellWrite` certified its
    // callback as running inside the outer critical section.
    expect(scanSource('hooks/useExcel.ts',
      `function withPinnedCellWrite(a, b, f) { setTimeout(f, 0); }\n`
      + `await withTableLocksKeys(['k'], async () => {\n`
      + `  withPinnedCellWrite(1, 2, () => { range.values = [['late']]; });\n});`).length).toBe(1);
    expect(scanTypedSource('hooks/useExcel.ts',
      `export async function f(r: Excel.Range, helpers: { withPinnedCellWrite(a: number, b: number, f: () => void): void }) {\n`
      + `  await withTableLocksKeys(['k'], async () => {\n`
      + `    helpers.withPinnedCellWrite(1, 2, () => { r.values = [['late']] as never; });\n  });\n}`).length).toBe(1);
  });

  it('R10 D-26: a per-row host batch nobody joins settles after the lock releases', () => {
    // R9-1 closed the `async` callback spelling via suspends(). A SYNCHRONOUS
    // callback that returns an unjoined promise is the same deferral: the
    // "returned, therefore joined" rule never asked where it was returned TO.
    const lock = (body: string) => `export async function f(rows: string[], r: Excel.Range) {\n`
      + `  await withTableLocksKeys(['k'], async () => {\n    ${body}\n  });\n}`;
    const batch = `Excel.run(async () => { r.values = [[row]] as never; })`;
    expect(scanTypedSource('hooks/useExcel.ts', lock(`rows.map((row) => ${batch});`)).length).toBe(1);
    expect(scanTypedSource('hooks/useExcel.ts', lock(`rows.forEach((row) => { return ${batch}; });`)).length).toBe(1);
    // `Promise.race` settles on the FIRST batch; the rest land after release.
    expect(scanTypedSource('hooks/useExcel.ts', lock(`await Promise.race(rows.map((row) => ${batch}));`)).length).toBe(1);
    expect(scanTypedSource('hooks/useExcel.ts', lock(`void Promise.all(rows.map((row) => ${batch}));`)).length).toBe(1);
    // Control: genuinely joined, and the production shape, stay unreported.
    expect(scanTypedSource('hooks/useExcel.ts', lock(`await Promise.all(rows.map((row) => ${batch}));`))).toEqual([]);
  });

  it('R10 D-27: an instance property initialiser declared in the lock runs at construction', () => {
    // R9-2 added accessors, constructors and static blocks. A property
    // initialiser is deferred by the same argument and was left out.
    expect(scanSource('hooks/useExcel.ts',
      `await withTableLocksKeys(['k'], async () => {\n`
      + `  class Late { p = (range.values = [['late']]); }\n  hold(Late);\n});`).length).toBe(1);
  });

  it('R10 control: a STATIC initialiser runs in place, so it is not a deferral', () => {
    // The instance/static distinction is load-bearing in both directions: a
    // static initialiser is evaluated with the class declaration, inside the
    // critical section. Treating it as deferred would report correct code.
    expect(scanSource('hooks/useExcel.ts',
      `await withTableLocksKeys(['k'], async () => {\n`
      + `  class Now { static p = (range.values = [['ok']]); }\n  hold(Now);\n});`)).toEqual([]);
  });

  // --- R11: the escape walk's own accepting side, now inverted ---------------

  it('R11 D-28: a value-carrying OPERATOR does not consume the acquisition', () => {
    // R10 introduced a carrier list to stop reporting a value CONSUMED in the
    // lock. `??` / `||` / `&&` / `,` / `satisfies` yield an operand UNCHANGED,
    // so they consume nothing -- R9 reported all of these and R10 did not. This
    // is the regression the inversion exists to make structurally impossible.
    const lock = (expr: string) => `export async function f(r: Excel.Range, fallback?: () => void) {\n`
      + `  let g: (() => void) | undefined;\n`
      + `  await withTableLocksKeys(['k'], async () => { ${expr} });\n  g!();\n}`;
    expect(scanTypedSource('hooks/useExcel.ts', lock(`g = fallback ?? r.clear.bind(r);`)).length).toBe(1);
    expect(scanTypedSource('hooks/useExcel.ts', lock(`g = fallback || r.clear.bind(r);`)).length).toBe(1);
    expect(scanTypedSource('hooks/useExcel.ts', lock(`g = r.clear.bind(r) satisfies (() => void);`)).length).toBe(1);
    expect(scanTypedSource('hooks/useExcel.ts',
      `export async function f(r: Excel.Range, fallback?: () => void) {\n`
      + `  const g: () => void = await withTableLocksKeys(['k'], async () => { return fallback ?? r.clear.bind(r); });\n`
      + `  g();\n}`).length).toBe(1);
  });

  it('R11 D-29: an in-place callee RUNS the callback here but hands its RESULT out', () => {
    // `runsInPlace` answers "does the callback run now"; the escape walk needs
    // "is the value consumed here". `map`/`flatMap`/`Excel.run`/a nested lock
    // all run in place but RETURN the callback's value, so the acquisition
    // rides out on the result. Only a DISCARDING in-place callee consumes it.
    const lock = (body: string) => `export async function f(r: Excel.Range, rows: string[]) {\n`
      + `  let g: any;\n  await withTableLocksKeys(['k'], async () => { ${body} });\n  g();\n}`;
    expect(scanTypedSource('hooks/useExcel.ts', lock(`g = rows.map(() => r.clear.bind(r));`)).length).toBe(1);
    expect(scanTypedSource('hooks/useExcel.ts', lock(`g = rows.flatMap(() => [r.clear.bind(r)]);`)).length).toBe(1);
    expect(scanTypedSource('hooks/useExcel.ts', lock(`g = await Excel.run(async () => r.clear.bind(r));`)).length).toBe(1);
    expect(scanTypedSource('hooks/useExcel.ts',
      `export async function f(r: Excel.Range, rows: string[]) {\n`
      + `  const g: (() => void)[] = await withTableLocksKeys(['k'], async () => rows.map(() => r.clear.bind(r)));\n`
      + `  g[0]();\n}`).length).toBe(1);
    // Controls: a value-DISCARDING in-place callee genuinely consumes it.
    expect(scanTypedSource('hooks/useExcel.ts',
      `export async function f(r: Excel.Range, rows: string[]) {\n`
      + `  await withTableLocksKeys(['k'], async () => { rows.forEach(r.clear); });\n}`)).toEqual([]);
    expect(scanTypedSource('hooks/useExcel.ts',
      `export async function f(r: Excel.Range, rows: string[]) {\n`
      + `  await withTableLocksKeys(['k'], async () => { rows.map((x) => { r.clear(); return x; }); });\n}`)).toEqual([]);
  });

  it('R11 D-30: an acquisition ASSIGNED into a body-local binding is followed too', () => {
    // R10-2 followed `const h = <acquisition>`. `let h; h = <acquisition>` is
    // the same escape with the declaration split off, and was not followed.
    expect(scanTypedSource('hooks/useExcel.ts',
      `export async function f(r: Excel.Range) {\n`
      + `  let g: (() => void) | undefined;\n`
      + `  await withTableLocksKeys(['k'], async () => { let h: (() => void) | undefined; h = r.clear.bind(r); g = h; });\n`
      + `  g!();\n}`).length).toBe(1);
    expect(scanTypedSource('hooks/useExcel.ts',
      `export async function f(r: Excel.Range) {\n`
      + `  const g: () => void = await withTableLocksKeys(['k'], async () => {\n`
      + `    let h: (() => void) | undefined; h = r.clear.bind(r); return h!; });\n  g();\n}`).length).toBe(1);
    // ...and through a holder object, which the parent-chain walk could not see.
    expect(scanTypedSource('hooks/useExcel.ts',
      `export async function f(r: Excel.Range) {\n`
      + `  let out: { g?: () => void } | undefined;\n`
      + `  await withTableLocksKeys(['k'], async () => { const box: { g?: () => void } = {}; box.g = r.clear.bind(r); out = box; });\n`
      + `  out!.g!();\n}`).length).toBe(1);
    // Control: assigned to a body-local and consumed there -- nothing leaves.
    expect(scanTypedSource('hooks/useExcel.ts',
      `export async function f(r: Excel.Range) {\n`
      + `  await withTableLocksKeys(['k'], async () => { let h: (() => void) | undefined; h = r.clear.bind(r); h(); });\n}`,
    )).toEqual([]);
  });

  it('R11 D-31: a DESTRUCTURED or REFLECTED acquisition escapes the lock too', () => {
    // The escaping-acquisition check was wired into rule 3 only. Rules 2 and 4
    // recorded unconditionally, so `inLock` suppressed them -- even though
    // `const { clear } = r` (gate 5 I-6) and `Reflect.get` (I-10) are the same
    // act as `r.clear`, which R9 I-17 already pins.
    expect(scanTypedSource('hooks/useExcel.ts',
      `export async function f(r: Excel.Range) {\n`
      + `  let g: (() => void) | undefined;\n`
      + `  await withTableLocksKeys(['k'], async () => { const { clear } = r; g = clear; });\n`
      + `  g!();\n}`).length).toBe(1);
    expect(scanTypedSource('hooks/useExcel.ts',
      `export async function f(r: Excel.Range) {\n`
      + `  const g: () => void = await withTableLocksKeys(['k'], async () => { const { clear } = r; return clear; });\n`
      + `  g();\n}`).length).toBe(1);
    expect(scanTypedSource('hooks/useExcel.ts',
      `export async function f(r: Excel.Range) {\n`
      + `  let g: any; let clear: () => void;\n`
      + `  await withTableLocksKeys(['k'], async () => { ({ clear } = r); g = clear; });\n  g();\n}`).length).toBe(1);
    expect(scanTypedSource('hooks/useExcel.ts',
      `export async function f(r: Excel.Range) {\n`
      + `  let g: (() => void) | undefined;\n`
      + `  await withTableLocksKeys(['k'], async () => { g = Reflect.get(r, 'clear') as () => void; });\n`
      + `  g!();\n}`).length).toBe(1);
  });

  it('R11 I-21: an Office COMMON-API cell writer is a cell write', () => {
    // `Office.Document` / `Office.Binding` own worksheet cells but are not in
    // CELL_OWNING_TYPES, so the fail-closed rule did not reach them -- and they
    // open no Excel.run, so EXCEL_RUN_MODULES did not bound them either. A
    // COMPONENT could write user cells with neither assertion firing.
    expect(scanTypedSource('utils/f.ts',
      `export function f(rows: string[][]) { Office.context.document.setSelectedDataAsync(rows, () => {}); }`,
    ).length).toBe(1);
    expect(scanTypedSource('components/X.tsx',
      `export function paste(rows: string[][]) { Office.context.document.setSelectedDataAsync(rows, () => {}); }`,
    ).length).toBe(1);
    expect(scanTypedSource('utils/f.ts',
      `export function f(b: Office.TableBinding, rows: string[][]) {\n`
      + `  b.setDataAsync(rows, () => {}); b.addRowsAsync(rows, () => {}); b.deleteAllDataValuesAsync(() => {});\n}`,
    ).length).toBe(3);
    // Control: reading the selection, and the settings bag the plugin really
    // uses, are not cell writes.
    expect(scanTypedSource('utils/f.ts',
      `export function f() { return Office.context.document.settings.get('k'); }`)).toEqual([]);
  });

  it('R11 control: an acquisition whose value is DISCARDED has not escaped', () => {
    // The inverted rule reports anything not provably consumed, so the
    // consumption allowlist has to include the plainest consumption there is:
    // an expression statement evaluates the value and throws it away. Without
    // that entry the walk runs on to the callback and reports correct code.
    expect(scanTypedSource('hooks/useExcel.ts',
      `export async function f(r: Excel.Range) {\n`
      + `  await withTableLocksKeys(['k'], async () => { r.clear.bind(r); });\n}`)).toEqual([]);
  });

  it('R11 I-22: the alias walk terminates on a cyclic binding graph', () => {
    // `aliasEscapes` recursed through the binding graph with no visited set; two
    // locals that alias each other overflowed the stack, turning the guard into
    // an uninterpretable crash instead of a finding.
    expect(() => scanTypedSource('hooks/useExcel.ts',
      `export async function f(r: Excel.Range) {\n`
      + `  await withTableLocksKeys(['k'], async () => {\n`
      + `    var h = r.clear;\n    var a = h;\n    var b = a;\n    var a = b;\n  });\n}`)).not.toThrow();
  });

  it('R11 S-J: a same-named helper imported from ANOTHER module does not open a lock span', () => {
    // Round 4 S-H closed the LOCALLY-declared shadow. `isRealLockHelper`
    // accepted any ImportSpecifier, so the shadow just moves one module out -- a
    // future `lockedCellWriteV2`, a test double, or a mis-aimed refactor.
    expect(scanSource('hooks/useExcel.ts',
      `import { withTableLocksKeys } from './notTheLock';\n`
      + `await withTableLocksKeys(['k'], async () => { range.values = [['unlocked']]; });`).length).toBe(1);
    expect(scanSource('hooks/useExcel.ts',
      `import { withPinnedCellWrite } from 'some-vendor-lib';\n`
      + `await withPinnedCellWrite(t, s, async () => { range.values = [['unlocked']]; });`).length).toBe(1);
    // Control: the real import still opens a span.
    expect(scanSource('hooks/useExcel.ts',
      `import { withTableLocksKeys } from '../utils/lockedCellWrite';\n`
      + `await withTableLocksKeys(['k'], async () => { range.values = [['ok']]; });`)).toEqual([]);
  });

  // --- R12: held-ness asked the same way on BOTH axes -------------------------

  it('R12 D-32: an acquisition INVOKED from a deferred position has still left the lock', () => {
    // `consumesInPlace` accepted "invoked here" as consumption without asking
    // whether "here" is still inside the HELD region. R10 reported all of these
    // -- its walk climbed past the invocation to the deferring callee -- so
    // R11's early "consumed" verdict RE-OPENED the D-17..D-23 deferral family
    // behind one binding hop, making the verdict depend on whether the author
    // wrote `registerUndo(restore)` or `registerUndo(() => restore())`. A fix
    // that opens a hole is invisible to mutation testing; only a differential
    // against the previous revision finds it.
    const lock = (body: string, sig: string) =>
      `export async function f(${sig}) {\n`
      + `  await withTableLocksKeys(['k'], async () => { ${body} });\n}`;
    expect(scanTypedSource('hooks/useExcel.ts',
      lock(`const h = r.clear.bind(r); setTimeout(() => h(), 0);`, 'r: Excel.Range')).length).toBe(1);
    expect(scanTypedSource('hooks/useExcel.ts',
      lock(`const h = r.clear.bind(r); el.addEventListener('click', () => h());`,
        'r: Excel.Range, el: HTMLElement')).length).toBe(1);
    expect(scanTypedSource('hooks/useExcel.ts',
      lock(`const h = r.clear.bind(r); sheet.onChanged.add(async () => { h(); });`,
        'r: Excel.Range, sheet: Excel.Worksheet')).length).toBe(1);
    expect(scanTypedSource('hooks/useExcel.ts',
      lock(`const { clear } = r; setTimeout(() => clear(), 0);`, 'r: Excel.Range')).length).toBe(1);
    expect(scanTypedSource('hooks/useExcel.ts',
      lock(`const h = r.clear.bind(r); void (async () => { await p; h(); })();`,
        'r: Excel.Range, p: Promise<void>')).length).toBe(1);
    expect(scanTypedSource('hooks/useExcel.ts',
      lock(`const h = r.clear.bind(r); setTimeout(() => { rows.forEach(h); }, 0);`,
        'r: Excel.Range, rows: string[]')).length).toBe(1);
    // The closure form: the alias is invoked from an arrow handed outward.
    expect(scanTypedSource('hooks/useExcel.ts',
      `export async function f(r: Excel.Range) {\n`
      + `  let g: (() => void) | undefined;\n`
      + `  await withTableLocksKeys(['k'], async () => { const h = r.clear.bind(r); g = () => h(); });\n`
      + `  g!();\n}`).length).toBe(1);
    // Controls: invoked where the lock is genuinely held.
    expect(scanTypedSource('hooks/useExcel.ts',
      lock(`const h = r.clear.bind(r); h();`, 'r: Excel.Range'))).toEqual([]);
    expect(scanTypedSource('hooks/useExcel.ts',
      lock(`const h = r.clear.bind(r); rows.forEach(() => h());`, 'r: Excel.Range, rows: string[]'))).toEqual([]);
  });

  it('R12 D-33: a SHORTHAND destructuring assignment whose target is body-local still escapes', () => {
    // `destructuredTargetEscapes` resolved the shorthand through
    // `getShorthandAssignmentValueSymbol`, then handed the raw IDENTIFIER to
    // `aliasEscapes`, which re-resolved it with `getSymbolAtLocation` and got
    // the synthetic property symbol back -- so no reference matched. R11 D-31's
    // third case passed only because its target is declared OUTSIDE the body and
    // short-circuits on `decls` before that walk ever runs: a fixture passing
    // for the wrong reason.
    expect(scanTypedSource('hooks/useExcel.ts',
      `export async function f(r: Excel.Range) {\n`
      + `  let g: any;\n`
      + `  await withTableLocksKeys(['k'], async () => { let clear: () => void; ({ clear } = r); g = clear; });\n`
      + `  g();\n}`).length).toBe(1);
    expect(scanTypedSource('hooks/useExcel.ts',
      `export async function f(r: Excel.Range) {\n`
      + `  const g: any = await withTableLocksKeys(['k'], async () => {\n`
      + `    let clear: () => void; ({ clear } = r); return clear; });\n  g();\n}`).length).toBe(1);
  });

  it('R12 control: READING cell data through a destructuring pattern is not a mutation', () => {
    // Rule 4 never received rule 3's callability requirement, so on a resolved
    // cell-owning type EVERY member absent from READ_ONLY_METHODS -- which holds
    // method names only -- read as a mutating acquisition, plain data included.
    // `const { values } = range` is the idiomatic Office.js read after
    // load/sync, and the failure message has no correct remedy for a READ, so
    // reporting it steers the next author into allowlisting a read -- a
    // permanent key that would then also sanction a WRITE of identical text.
    expect(scanTypedSource('hooks/useExcel.ts',
      `export function f(r: Excel.Range) { const { values } = r; return values; }`)).toEqual([]);
    expect(scanTypedSource('hooks/useExcel.ts',
      `export async function f(r: Excel.Range) {\n`
      + `  return withTableLocksKeys(['k'], async () => { const { values, formulas } = r; return [values, formulas]; });\n}`)).toEqual([]);
    expect(scanTypedSource('hooks/useExcel.ts',
      `export async function f(r: Excel.Range) {\n`
      + `  await withTableLocksKeys(['k'], async () => { const { address } = r; hold(address); });\n}`)).toEqual([]);
    // ...and a destructured METHOD, or a rest element that captures every
    // member, is still an acquisition.
    expect(scanTypedSource('hooks/useExcel.ts',
      `export function f(r: Excel.Range) { const { clear } = r; clear(); }`).length).toBe(1);
    expect(scanTypedSource('hooks/useExcel.ts',
      `export function f(r: Excel.Range) { const { ...rest } = r; return rest; }`).length).toBe(1);
  });

  it('R12 S-K: a lock helper is judged by where its import RESOLVES, not by the specifier text', () => {
    // The specifier regex has to accept a bare `./workbookMetadata` (the form
    // lockedCellWrite.ts and tableRefresh.ts really use), so it pins only a
    // BASENAME -- `../__mocks__/lockedCellWrite` or a vendor module of the same
    // name would still open a span. This is the only fixture in the file that
    // needs a MULTI-FILE program: with one file nothing resolves and the
    // specifier fallback is all that ever runs.
    const helper = `export async function withTableLocksKeys<T>(k: string[], f: () => Promise<T>): Promise<T> { return f(); }\n`;
    const body = (spec: string) => `import { withTableLocksKeys } from '${spec}';\n`
      + `export async function f(r: Excel.Range) {\n`
      + `  await withTableLocksKeys(['k'], async () => { r.values = [['x']] as never; });\n}`;
    // A test double resolving OUTSIDE utils/ does not certify the write.
    expect(scanTypedSource('hooks/useExcel.ts', body('../__mocks__/lockedCellWrite'),
      { '__mocks__/lockedCellWrite.ts': helper }).length).toBe(1);
    expect(scanTypedSource('hooks/useExcel.ts', body('./notTheLock'),
      { 'hooks/notTheLock.ts': helper }).length).toBe(1);
    // Control: the real module still opens a span.
    expect(scanTypedSource('hooks/useExcel.ts', body('../utils/lockedCellWrite'),
      { 'utils/lockedCellWrite.ts': helper })).toEqual([]);
  });

  it('R13 S-L: a same-named helper reached through a NAMESPACE import or an imported OBJECT does not open a lock span', () => {
    // R11 S-J / R12 S-K closed the NAMED-import spelling. `import * as L from
    // '...'; L.withTableLocksKeys(...)` and `locks.withTableLocksKeys(...)`
    // resolve the identifier straight to the TARGET declaration, so no
    // ImportSpecifier is among its declarations, the lock-helper-FILE branch was
    // skipped, and the check fell through to "declared in another file,
    // therefore real" -- the shadow moved one syntax over rather than one module.
    const helper = `export async function withTableLocksKeys<T>(k: string[], f: () => Promise<T>): Promise<T> { return f(); }\n`;
    const ns = (spec: string) => `import * as L from '${spec}';\n`
      + `export async function f(r: Excel.Range) {\n`
      + `  await L.withTableLocksKeys(['k'], async () => { r.values = [['x']] as never; });\n}`;
    expect(scanTypedSource('hooks/useExcel.ts', ns('./notTheLock'),
      { 'hooks/notTheLock.ts': helper }).length).toBe(1);
    expect(scanTypedSource('hooks/useExcel.ts',
      `import { locks } from './notTheLock';\n`
      + `export async function f(r: Excel.Range) {\n`
      + `  await locks.withTableLocksKeys(['k'], async () => { r.values = [['x']] as never; });\n}`,
      { 'hooks/notTheLock.ts': `export const locks = { async withTableLocksKeys<T>(k: string[], f: () => Promise<T>) { return f(); } };\n` },
    ).length).toBe(1);
    // Control: the REAL module reached through a namespace import still opens one.
    expect(scanTypedSource('hooks/useExcel.ts', ns('../utils/lockedCellWrite'),
      { 'utils/lockedCellWrite.ts': helper })).toEqual([]);
  });

  it('R13 I-23: an OPTIONAL mutating member destructured off a cell-owning receiver is still an acquisition', () => {
    // `memberValueIsCallable` read the property type RAW, so `(() => void) |
    // undefined` reported zero call signatures and rule 4 silenced it -- while
    // rule 3's `isCallableMember`, which non-nullable-normalises first, still
    // reported the dot-alias spelling of the SAME acquisition.
    const rangey = `interface Rangey { values: unknown[][]; newHostMutator?: () => void }\n`;
    expect(scanTypedSource('utils/f.ts',
      rangey + `export function f(r: Rangey) { const { newHostMutator } = r; newHostMutator!(); }`).length).toBe(1);
    expect(scanTypedSource('utils/f.ts',
      `export function f(r: Excel.Range) {\n`
      + `  const { newHostMutator } = r as never as { newHostMutator?: () => void }; newHostMutator!();\n}`).length).toBe(1);
    // ...and the dot-alias spelling, which already reported, still does.
    expect(scanTypedSource('utils/f.ts',
      rangey + `export function f(r: Rangey) { const g = r.newHostMutator; g!(); }`).length).toBe(1);
    // Control: an ordinary Office.js READ is still not a mutation.
    expect(scanTypedSource('utils/f.ts',
      `export function f(r: Excel.Range) { const { values } = r; return values; }`)).toEqual([]);
  });

  it('R13 control: a RENAMED import of the real lock helper still opens a lock span', () => {
    // The span used to be gated on the LOCAL name, so `withTableLocksKeys as
    // withLocks` reported every write inside it. Fail-closed, but a false
    // positive whose only "remedy" is to un-rename the import -- which is how a
    // guard gets weakened rather than obeyed.
    const helper = `export async function withTableLocksKeys<T>(k: string[], f: () => Promise<T>): Promise<T> { return f(); }\n`;
    expect(scanTypedSource('hooks/useExcel.ts',
      `import { withTableLocksKeys as withLocks } from '../utils/lockedCellWrite';\n`
      + `export async function f(r: Excel.Range) {\n`
      + `  await withLocks(['k'], async () => { r.values = [['x']] as never; });\n}`,
      { 'utils/lockedCellWrite.ts': helper })).toEqual([]);
    // ...and a rename does NOT launder a helper from somewhere else.
    expect(scanTypedSource('hooks/useExcel.ts',
      `import { withTableLocksKeys as withLocks } from './notTheLock';\n`
      + `export async function f(r: Excel.Range) {\n`
      + `  await withLocks(['k'], async () => { r.values = [['x']] as never; });\n}`,
      { 'hooks/notTheLock.ts': helper }).length).toBe(1);
  });

  it('R13 S-M: the rename allowance stops at an ALIAS of the real declaration', () => {
    // `isLockEntryPoint` accepts a callee whose RESOLVED target is an entry
    // point declared in a lock-helper file. Two spellings sit right on that
    // boundary and neither may widen it: a re-export chain, which renames a
    // FOREIGN helper one module further out; and a local `const`, which is not
    // an import alias at all and must stay fail-closed.
    const helper = `export async function withTableLocksKeys<T>(k: string[], f: () => Promise<T>): Promise<T> { return f(); }\n`;
    const call = `export async function f(r: Excel.Range) {\n`
      + `  await withLocks(['k'], async () => { r.values = [['x']] as never; });\n}`;
    // A re-export cannot launder a foreign helper into a rename.
    expect(scanTypedSource('hooks/useExcel.ts', `import { withLocks } from './reexport';\n` + call, {
      'hooks/reexport.ts': `export { withTableLocksKeys as withLocks } from './notTheLock';\n`,
      'hooks/notTheLock.ts': helper,
    }).length).toBe(1);
    // ...while a re-export of the REAL helper is still the real helper.
    expect(scanTypedSource('hooks/useExcel.ts', `import { withLocks } from './reexport';\n` + call, {
      'hooks/reexport.ts': `export { withTableLocksKeys as withLocks } from '../utils/workbookMetadata';\n`,
    })).toEqual([]);
    // A local `const` off a namespace import is not an alias symbol -> fail closed.
    expect(scanTypedSource('hooks/useExcel.ts',
      `import * as W from '../utils/workbookMetadata';\nconst withLocks = W.withTableLocksKeys;\n` + call,
    ).length).toBe(1);
    // ...and living in a lock-helper MODULE is not enough on its own: the
    // resolved target must be a lock ENTRY POINT. That module also exports
    // block-key helpers and metadata writers, none of which holds anything.
    expect(scanTypedSource('hooks/useExcel.ts',
      `import { somethingElse as withLocks } from '../utils/workbookMetadata';\n` + call,
      { 'utils/workbookMetadata.ts': `export function somethingElse<T>(k: string[], f: () => Promise<T>): Promise<T> { return f(); }\n` },
    ).length).toBe(1);
  });

  it('R12 control: a BOOLEAN TEST on an acquisition cannot carry it out of the lock', () => {
    // The inverted walk reports anything not provably consumed. An `if` /
    // `while` / ternary CONDITION coerces to boolean and can never yield the
    // function, so without these entries ordinary defensive code is reported.
    const lock = (body: string) => `export async function f(r: Excel.Range) {\n`
      + `  await withTableLocksKeys(['k'], async () => { ${body} });\n}`;
    expect(scanTypedSource('hooks/useExcel.ts', lock(`const h = r.clear.bind(r); if (h) { h(); }`))).toEqual([]);
    expect(scanTypedSource('hooks/useExcel.ts', lock(`const h = r.clear.bind(r); while (h) { h(); break; }`))).toEqual([]);
    // ...and a condition is not an escape hatch: the value still has to be
    // consumed on every path it actually travels.
    expect(scanTypedSource('hooks/useExcel.ts',
      `export async function f(r: Excel.Range) {\n`
      + `  let g: any;\n`
      + `  await withTableLocksKeys(['k'], async () => { const h = r.clear.bind(r); if (h) { g = h; } });\n`
      + `  g();\n}`).length).toBe(1);
  });

  it('R10 control: a value CONSUMED inside the lock has not escaped it', () => {
    // The escape walk tracks whether every hop merely MOVED the acquisition.
    // Without that, anything mentioning a mutating member anywhere on a return
    // path would be reported -- `typeof r.clear` returns a string, not a way to
    // write cells, and reporting it would be the cry-wolf failure mode.
    expect(scanTypedSource('hooks/useExcel.ts',
      `export async function f(r: Excel.Range) {\n`
      + `  const kind = await withTableLocksKeys(['k'], async () => typeof r.clear);\n`
      + `  return kind;\n}`)).toEqual([]);
    expect(scanTypedSource('hooks/useExcel.ts',
      `export async function f(r: Excel.Range) {\n`
      + `  const ok = await withTableLocksKeys(['k'], async () => { const h = r.clear.bind(r); return !!h; });\n`
      + `  return ok;\n}`)).toEqual([]);
  });

  // --- Bug-8690: declaration-site type narrowing ------------------------------

  it('Bug-8690: a DECLARATION-SITE type narrowing does not erase the Office origin', () => {
    // `underlyingExpression` strips EXPRESSION-level narrowing (as / parenthesised
    // / non-null / assertion / satisfies), which is why
    // `(range as unknown as CellSink).values = rows` is caught. A variable
    // DECLARATION's own type annotation is structurally none of those five forms,
    // so `const c: CellSink = range; c.values = rows` slipped past the guard while
    // performing the identical unlocked worksheet mutation.
    //
    // This is the guard's own ENUMERATION blind spot, not a gap in the code it
    // guards -- the class CLAUDE.md's coverage-tool blind-spot rule names.

    // A mutating METHOD on a declaration-narrowed receiver.
    expect(scanTypedSource('utils/f.ts',
      `interface Clearable { clear(): void }\n`
      + `export function f(r: Excel.Range) {\n`
      + `  const c: Clearable = r as never; c.clear();\n}`,
    ).length).toBe(1);

    // A DATA property assignment on a declaration-narrowed receiver -- the
    // wrong-numbers shape: cells overwritten outside any lock.
    expect(scanTypedSource('utils/f.ts',
      `interface CellSink { values: unknown }\n`
      + `export function f(r: Excel.Range, rows: unknown[][]) {\n`
      + `  const w: CellSink = r as never; w.values = rows;\n}`,
    ).length).toBe(1);

    // An UNCLASSIFIED method on a declaration-narrowed CELL-OWNING receiver: the
    // fail-closed rule must still fire, so a future office-js mutator cannot be
    // aliased past the guard this way either.
    expect(scanTypedSource('utils/f.ts',
      `interface Minimal { futureHostMutator(): void }\n`
      + `export function f(r: Excel.Range) {\n`
      + `  const c: Minimal = r as never; c.futureHostMutator();\n}`,
    ).length).toBe(1);

    // Control: a genuinely NON-Office object with a compatible interface is not
    // reported. Widening the rule into every local class that owns a `clear()`
    // is the cry-wolf failure mode this guard has already been rebuilt to avoid.
    expect(scanTypedSource('utils/f.ts',
      `class LocalCleaner { clear() { void 0; } }\n`
      + `export function f() {\n`
      + `  const c: { clear(): void } = new LocalCleaner(); c.clear();\n}`,
    )).toEqual([]);

    // Control: a `let` is NOT followed. It can be reassigned, so its initialiser
    // does not establish what the receiver is at the point of the mutation.
    expect(scanTypedSource('utils/f.ts',
      `interface Clearable { clear(): void }\n`
      + `export function f(r: Excel.Range) {\n`
      + `  let c: Clearable = r as never; c = { clear() {} }; c.clear();\n}`,
    )).toEqual([]);

    // Control: a const with NO type annotation already carries the Office type by
    // inference -- it was never the blind spot and must not regress.
    expect(scanTypedSource('utils/f.ts',
      `export function f(r: Excel.Range) {\n`
      + `  const c = r; c.clear();\n}`,
    ).length).toBe(1);

    // Control: an `any`-typed initialiser must NOT propagate Office origin. A
    // `const s: Set<string> = range.values as unknown as Set<string>` would
    // otherwise make every Set/Map in the codebase read as a worksheet.
    expect(scanTypedSource('utils/f.ts',
      `export function f(r: Excel.Range) {\n`
      + `  const s: Set<string> = r.values as unknown as Set<string>; s.clear();\n}`,
    )).toEqual([]);
  });

  it('Bug-8690: a CIRCULAR const chain terminates instead of overflowing the stack', () => {
    // A verification tool must not assume the code it inspects type-checks. A
    // circular const chain is a valid AST, and following initialisers blindly
    // would recurse forever and take the whole guard down. The guard must return
    // an answer -- any answer -- rather than crash.
    expect(() => scanTypedSource('utils/f.ts',
      `interface Clearable { clear(): void }\n`
      + `export function f() {\n`
      + `  const a: Clearable = b; const b: Clearable = a; a.clear();\n}`,
    )).not.toThrow();
  });
});

/** Test-visible alias so the JS-collection fixture drives the real scanner. */
function scanFileForTest(rel: string, sf: ts.SourceFile, checker: ts.TypeChecker): MutationSite[] {
  return scanFile(rel, sf, checker);
}
