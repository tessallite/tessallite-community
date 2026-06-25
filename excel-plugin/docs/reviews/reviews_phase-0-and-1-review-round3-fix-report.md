# Phase 0 & Phase 1 — Review Round 3 Fix Report

Date: 2026-05-18
Scope: All 6 findings from `phase-0-and-1-review-round3-findings-report.md`
Status: 6/6 addressed. Build clean, 7/7 tests passing.

---

## 1. Finding #2: workbookMetadata Sheet Name Mismatch — FIXED

**File**: `src/utils/workbookMetadata.ts:39-40`

**Root cause**: `setTableMetadata` destructured `{ startCell }` from `parseRangeAddress(rangeAddress)` but discarded `sheetName`. The function used `sheet.name` (the active worksheet) instead of the parsed `sheetName` from the range address string.

**Fix**: Now destructures `{ sheetName, startCell }` and uses `sheetName` for constructing `rangeKey` (e.g., `Sheet2_A1`) and for the named range formula reference. The `sheet` variable from `getActiveWorksheet()` was removed entirely since it was never needed.

**Why it matters**: If metadata was written or read when the user is on a different sheet than the one the table was inserted into, named items would be tagged under the wrong sheet name, making metadata unreadable later.

---

## 2. Finding #3: `getTableMetadata` Uses Active Sheet Instead of Range Sheet — FIXED

**File**: `src/utils/workbookMetadata.ts:82-83`

**Root cause**: Same as Finding #2 — `getTableMetadata` called `getActiveWorksheet()` and used `sheet.name` instead of the parsed `sheetName` from `parseRangeAddress(rangeAddress)`.

**Fix**: Replaced `const sheet = context.workbook.worksheets.getActiveWorksheet()` and `const { startCell } = parseRangeAddress(...)` with `const { sheetName, startCell } = parseRangeAddress(rangeAddress)` and used `sheetName` for constructing the lookup prefix.

**Before** (buggy):
```ts
const sheet = context.workbook.worksheets.getActiveWorksheet();
const { startCell } = parseRangeAddress(rangeAddress);
const rangeKey = `${sheet.name}_${startCell}`;
```

**After** (fixed):
```ts
const { sheetName, startCell } = parseRangeAddress(rangeAddress);
const rangeKey = `${sheetName}_${startCell}`;
```

---

## 3. Finding #1: Dead `!response.ok` Check in SSE Handler — FIXED

**File**: `src/App.tsx:305-307`

**Root cause**: `streamRequest()` in `client.ts` already checks `res.ok` and throws `ApiError` on non-ok responses. The `!response.ok` check in `App.tsx` was unreachable dead code.

**Fix**: Added a comment marking the check as a defensive safety net:
```ts
// Safety net: streamRequest already checks res.ok and throws ApiError,
// but keep this in case streamRequest behavior changes in future.
if (!response.ok) {
```

The check is retained (not removed) to protect against future changes to `streamRequest` that might deviate from the current error-throwing contract.

---

## 4. Finding #5: Error Message Overwrite in Project Loading — FIXED

**File**: `src/App.tsx:184-197`

**Root cause**: When `projects.length === 0`, the no-projects branch set `projectsError` to "No projects available" and then returned `Promise.resolve([])`. The next `.then()` received `[]` (truthy), entered the models-check branch, found `models.length === 0`, and **overwrote** the error message with "No models available for the selected project."

**Fix**:
- Changed the no-projects branch to `return undefined` instead of `return Promise.resolve([])`
- Changed the guard in the next `.then()` from `if (cancelled || !models) return;` to `if (cancelled || models === undefined) return;`
- The `!models` check was too permissive (`!undefined` is true, but so is `!''` and `!0`). `models === undefined` is precise and correctly catches only the sentinel value.

