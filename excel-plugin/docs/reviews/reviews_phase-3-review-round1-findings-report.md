# Phase 3 -- Review Round 1 Findings

Date: 2026-05-19
Scope: `tessallite/excel-plugin/src/` -- Phase 3 implementation (Charts, Local PivotTables, Persona Switcher, Drill-Through)
Build: `tsc` zero errors, Vite zero warnings, 27/27 tests passing (4 test files)
New files: 7 (`excelCharts.ts`, `excelPivotTables.ts`, `cellContext.ts`, `usePersona.ts`, `PersonaDropdown.tsx`, `DrillPanel.tsx`, `DrillPathPicker.tsx`)
New tests: 3 (`excelCharts.test.ts`, `cellContext.test.ts`, `reportTemplates.test.ts`)

---

## CRITICAL

*None identified.*

---

## HIGH

### Finding P3-1: `usePersonaFiltered` hook personafiltering is a no-op

**File**: `src/hooks/usePersona.ts:24-27, 32-33`
**Severity**: High -- Core Feature Broken

```ts
const totalMeasureCount = measures?.length ?? 0;
const totalDimensionCount = dimensions?.length ?? 0;
const activeMeasureCount = measures?.length ?? 0;    // same as totalMeasureCount
const activeDimensionCount = dimensions?.length ?? 0; // same as totalDimensionCount

return {
    filteredMeasures: measures ?? [],    // raw, unfiltered measures
    filteredDimensions: dimensions ?? [], // raw, unfiltered dimensions
```

The hook accepts `activePersonaId` but never uses it to filter measures or dimensions. `filteredMeasures` and `filteredDimensions` are always the full unfiltered lists. `activeMeasureCount` always equals `totalMeasureCount`. The persona switcher appears functional but does nothing.

**Spec & Plan requirement** (Workstream C, Task 2): "Filter: Ask context, Report Builder libraries, Glossary lookup, CUBE formula catalog -- based on selected persona." (Specs 4.7): "Switching persona changes available measures/dimensions."

**Consequence**: Info bar text is misleading: "Viewing as Executive. 5 of 5 measures shown" regardless of persona. Users in different roles see identical model catalog.

**Fix**: The hook must call `GET /api/v1/projects/{id}/models/{mid}/personas/{pid}/tag-restrictions` (specs Section 5.1) or filter measures/dimensions client-side based on persona configuration. At minimum, pass `persona_id` to the `useMeasures`/`useDimensions` query and let the backend filter.

---

### Finding P3-2: `insertChart` and `insertLocalPivot` write at hardcoded `(0, 0)` with no overwrite protection

**File**: `src/hooks/useExcel.ts:172, 208`
**Severity**: High -- Data Loss Risk

```ts
const range = sheet.getRangeByIndexes(0, 0, rowCount, colCount);  // always A1
```

Both `insertChart` and `insertLocalPivot` write data to the active worksheet starting at cell (0, 0). This silently overwrites content at `A1` without any prompt, guard, or confirmation. The `insertTable` function has overwrite confirmation via `OVERWRITE_WARNING`, but `insertChart` and `insertLocalPivot` skip all guards.

Additionally, `insertChart` writes data even if `insertTable` was already called (which also wrote data), creating a second copy at A1. Both functions create a table from the range, so a user clicking "Insert Table" then "Chart" will get two copies of the data.

**Consequence**: Data loss on active worksheet. User clicks "Chart" and whatever was at A1 is gone. No way to cancel.

**Fix**: 
1. Move write to a new sheet by default (like `insertResultTable` in `officeSpike.ts` does), or
2. Check for content at target range and issue overwrite confirmation, or
3. Accept a `targetSheet` parameter so the caller can control placement.

---

### Finding P3-3: `insertPivotTableWithMapping` source range address may not include sheet name

**File**: `src/hooks/useExcel.ts:217-218`, `src/utils/excelPivotTables.ts:32-37`
**Severity**: High -- PivotTable creation fails

```ts
// useExcel.ts:217
rangeAddress = range.address;  // e.g. "A1:C10" -- NO sheet name

// excelPivotTables.ts:29-30
const newSheet = sheets.add(sheetName);  // creates NEW sheet
```

The PivotTable is created on a NEW sheet, but the source range address `"A1:C10"` lacks the sheet qualifier (`"Sheet1!A1:C10"`). Excel's `pivotTables.add(sourceRangeAddress, ...)` may not resolve this cross-sheet reference correctly when called from a different sheet.

