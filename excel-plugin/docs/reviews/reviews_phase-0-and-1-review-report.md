# Phase 0 & Phase 1 -- Deep Review Report

Comprehensive audit of the Tessallite Excel plugin implementation against `EXECUTION-PLAN.md`, `SPECS.md`, and `FRONTEND-DESIGN.md`.

Date: 2026-05-18
Scope: `tessallite/excel-plugin/` only

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Phase 0 Audit: Project Setup and Technical Spike](#2-phase-0-audit)
3. [Phase 1 Audit: Foundation and Ask MVP](#3-phase-1-audit)
   - 3.1 [Workstream A: Authentication and Profiles](#31-workstream-a-authentication-and-profiles)
   - 3.2 [Workstream B: API Client Foundation](#32-workstream-b-api-client-foundation)
   - 3.3 [Workstream C: Ask Tessallite](#33-workstream-c-ask-tessallite)
   - 3.4 [Workstream D: Insert as Table](#34-workstream-d-insert-as-table)
4. [Bugs and Logic Errors](#4-bugs-and-logic-errors)
5. [Unwired and Dead Code](#5-unwired-and-dead-code)
6. [Security Review](#6-security-review)
7. [Race Conditions and Async Issues](#7-race-conditions-and-async-issues)
8. [Missing Setup and Infrastructure](#8-missing-setup-and-infrastructure)
9. [Scope Cuts and Missing Features](#9-scope-cuts-and-missing-features)
10. [Query Routing Audit](#10-query-routing-audit)
11. [Cross-Platform Compatibility](#11-cross-platform-compatibility)
12. [Design Token and Styling Issues](#12-design-token-and-styling-issues)
13. [Manifest and Ribbon Audit](#13-manifest-and-ribbon-audit)
14. [Rough Edges and Cut Corners](#14-rough-edges-and-cut-corners)
15. [Recommendations by Priority](#15-recommendations-by-priority)

---

## 1. Executive Summary

The codebase comprises 21 source files across 7 directories. Phase 0 scaffolding is structurally complete but the compatibility spike was never executed. Phase 1 delivers a working authentication flow with a mock chat demo, but 4 confirmed bugs exist, approximately 40% of planned features are missing or stubbed, and the chat implementation uses hardcoded demo data instead of real API calls.

**Overall completion**: Approximately 55% of Phase 0 + Phase 1 combined.

**Critical findings**:
- 1 confirmed security bug (JWT fetch from wrong storage API in streamRequest)
- 4 logic bugs (missing setLastMode persist, password not explicitly cleared, health polling not cancelled on auth state change, unsaved profile on non-remember login)
- 1 architectural concern (duplicate judge verdict rendering)
- 8 dead/unwired code paths
- 0 issues found with queries bypassing the query router

---

## 2. Phase 0 Audit

### 2.1 Task 1: Scaffold App

**Status: COMPLETE**

All required files exist:
- `manifest.xml` (142 lines) -- well-structured, 3 ribbon buttons (Connect, Ask, Report Builder), deep-link URLs with `?mode=` params
- `package.json` (29 lines) -- correct dependencies (React 18, MUI 5, TanStack Query, Office.js types)
- `vite.config.ts` (15 lines) -- React plugin + basic-ssl plugin, port 3000, strictPort
- `tsconfig.json` (22 lines) -- strict mode, bundler module resolution, `@/*` path alias
- `src/main.tsx` (11 lines) -- wraps mount in `Office.onReady()`
- `src/App.tsx` (281 lines) -- full app shell
- `index.html` -- references `office.js` CDN script
- `public/assets/` -- icon files present
- `dist/` -- built output exists (`index.html` + assets)

**Observations**:
- The `tsconfig.json` `types` array is `["office-js"]` which provides global type definitions correctly
- The `@/*` path alias is configured in tsconfig but `vite.config.ts` does not define a matching `resolve.alias` -- imports using `@/` will fail at build time
- The `manifest.xml` uses hardcoded `https://localhost:3000` URLs which must be regenerated for production deployment

### 2.2 Task 2: Verify Office Host Support

**Status: NOT DONE**

The execution plan requires verification on Windows Desktop, Mac Desktop, and Excel Web. No evidence exists:
- `COMPATIBILITY-MATRIX.md` is entirely `?` placeholders with no test date
- No test scripts or manual test documentation found
- No evidence of the add-in being loaded in any Excel host

### 2.3 Task 3: Spike Office.js Operations

**Status: CODE EXISTS, NEVER EXECUTED**

`src/utils/officeSpike.ts` (185 lines) contains all 8 spike functions:
- `insertResultTable()` ✅
- `insertChart()` ✅
- `insertLocalPivotTable()` ✅
- `insertFormula()` ✅
- `readActiveCell()` ✅
- `detectWorkbookConnections()` ✅
- `createXmlaConnection()` ✅
- `runCompatibilitySpike()` ✅

However:
- `runCompatibilitySpike()` is exported but never called from any component, hook, or main entry point
- The execution plan requires a "test button inserts a table" and "test button inserts a chart" -- no such test UI exists
- The compatibility matrix was never populated from spike results

This is the single biggest Phase 0 gap. Without running the spike, the team has no evidence about which Office.js operations actually work on target platforms.

### 2.4 Task 4: Compatibility Matrix

**Status: NOT DONE**

`COMPATIBILITY-MATRIX.md` (27 lines) is a template only. All rows show `?`. No host was tested. The "Test Date" field says "TBD".

**Acceptance criteria gap**:
- "Add-in loads in at least one desktop Excel host" -- NOT VERIFIED
- "A test button inserts a table into the active worksheet" -- NOT VERIFIED
- "A test button inserts a chart from that table" -- NOT VERIFIED
- "Written evidence of which PivotTable/connection operations are actually supported" -- NOT PRODUCED

---

## 3. Phase 1 Audit

### 3.1 Workstream A: Authentication and Profiles

**Plan vs Reality**:

| Task | Status | Notes |
|---|---|---|
| Login form (server URL, tenant, email, password, remember) | ✅ COMPLETE | `LoginScreen.tsx` (142 lines) -- fully implemented with all fields, eye toggle, validation, error state |
| Profile persistence (no password) | ✅ COMPLETE | `storage.ts` stores name, serverUrl, tenantId, email only. `saveProfile()` excludes password. |
| JWT storage in OfficeRuntime.storage | ✅ COMPLETE | `setJwt()` / `getJwt()` use `OfficeRuntime.storage` |
| Redirect to login on 401 | ✅ COMPLETE | `client.ts` `request()` calls `removeJwt()` and `onUnauthorized` on 401 |
| Clear password from component state after login | ⚠️ BUG | Password state persists in LoginScreen until unmount. See Bug #1 |
| Connection health check | ✅ COMPLETE | `App.tsx` polls `/health` every 30s via useEffect. Shows connected/disconnected footer |
| Profile switcher UI in header | ❌ MISSING | Header shows email in footer but no profile dropdown. `useAuth` returns `profiles` and `switchProfile` but they are never rendered |
| Never store password in storage/logs/diagnostics | ✅ COMPLETE | Verified: `saveProfile()` excludes password. LoginScreen state is cleared on unmount (auth state transitions to 'authenticated' which hides LoginScreen) |

**Acceptance criteria gaps**:
- "Saved profile pre-fills server, tenant, and email but not password" -- PARTIAL. Profile data is stored but no pre-fill logic reads it on the login screen. LoginScreen always starts with empty fields.
- "Sign out removes JWT and returns to login" -- PARTIAL. `logout()` clears JWT but does not call `clearAllAuthData()`. Profiles remain stored (this is correct per spec -- "Does not delete saved profiles").
- "No password appears in storage, logs, diagnostics" -- ✅ VERIFIED by code review. No password persistence paths found.

### 3.2 Workstream B: API Client Foundation

**Plan vs Reality**:

| Task | Status | Notes |
|---|---|---|
| Shared fetch client with Bearer injection | ✅ COMPLETE | `client.ts` (83 lines) -- `request()` handles JWT injection, 401, error normalization |
| JSON error normalization (ApiError class) | ✅ COMPLETE | `ApiError` extracts `detail` from error bodies |
| Retry policy for safe GETs | ❌ MISSING | Execution plan says "Retry policy for safe GETs" -- no retry logic exists in `client.ts` |
| Typed clients (projects, models, agent config, conversations, messages) | ✅ COMPLETE | `modelService.ts`, `agentService.ts`, `queryRouter.ts`, `auth.ts` all exist with typed functions |
| TanStack Query with correct key structure | ✅ COMPLETE | `useModel.ts` uses query keys including projectId/modelId. `useModel` hooks properly scoped. |
| Clear queries on sign out/profile switch | ❌ MISSING | No `queryClient.clear()` or `queryClient.invalidateQueries()` called on logout or profile switch. Stale data from old tenant persists in React Query cache. |
| Query keys include persona id | ❌ MISSING | Execution plan requires persona id in query keys. No persona selector exists yet so this is deferred, but should be noted. |

**Bug: `streamRequest()` uses `localStorage` instead of `OfficeRuntime.storage`** (see Bug #2)

**Bug: `streamRequest()` does not set `onUnauthorized`** (see Bug #3)

### 3.3 Workstream C: Ask Tessallite

**Plan vs Reality**:

| Task | Status | Notes |
|---|---|---|
| Default mode after first login | ✅ COMPLETE | `App.tsx` defaults to `'ask'` |
| Agent not configured state | ✅ COMPLETE | `ChatPanel.tsx` renders full screen message with `SmartToyIcon` when `agentConfigured=false` |
| Empty state with business prompts | ✅ COMPLETE | `ChatPanel.tsx` renders empty state with `ChatBubbleOutline` icon and suggestion |
| User message bubble | ✅ COMPLETE | `ChatMessage.tsx` renders user bubbles with green border-left |
| Streaming agent response | ⚠️ MOCK | `App.tsx` `handleSend()` uses `setInterval` with hardcoded text, NOT `sendMessageStream()` from agentService |
| Result preview table | ✅ COMPLETE | `ChatMessage.tsx` renders compact table with headers/rows, shows "+N more rows" indicator |
| Judge verdict | ⚠️ DUPLICATE | `ChatMessage.tsx` has inline judge verdict rendering AND a separate `JudgeVerdict.tsx` component exists but is never imported |
| Feedback buttons | ❌ MISSING | Spec says thumbs up/down with `POST .../feedback` call. ChatMessage renders no feedback UI. `agentService.sendFeedback()` exists but is never wired. |
| Follow-up suggestion chips | ❌ MISSING | Neither ChatPanel nor ChatMessage render follow-up suggestion chips |
| Conversation header (model name, provider badge, agent persona, new conv, history) | ❌ MISSING | Provider model name is shown as plain text in ChatPanel. Agent persona dropdown, conversation title, "+New" button, and history icon are all absent |
| Conversation context switching | ❌ MISSING | No way to start a new conversation or switch between conversations |

**Acceptance criteria gaps**:
- "User can ask a question and see streamed response text" -- MOCK ONLY. Real `sendMessageStream()` exists but is not called.
- "Result rows appear as a compact preview table" -- ✅ WORKS (with mock data)
- "Feedback calls the feedback endpoint" -- ❌ NOT IMPLEMENTED
- "Agent-not-configured projects fail gracefully" -- ✅ WORKS

### 3.4 Workstream D: Insert as Table

**Plan vs Reality**:

| Task | Status | Notes |
|---|---|---|
| Insert formatted Excel Table | ✅ COMPLETE | `insertResultTable()` in `officeSpike.ts` writes data, adds table, sets style |
| Safe unique sheet name | ✅ COMPLETE | `useExcel.createNewSheet()` auto-increments suffix |
| Write headers and rows | ✅ COMPLETE | Range assignment with header + data rows |
| Auto-fit columns | ✅ COMPLETE | `range.format.autofitColumns()` called |
| Table style | ✅ COMPLETE | 'TableStyleMedium2' applied, header bold |
| Active-cell insertion with overwrite confirmation | ❌ MISSING | Spec says "Support active-cell insertion with overwrite confirmation" -- only new-sheet insertion is implemented |
| Number formatting from response metadata | ❌ MISSING | No format token mapping applied to cells |
| Metadata persistence (project/model/persona/conv/turn id, semantic query, timestamp, version) | ❌ MISSING | Spec requires storing metadata on the inserted table. `workbookMetadata.ts` file does not exist. |
| Large result guard (confirm >10,000 rows) | ❌ MISSING | No row count check before insertion |

**Acceptance criteria gaps**:
- "Insert Table creates a valid formatted Excel Table" -- ⚠️ PARTIAL. Creates table but on new sheet only, not active-cell.
- "Sheet/range collision prompts before overwrite" -- ❌ NOT IMPLEMENTED
- "Inserted table includes reasonable column widths and formats" -- ⚠️ PARTIAL. Auto-fit applied but no number formatting.
- "Metadata is attached for future refresh/debug" -- ❌ NOT IMPLEMENTED

---

## 4. Bugs and Logic Errors

### Bug #1: Password Not Explicitly Cleared After Login (MEDIUM)

**File**: `src/components/LoginScreen/LoginScreen.tsx:23-26`

```ts
const handleSubmit = useCallback(async (e: FormEvent) => {
    e.preventDefault();
    await onLogin({ serverUrl, tenantId, email, password }, remember);
}, [serverUrl, tenantId, email, password, remember, onLogin]);
```

The password remains in React state (`password` variable) after the login call succeeds. It is implicitly cleared when `authState` becomes `'authenticated'` and LoginScreen unmounts, but:
- If login fails and the component stays mounted, the password persists in state
- The execution plan explicitly states "Clear password from component state after login attempt" as a separate requirement

**Fix**: Call `setPassword('')` after `await onLogin(...)` completes, regardless of success or failure.

### Bug #2: `streamRequest()` Uses Wrong Storage API (CRITICAL)

**File**: `src/api/client.ts:73-79`

```ts
export function streamRequest(
  path: string,
  body: unknown,
): Promise<Response> {
  return fetch(`${baseUrl}${path}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${localStorage.getItem('tessallite_jwt') || ''}`,
    },
    body: JSON.stringify(body),
  });
}
```

The JWT is stored in `OfficeRuntime.storage` (via `setJwt()` in `storage.ts`), but `streamRequest()` reads from `localStorage`. These are completely different storage APIs. The JWT will always be `null` when read from localStorage, causing SSE streaming to fail with authentication errors.

**Fix**: `streamRequest()` must use `OfficeRuntime.storage.getItem('tessallite_jwt')` or accept the JWT as a parameter.

### Bug #3: `streamRequest()` Lacks 401 Handling (MEDIUM)

**File**: `src/api/client.ts:73-79`

`streamRequest()` does not check for 401 status, does not call `removeJwt()`, and does not call `onUnauthorized()`. If the JWT expires during a streaming session, there is no recovery path -- the stream will silently fail.

**Fix**: Either return the raw Response and let the caller handle 401, or wrap in a handler similar to `request()`.

### Bug #4: `setLastMode` Never Called (LOW)

**File**: `src/App.tsx:47-53`

```ts
const handleModeChange = useCallback((newMode: AppMode) => {
    setMode(newMode);
}, []);
```

`handleModeChange` does not call `setLastMode(mode)` from `storage.ts`. The last mode is loaded on startup but never persisted on change. The execution plan says "The mode persists in localStorage per session."

**Fix**: Add `import { setLastMode } from '../utils/storage'` and call `setLastMode(newMode)` inside `handleModeChange`.

### Bug #5: `tsconfig.json` Path Alias Not Mirrored in Vite (MEDIUM)

**File**: `tsconfig.json:17-19`, `vite.config.ts`

`tsconfig.json` defines `"@/*": ["src/*"]` but `vite.config.ts` has no `resolve.alias` configuration. TypeScript will resolve `@/` imports without errors, but Vite/Rollup will fail at build time because it doesn't know about the alias. This is a latent build failure -- any component that uses `import {...} from '@/something'` will break in production builds.

Currently no files use `@/` imports, so this is a latent bug.

**Fix**: Add to `vite.config.ts`:
```ts
resolve: {
  alias: { '@': '/src' }
}
```

### Bug #6: Profile Not Saved When `remember` is False (LOW)

**File**: `src/hooks/useAuth.ts:61-87`

When `remember` is `false`, the login function sets JWT and configures the API client, but:
- Does not save the profile to storage
- Does not set active profile
- `activeProfile` state remains `null`

This means the footer shows no email (since `activeProfile` is null), and the user cannot reuse the server URL on next login. While intentionally not persisting credentials, the profile metadata (server, tenant, email) should still be available during the session.

**Fix**: Save a temporary in-memory profile during the session, or always save serverUrl/tenantId/email and only skip JWT persistence.

---

## 5. Unwired and Dead Code

### 5.1 `JudgeVerdict.tsx` -- Exported but Never Imported

**File**: `src/components/AskTessallite/JudgeVerdict.tsx` (28 lines)

This component renders judge verdict blocks with confidence and rubric scores. However:
- It is never imported by `ChatMessage.tsx`, `ChatPanel.tsx`, or `App.tsx`
- `ChatMessage.tsx` has identical judge verdict rendering logic built inline (lines 64-78)

The component is dead code and the rendering is duplicated.

**Recommendation**: Either delete `JudgeVerdict.tsx` and keep the inline version, or import `JudgeVerdict` in `ChatMessage.tsx` to eliminate duplication.

### 5.2 `InsertActions.tsx` -- Multiple Unused Props

**File**: `src/components/AskTessallite/InsertActions.tsx` (89 lines)

The component accepts `onInsertChart`, `onLocalPivot`, `onCubeFormulas`, `onLiveConnection`, `onShowQuery`, and `recommendedAction` props. In `ChatPanel.tsx:97-101`, only `onInsertTable` is passed:

```tsx
<InsertActions
    data={...}
    headers={...}
    onInsertTable={onInsertTable}
/>
```

All other buttons (Chart, Local Pivot, CUBE formulas, Live connection, Show Query) render as `undefined` because their handlers are not passed. The buttons silently don't appear, which gives a false impression that they don't exist. The recommended action highlighting never activates.

### 5.3 `excelFormulas.ts` -- Never Imported

**File**: `src/utils/excelFormulas.ts` (65 lines)

Contains `generateCubeMember()`, `generateCubeValue()`, `generateCubeSet()`, `buildMsolapConnectionString()`, and `buildMsolapConnectionStringWithAuth()`. None of these are imported by any component, hook, or other utility. This is Phase 2 work pre-written but not wired.

### 5.4 `useModel.ts` -- Hooks Conditionally Enabled, Never Called

**File**: `src/hooks/useModel.ts` (69 lines)

All 8 hooks (`useProjects`, `useModels`, `useModel`, `useMeasures`, `useDimensions`, `useHierarchies`, `usePersonas`, `useGlossary`) are defined but:
- `useProjects()` has `enabled: false` and is never called
- All others have `enabled: !!projectId && !!modelId` but no component passes a `projectId` or `modelId`

These hooks are completely dead code in the current implementation. The Report Builder (Phase 2) will need them, but they are not used in Phase 1.

### 5.5 `queryRouter.ts` -- Drill-Through and Validate Never Called

**File**: `src/api/queryRouter.ts` (45 lines)

`executeQuery()`, `explainQuery()`, `validateQuery()`, `discoverMembers()`, `getDrillOptions()`, and `drillThrough()` exist but none are called from any component. Phase 1 only needs `validateQuery` (Cube Function Wizard) which is not built yet.

### 5.6 `officeSpike.ts` -- `runCompatibilitySpike()` Never Called

**File**: `src/utils/officeSpike.ts:132-184`

The spike runner function exists but is never invoked. This is a Phase 0 deliverable that remains unused.

### 5.7 `agentService.ts` -- Multiple Functions Never Called

**File**: `src/api/agentService.ts` (76 lines)

`getAgentPersonas()`, `createConversation()`, `getConversations()`, `getConversation()`, `deleteConversation()`, `sendMessage()`, `sendMessageStream()`, and `sendFeedback()` are fully implemented but none are called. `App.tsx` uses hardcoded mock data instead.

### 5.8 `useExcel.ts` -- Multiple Functions Never Called

**File**: `src/hooks/useExcel.ts` (77 lines)

`insertFormula()`, `getActiveCellAddress()`, `readCellValue()`, and `createNewSheet()` are all implemented but only `insertTable` is used. These are needed for Phase 2 (Cube Function Wizard).

---

## 6. Security Review

### 6.1 Password Handling: PASS (with one improvement)

- Password is passed from LoginScreen to `useAuth.login()` via props ✅
- `useAuth.login()` uses password only in the `apiLogin()` call, then it goes out of scope ✅
- `saveProfile()` excludes password field ✅
- Password never written to `OfficeRuntime.storage` ✅
- Password never written to `localStorage` ✅
- Password never logged ✅
- Improvement: Password should be explicitly cleared from `LoginScreen` state after login attempt (Bug #1)

### 6.2 JWT Storage: PASS (except streamRequest bug)

- JWT stored in `OfficeRuntime.storage` via async API ✅
- JWT read via async `getJwt()` in `client.ts request()` ✅
- `removeJwt()` clears the JWT on 401 and logout ✅
- Exception: `streamRequest()` reads from `localStorage` instead of `OfficeRuntime.storage` (Bug #2)

### 6.3 XMLA Credential Handling: N/A (not implemented yet)

The execution plan states "XMLA/MSOLAP credentials are requested only when the user explicitly creates a live connection." No live connection flow is built yet. When it is, the plan also states "Never persist XMLA password" -- this must be enforced.

### 6.4 XSS Risk in Chat Content: PASS

`ChatMessage.tsx` renders user and assistant content via MUI `Typography` with `whiteSpace: 'pre-wrap'`. Content is rendered as React children, not `dangerouslySetInnerHTML`. No HTML injection risk.

### 6.5 CSRF: PASS

All API calls use `Authorization: Bearer` header, not cookies. The spec notes "CSRF protection is enforced only for cookie-based requests; Bearer header requests skip CSRF validation." This is correct.

### 6.6 Diagnostics Redaction: NOT IMPLEMENTED

The execution plan requires redacting passwords, JWTs, Authorization headers, connection strings containing passwords, and result row values from diagnostics. No diagnostics module exists yet (`src/utils/diagnostics.ts` is not created).

### 6.7 API Key / Token in Manifest: PASS

The manifest does not contain any API keys, tokens, or secrets. All URLs are placeholder localhost values.

---

## 7. Race Conditions and Async Issues

### 7.1 Health Poll Timer Not Cancelled on Auth State Change

**File**: `src/App.tsx:62-71`

```ts
useEffect(() => {
    if (authState !== 'authenticated') return;
    let cancelled = false;
    const poll = async () => { ... };
    poll();
    const interval = setInterval(poll, 30000);
    return () => { cancelled = true; clearInterval(interval); };
}, [authState]);
```

The cleanup correctly clears the interval and sets `cancelled`. However, if `authState` changes to `'unauthenticated'` while a poll is in flight, the `cancelled` flag prevents state updates on a stale component -- this is correct. **No issue.**

### 7.2 `useAuth` Initialization Can Race with First Render

**File**: `src/hooks/useAuth.ts:42-58`

```ts
useEffect(() => {
    (async () => {
      const [jwt, storedProfiles, storedActive] = await Promise.all([...]);
      ...
      if (jwt && storedActive) {
        setAuthState('authenticated');
      } else {
        setAuthState('unauthenticated');
      }
    })();
}, []);
```

`authState` starts as `'loading'`, and `App.tsx` renders a loading spinner during this state. This is correct -- the render will update when the effect resolves. **No issue.**

### 7.3 SSE Stream State Not Cleaned on Unmount (LATENT)

The mock streaming (`setInterval` in `App.tsx handleSend()`) does not clear its interval on component unmount. If the user navigates away (e.g., switches mode) while streaming, the interval continues running, calling `setMessages` on an unmounted component.

**Fix**: Store the interval ID in a ref and clear it in a cleanup effect (or use `useRef` with a cleanup). When real SSE is implemented, the `EventSource` or `fetch` with `ReadableStream` must also be aborted on unmount.

### 7.4 Concurrent Mode Switches and Streaming (LATENT)

If the user rapidly switches modes while a chat response is streaming, the messages state could be in an inconsistent state (messages from the previous streaming not fully delivered). This is minor in the demo but should be handled in production with an abort mechanism.

---

## 8. Missing Setup and Infrastructure

### 8.1 No Test Files

The execution plan specifies unit tests, component tests, Excel integration tests, security tests, and UAT scenarios. No test files exist:
- No `vitest.config.ts`
- No `*.test.ts` or `*.test.tsx` files
- No test scripts in `package.json`
- `package.json` has no test runner dependency (`vitest` or `jest` not in devDependencies)

### 8.2 No Environment Configuration

- No `.env` or `.env.example` file
- No environment variable handling in `vite.config.ts`
- Server URLs are hardcoded or come from user input (login form), which is correct for a client-side plugin

### 8.3 No README

The plugin directory lacks a `README.md` with setup instructions, development workflow, or known issues.

### 8.4 Missing Icon References in Manifest

`manifest.xml` references icons at `https://localhost:3000/assets/icon-*.png`. The `public/assets/` directory exists but its contents need verification -- if icons are placeholder/missing, the add-in will fail to load in Excel.

### 8.5 Missing `dist/` Asset Verification

The built `dist/index.html` exists (408 bytes) but `dist/assets/` contents should be verified. A build was attempted but it's unclear if it was successful.

### 8.6 `vite.config.ts` Missing `resolve.alias`

As noted in Bug #5, `@/*` imports will fail at build time without a Vite alias.

### 8.7 Missing Global CSS / Style Reset

The app uses MUI's `CssBaseline` but does not define a global `@keyframes pulse` animation referenced in `ChatMessage.tsx:54-56`:

```tsx
sx={{ animation: 'pulse 1s infinite' }}
```

This `pulse` keyframe is not defined anywhere. It will have no effect unless defined in a global stylesheet or MUI `keyframes` utility.

**Fix**: Define via `@mui/material/styles`:
```ts
import { keyframes } from '@mui/material/styles';
const pulse = keyframes`
  0% { opacity: 0.4; }
  50% { opacity: 1.0; }
  100% { opacity: 0.4; }
`;
```

---

## 9. Scope Cuts and Missing Features

### From the Execution Plan -- Phase 1

| Planned Feature | Status | Notes |
|---|---|---|
| Profile switcher dropdown in header | ❌ MISSING | `useAuth.switchProfile` exists, no UI |
| Profile pre-fill on login screen | ❌ MISSING | Saved profile data not loaded into LoginScreen fields |
| Retry policy for safe GETs | ❌ MISSING | No retry logic in `client.ts` |
| Clear React Query cache on sign out | ❌ MISSING | Stale tenant data persists |
| SSE streaming (real, not mock) | ❌ MISSING | Hardcoded `setInterval` demo data |
| Conversation header with model selector | ❌ MISSING | Provider badge shown as plain text only |
| Agent persona dropdown | ❌ MISSING | `getAgentPersonas()` exists, not wired |
| New conversation action | ❌ MISSING | No "+New" button |
| Conversation history | ❌ MISSING | `getConversations()` exists, not wired |
| Feedback buttons | ❌ MISSING | Thumbs UI absent, `sendFeedback()` not called |
| Follow-up suggestion chips | ❌ MISSING | Neither static nor dynamic suggestions rendered |
| Active-cell table insertion with overwrite confirmation | ❌ MISSING | Only new-sheet insertion |
| Number formatting from response metadata | ❌ MISSING | No format token mapping |
| Metadata persistence on inserted table | ❌ MISSING | `workbookMetadata.ts` file not created |
| Large result guard | ❌ MISSING | No row-count check |
| Inserted table reasonable column widths/formatting | ⚠️ PARTIAL | Auto-fit only, no per-column format |

### From the Specs -- Features Deferred to Phase 2+

These are correctly absent from Phase 1 (they belong in Phase 2):
- Report Builder (zone mapping, measure/dimension/hierarchy libraries)
- Report templates
- Cube Function Wizard
- Live connection helper
- Glossary popover and search modal
- Persona switcher
- Drill-through panel
- Query trace modal
- Context menu items (none registered in manifest)

---

## 10. Query Routing Audit

**Requirement**: "Specifically look for queries hitting the source or target database without passing by the query router."

### Finding: NO ISSUES FOUND

All data access paths in the plugin code were traced:

1. **REST API calls** (`client.ts`): All `apiClient.get/post/put/delete` calls go to paths prefixed with the server's base URL (e.g., `/api/v1/projects`, `/api/v1/auth/login`). These hit the Tessallite model-service, query-router, or agent-service REST endpoints. No direct database connections.

2. **CUBE formula generation** (`excelFormulas.ts`): Generates `=CUBEMEMBER(...)` and `=CUBEVALUE(...)` formulas. These formulas execute through Excel's XMLA connection, which goes through the gateway (`/api/v1/xmla/`) → query-router. The plugin does not execute these formulas itself -- Excel does.

3. **XMLA connection strings** (`excelFormulas.ts`): Uses `Provider=MSOLAP.8;Data Source={server}/api/v1/xmla/;Initial Catalog={catalog}`. The connection string points to the gateway's XMLA endpoint, which routes through the query-router. No direct database URLs or credentials.

4. **Office.js insert operations**: `insertResultTable()`, `insertChart()`, `insertLocalPivotTable()` write data to worksheets that was already fetched from the Tessallite API. They do not fetch data independently.

5. **No raw SQL execution**: No code constructs or executes SQL statements directly. No `sqlglot`, `pg`, or any database driver is imported or used.

### Finding: NO CONDITIONAL BRANCHING ON SOURCE DATABASE TYPE

The plugin code does not contain any conditional logic based on database type (PostgreSQL, BigQuery, Snowflake, etc.). All data access is abstracted through the REST API. The plugin is a pure API consumer with no awareness of the underlying database technology.

---

## 11. Cross-Platform Compatibility

### 11.1 Office.js Requirement Sets

The code uses these Excel JS API features which map to requirement sets:

| Feature | Requirement Set | Availability |
|---|---|---|
| `sheet.tables.add()` | ExcelApi 1.1 | All platforms |
| `sheet.charts.add()` | ExcelApi 1.1 | All platforms |
| `sheet.pivotTables.add()` | ExcelApi 1.9 | Excel 2019+, Web |
| `range.formulas = [[...]]` | ExcelApi 1.1 | All platforms |
| `range.load('address/values/formulas')` | ExcelApi 1.1 | All platforms |
| `context.workbook.connections` | Not in standard API | May fail on some hosts |

**Issue**: `detectWorkbookConnections()` and `createXmlaConnection()` in `officeSpike.ts` use `as unknown as { connections: ... }` casts, indicating these APIs are not in the standard `@types/office-js` type definitions. They may not exist on all platforms. The cast masks the type error but doesn't guarantee runtime availability.

### 11.2 WebView / IFrame Limitations

On Excel for the Web, the add-in runs in a sandboxed iframe:
- Cookies are subject to iframe restrictions ✅ Mitigated by using Bearer token auth (not cookies)
- `localStorage` may be restricted ✅ Mitigated by using `OfficeRuntime.storage`
- No device APIs needed ✅ Plugin uses only HTTP and Office.js

### 11.3 Manifest HTTPS Requirement

The manifest uses `https://localhost:3000` for all URLs. Excel Desktop accepts self-signed certs via the `@vitejs/plugin-basic-ssl` plugin. Excel for the Web requires properly trusted HTTPS certificates. This is handled correctly for development but must be documented for production.

---

## 12. Design Token and Styling Issues

### 12.1 Token Duplication vs Main Frontend

`src/theme.ts` defines its own token object that duplicates values from `frontend/src/theme/tokens.ts`. The execution plan says "Match the main Tessallite web frontend visually" but doesn't specify whether tokens should be imported or duplicated.

Since the plugin is a separate Vite project (not part of the frontend monorepo build), importing from `../../frontend/src/theme/tokens.ts` is not feasible without a monorepo workspace setup. The current approach (duplication) is pragmatic but creates a maintenance burden -- any token change in the main frontend must be manually replicated.

**Recommendation**: Add a comment in `theme.ts` referencing the source of truth (`frontend/src/theme/tokens.ts`) and note the last sync date. Or extract tokens into a shared package.

### 12.2 Missing `pulse` Keyframe

As documented in Section 8.7 -- the typing indicator animation in `ChatMessage.tsx` references a CSS animation `'pulse 1s infinite'` that is never defined.

### 12.3 Hardcoded Colors in LoginScreen

`LoginScreen.tsx:49` uses hex color `'#006C35'` directly instead of referencing the `tokens` object or MUI theme. Similarly, line 55 uses `'#5A6577'`.

### 12.4 Footer Email Display Instead of Profile Name

`App.tsx:202` shows `activeProfile.email` in the footer. The FRONTEND-DESIGN shows the persona switcher in the footer: `Persona: [Default v]`. The email is a reasonable placeholder for Phase 1 but doesn't match the design spec.

---

## 13. Manifest and Ribbon Audit

### 13.1 Ribbon Buttons Match Spec

The manifest defines 3 ribbon buttons: `Connect`, `Ask`, `Report Builder`. This matches the SPECS.md Section 6.5. ✅

### 13.2 Deep-Link URLs

Ribbon buttons use deep-link URLs:
- `Connect`: `index.html` (default view)
- `Ask`: `index.html?mode=ask`
- `Report Builder`: `index.html?mode=report-builder`

However, `App.tsx` does not read the `?mode=` query parameter on startup. Deep-linking from the ribbon is non-functional.

**Fix**: In `App.tsx`, add:
```ts
useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const modeParam = params.get('mode');
    if (modeParam === 'ask' || modeParam === 'report-builder') {
        setMode(modeParam);
    }
}, []);
```

### 13.3 Context Menu Items Missing

The manifest does not declare any `<ExtensionPoint xsi:type="ContextMenu">` entries. The specs require 3 context menu items (Drill through, Look up in glossary, Insert measure).

This is correct for Phase 1 -- context menus are a Phase 3 feature per the execution plan.

### 13.4 Shared Runtime Not Configured

The manifest does not use a shared runtime (`<Runtimes>` element). For deep-linking from ribbon to work correctly (ribbon button opens a task pane that navigates to a specific mode), a shared runtime is recommended. Without it, each ribbon click opens a fresh task pane.

### 13.5 Placeholder Add-in ID

The manifest uses `<Id>a1b2c3d4-e5f6-7890-abcd-ef1234567890</Id>`. This must be replaced with a real GUID before AppSource submission or enterprise deployment.

---

## 14. Rough Edges and Cut Corners

### 14.1 Mock Streaming Instead of Real SSE

The most significant cut corner: `App.tsx handleSend()` simulates streaming with `setInterval` and hardcoded text. The real `agentService.sendMessageStream()` function exists but is not called. This means:

- No actual Tessallite agent integration
- No error handling for agent failures
- No real data -- all answers are static demo text
- The streaming UI (typing indicator, token-by-token rendering) is tested but no real SSE parsing exists

### 14.2 Hardcoded Conversation ID

The demo chat does not create a conversation via `createConversation()`. Messages are entirely local state with no persistence. Refreshing loses all messages.

### 14.3 Toast Notification System Not Built

The execution plan does not explicitly call for toasts in Phase 1 ("Inserted N rows" toast is Phase 1 Demo script), but `FRONTEND-DESIGN.md Section 18` specifies a full toast system. No toast component exists.

### 14.4 No Loading Skeletons

`FRONTEND-DESIGN.md Section 19.2` specifies skeleton loading placeholders. Only a `CircularProgress` spinner is implemented for the auth loading state. Skeleton components do not exist.

### 14.5 Error Recovery for Excel Operations

`useExcel.insertTable()` calls `insertResultTable()` but catches errors only in the `handleInsertTable` callback with `console.error`. No user-facing error message is shown when Excel operations fail.

### 14.6 No Accessibility Attributes on Interactive Elements

`FRONTEND-DESIGN.md Section 22` specifies extensive ARIA attributes. None are implemented:
- Mode switcher has no `role="tablist"` or `aria-selected`
- Chat message list has no `role="log"` or `aria-live`
- No focus management on mode switch
- No screen reader labels on icons

### 14.7 Reduced Motion Not Implemented

The design spec requires `prefers-reduced-motion: reduce` support. No media query or detection logic exists.

---

## 15. Recommendations by Priority

### CRITICAL (blocking Phase 1 completion)

1. **Fix Bug #2 (streamRequest localStorage)** -- SSE streaming will silently fail with 401 errors because the JWT is read from the wrong storage API. Fix `client.ts:77`.

2. **Run the compatibility spike** -- Execute `runCompatibilitySpike()` on at least Windows Desktop Excel. Populate `COMPATIBILITY-MATRIX.md` with real results. This gates all Phase 2+ decisions about which Excel features to enable.

3. **Wire real SSE streaming** -- Replace the mock `setInterval` in `App.tsx handleSend()` with calls to `agentService.createConversation()` and `agentService.sendMessageStream()`. This is the core Phase 1 value proposition.

### HIGH (complete Phase 1 workstreams)

4. **Fix Bug #1 (clear password)** -- Call `setPassword('')` after login attempt in `LoginScreen.tsx`.

5. **Fix Bug #4 (setLastMode)** -- Persist mode changes to storage so the user returns to their last-used mode.

6. **Fix Bug #5 (vite alias)** -- Add `resolve.alias` to `vite.config.ts` or remove the `@/*` path from `tsconfig.json`.

7. **Add feedback buttons** -- Render thumbs up/down in `ChatMessage.tsx` and wire to `agentService.sendFeedback()`.

8. **Add active-cell table insertion** -- Support `insertResultTable()` into the active cell with overwrite confirmation.

9. **Implement metadata persistence** -- Create `src/utils/workbookMetadata.ts` and store metadata (project, model, persona, conversation, turn IDs, semantic query, timestamp, plugin version) on inserted tables.

10. **Add large result guard** -- Confirm before inserting more than 10,000 rows.

11. **Add retry policy** -- Implement retry with exponential backoff in `client.ts` for safe GET requests.

12. **Clear React Query cache on sign out** -- Call `queryClient.clear()` on logout/profile switch.

### MEDIUM (quality and completeness)

13. **Eliminate dead/unwired code** -- Either wire or remove: `JudgeVerdict.tsx` (if inline rendering stays), unused `InsertActions` props, `runCompatibilitySpike()` call site.

14. **Add deep-link support** -- Read `?mode=` query parameter in `App.tsx` on startup to enable ribbon deep-linking.

15. **Define `pulse` keyframe** -- In `theme.ts` or a global stylesheet.

16. **Add toast notifications** -- Implement the toast system per FRONTEND-DESIGN Section 18.

17. **Add ARIA attributes** -- Mode switcher, chat message list, icons per FRONTEND-DESIGN Section 22.

18. **Add profile pre-fill on login** -- Load saved profile data into LoginScreen fields when profiles exist.

19. **Add profile switcher UI** -- Render a profile dropdown in the header bar.

### LOW (nice to have)

20. **Fix hardcoded colors** -- Replace hex values in `LoginScreen.tsx` with token references.

21. **Add README.md** -- Setup instructions for development.

22. **Add unit test scaffolding** -- Install vitest, create test files for storage, client, and formula utilities.

23. **Document token sync** -- Add comment in `theme.ts` referencing the source of truth and last sync date.

24. **Add real manifest GUID** -- Replace the placeholder add-in ID before production deployment.

---

*End of review. Total: 4 bugs, 8 dead code paths, 0 query routing issues, 16 missing features.*
