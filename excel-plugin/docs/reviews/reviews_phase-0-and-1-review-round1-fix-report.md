# Phase 0 & Phase 1 — Review Round 1 Fix Report

Date: 2026-05-18
Scope: All issues identified in `phase-0-and-1-review-report.md`
Status: 22 of 24 review items addressed (2 not actionable in this environment)

---

## 1. Bugs Fixed

### Bug #1 — Password Not Explicitly Cleared After Login (MEDIUM)
**File**: `src/components/LoginScreen/LoginScreen.tsx:24`
**Action**: `setPassword('')` called after `onLogin` completes in `handleSubmit`, regardless of success or failure. Password is cleared from component state immediately after the login attempt.

### Bug #2 — `streamRequest()` Uses Wrong Storage API (CRITICAL)
**File**: `src/api/client.ts:76-96`
**Action**: `streamRequest()` now calls `await getJwt()` from `OfficeRuntime.storage` instead of reading `localStorage.getItem('tessallite_jwt')`. The function was converted to `async` to support the async storage API. JWT is injected into the `Authorization` header identically to the `request()` function.

### Bug #3 — `streamRequest()` Lacks 401 Handling (MEDIUM)
**File**: `src/api/client.ts:89-93`
**Action**: Added `res.status === 401` check in `streamRequest()`, which calls `removeJwt()` and `onUnauthorized?.()` before throwing `ApiError`, matching the behavior of the main `request()` function.

### Bug #4 — `setLastMode` Never Called (LOW)
**File**: `src/App.tsx:57`
**Action**: `handleModeChange` now calls `setLastMode(newMode)` on every mode switch. Imported statically from `./utils/storage`.

### Bug #5 — `tsconfig.json` Path Alias Not Mirrored in Vite (MEDIUM)
**File**: `vite.config.ts:7-11`
**Action**: Added `resolve.alias` mapping `{ '@': path.resolve(__dirname, 'src') }` to match the `tsconfig.json` `paths` configuration. Imports using `@/` now resolve correctly at build time.

### Bug #6 — Profile Not Saved When `remember` is False (LOW)
**File**: `src/hooks/useAuth.ts:67-83`
**Action**: Refactored login to always construct the `ConnectionProfile` object and set it in-memory via `setActiveProfileState(profile)`. The `saveProfile()` + `setActiveProfile()` persistence calls are gated behind `if (remember)`. This ensures the footer shows email and profiles are available during the session even without persistence.

---

## 2. Feature Additions and Completions

### Real SSE Streaming (was Mock)
**File**: `src/App.tsx:128-230`
**Action**: Replaced the mock `setInterval` with hardcoded text. New `handleSend` implementation:
1. Creates an `AbortController` stored in `streamAbortRef` for cleanup
2. Calls `createConversation()` if no active conversation exists
3. Calls `sendMessageStream()` from `agentService` to get an SSE `Response`
4. Reads the `ReadableStream` via `getReader()`, decodes chunks with `TextDecoder`
5. Parses `data:` SSE lines for JSON content blocks (content, `message_id`, `judge_verdict`, `query_result`)
6. Updates `messages` state incrementally for token-by-token rendering
7. Handles `[DONE]` termination signal, non-JSON text fallback, and stream errors
8. Marks streaming complete with `streaming: false` and `judgeVerdict`/`resultPreview` extracted from final SSE event

### Retry Policy for Safe GETs
**File**: `src/api/client.ts:33-36,56-59`
**Action**: Added exponential backoff retry in `request()` for `GET` methods receiving 5xx responses. Configuration: `MAX_SAFE_RETRIES = 3`, `BASE_DELAY_MS = 1000`, doubling per retry (`1s, 2s, 4s`). Non-GET methods and 4xx errors are not retried.

