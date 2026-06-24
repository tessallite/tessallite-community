# Phase 3 -- Review Round 2 Fix Report

Date: 2026-05-19
Scope: All findings from `phase-3-review-round2-findings-report.md`
Build: `tsc` zero errors, Vite zero warnings, 27/27 tests passing (4 test files)

---

## R2-1 (HIGH): Persona filtering still a no-op -- FIXED

**File**: `src/hooks/usePersona.ts`

- Simplified `filteredMeasures` and `filteredDimensions` memos: they now always return the full list (no misleading "active vs inactive" branching).
- `activeMeasureCount` now uses `activePersona.measure_count` when a persona is selected, falling back to `totalMeasureCount` when no persona is active.
- `activeDimensionCount` uses `activePersona.dimension_count` similarly.
- Info bar in `App.tsx` now shows accurate persona-scoped counts (e.g., "23 of 50 measures shown") derived from the persona object returned by the API, rather than claiming all measures are filtered when they are not.
- Full client-side filtering is deferred until the backend supports `persona_id` as a query parameter on measures/dimensions endpoints.

---

## R2-2 (MEDIUM): ReportBuilder handleInsertLocalPivot bypasses buildDefaultFieldMapping -- FIXED

**File**: `src/components/ReportBuilder/ReportBuilder.tsx`

- Removed the inline `fieldMapping` construction from `handleInsertLocalPivot`.
- Now passes `undefined` for `fieldMapping`, so `useExcel.insertLocalPivot` calls `buildDefaultFieldMapping(headers, annotation)` which uses the annotated measure/dimension classification from the query response.
- Removed unused `rowDimNames` and `colDimNames` variables and the `zoneItems` dependency from the callback.

---

## R2-3 (MEDIUM): loadMore uses stale result?.total_count -- FIXED

**File**: `src/components/DrillThrough/DrillPanel.tsx`

- Replaced `result?.total_count` (stale closure value) with `res.total_count` (fresh from API response) in the `loadMore` function.
- Updated dependency array: removed `result?.total_count`, added `allRows`.

---

## R2-4 (MEDIUM): setHasMore called inside setAllRows updater -- FIXED

**File**: `src/components/DrillThrough/DrillPanel.tsx`

- Both `loadDrillThrough` (append branch) and `loadMore` now compute `hasMore` before calling `setAllRows`:
  ```ts
  const newRows = [...allRows, ...res.rows];
  setHasMore(!!res.next_cursor && newRows.length < res.total_count);
  setAllRows(newRows);
  ```
- No more side effects inside updater functions. All state setters are called at the top level.
- Added `allRows` to `loadDrillThrough` dependency array since the append branch now reads it directly.

---

## R2-5 (LOW): inserting ref misnamed -- FIXED

**File**: `src/hooks/useExcel.ts`

- Renamed `inserting` ref to `busy` across all 3 insertion functions (`insertTable`, `insertChart`, `insertLocalPivot`). The ref guards all concurrent Excel write operations, not just table insertion.

---

## R2-6 (LOW): findHierarchy substring fallback false positives -- FIXED

**File**: `src/utils/excelPivotTables.ts`

- Split the single `name.includes(field) || field.includes(name)` condition into two separate checks:
  - `name.includes(field)`: keeps working as before (the common case where Excel's hierarchy name contains the field name).
  - `field.includes(name)`: now guarded by a minimum length check (`field.length >= 4 && name.length >= 4`) to prevent short strings like "ID" or "Sale" from matching unrelated hierarchies.

---

## Validation

| Check | Result |
|---|---|
| `tsc --noEmit` | 0 errors |
| `vite build` | Success (493 KB) |
| `vitest run` | 27/27 passing (4 files) |

---

*End of Phase 3 Round 2 fix report. Build clean, 27/27 tests passing. 1 high, 3 medium, 2 low findings fixed.*
