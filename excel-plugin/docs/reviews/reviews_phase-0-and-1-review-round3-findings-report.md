# Phase 0 & Phase 1 -- Review Round 3 Findings

Re-review of all code changes from the Round 2 fix report. Verifies claimed fixes and identifies remaining or newly introduced issues.

Date: 2026-05-18
Scope: `tessallite/excel-plugin/` only

---

## 1. Fix Verification Summary

| Round 2 Claim | Verified | Notes |
|---|---|---|
| #7 Toast system wired | YES | `useToast` imported at App.tsx:15; called at lines 130, 138, 250, 253, 263, 265 |
| #8 agentConfigured from API | YES | `useEffect` at 216-238 calls `getAgentConfig(projectId)`. Prop at 574 uses `agentConfig.configured` |
| #9 providerModel from API | YES | Lines 430-432 construct string from `agentConfig.provider` and `agentConfig.model`. Shows "Loading provider info..." while fetching |
| #10 Project/model error handling | YES | `.then().then().catch()` chain at 184-210. `projectsLoading` state at 559 shows spinner. `projectsError` state at 564 renders error |
| 7.1 clearSessionState | YES | Lines 92-105 resets all chat + project state. Called from handleLogout (108) and handleSwitchProfile (114) |
| #12 Named item accumulation | YES | Deterministic keys at 60: `__tessallite_{sheetName}_{startCell}_{key}`. Old items deleted at 49-55 before writing |
| 7.4 Overwrite warning UI | YES | useExcel.ts:46-61 catches OVERWRITE_WARNING, shows confirm dialog, re-inserts without active-cell restriction |
| #11 ARIA label mismatch | YES | Ask mode at 553-556 has `role="log" aria-label="Chat messages"`. Report Builder at 583-585 has `role="region" aria-label="Report Builder placeholder"`. Outer `<main>` has no ARIA overrides |
| #13 getTableMetadata filtering | YES | Line 89 constructs prefix from range address. Line 96 filters by prefix |
| Dead code markers | YES | Phase 2/3 markers added to InsertActions, excelFormulas, useModel, queryRouter, officeSpike, agentService, useExcel |
| ESM __dirname fix | YES | Both vite.config.ts:4-7 and vitest.config.ts:2-5 use `fileURLToPath(import.meta.url)` |

**Result: 11/11 Round 2 fixes verified.**

---

## 2. New and Remaining Findings

### Finding #1: Dead Code in SSE Handler (LOW)

**File**: `src/App.tsx:309-311`

```ts
const response = await sendMessageStream(projectId!, convId, content);
if (abortController.signal.aborted) return;
if (!response.ok) {
    throw new Error(`Stream request failed with status ${response.status}`);
}
```

`streamRequest()` in `client.ts:109-113` already checks `res.ok` and throws an `ApiError` on non-ok responses. The `!response.ok` check in `App.tsx` is dead code -- `response.ok` will always be `true` when `streamRequest` returns without throwing. If somehow it's not ok, then `streamRequest` already threw and this line is unreachable.

**Impact**: None (harmless dead code). The error handling is correctly handled by the outer `catch` block which catches the `ApiError` from `streamRequest`.

**Recommendation**: Remove the dead check or keep it as defensive code but add a comment noting it's a safety net for unexpected streamRequest behavior.

### Finding #2: workbookMetadata Sheet Name Mismatch Risk (MEDIUM)

**File**: `src/utils/workbookMetadata.ts:40, 64`

```ts
await Excel.run(async (context) => {
    const sheet = context.workbook.worksheets.getActiveWorksheet();
    const { startCell } = parseRangeAddress(rangeAddress);
    const rangeKey = `${sheet.name}_${startCell}`;
    ...
    const namedItem = context.workbook.names.add(
        name,
        `${sheet.name}!${rangeAddress.split('!')[1] || rangeAddress}`,
    );
```

The code uses `sheet.name` (the **active** worksheet) instead of the sheet name parsed from `rangeAddress`. `parseRangeAddress()` correctly extracts `{ sheetName, startCell }` from the range address string (e.g., "Sheet2!A1:D10" → sheetName: "Sheet2", startCell: "A1"), but `sheetName` is destructured and NOT used. Instead, `sheet.name` (the active sheet) is used.

**When does this matter?** `insertResultTable()` uses `getActiveWorksheet()` to insert data and returns `range.address`. If the user has not switched sheets between insertion and the metadata call (which is the normal case -- metadata is written immediately after insertion), there is no mismatch. However:
- If metadata is written later (e.g., batch write), the active sheet could differ from the insertion sheet
- `getTableMetadata()` may be called long after insertion, when the user is on an entirely different sheet

**Fix**: Use `parseRangeAddress(rangeAddress).sheetName` instead of `sheet.name`, or at minimum use the parsed `sheetName` for the `rangeKey` construction. For the named range formula on line 64, also use the parsed sheet name.

### Finding #3: `getTableMetadata` Uses Active Sheet Instead of Range Sheet (MEDIUM)

**File**: `src/utils/workbookMetadata.ts:86`

```ts
const sheet = context.workbook.worksheets.getActiveWorksheet();
```

`getTableMetadata` loads the **active** worksheet but never uses `sheet` for anything besides `sheet.name` on line 88 (which is used in the prefix). However, named items are **cross-sheet** -- `context.workbook.names` returns all named items for the entire workbook. The `sheet` reference on line 86 is loaded but its only use is `sheet.name` on line 88.