**Consequence**: PivotTable may appear empty or throw an error on Excel for Web or Mac.

**Fix**: Persist the source sheet name alongside the range address, and construct the full address as `SheetName!A1:C10` when passing to `pivotTables.add`.

---

### Finding P3-4: `insertPivotTableWithMapping` hierarchy name lookup is fragile

**File**: `src/utils/excelPivotTables.ts:46-57`
**Severity**: High -- Field mapping silently fails

```ts
hierarchyMap.set(item.name, item);  // item.name might be "Revenue (Sum)"
// ...
const hier = hierarchyMap.get(field);  // field is "Revenue"
```

Excel auto-generates hierarchy names in a local PivotTable -- they may be different from the raw header names. For example, a numeric column "Revenue" becomes "Revenue (Sum)" in the pivot table hierarchy. The lookup by exact header name (`fieldMapping.rowFields`, `dataFields` etc.) will often fail silently (`hier` is undefined, so `if (hier)` passes and nothing happens).

**Consequence**: PivotTable is created but with no fields mapped -- appears empty. Silently fails with no toast or error.

**Fix**: Use `item.name.includes(field)` fuzzy matching, or store the header-to-hierarchy mapping during the data write step and pass it explicitly.

---

### Finding P3-5: `insertChart` and `insertLocalPivot` have no concurrent call guard

**File**: `src/hooks/useExcel.ts:148, 194`
**Severity**: High -- Duplicate Artifacts

`insertTable` has the `inserting` ref guard (line 40-41, 48). But `insertChart` and `insertLocalPivot` have no such guard. Rapid double-click of "Chart" or "Local Pivot" will insert two charts/tables onto the worksheet.

**Consequence**: Duplicate Excel artifacts created. Duplicate metadata written to the same range address (second call overwrites first).

**Fix**: Add a general-purpose lock or per-function guards similar to `insertTable`'s `inserting` ref.

---

### Finding P3-6: `DrillPanel.loadDrillThrough` uses stale `allRows` in `hasMore` computation

**File**: `src/components/DrillThrough/DrillPanel.tsx:72`
**Severity**: High -- Bug

```ts
const res = await drillThrough(measureId, { ...context, hierarchy_id: selectedPath }, cursor);
if (resetResults) {
    setAllRows(res.rows);        // state update -- not reflected yet
    setResult(res);
}
// ...
setHasMore(!!res.next_cursor && (allRows.length + res.rows.length) < res.total_count);
//                                ^^^^^^ stale closure -- allRows is still []
```

After `setAllRows(res.rows)` is called, `allRows` in the closure still holds the old value (`[]` on first load). The `hasMore` computation uses this stale value, meaning `allRows.length` is 0 on the first load instead of `res.rows.length`. This causes incorrect pagination state -- `loadMore` may be available when it shouldn't be, or unavailable when it should.

**Fix**: Use `res.rows.length` (not `allRows.length`) in the `resetResults` branch, since `allRows` is about to become `res.rows`:
```ts
const rowCount = resetResults ? res.rows.length : allRows.length + res.rows.length;
setHasMore(!!res.next_cursor && rowCount < res.total_count);
```

---

## MEDIUM

### Finding P3-7: `ReportBuilder` `personaId` prop accepted but never used

**File**: `src/components/ReportBuilder/ReportBuilder.tsx:28, 31`
**Severity**: Medium -- Dead Feature / Misleading

The `personaId` prop is destructured in the function signature but never passed to `useMeasures`, `useDimensions`, `useHierarchies`, or any API call. The `usePersonaFiltered` hook (imported in `App.tsx` but NOT in `ReportBuilder.tsx`) could perform filtering but is not used here.

**Consequence**: Persona switching doesn't filter the Report Builder's measure/dimension catalog. App.tsx line 701 passes `personaId={activePersonaId}` which is silently ignored.

**Fix**: Either:
1. Import `usePersonaFiltered` into ReportBuilder and apply filtering, or
2. Pass `personaId` to `useMeasures`/`useDimensions` so the backend scopes results, or
3. Remove the prop until persona filtering is implemented end-to-end.

---

### Finding P3-8: Drift-through panel missing breadcrumb navigation

**File**: `src/components/DrillThrough/DrillPanel.tsx:136-140`
**Severity**: Medium -- Spec Cut

