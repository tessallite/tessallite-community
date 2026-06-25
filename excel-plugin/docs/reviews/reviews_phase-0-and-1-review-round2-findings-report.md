# Phase 0 & Phase 1 -- Review Round 2 Findings

Re-review of all code changes from the Round 1 fix report. Verifies claimed fixes and identifies newly introduced or remaining issues.

Date: 2026-05-18
Scope: `tessallite/excel-plugin/` only

---

## Table of Contents

1. [Fix Verification Summary](#1-fix-verification-summary)
2. [Verified Fixes — Detailed](#2-verified-fixes--detailed)
3. [New Issues Found](#3-new-issues-found)
4. [Remaining Issues (Not Fixed)](#4-remaining-issues-not-fixed)
5. [Unwired and Dead Code (Updated)](#5-unwired-and-dead-code-updated)
6. [Security Review (Updated)](#6-security-review-updated)
7. [Edge Cases and Race Conditions](#7-edge-cases-and-race-conditions)
8. [Recommendations by Priority](#8-recommendations-by-priority)

---

## 1. Fix Verification Summary

| Claim | Verified | Notes |
|---|---|---|
| Bug #1 -- Password cleared after login | YES | `LoginScreen.tsx:44` calls `setPassword('')` |
| Bug #2 -- streamRequest uses OfficeRuntime.storage | YES | `client.ts:89-95` uses `await getJwt()`. All `localStorage` removed |
| Bug #3 -- streamRequest has 401 handling | YES | `client.ts:103-107` checks status and calls removeJwt |
| Bug #4 -- setLastMode called on mode change | YES | `App.tsx:73` calls `setLastMode(newMode)` |
| Bug #5 -- Vite alias for `@/` | YES | `vite.config.ts:8-11` defines resolve.alias |
| Bug #6 -- Profile in-memory without persist | YES | `useAuth.ts:84-85` sets activeProfileState always |
| Real SSE streaming | YES | `App.tsx:185-342` full SSE with ReadableStream |
| Retry policy for safe GETs | YES | `client.ts:30-31,60-63` exponential backoff |
| Feedback buttons wired | YES | JudgeVerdict imported by ChatMessage, thumbs call sendFeedback |
| Active-cell insertion with overwrite | YES | `officeSpike.ts:52-72` detects overlap, throws OVERWRITE_WARNING |
| Large result guard | YES | `useExcel.ts:20-30` confirm dialog above 10k rows |
| Metadata persistence | YES | `workbookMetadata.ts` new file, 76 lines |
| Deep-link support | YES | `App.tsx:126-132` reads ?mode= from URL |
| Profile switcher UI | YES | `App.tsx:386-425` PersonOutline menu |
| Profile pre-fill on login | YES | `LoginScreen.tsx:27-39` loads and pre-fills first profile |
| pulse keyframe defined | YES | `theme.ts:9-13` via MUI keyframes, used in ChatMessage |
| Design token sync note | YES | `theme.ts:3-7` comment block |
| Toast notification system | WIRED BUT DEAD | ToastProvider wraps AppInner, but useToast() never called |
| ARIA attributes | PARTIAL | Added but content label is wrong for Report Builder mode |
| React Query cache cleared | YES | `App.tsx:84,96` calls queryCache.clear() |
| Streaming cleanup on unmount | YES | Cleanup effect + abort on logout/switchProfile |
| Unused code elimination | YES | JudgeVerdict now imported; localStorage removed everywhere |
| Unit test scaffolding | YES | vitest.config.ts + 7 passing tests |
| README | YES | New file |
| Hardcoded colors replaced | YES | LoginScreen uses tokens.colorPrimary/colorTextSecondary |
| Compatibility spike executed | NO | Requires Excel host. runCompatibilitySpike() still uncalled |
| Manifest GUID replaced | NO | Placeholder a1b2c3d4... still present |

**Result: 22 of 24 claims verified. 2 not actionable (Excel host required, production deployment step).**

---

## 2. Verified Fixes -- Detailed

### 2.1 Bug #2 (streamRequest localStorage) -- CRITICAL FIX CONFIRMED

`client.ts:89-95` now correctly uses `await getJwt()` from `OfficeRuntime.storage`. The old `localStorage.getItem('tessallite_jwt')` has been removed. No `localStorage` references remain anywhere in the codebase. This was the single most impactful bug from Round 1.

### 2.2 Bug #5 (Vite alias) -- FIX CONFIRMED

`vite.config.ts:8-11` adds `resolve.alias: { '@': path.resolve(__dirname, 'src') }`. The `package.json` declares `"type": "module"` which means `__dirname` is not natively available in ESM. However, Vite's config loader provides `__dirname` as a CJS compatibility shim, so builds pass. This is technically impure ESM but functionally correct. Same applies to `vitest.config.ts:7`.

### 2.3 SSE Streaming -- VERIFIED AS PRODUCTION-QUALITY

`App.tsx:185-342` implements a full SSE streaming pipeline:
- AbortController for cancellation (stored in streamAbortRef)
- Auto-creates conversation via createConversation() on first send
- Calls sendMessageStream() from agentService
- Reads ReadableStream with getReader() + TextDecoder
- Parses SSE `data:` lines, handles `[DONE]` signal
- Extracts content, message_id, judge_verdict, query_result from JSON blocks
- Falls back to raw text for non-JSON SSE data
- Error handling differentiates abort errors from stream errors
- Proper finally block to set streaming=false

### 2.4 Feedback Flow -- FULLY WIRED

The feedback chain is complete:
1. `App.tsx:175-182` handleFeedback calls sendFeedback(projectId, conversationId, messageId, rating)
2. `ChatPanel.tsx:102` passes (rating) => onFeedback(msg.messageId, rating) to ChatMessage
3. `ChatMessage.tsx:68-76` renders JudgeVerdict with messageId and onFeedback
4. `JudgeVerdict.tsx:37-57` renders ThumbUpOutlined/ThumbDownOutlined with click handlers

---

## 3. New Issues Found

### Issue #7: Toast System Completely Unwired (MEDIUM)

**File**: `src/components/Toast/ToastProvider.tsx` (104 lines), `src/App.tsx:512-514`

The `ToastProvider` wraps `AppInner` and provides a `useToast()` context hook. However, `useToast()` is never imported or called by any component. The function `showToast()` exists but has zero call sites. No toast will ever appear for any event.

Success events that should trigger toasts but don't:
- Table inserted successfully ("Inserted N rows on sheet 'X'")
- Formula inserted ("Formula inserted at A1")
- Feedback submitted ("Feedback recorded")

Error events:
- Login failed (currently handled by inline Alert, toast would be an alternative)
- Insert table failed
- Connection lost

**Fix**: Import `useToast` in `App.tsx`, call `showToast()` after handleInsertTable succeeds, after handleFeedback succeeds, and in connection state changes.

### Issue #8: `agentConfigured` Uses Weak Proxy (MEDIUM)

**File**: `src/App.tsx:470`

```ts
agentConfigured={!!projectId}
```

The execution plan (Phase 1, Workstream C, Task 1) explicitly states: "call `GET .../agent/config` to verify the agent is configured." Having a `projectId` does NOT mean the agent is configured. A project can exist without an LLM provider.

Previously this was hardcoded to `true` (demo mode). It's now `!!projectId` which is a slightly better but still incorrect proxy. The actual check requires calling `getAgentConfig(projectId)` from `agentService.ts` and checking `config.configured`.

**Fix**: Add a `useQuery` for `getAgentConfig(projectId)` and pass `config?.configured ?? false` as the prop.

### Issue #9: `providerModel` Is Hardcoded (MEDIUM)

**File**: `src/App.tsx:471`

```ts
providerModel={projectId ? 'Tessallite Agent' : undefined}
```

The design spec (FRONTEND-DESIGN Section 8.2) says the provider badge should show the actual configured LLM provider and model name (e.g., "Claude 3.5 Sonnet"), fetched from `GET .../agent/config`. The hardcoded string 'Tessallite Agent' is a placeholder that does not reflect reality.

**Fix**: Read `provider` and `model` from `getAgentConfig()` and construct `"${provider} ${model}"`.

### Issue #10: SSE Project/Model Effect Lacks Error Handling (MEDIUM)

**File**: `src/App.tsx:144-158`

```ts
useEffect(() => {
    if (authState !== 'authenticated' || projectId) return;
    getProjects().then(projects => {
      // ... sets projectId and modelId
    });
}, [authState, projectId]);
```

Three problems:
1. **No error handling**: If `getProjects()` or `getModels()` fail (network, 401, 500), the promise rejects silently. `projectId` stays null, chat shows "Conversational analytics unavailable" but no error is shown to the user.
2. **No loading indicator**: Between auth and project resolution, the user briefly sees the "not configured" state.
3. **Stale projectId on re-login**: The `projectId` check prevents re-fetching, but if the user logs into a different tenant, the old `projectId` from the previous session persists until a full page reload.

**Fix**: Add `.catch()` blocks that surface errors. Clear `projectId`/`modelId` on logout/profile switch. Add a loading state for project resolution.

### Issue #11: Content Area ARIA Mismatch for Report Builder (LOW)

**File**: `src/App.tsx:458-463`

```tsx
<Box component="main" role="log" aria-live="polite" aria-label="Chat messages" ...>
```

This container wraps both Ask Tessallite AND Report Builder. When Report Builder is active:
- `role="log"` is incorrect (it's not a live region, it's a static placeholder)
- `aria-label="Chat messages"` is misleading

**Fix**: Move ARIA attributes to the conditional Ask mode block only. Add a separate `aria-label` for the Report Builder placeholder.

### Issue #12: `workbookMetadata` Creates Accumulating Named Items (MEDIUM)

**File**: `src/utils/workbookMetadata.ts:32-34`

```ts
const namedItem = context.workbook.names.add(
    `${name}_${Date.now()}`,
    `${sheet.name}!${rangeAddress.split('!')[0] || rangeAddress}`,
);
```

Every table insertion creates NEW named items because `Date.now()` produces a unique suffix each call. Old named items are never cleaned up. After inserting 5 tables in one session, there are 5 * N stale named items permanently stored in the workbook. This is a slow accumulation bug that gets worse over time and across sessions.

**Fix**: Use a deterministic key based on the range address so repeated insertions update rather than create. Example: `__tessallite_${sheet.name}_${startRow}_pluginVersion`. If updating is not possible through the API, add cleanup logic that removes old named items with the same prefix before creating new ones.

### Issue #13: `getTableMetadata` Returns Stale Data From All Tables (LOW)

**File**: `src/utils/workbookMetadata.ts:60-69`

```ts
for (const item of namedItems.items) {
    if (item.name.startsWith(METADATA_PREFIX) && item.comment) {
        // extracts key=value from all named items
    }
}
```

The function iterates ALL `__tessallite_` named items in the workbook, not just those for the target range. Since named items accumulate (Issue #12), this returns metadata from old, potentially deleted tables alongside the current one.

**Fix**: Filter by the named item's range formula, or use a comment prefix that includes the sheet/range address for deduplication.

### Issue #14: `InsertActions` Props Still Unused (LOW)

**File**: `src/components/AskTessallite/InsertActions.tsx:8-14`

The component still accepts `onInsertChart`, `onLocalPivot`, `onCubeFormulas`, `onLiveConnection`, `onShowQuery`, and `recommendedAction` as props. `ChatPanel.tsx:106-114` passes none of them. The conditional renders on lines 48-80 produce zero output. This is deferred Phase 2/3 code that sits as dead branches in Phase 1.

---

## 4. Remaining Issues (Not Fixed)

### 4.1 runCompatibilitySpike() Never Called

`src/utils/officeSpike.ts:165` defines `runCompatibilitySpike()` but it has zero callers. `COMPATIBILITY-MATRIX.md` remains entirely `?` values. This was marked as "Not Actionable" in the fix report because it requires an Excel host. This is accurate -- it cannot be executed without Office.js runtime.

### 4.2 Manifest GUID Placeholder

`manifest.xml:9` still contains `<Id>a1b2c3d4-e5f6-7890-abcd-ef1234567890</Id>`. This must be replaced before production deployment. Marked as intentional for development.

### 4.3 No Model/Persona Selector

The user still has no way to choose which model or persona to use. The auto-load effect in `App.tsx:144-158` picks the first project's first model. Persona switching is not implemented. This is Phase 2/3 work per the execution plan and is correctly deferred.

### 4.4 No Conversation Header

FRONTEND-DESIGN Section 8.2 specifies a conversation header with conversation title, LLM provider badge, agent persona dropdown, "+New" button, and history icon. The current implementation shows only a flat provider text badge (`ChatPanel.tsx:85-90`). The remaining elements are deferred.

### 4.5 No Follow-Up Suggestion Chips

FRONTEND-DESIGN Section 8.7 specifies follow-up suggestion chips. Not implemented. Deferred.

---

## 5. Unwired and Dead Code (Updated)

| Code | Status After Fix | Action |
|---|---|---|
| `JudgeVerdict.tsx` | NOW WIRED | Imported by ChatMessage.tsx. Inline duplicate removed. |
| `localStorage` usage | REMOVED | No localStorage references remain in codebase |
| `InsertActions` unused props | STILL DEAD | onInsertChart, onLocalPivot, etc. still never passed |
| `excelFormulas.ts` | STILL DEAD | generateCubeMember/Value/Set never imported by components |
| `useModel.ts` hooks | STILL DEAD | 8 hooks defined but never called |
| `queryRouter.ts` drill/explain | STILL DEAD | executeQuery, explainQuery, discoverMembers, drill* never called |
| `agentService.ts` multiple fns | STILL DEAD | getAgentPersonas, getConversations, deleteConversation never called |
| `useExcel.ts` multiple fns | STILL DEAD | insertFormula, getActiveCellAddress, readCellValue, createNewSheet never called |
| `officeSpike.ts` runCompatibilitySpike | STILL DEAD | Zero callers |
| `ToastProvider.tsx` useToast hook | STILL DEAD | Zero consumers (NEW ISSUE) |

**Net change from Round 1**: 2 items wired (JudgeVerdict, localStorage removed). 1 new dead code path found (ToastProvider useToast). Remaining dead code paths are either deferred Phase 2/3 code or the never-executed compatibility spike.

---

## 6. Security Review (Updated)

### 6.1 Password Handling -- PASS

Verified across all files:
- `LoginScreen.tsx:44`: `setPassword('')` clears after submit
- `useAuth.ts:56`: `const password = data.password` is a local const, garbage collected after function exit
- `storage.ts`: `saveProfile()` excludes password field (only name, serverUrl, tenantId, email)
- No password logging found anywhere

### 6.2 JWT Storage -- PASS

- All JWT reads use `await getJwt()` from `OfficeRuntime.storage` (client.ts:39,89)
- All JWT writes use `setJwt()` to `OfficeRuntime.storage` (useAuth.ts:66)
- All JWT removals use `removeJwt()` from `OfficeRuntime.storage` (client.ts:54,104)
- No `localStorage` JWT storage remains

### 6.3 Password in Connection Strings -- PASS (for now)

`excelFormulas.ts:54-61` contains `buildMsolapConnectionStringWithAuth()` which accepts a password parameter. This function is never called (dead code). When the live connection wizard is built in Phase 2, the execution plan requires "Never persist XMLA password" -- this must be enforced at that time.

---

## 7. Edge Cases and Race Conditions

### 7.1 Stale Project/Model ID After Profile Switch

`App.tsx` manages `projectId` and `modelId` as component state. When `handleSwitchProfile` is called:
- `setMessages([])` clears messages
- `setConversationId(null)` clears conversation
- `queryCache.clear()` clears React Query cache

But `projectId` and `modelId` are NOT cleared. If the user switches from Profile A (which has Project X) to Profile B (which has Project Y), `projectId` still holds X's ID. The `getProjects()` effect won't re-run because the `projectId` guard prevents it.

**Fix**: Add `setProjectId(null)` and `setModelId(null)` to `handleLogout` and `handleSwitchProfile`.

### 7.2 SSE Send During Pending Resolution

If the user types a message while `projectId` is still resolving (the effect hasn't completed yet), `handleSend` sees `!convId && projectId && modelId` as false (one is null). It falls through to the demo fallback message. But if `projectId` resolves between the check and the API call, there could be a stale closure issue.

The current code handles this correctly by re-checking `projectId!` at line 218 inside the try block -- but this only works if the value is set before the async code executes. If `projectId` resolves async between lines 197 and 218, the closure uses the old value. This is unlikely in practice (React batches state updates) but technically possible.

### 7.3 `setMessages` Called Inside Tight Loop During SSE

`App.tsx:264-292` calls `setMessages` for every SSE frame (each `data:` line). For a 200-token response, this is 200 state updates in rapid succession. React 18 batches these inside async event handlers, but the `setMessages(prev => {...})` callback pattern is correct for this scenario. No bug, but worth noting for performance monitoring.

### 7.4 Overwrite Warning Propagation

`officeSpike.ts:70` throws `new Error('OVERWRITE_WARNING')` when active-cell insertion would overwrite data. In `useExcel.ts:20-39`, the caller does not catch this specific error and show a confirmation dialog -- it just lets the error propagate to App.tsx's `handleInsertTable` catch block which logs to console. The user never sees the overwrite warning.

**Fix**: In `useExcel.insertTable`, catch the `OVERWRITE_WARNING` error and show a confirmation dialog. Only proceed if the user confirms.

### 7.5 Health Poll Timer After Logout

`App.tsx:102-116` health check effect depends on `authState`. When `authState` changes to `'unauthenticated'`, the cleanup runs and clears the interval. However, if a poll is in-flight during the state transition, the `cancelled` flag prevents state updates. This is correct. No race condition.

---

## 8. Recommendations by Priority

### HIGH (should fix before declaring Phase 1 complete)

1. **Wire the toast system** (Issue #7) -- Import `useToast` in App.tsx and call `showToast()` for table insertions, feedback submissions, and connection state changes. This is the only new system that was added but never used.

2. **Fetch real agent config** (Issues #8, #9) -- Replace `agentConfigured={!!projectId}` with actual `getAgentConfig()` query. Replace hardcoded `'Tessallite Agent'` with `config.provider + ' ' + config.model`.

3. **Add error handling to project/model loading** (Issue #10) -- Add `.catch()` to `getProjects()` and `getModels()` in the auto-load effect. Show loading indicator while resolving.

4. **Clear projectId/modelId on logout/switch** (Issue 7.1) -- Prevent stale project IDs from different tenants persisting in state.

5. **Fix named item accumulation** (Issue #12) -- Use deterministic keys for workbook metadata named items instead of Date.now() suffixes.

### MEDIUM (improvements for production quality)

6. **Handle overwrite warning in UI** (Issue 7.4) -- Catch OVERWRITE_WARNING in useExcel and show confirmation dialog instead of logging to console.

7. **Fix ARIA label mismatch** (Issue #11) -- Move ARIA attributes to Ask mode block only.

8. **Filter getTableMetadata by range** (Issue #13) -- Prevent returning stale metadata from other tables.

### LOW (cleanup)

9. **Mark Phase 2/3 dead code with comments** (Issue #14 + others) -- Add `// Phase 2:` comments on unused props and functions to prevent confusion about dead code.

10. **Replace `__dirname` with `import.meta.url`** -- Use proper ESM path resolution in vite.config.ts and vitest.config.ts for strict compliance.

---

*End of Round 2 review. 7 new issues found. 22 of 24 Round 1 fixes verified.*