If the user is on "Sheet3" when `getTableMetadata("Sheet2!A1:D10")` is called, the prefix becomes `__tessallite_Sheet3_A1_` instead of `__tessallite_Sheet2_A1_`, and no metadata will be found because it was stored under `__tessallite_Sheet2_A1_`.

**Same root cause as Finding #2**: The parsed `sheetName` from `parseRangeAddress` is available but not used. 

**Fix**: Replace `sheet.name` on line 88 with the `sheetName` from `parseRangeAddress(rangeAddress)`.

### Finding #4: `useExcel.insertTable` Return Value Not Consumed Everywhere (LOW)

**File**: `src/App.tsx:245`, `src/hooks/useExcel.ts:21`

```ts
// App.tsx
const rangeAddress = await excelInsertTable(headers, rows);
if (rangeAddress) { showToast(...); }

// useExcel.ts returns Promise<string | null>
```

This works correctly currently. However, if the caller passes `useActiveCell: true` in `options` and the user cancels the overwrite dialog, `insertTable` returns `null`. The caller at App.tsx:245 does not pass `options` (uses default `undefined`), so `useActiveCell` defaults to `false` and `insertResultTable` creates a new table at A1. This is fine.

No bug here in current usage, but worth noting: the `InsertTableOptions` interface has a `confirmOverwrite` property that is never read or used (the confirm dialog is always shown in the catch block regardless of this option flag).

### Finding #5: `projectsLoading` Never Set to False on Empty Projects Branch (LOW)

**File**: `src/App.tsx:191-193`

```ts
} else {
    setProjectsLoading(false);
    setProjectsError('No projects available. Create one in the Tessallite web app.');
}
return Promise.resolve([]);
```

When `projects.length === 0`, the else branch sets `projectsLoading` to `false` and sets an error message. Then `return Promise.resolve([])` executes. The next `.then(models => ...)` checks `if (cancelled || !models) return;` -- since `models` is `[]` (truthy), it proceeds. It then checks `if (models.length > 0)` which is false, so it sets another error: `'No models available for the selected project.'`. This **overwrites** the original error message from line 193.

**Result**: The user sees "No models available for the selected project" instead of the more accurate "No projects available."

**Fix**: Add `return;` before `return Promise.resolve([])` in the no-projects branch, or add `if (!cancelled && models !== undefined) return;` guard in the next `.then()`.

---

## 3. Deep Scan: New Code Paths Introduced in Round 2

### 3.1 Health Poll Toast Debouncing (App.tsx:121-148)

The `wasConnected` tracker correctly prevents toast spam on every poll interval. The effect has `showToast` in the dependency array. Since `showToast` is a stable reference (useCallback with empty deps in ToastProvider), this won't cause excessive re-renders. Verified correct.

### 3.2 Agent Config Loading Effect (App.tsx:216-238)

Uses a `cancelled` flag for cleanup. On error, falls back to `{ configured: false }`. `projectId` is the dependency -- the effect re-runs when projectId changes. On logout (clearSessionState sets projectId to null), the effect sets `agentConfig = { configured: false }` because line 217-218 checks `!projectId`. Verified correct.

### 3.3 clearSessionState (App.tsx:92-105)

Stable callback (empty deps `[]`). References `streamAbortRef` (ref, stable) and state setters (stable). Called from `handleLogout` and `handleSwitchProfile` which include it in their deps. The callback itself never changes between renders. Verified correct.

### 3.4 providerModel Construction (App.tsx:430-432)

```ts
const providerModel = agentConfig.configured && agentConfig.provider && agentConfig.model
    ? `${agentConfig.provider} ${agentConfig.model}`
    : (projectId ? 'Loading provider info...' : undefined);
```

Three states: config loaded and present → shows actual provider; config loading → shows "Loading..."; no project → undefined. The ChatPanel renders the providerModel only when truthy. Verified correct.

### 3.5 Overwrite Warning Re-Insert (useExcel.ts:46-61)

Catch block for `OVERWRITE_WARNING`. Shows `confirm()`, then re-inserts with `useActiveCell: false`. The second `insertResultTable` call goes through the same large-result guard (line 22-32). If the user confirmed the large result guard on the first call, it will fire again on the re-insert attempt -- a minor UX annoyance (double confirmation).

---

## 4. Summary

| Category | Count |
|---|---|
| Round 2 fixes verified | 11 / 11 |
| New findings | 5 (2 medium, 3 low) |
| Security issues | 0 |
| Race conditions | 0 |
| Unwired code (new) | 0 |

**Build status**: `tsc` zero errors, Vite build zero warnings, 7/7 tests passing.

---

## 5. Recommendations by Priority

### MEDIUM
1. **Fix workbookMetadata sheet name** (Findings #2, #3) -- Use `parseRangeAddress` result consistently, replacing `sheet.name` with the parsed `sheetName` in both `setTableMetadata` and `getTableMetadata`.

### LOW
2. **Remove dead `!response.ok` check** (Finding #1) -- In App.tsx:309-311, either remove the dead check or add a comment explaining it's a safety net.
3. **Fix error message overwrite** (Finding #5) -- In App.tsx:191-195, add `return;` before `return Promise.resolve([])` to prevent the "no projects" error from being overwritten by "no models."
4. **Double confirmation on re-insert** (Finding 3.5) -- When the overwrite warning triggers a re-insert, the large-result guard fires again. Consider passing `suppressLargeGuard: true` on the re-insert call.
5. **Remove or implement `confirmOverwrite` option** (Finding #4) -- The `InsertTableOptions.confirmOverwrite` property is defined but never used. Either wire it or remove it.

---

*End of Round 3 review. 11/11 Round 2 fixes verified. 5 new findings, 0 critical.*