The execution plan (Workstream D, Task 4) and specs (Section 4.5) require: "Breadcrumb context: `Revenue > Country: US > Date: 2025-Q4`. Clicking a breadcrumb level navigates back up to that summarisation level."

**Current state**: Only shows a single line: `{measureName} > {dimension}` -- not clickable, not hierarchical.

**Fix**: Implement breadcrumb navigation with clickable segments. Each segment should reload drill-through at that level.

---

### Finding P3-9: Drill panel does not auto-load results on path selection

**File**: `src/components/DrillThrough/DrillPanel.tsx:146-152`
**Severity**: Medium -- UX

When the user selects a drill path from `DrillPathPicker`, the panel clears results (`setResult(null)`) but does NOT auto-load the drill-through. The user must click the "Drill" button. This is a 2-click flow when 1-click would suffice (the `selectedPath` useEffect could auto-trigger `loadDrillThrough`).

**Fix**: Call `loadDrillThrough(true)` inside the `onSelect` handler or in a `useEffect` that reacts to `selectedPath` changes.

---

### Finding P3-10: ReportBuilder query execution duplicated 3 times

**File**: `src/components/ReportBuilder/ReportBuilder.tsx:109-221`
**Severity**: Medium -- DRY / Maintenance

`handleInsertTable`, `handleInsertChart`, and `handleInsertLocalPivot` all contain the identical ~25 lines of:
1. Build `SemanticQuery` from zone items
2. Call `executeQuery`
3. Guard for empty result
4. Parse headers from annotation (or fallback)
5. Convert rows to `(string | number)[][]`

If the query structure or annotation parsing changes, all 3 functions must be updated identically.

**Fix**: Extract a shared helper like `buildQueryFromZones` and `executeAndFormatQuery`. Each handler becomes ~5 lines.

---

### Finding P3-11: `insertChart` has no large-result guard

**File**: `src/hooks/useExcel.ts:148-192`
**Severity**: Medium -- UX / Performance

`insertTable` has a `largeGuardConfirmed` ref that checks `rows.length > LARGE_ROW_THRESHOLD`. `insertChart` bypasses this entirely. Inserting 500K rows for a chart will freeze Excel.

**Fix**: Add the same large-result check before data write in `insertChart` (and `insertLocalPivot`).

---

### Finding P3-12: `insertChart` creates a duplicate data table alongside the chart

**File**: `src/hooks/useExcel.ts:177-179`
**Severity**: Medium -- Duplicate Data

```ts
const table = sheet.tables.add(range, true);  // creates Excel Table
await insertChartFromRange(range, excelChartType, sheet); // creates Chart
```

When the user clicks "Chart" after already clicking "Insert Table", a second table is created on the active sheet alongside the first one. If the user only wants the chart, they still get a backing table (which is fine), but if they already have the table, they get a duplicate.

**Fix**: Set `useActiveCell` or insert on a new sheet. Or accept an option to reuse an existing table range.

---

### Finding P3-13: `insertLocalPivot` default field mapping heuristic is fragile

**File**: `src/hooks/useExcel.ts:227-232`
**Severity**: Medium -- Incorrect Results

```ts
const mapping = fieldMapping || {
    rowFields: headers.slice(0, Math.min(2, headers.length > 1 ? 1 : 0)),
    dataFields: headers.length > 1 ? [headers[headers.length - 1]] : [],
```

Without proper annotation (which columns are measures vs dimensions), the default mapping puts the first column in rows and the last column in data. For a query returning `[Country, Date, Revenue, Margin]`, this maps `Country` to rows and `Margin` to data, missing `Revenue` entirely.

**Fix**: Require annotation from the query response and use it to build the mapping. `dataFields` should include all columns tagged as measures, `rowFields` all those tagged as dimensions.

---

## LOW

### Finding P3-14: `usePersona` imports `Measure` and `Dimension` types unused

**File**: `src/hooks/usePersona.ts:4`
**Severity**: Low -- Dead Import

```ts
import type { Persona, Measure, Dimension } from '../types/tessallite';
```

`Measure` and `Dimension` are never referenced in the file body. They appear in the function signature's `measures?: Measure[]` but are only used via the generic parameter, not as explicit type annotations the import enables.

**Fix**: Remove `Measure, Dimension` from the import. The types propagate through the function signature without explicit imports.

---

### Finding P3-15: `resolvePluginTableContext` ignores `address` and `value` parameters

