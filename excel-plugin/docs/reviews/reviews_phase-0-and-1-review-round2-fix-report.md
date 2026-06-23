# Phase 0 & Phase 1 — Review Round 2 Fix Report

Date: 2026-05-18
Scope: All 11 findings from `phase-0-and-1-review-round2-findings-report.md`
Status: 11/11 addressed. Build clean, 7/7 tests passing.

---

## 1. Issue #7: Toast System Completely Unwired — FIXED

**File**: `src/App.tsx`

The `ToastProvider` was wrapping `AppInner` but `useToast()` was never called. Fixed:

- `useToast()` imported and called at line 15
- **Table insertion**: `showToast(`Inserted ${N} rows`, 'success')` on success, `showToast('Insert table failed', 'error')` on failure
- **Feedback submission**: `showToast('Feedback recorded', 'success')` on success, `showToast('Feedback submission failed', 'error')` on failure
- **Connection state**: `showToast('Connection restored', 'success')` on reconnect, `showToast('Connection lost. Retrying...', 'error')` on disconnect (debounced via `wasConnected` tracker in health poll effect)

---

## 2. Issue #8: `agentConfigured` Uses Weak Proxy — FIXED

**File**: `src/App.tsx`

Replaced `agentConfigured={!!projectId}` with actual agent config state:

- Added `AgentConfigState` interface with `configured`, `provider`, `model` fields
- Added `agentConfig` state initialized to `{ configured: false }`
- Added `useEffect` (line 170-185) that calls `getAgentConfig(projectId)` whenever `projectId` changes
- Handles cancellation and errors gracefully (falls back to `{ configured: false }`)
- Cleared on logout and profile switch via `clearSessionState()`
- `agentConfigured={agentConfig.configured}` passed to `ChatPanel`

---

## 3. Issue #9: `providerModel` Is Hardcoded — FIXED

**File**: `src/App.tsx:310-313`

Replaced hardcoded `'Tessallite Agent'` with a computed provider model string:

```ts
const providerModel = agentConfig.configured && agentConfig.provider && agentConfig.model
  ? `${agentConfig.provider} ${agentConfig.model}`
  : (projectId ? 'Loading provider info...' : undefined);
```

Shows e.g. "Claude 3.5 Sonnet" when agent config is loaded, "Loading provider info..." while fetching, and nothing when no project exists.

---

## 4. Issue #10: SSE Project/Model Effect Lacks Error Handling — FIXED

**File**: `src/App.tsx:145-168`

Three fixes applied:

1. **Error handling**: Wrapped `getProjects()` / `getModels()` chain in a `.then().then().catch()` pipeline. Network errors, 401s, and empty resource set error messages stored in `projectsError` state.

2. **Loading indicator**: Added `projectsLoading` state. While loading, the Ask mode area shows a `CircularProgress` spinner instead of briefly flashing the "not configured" state. After loading, shows either the error message or the ChatPanel.

3. **Stale projectId on re-login**: Extracted `clearSessionState()` as a reusable function called by both `handleLogout` and `handleSwitchProfile`. It resets `projectId`, `modelId`, `projectsLoading`, `projectsError`, and `agentConfig` to their initial values. This ensures a clean state on every auth transition.

---

## 5. Issue 7.1: Stale Project/Model ID After Profile Switch — FIXED

**File**: `src/App.tsx:84-92`

Created `clearSessionState()` callback that resets all chat and project state:

```ts
const clearSessionState = useCallback(() => {
  if (streamAbortRef.current) {
    streamAbortRef.current.abort();
    streamAbortRef.current = null;
  }
  setMessages([]);
  setConversationId(null);
  setStreaming(false);
  setProjectId(null);
  setModelId(null);
  setProjectsLoading(false);
  setProjectsError(null);
  setAgentConfig({ configured: false });
}, []);
```

Called from `handleLogout` and `handleSwitchProfile` before the auth transition. The project/model auto-load effect re-runs after login because `projectId` is null.

---

## 6. Issue #12: Named Item Accumulation in workbookMetadata — FIXED

**File**: `src/utils/workbookMetadata.ts`

Replaced `Date.now()` suffix with deterministic keys based on sheet name and range address:

- Added `parseRangeAddress()` helper to extract sheet name and start cell from a range address
- Named items now use pattern: `__tessallite_{sheetName}_{startCell}_{key}` (e.g. `__tessallite_Sheet1_A1_pluginVersion`)
- `setTableMetadata()` now deletes old named items matching the same prefix before writing new ones
- This prevents accumulation: re-inserting into the same range updates existing metadata instead of creating duplicates

---

## 7. Issue 7.4: Overwrite Warning Not Propagated to UI — FIXED

**File**: `src/hooks/useExcel.ts:32-52`

`useExcel.insertTable()` now catches the `OVERWRITE_WARNING` error thrown by `officeSpike.ts`:

