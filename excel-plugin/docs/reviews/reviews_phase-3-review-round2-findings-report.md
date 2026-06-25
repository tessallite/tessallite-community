# Phase 3 -- Review Round 2 Findings

Date: 2026-05-19
Scope: Verification of all fixes from `phase-3-review-round1-fix-report.md` plus full re-scan.
Build: `tsc` zero errors, Vite zero warnings, 27/27 tests passing (4 test files).

---

## 1. Fix Verification

| Round 1 Finding | Fix Claim | Verified | Evidence |
|---|---|---|---|
| P3-1 Persona filtering | Rewrote hook, wired to ReportBuilder | **PARTIAL** | See R2-1 below |
| P3-2 Overwrite A1 | New sheets ("Chart Data", "Pivot Data") | YES | `useExcel.ts:192-203, 254-268` -- collision-safe sheet creation |
| P3-3 Sheet name qualifier | `'SheetName'!A1:C10` format | YES | `useExcel.ts:281` |
| P3-4 Hierarchy name lookup | `findHierarchy()` substring fallback | YES | `excelPivotTables.ts:10-17` |
| P3-5 Concurrent guard | `inserting.current` check | YES | `useExcel.ts:166, 245` |
| P3-6 Stale closure | `res.rows.length` + updater | YES | `DrillPanel.tsx:71, 73-77` |
| P3-7 personaId unused | Wired `usePersonaFiltered` | YES | `ReportBuilder.tsx:9, 40-42` |
| P3-8 Breadcrumb | Rendered with `NavigateNext` | YES | `DrillPanel.tsx:127-189` |
| P3-9 Auto-load | `handlePathSelect` triggers load | YES | `DrillPanel.tsx:90-96` |
| P3-10 DRY query | `executeZoneQuery` extracted | YES | `ReportBuilder.tsx:113-146` |
| P3-11 Large guard | `confirmLargeResult` helper | YES | `useExcel.ts:38-45, 170, 249` |
| P3-12 Duplicate table | `existingRangeAddress` param | YES | `useExcel.ts:162, 186-190` |
| P3-13 Annotation mapping | `buildDefaultFieldMapping` | YES | `useExcel.ts:311-335` |
| P3-14 Unused imports | Removed/re-added properly | YES | `usePersona.ts:4` -- `Measure`, `Dimension` used in signature |
| P3-15 Unused params | Removed from `resolvePluginTableContext` | YES | `cellContext.ts:47-48` |
| P3-17 Static chart title | `title` parameter | YES | `excelCharts.ts:98, 107` |
| P3-18 Duplicated type | Removed from `tessallite.ts` | YES | No `ChartTypeRecommendation` in types file |

**17/18 verified. 1 PARTIAL (R2-1 below).**

---

## 2. New Findings

### R2-1 (HIGH): Persona filtering still a no-op -- both branches return full list

**File**: `src/hooks/usePersona.ts:27-37`
**Severity**: High -- Fix Incomplete

```ts
const filteredMeasures = useMemo(() => {
    if (!measures) return [];
    if (!activePersona) return measures;  // no persona = all
    return measures;                       // has persona = ALL again
}, [measures, activePersona]);

const filteredDimensions = useMemo(() => {
    if (!dimensions) return [];
    if (!activePersona) return dimensions;
    return dimensions;                     // same: always all
}, [dimensions, activePersona]);
```

Both branches of the `if (!activePersona)` check return the identical full list. The `activePersona` is loaded from the API but never used to filter. When a persona is selected, `filteredMeasures` still contains all measures.

**Consequence**: `activeMeasureCount` always equals `totalMeasureCount`. The info bar in `App.tsx:635-652` misleadingly says "5 of 5 measures shown" regardless of persona. No scoping happens.

**Root cause**: The hook structure was prepared for filtering but the actual filter predicate was never implemented. The fix report says: "the actual filtering is a passthrough until the backend supports it."

**Fix**: At minimum, use `persona.measure_count` and `persona.dimension_count` from the `Persona` type to compute correct counts. For display, show the persona's configured counts even without client-side filtering. The info bar should read "Showing up to 23 measures" instead of making a claim it can't fulfill.

---

### R2-2 (MEDIUM): `handleInsertLocalPivot` overrides `buildDefaultFieldMapping` from annotation

**File**: `src/components/ReportBuilder/ReportBuilder.tsx:178-183`
**Severity**: Medium -- Fix Bypassed

```ts
const fieldMapping = {
    rowFields: rowDimNames.length > 0 ? rowDimNames : result.headers.slice(0, 1),
    columnFields: colDimNames,
    dataFields: result.headers.filter(h => !rowDimNames.includes(h) && !colDimNames.includes(h)).slice(0, 3),
    filterFields: [],
};
await excelInsertLocalPivot(result.headers, result.rows, fieldMapping, result.annotation);
```

`insertLocalPivot` in `useExcel.ts:291` only calls `buildDefaultFieldMapping` when `fieldMapping` is falsy:
```ts
const mapping = fieldMapping || buildDefaultFieldMapping(headers, annotation);
```