**File**: `src/utils/cellContext.ts:47-60`
**Severity**: Low -- Dead Params

Both parameters are accepted in the function signature but never used. The function could use them to improve context (e.g., reading surrounding cells from the metadata range to determine which cell in the table was clicked).

**Fix**: Remove the unused params or implement the improvement.

---

### Finding P3-16: `resolveCubeFormulaContext` extracts only the first `[Measures]` match

**File**: `src/utils/cellContext.ts:36-44`
**Severity**: Low -- Edge Case

For formulas like `=CUBEVALUE("conn","[Measures].[Revenue]","[Measures].[Cost]")`, only `Revenue` is extracted. The drill-through should ideally focus on the specific measure cell the user selected, but in a multi-measure formula, the context is ambiguous.

**Fix**: No immediate fix needed -- document as known limitation. The cell contains multiple measures; drill-through is sent to the first one.

---

### Finding P3-17: `excelCharts.ts:insertChartFromRange` always sets static chart title

**File**: `src/utils/excelCharts.ts:106`
**Severity**: Low -- UX

```ts
chart.title.text = 'Tessallite Result';
```

All charts get the same title regardless of what data they represent.

**Fix**: Accept a `title` parameter and pass the measure name(s) or a description.

---

### Finding P3-18: `excelCharts.ts` `ChartTypeRecommendation` type duplicated in `types/tessallite.ts`

**File**: `src/utils/excelCharts.ts:3`, `src/types/tessallite.ts:189`
**Severity**: Low -- Duplication

Both files export `ChartTypeRecommendation`. The types file has a bare `export type` without a source reference.

**Fix**: Define in one place (types file) and re-export or import from there.

---

## MISSING WORK (Phase 3 scope items not implemented)

| # | Item | Plan Reference | Status |
|---|---|---|---|
| M1 | Persona measure/dimension filtering | Workstream C, Task 2 | Not implemented. Hook exists but filtering is a no-op. |
| M2 | Persona-filtered glossary lookup | Workstream C, Task 2 | Not implemented. |
| M3 | Persona-filtered CUBE formula catalog | Workstream C, Task 2 | Not implemented. |
| M4 | Drill-through breadcrumb navigation | Workstream D, Task 4 | Not implemented. |
| M5 | Drill-through context menu integration | Specs 4.5, 6.4 | Not implemented. Only header button trigger. |
| M6 | Ribbon integration | Phase 3 scope (specs 6.5) | Not implemented. Manifest has ribbon buttons but no Phase 3 integration. |
| M7 | Chart recommendation from agent metadata | Workstream A, Task 1 | Partially implemented. ChatPanel uses `recommendChartType` heuristic but doesn't use agent-provided chart hints. |
| M8 | DrillThroughSet column configuration | Workstream D (specs 4.5) | Not implemented. No call to `GET .../drill-through-set` to determine detail columns. |
| M9 | Persona info bar measure/dimension counts correct | Workstream C, Task 5 | Not implemented. Counts always equal total. |

---

## COMPATIBILITY NOTES

| Item | Finding | Severity |
|---|---|---|
| `getRangeByIndexes` | Used in `insertChart` and `insertLocalPivot` -- available in ExcelApi 1.7+ (Excel 2019+, Web). | Info |
| `sheet.pivotTables.add` | Used in `excelPivotTables.ts` -- available in ExcelApi 1.8+ (Excel 2019+, Web). Not available on Excel 2016. | Info |
| `sheet.charts.add` | Used in `excelCharts.ts` -- available in ExcelApi 1.1+. Widely supported. | Info |

---

## SUMMARY

| Category | Count |
|---|---|
| High | 6 |
| Medium | 7 |
| Low | 5 |
| Missing scope items | 9 |
| **Total findings** | **27** |

### Critical path for Phase 3 completion:

1. **P3-1**: Persona filtering must actually filter measures/dimensions -- core feature non-functional
2. **P3-2**: `insertChart` and `insertLocalPivot` overwrite A1 without warning -- data loss risk
3. **P3-3**: PivotTable source range cross-sheet reference may fail -- PivotTable broken
4. **P3-4**: Hierarchy name lookup won't match Excel's auto-generated names -- PivotTable field mapping broken
5. **P3-6**: Stale closure bug in `loadDrillThrough` -- pagination state incorrect

---

*End of Phase 3 Round 1 review. Build clean, 27/27 tests passing. 6 high, 7 medium, 5 low, 9 missing scope items.*