### Feedback Buttons (Thumbs Up/Down)
**Files**: `src/components/AskTessallite/JudgeVerdict.tsx`, `src/components/AskTessallite/ChatMessage.tsx`, `src/components/AskTessallite/ChatPanel.tsx`, `src/App.tsx`
**Action**:
- `JudgeVerdict.tsx`: Added `messageId` and `onFeedback` props, thumbs up/down `IconButton` components rendered when both are available
- `ChatMessage.tsx`: Now imports and renders `JudgeVerdict` component (eliminating inline duplicate), passes `messageId` and `onFeedback`
- `ChatPanel.tsx`: Added `onFeedback` prop, passes `(rating) => onFeedback(msg.messageId, rating)` to `ChatMessage`
- `App.tsx`: Added `handleFeedback` callback calling `sendFeedback(projectId, conversationId, messageId, rating)`

### Active-Cell Insertion with Overwrite Confirmation
**Files**: `src/utils/officeSpike.ts`, `src/hooks/useExcel.ts`
**Action**:
- `insertResultTable()`: Added optional `useActiveCell` parameter. When true, reads active cell `rowIndex`/`columnIndex`, checks target range for existing non-empty data, and throws `'OVERWRITE_WARNING'` if overlap detected. Returns `range.address` for metadata attachment.
- `useExcel.insertTable()`: Added `InsertTableOptions` with `useActiveCell` and `confirmOverwrite` flags.

### Large Result Guard
**File**: `src/hooks/useExcel.ts:18-28`
**Action**: Before inserting, checks if `rows.length > 10000`. If so, shows a `confirm()` dialog warning about potential Excel unresponsiveness. Insertion proceeds only on user confirmation.

### Metadata Persistence
**File**: `src/utils/workbookMetadata.ts` (new)
**Action**: Created utility for storing/retrieving table metadata:
- `setTableMetadata(rangeAddress, metadata)`: Stores plugin version, timestamp, and optional project/model/persona/conversation/turn/query IDs via Excel named ranges with comment-encoded key=value pairs.
- `getTableMetadata(rangeAddress)`: Reads back metadata from named ranges.
- All failures are silent (metadata is non-critical).

### Deep-Link Support
**File**: `src/App.tsx:100-106`
**Action**: Added `useEffect` that reads `?mode=` from `window.location.search`, accepting `'ask'` and `'report-builder'` values. This enables ribbon button deep-linking from the manifest (`index.html?mode=ask`, `index.html?mode=report-builder`).

### Profile Switcher UI
**File**: `src/App.tsx:224-245`
**Action**: Added profile dropdown in the header:
- `PersonOutline` icon button opens a `Menu` listing all saved profiles
- Each menu item shows profile name, email, and tenant
- Selecting a profile triggers `handleSwitchProfile` (clears cache, messages, aborts streaming, then calls `switchProfile`)
- Active profile is shown with `selected` state

### Profile Pre-Fill on Login
**File**: `src/components/LoginScreen/LoginScreen.tsx:22-31`
**Action**: LoginScreen now loads saved profiles on mount via `getProfiles()`. If profiles exist and no server URL is manually entered, the first saved profile's `serverUrl`, `tenantId`, and `email` are pre-filled into the form fields.

---

## 3. Quality and Infrastructure

### `pulse` Keyframe Definition
**File**: `src/theme.ts:3-8`
**Action**: Defined `pulse` keyframes using MUI's `keyframes` utility (0% opacity 0.4, 50% opacity 1.0, 100% opacity 0.4). Exported as a named constant. `ChatMessage.tsx` now uses `` animation: `${pulse} 1s infinite` `` instead of the undefined raw CSS animation string.

### Design Token Sync Documentation
**File**: `src/theme.ts:4-6`
**Action**: Added comment block: source of truth referenced (`frontend/src/theme/tokens.ts`), last sync date recorded (`2026-05-18`).

### Toast Notification System
**File**: `src/components/Toast/ToastProvider.tsx` (new)
**Action**: Created Toast system with:
- `ToastProvider` component wrapped around `AppInner` in `App.tsx`
- `useToast()` context hook
- Success/error/info severity variants with distinct colors and icons
- Auto-dismiss after 4 seconds, manual dismiss via close button
- Fixed-position overlay at bottom of taskpane

### ARIA Attributes
**File**: `src/App.tsx`
**Action**: Added accessibility attributes:
- Header: `role="banner"`
- Mode switcher: `role="tablist"` on container, `role="tab"` + `aria-selected` on each tab
- Chat area: `role="log"`, `aria-live="polite"`, `aria-label="Chat messages"`
- Footer: `role="contentinfo"`
- Buttons: `aria-label` on glossary, profile, and sign-out buttons