- Shows `confirm()` dialog: "The active cell range already contains data. Overwrite existing data?"
- If confirmed, re-inserts using `insertResultTable(headers, rows, false)` (forces top-left insertion on current sheet)
- If cancelled, returns `null` (no table inserted)
- Changed return type to `Promise<string | null>` so callers can distinguish success/failure
- `App.tsx` handler checks `rangeAddress` before showing success toast

---

## 8. Issue #11: Content Area ARIA Label Mismatch — FIXED

**File**: `src/App.tsx:360-395`

Moved ARIA attributes from the shared content container to the conditional mode blocks:

- Ask Tessallite block: `<Box role="log" aria-live="polite" aria-label="Chat messages">`
- Report Builder block: `<Box role="region" aria-label="Report Builder placeholder">`
- The outer `component="main"` wrapper retains no ARIA overrides, only structural layout

---

## 9. Issue #13: `getTableMetadata` Returns Stale Data From All Tables — FIXED

**File**: `src/utils/workbookMetadata.ts:76-99`

`getTableMetadata()` now filters named items by the specific table's prefix:

- Uses the same `parseRangeAddress()` helper to derive the prefix `__tessallite_{sheetName}_{startCell}_`
- Only processes named items whose name starts with that prefix
- Previously iterated all `__tessallite_*` items regardless of which table they belonged to

---

## 10. LOW: Phase 2/3 Dead Code Comments — FIXED

Added `// Phase 2:` and `// Phase 3:` markers to file headers of unused code:

| File | Marker |
|------|--------|
| `InsertActions.tsx` | `// Phase 2: Chart, Local Pivot, CUBE formulas, Live connection, Show Query` on unused props |
| `excelFormulas.ts` | `Phase 2: Used by the Cube Function Wizard` |
| `useModel.ts` | `Phase 2: Used by the Report Builder component` |
| `queryRouter.ts` | `Phase 2: Report Builder / Phase 3: Drill-through` |
| `officeSpike.ts` | `Phase 0: runCompatibilitySpike() validates platform support. Must be executed manually` |
| `agentService.ts` | `Phase 2: getAgentPersonas, getConversations, ... / Phase 1: sendMessage uncalled` |
| `useExcel.ts` | `Phase 2: insertFormula, getActiveCellAddress, ... used by Cube Function Wizard` |

---

## 11. LOW: Replace `__dirname` with `import.meta.url` — FIXED

**Files**: `vite.config.ts`, `vitest.config.ts`

Replaced `path.resolve(__dirname, 'src')` with proper ESM-compatible resolution:

```ts
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';
const __dirname = dirname(fileURLToPath(import.meta.url));
```

Both config files now use `import.meta.url`-based path resolution, eliminating reliance on the Vite CJS compatibility shim.

---

## 12. Verification

| Check | Result |
|-------|--------|
| TypeScript compilation (`tsc`) | Zero errors |
| Vite production build | Zero warnings |
| Unit tests (`vitest run`) | 7/7 passing |
| `ToastProvider` wrapping | Active — imported and renders `AppInner` |
| `useToast()` call sites | 3 sites: insert/feedback/connection |

### Command Output

```
$ npm run build
> tsc && vite build
vite v5.4.21 building for production...
✓ 11576 modules transformed.
dist/index.html                  0.41 kB │ gzip:   0.27 kB
dist/assets/index-j11o2Oog.js  400.81 kB │ gzip: 126.98 kB
✓ built in 8.42s

$ npm test
> vitest run
 ✓ src/__tests__/excelFormulas.test.ts  (7 tests) 6ms
 Test Files  1 passed (1)
      Tests  7 passed (7)
```

### Files Modified

| File | Lines Changed | Issues Addressed |
|------|--------------|-----------------|
| `src/App.tsx` | ~110 lines | #7, #8, #9, #10, 7.1, #11 |
| `src/hooks/useExcel.ts` | ~15 lines | 7.4 |
| `src/utils/workbookMetadata.ts` | ~50 lines | #12, #13 |
| `vite.config.ts` | 4 lines changed | ESM compliance |
| `vitest.config.ts` | 4 lines changed | ESM compliance |
| `InsertActions.tsx` | 1 comment line | Dead code markers |
| `excelFormulas.ts` | 1 comment line | Dead code markers |
| `useModel.ts` | 1 comment line | Dead code markers |
| `queryRouter.ts` | 2 comment lines | Dead code markers |
| `officeSpike.ts` | 2 comment lines | Dead code markers |
| `agentService.ts` | 3 comment lines | Dead code markers |
| `useExcel.ts` | 3 comment lines | Dead code markers |

---

## 13. Remaining Deferred Items (Not Actionable in This Environment)

| Item | Status |
|------|--------|
| `runCompatibilitySpike()` | Requires Excel desktop host. Function exists, never called. |
| Manifest GUID `a1b2c3d4-...` | Placeholder intentional for dev. Replace before AppSource submission. |
| Conversation header, persona switcher, follow-up chips | Phase 2 features per execution plan. |

---

*End of Round 2 fix report. 11/11 findings addressed. Build clean, 7/7 tests passing.*
