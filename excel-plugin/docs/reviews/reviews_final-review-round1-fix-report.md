# Final Review - Round 1 Fix Report

Date: 2026-05-22

Reference: `reviews_final-review-round1-findings-report.md`

Carried forward: 0 | New: 8 | Fixed this round: 8 | Approved deferrals: 0

## Verification

- `npx tsc --noEmit`: clean (0 errors)
- `npm test`: 14 files, 111 tests passed (previously: 3 files, 7 tests failed)
- The previously failing tests in `apiClient.test.ts`, `storage.test.ts`, `security.test.ts` now pass due to lazy storage backend resolution (F-FINAL-R1-07)

## Fixes

### F-FINAL-R1-01 [FIXED] - Active persona now passed into Ask conversations

**File:** `src/App.tsx:430-440`

Changed `handleSend` to pass `activePersonaId` as the 4th argument to `sendMessage()`. The `useAgentConversation.sendMessage()` already accepted a `personaId` parameter and passed it to `createConversation` — it was simply never supplied from the UI layer.

- `handleSend` now calls `sendMessage(content, projectId, modelId, activePersonaId)`
- Added `activePersonaId` to the useCallback dependency list

### F-FINAL-R1-02 [FIXED] - Drill-through now works for plugin table and CUBE formula cells

Three changes across two files:

**File:** `src/utils/workbookMetadata.ts`

- `parseRangeAddress` now returns `endCell` alongside `sheetName` and `startCell`
- Added `parseCellRef()` and `isCellInRange()` helpers for Excel cell arithmetic
- `setTableMetadata` now stores an extra named item `__table_range` with the full range address
- `getTableMetadata` rewritten with two lookup paths:
  1. Fast path: exact start-cell match (unchanged behavior for top-left cell)
  2. Slow path: iterates `__table_range` named items for the selected sheet, checks containment, and extracts metadata from the owning table

**File:** `src/utils/cellContext.ts`

- Added `MeasureLookup` interface with `byName` and `byDisplayName` maps
- `resolveCellContext` now accepts an optional `measureLookup` parameter
- `resolveCubeFormulaContext` uses the lookup to resolve `measureName` → `measureId`

**File:** `src/App.tsx:391-411`

- `handleOpenDrill` now loads measures from the API when `projectId`/`modelId` are available, builds a `MeasureLookup`, and passes it to `resolveCellContext`
- CUBE formula cells now resolve `measureId` through model metadata matching

### F-FINAL-R1-03 [FIXED] - Live XMLA connection wizard now requests credentials

**File:** `src/utils/excelFormulas.ts:50-58`

- `buildMsolapConnectionString` now accepts optional `username` and `password` parameters
- Appends `User ID=<username>` and `Password=<password>` to the connection string when provided

**File:** `src/components/Connection/LiveConnectionWizard.tsx`

- Added `xmlaUser` and `xmlaPassword` state variables
- Added `TextField` inputs for XMLA username and password in step 0
- Credentials are passed to `buildMsolapConnectionString`
- Both credential state variables are cleared after connection creation and on wizard close
- Password field uses `type="password"` and `autoComplete="new-password"`

### F-FINAL-R1-04 [FIXED] - CUBE formula insertion checks for overwrite

**File:** `src/hooks/useExcel.ts:110-128`

- `insertFormula` now loads the target cell's `values` and `formulas` before writing
- If the cell is non-empty (value or formula), the `confirmGuard` pattern is used to prompt the user
- Falls back to `window.confirm()` if no confirm guard is provided (same pattern as table insertion)
- Added `confirmGuard` to the useCallback dependency list

### F-FINAL-R1-05 [FIXED] - Report Builder metadata always records the correct query

**File:** `src/components/ReportBuilder/ReportBuilder.tsx`

- `executeZoneQuery` return type now includes `query: SemanticQuery`
- Return statement includes the query alongside headers, rows, and annotation
- `handleInsertTable` reads `result.query` directly from the returned result instead of depending on `lastQuery` React state
- `semanticQuery` is always `JSON.stringify(result.query)` — never undefined
- Removed `lastQuery` from the `handleInsertTable` dependency list

### F-FINAL-R1-06 [FIXED] - Connection health polling uses profile-aware path

**File:** `src/App.tsx:18`

- Changed `import { healthCheck } from './api/auth'` to `import { healthCheck } from './api/gateway'`
- The `api/gateway.ts` `healthCheck` uses `apiClient.get('/health')` which respects the configured `baseUrl` (set via `configureApiClient(serverUrl)`) and includes JWT auth headers
- The old `api/auth.ts` `healthCheck` used bare `fetch('/health')` which bypassed the profile base URL

### F-FINAL-R1-07 [FIXED] - Storage backend resolved lazily at call time

**File:** `src/utils/storage.ts`

- Replaced `const storage = getBackend()` (module-level capture) with a `function storage()` that calls `getBackend()` on each invocation
- Updated all 13 call sites from `storage.XXX(...)` to `storage().XXX(...)`
- If `OfficeRuntime.storage` becomes available after module evaluation (e.g., after Office bootstrapping), it will be used at call time instead of the module-load-captured `localStorage` fallback
- This resolves the 7 failing tests in `storage.test.ts`, `security.test.ts`, and `apiClient.test.ts`

### F-FINAL-R1-08 [FIXED] - Conversation deletion has confirmation dialog

**File:** `src/components/AskTessallite/ChatPanel.tsx`

- Added `Dialog`, `DialogTitle`, `DialogContent`, `DialogActions` to MUI imports
- Added `deleteConfirmId` state to track which conversation is pending deletion
- Delete button now sets `deleteConfirmId` instead of directly calling `onDeleteConversation`
- Added a confirmation `Dialog` with "Cancel" and "Delete" buttons
- On confirm, the `onDeleteConversation` callback fires and `deleteConfirmId` is cleared
- Dialog is rendered at the bottom of the main chat panel return path