Since ReportBuilder always provides a non-null `fieldMapping`, `buildDefaultFieldMapping` (and its annotation-driven column classification) is never reached from this code path. The ChatPanel path (`App.tsx:312-316` calling `handleInsertLocalPivot` which calls `excelInsertLocalPivot(headers, rows)`) does NOT provide `fieldMapping`, so it correctly uses `buildDefaultFieldMapping`.

**Consequence**: In Report Builder, the PivotTable field mapping uses raw header string names guessed from zone names rather than the annotated measure/dimension classification. This is a 50% fix -- one path uses annotation, the other doesn't.

**Fix**: In ReportBuilder, pass `undefined` for fieldMapping and let `insertLocalPivot` use `buildDefaultFieldMapping`. The zone items already tell us which are dimensions (rows/columns) vs measures (values) -- pass this as a separate argument instead of building the field mapping inline.

---

### R2-3 (MEDIUM): `loadMore` uses stale `result?.total_count` from closure instead of API response

**File**: `src/components/DrillThrough/DrillPanel.tsx:105`
**Severity**: Medium -- Stale Data

```ts
setHasMore(!!res.next_cursor && updated.length < (result?.total_count ?? updated.length));
```

`result` is captured in the `useCallback` closure. When `loadMore` is called, `result` holds whatever value it had when `useCallback` last updated. The response object `res` has a `total_count` field that should be used instead:

**Fix**: Use `res.total_count`:
```ts
setHasMore(!!res.next_cursor && updated.length < res.total_count);
```

Note: P3-6 fixed the same bug in `loadDrillThrough`, and this same pattern persists in `loadMore`.

---

### R2-4 (MEDIUM): `setHasMore` called inside `setAllRows` updater -- anti-pattern

**File**: `src/components/DrillThrough/DrillPanel.tsx:73-77, 103-107`
**Severity**: Medium -- React Anti-Pattern

```ts
setAllRows(prev => {
    const updated = [...prev, ...res.rows];
    setHasMore(!!res.next_cursor && updated.length < res.total_count);  // side effect inside updater
    return updated;
});
```

Calling `setHasMore` inside a `setAllRows` updater function is a side effect in a pure updater. While React currently tolerates this, it violates the principle that updater functions should be pure. If React changes batching behavior or React Strict Mode double-invokes updaters, this could cause issues.

**Fix**: Compute `hasMore` from `res.rows.length + allRows.length` before the state update, or use `useEffect` to derive `hasMore` from `allRows`/`result`/`nextCursor`:

```ts
const newRows = [...allRows, ...res.rows];
setHasMore(!!res.next_cursor && newRows.length < res.total_count);
setAllRows(newRows);
```

---

### R2-5 (LOW): `inserting` ref shared across all 3 insertion functions -- misnamed

**File**: `src/hooks/useExcel.ts:48`
**Severity**: Low -- Naming

The `inserting` ref guards all three insertion functions (`insertTable`, `insertChart`, `insertLocalPivot`). While the intent is correct (prevent concurrent Excel writes), the name `inserting` is misleading -- it suggests only "table insertion" is in progress when chart or pivot insertion could be the active one.

**Fix**: Rename to `busy` or `operationInProgress`.

---

### R2-6 (LOW): `findHierarchy` substring fallback `field.includes(name)` can produce false positives

**File**: `src/utils/excelPivotTables.ts:14`
**Severity**: Low -- Edge Case

```ts
if (name.includes(field) || field.includes(name)) return hier;
```

If `field` is a very short common string (e.g., "ID") and `name` is "Valid" (contains no "ID"), the second branch `field.includes(name)` would never match. But if `field` is "Sales" and `name` is "Sale" (truncated), `field.includes(name)` is true. This is very unlikely with real Excel pivot hierarchy names but could match the wrong hierarchy with short names.

**Fix**: Consider only using `name.includes(field)` (the more common case: "Revenue (Sum)".includes("Revenue")), or both with a minimum length check.

---

## 3. Summary

| Category | Count |
|---|---|
| High | 1 |
| Medium | 3 |
| Low | 2 |
| **Total new findings** | **6** |

**17/18 Round 1 fixes verified. 6 new findings.**

### Outstanding from Round 1:

The `remains open` items from the fix report are deferred to later phases and are not re-reviewed:
- M1-M3: Persona backend filtering (depends on API change)
- M5-M8: Context menu, ribbon, agent hints, DrillThroughSet (backend/Office.js constraints)

### Actionable items for next fix round:

1. **R2-1** (HIGH): Persona filtering still returns full lists. Use `persona.measure_count`/`dimension_count` for accurate info bar display.
2. **R2-2** (MEDIUM): ReportBuilder `handleInsertLocalPivot` bypasses `buildDefaultFieldMapping`.
3. **R2-3** (MEDIUM): `loadMore` uses stale `result?.total_count` instead of `res.total_count`.
4. **R2-4** (MEDIUM): `setHasMore` called inside `setAllRows` updater -- extract out of the updater function.

---

*End of Phase 3 Round 2 review. 17/18 fixes verified. 1 high, 3 medium, 2 low new findings.*