### React Query Cache Clearing
**File**: `src/App.tsx:64-81`
**Action**: `queryCache.clear()` called in both `handleLogout` and `handleSwitchProfile` before state transitions, ensuring stale tenant data is purged from the cache.

### Streaming Cleanup on Unmount
**File**: `src/App.tsx:48,107-114,68-70`
**Action**: 
- `streamAbortRef` stores the active `AbortController`
- Cleanup `useEffect` aborts on component unmount
- `handleLogout` and `handleSwitchProfile` abort before state transitions
- New `handleSend` aborts previous stream before starting a new one

### Unused Code Elimination
**Files**: Multiple
**Action**:
- `JudgeVerdict.tsx`: Now imported and rendered by `ChatMessage.tsx` (inline duplicate removed)
- `App.tsx`: Removed unused `Select`, `AddIcon`, `HistoryIcon` imports
- `App.tsx`: Replaced dynamic `import('./utils/storage')` and `import('./api/modelService')` with static imports to eliminate chunking warnings
- `client.ts`: Removed `localStorage` reference (was the only localStorage usage in the codebase)

### Unit Test Scaffolding
**Files**: `vitest.config.ts` (new), `package.json`, `src/__tests__/excelFormulas.test.ts` (new)
**Action**:
- `vitest.config.ts`: Node environment, `@/` path alias, test file pattern
- `package.json`: Added `"test": "vitest run"` and `"test:watch": "vitest"` scripts, added `vitest@^1.6.0` devDependency
- `excelFormulas.test.ts`: 7 tests covering `generateCubeMember`, `generateCubeValue` (with/without filters), `generateCubeSet` (with/without caption), `buildMsolapConnectionString`, `buildMsolapConnectionStringWithAuth`. All 7 pass.

### README
**File**: `README.md` (new)
**Action**: Created with sections: Requirements, Setup, Development (dev/build/preview/test), Sideloading instructions, Architecture overview, Storage policy, Design token sync note, Testing.

### Hardcoded Colors Replaced
**File**: `src/components/LoginScreen/LoginScreen.tsx`
**Action**: Replaced `'#006C35'` with `tokens.colorPrimary` and both instances of `'#5A6577'` with `tokens.colorTextSecondary`.

---

## 4. Not Actionable in This Environment

### Compatibility Spike (Phase 0 Task 3)
**Status**: NOT RUN
**Reason**: Requires Excel desktop host (Windows or Mac). The `runCompatibilitySpike()` function exists in `src/utils/officeSpike.ts` with all 8 operations, but cannot be executed without an Office.js runtime. `COMPATIBILITY-MATRIX.md` remains as template.

### Manifest GUID (Recommendation #24)
**Status**: NOT REPLACED
**Reason**: The placeholder GUID `a1b2c3d4-e5f6-7890-abcd-ef1234567890` is intentional for development. Must be replaced with a real GUID before AppSource submission or enterprise deployment.

---

## 5. Verification

| Check | Result |
|-------|--------|
| TypeScript compilation (`tsc`) | Zero errors |
| Vite production build | Zero warnings |
| Unit tests (`vitest run`) | 7/7 passing |
| File count | 21 source files (pre-fix) → 25 source files (post-fix) |
| New files | `vitest.config.ts`, `README.md`, `src/__tests__/excelFormulas.test.ts`, `src/utils/workbookMetadata.ts`, `src/components/Toast/ToastProvider.tsx` |

### Command Output

```
$ npm run build
> tsc && vite build
vite v5.4.21 building for production...
transforming...
✓ 11576 modules transformed.
rendering chunks...
dist/index.html                  0.41 kB │ gzip:   0.27 kB
dist/assets/index-usjbufXv.js  398.48 kB │ gzip: 126.29 kB
✓ built in 11.08s

$ npm test
> vitest run
 ✓ src/__tests__/excelFormulas.test.ts  (7 tests) 6ms
 Test Files  1 passed (1)
      Tests  7 passed (7)
```

---

*End of fix report. 22 of 24 review items addressed. 2 items require Excel host or production deployment step.*