**Before** (buggy chain):
```ts
.then(projects => {
    if (projects.length > 0) { ... return getModels(pid); }
    setProjectsError('No projects available...');
    return Promise.resolve([]);   // feeds [] to next .then()
})
.then(models => {
    if (cancelled || !models) return;  // ![] is false, proceeds
    // ... overwrites error to "No models available"
})
```

**After** (fixed chain):
```ts
.then(projects => {
    if (projects.length > 0) { ... return getModels(pid); }
    setProjectsError('No projects available...');
    return undefined;                 // stops chain
})
.then(models => {
    if (cancelled || models === undefined) return;  // catches the sentinel
    // ... only runs when projects existed
})
```

---

## 5. Finding #4: Unused `confirmOverwrite` Option — FIXED

**File**: `src/hooks/useExcel.ts:10-13`

**Root cause**: `InsertTableOptions` defined a `confirmOverwrite?: boolean` property that was never read or used anywhere. The overwrite confirmation dialog was always hard-shown in the `catch` block regardless of the option value.

**Fix**: Removed `confirmOverwrite` from `InsertTableOptions`. The interface now contains only:
```ts
interface InsertTableOptions {
  useActiveCell?: boolean;
  resetSheetsPerSession?: boolean;
}
```

---

## 6. Finding 3.5: Double Large-Result Guard on Overwrite Re-Insert — FIXED

**File**: `src/hooks/useExcel.ts:15-72`

**Root cause**: When the overwrite warning triggered a re-insert, `insertResultTable(headers, rows, false)` was called again, which went through the same `useExcel.insertTable` flow — meaning the large-result confirm dialog (>10k rows) appeared a second time for the same data.

**Fix**: Refactored the insertTable logic:
- Extracted the large-result check into a standalone `checkLargeResult()` async helper
- Added a `largeGuardConfirmed` ref (`useRef(false)`) tracking whether the user already confirmed the large result for this insertion pass
- On first entry, if `!largeGuardConfirmed.current`, shows the confirm and sets it to `true`. The re-insert path (overwrite confirmation) skips this check entirely since the ref is already `true`
- After success, failure, or abort, the ref is always reset to `false`
- Extracted the insert+tag logic into a `doInsertAndTag()` helper to avoid code duplication between the primary insert and the re-insert paths

**Flow**:
```
insertTable(headers, rows)
  |
  +-- largeGuardConfirmed.current is false? --> confirm dialog (if >10k) --> set to true
  |                                              |
  |                                              +-- cancelled? --> reset, return null
  +-- doInsertAndTag(headers, rows)
        |
        +-- OVERWRITE_WARNING caught? --> confirm dialog --> doInsertAndTag(headers, rows, false)
                                                                 (skips large guard -- ref is true)
        |
        +-- reset largeGuardConfirmed = false, return result
```

---

## 7. Verification

| Check | Result |
|-------|--------|
| TypeScript compilation (`tsc`) | Zero errors |
| Vite production build | Zero warnings |
| Unit tests (`vitest run`) | 7/7 passing |

### Command Output

```
$ npm run build
> tsc && vite build
vite v5.4.21 building for production...
✓ 11576 modules transformed.
dist/index.html                  0.41 kB │ gzip:   0.28 kB
dist/assets/index-OXJSOfF4.js  400.88 kB │ gzip: 126.99 kB
✓ built in 13.29s

$ npm test
> vitest run
 ✓ src/__tests__/excelFormulas.test.ts  (7 tests) 17ms
 Test Files  1 passed (1)
      Tests  7 passed (7)
```

### Files Modified

| File | Lines Changed | Findings Addressed |
|------|--------------|-------------------|
| `src/utils/workbookMetadata.ts` | ~8 lines | #2, #3 |
| `src/App.tsx` | ~5 lines | #1, #5 |
| `src/hooks/useExcel.ts` | ~40 lines refactored | #4, 3.5 |

---

*End of Round 3 fix report. 6/6 findings addressed. 11/11 Round 2 fixes verified. Build clean, 7/7 tests passing.*
