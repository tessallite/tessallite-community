# Excel Plugin Phase 4 Review — Round 1 Fix Report

Date: 2026-05-20
Source: `reviews_phase-4-review-round1-findings-report.md`
Status: Fixes applied and verified

---

## Summary

| Category | Count |
|---|---|
| Findings in review | 6 |
| Fixed | 6 |
| Deferred (needs backend/user input) | 0 |
| Verified (tsc + tests + build) | Pass |

---

## F-4R1-01 [High] — Report Builder numeric coercion → FIXED

**File:** `src/components/ReportBuilder/ReportBuilder.tsx:188`

**Fix:** Changed row value mapping from `typeof v === 'string' || typeof v === 'number' ? String(v) : JSON.stringify(v)` to `v == null ? '' : (typeof v === 'string' || typeof v === 'number') ? v : JSON.stringify(v)`. Numbers now pass through as native numeric types to Excel, preserving sort/aggregation/chart fidelity. Null values become empty strings.

**Before:**
```ts
Object.values(r).map(v => (typeof v === 'string' || typeof v === 'number') ? String(v) : JSON.stringify(v)),
```

**After:**
```ts
Object.values(r).map(v => v == null ? '' : (typeof v === 'string' || typeof v === 'number') ? v : JSON.stringify(v)),
) as (string | number)[][];
```

---

## F-4R1-02 [High] — Query Trace payload contract → FIXED

**Files:** `src/api/queryRouter.ts:38-53`, `src/components/QueryTrace/TraceModal.tsx`, `src/components/ReportBuilder/ReportBuilder.tsx:456-461`

**Fix:** Replaced `explainQuery(query)` which sent `{ query }` with `explainQuery({ modelId, query, personaId })` which sends `{ model_id, raw_query, measures, dimensions, filters, limit, persona_id }` matching the backend `ExecuteRequest` contract. TraceModal now accepts `modelId` and `personaId` props. ReportBuilder passes both through.

**Changes:**
- `src/api/queryRouter.ts` — `explainQuery` signature changed from `(query: SemanticQuery)` to `(params: ExplainParams)` where `ExplainParams = { modelId, query, personaId?, rawQuery? }`
- `src/components/QueryTrace/TraceModal.tsx` — Added `modelId` and `personaId` props; passes them to `explainQuery`
- `src/components/ReportBuilder/ReportBuilder.tsx` — TraceModal usage updated with `modelId={modelId}` and `personaId={personaId}`

---

## F-4R1-03 [Medium] — Missing ARIA labels → FIXED

**Files:** `ChatPanel.tsx`, `LoginScreen.tsx`, `DiagnosticsPanel.tsx`

**Fix:** Added explicit `aria-label` attributes to all icon-only controls identified:

| Component | Control | Label |
|---|---|---|
| ChatPanel | New conversation button | `aria-label="New conversation"` |
| ChatPanel | Conversation history button | `aria-label="Conversation history"` |
| ChatPanel | Send message button | `aria-label="Send message"` |
| LoginScreen | Password visibility toggle | `aria-label="Show password"` / `"Hide password"` |
| DiagnosticsPanel | Close button | `aria-label="Close diagnostics"` |

---

## F-4R1-04 [Medium] — Conversation history clears messages → FIXED

**File:** `src/hooks/useAgentConversation.ts:250-258`

**Fix:** Removed `setMessages([])` from `loadConversation()`. The `getConversation` API returns metadata only (id, title, created_at, model_id, persona_id) — no messages endpoint exists in the current backend. Selecting a history conversation now sets the active conversation ID without destroying the current message view, so subsequent sends route to the correct conversation. Explicit `resetConversation()` (via "New conversation" button) handles the clear-messages case.

**Before:**
```ts
setConversationId(conv.id);
setMessages([]);
```

**After:**
```ts
setConversationId(conv.id);
```

---

## F-4R1-05 [Medium] — CUBE validation stubbed → FIXED

**File:** `src/components/CubeFunctions/CubeFormulaWizard.tsx:97-102`

**Fix:** Replaced the stubbed `useEffect` that unconditionally set `validated=true` with a real async validation flow that calls `validateQuery(modelId, rawQuery, { personaId })` when the user reaches step 2. Validation runs against the backend and populates `validationErrors` from the response. Falls back to `validated=true` on network error (non-blocking). The insert button remains disabled while validating or when errors are present.

**Key behavior:**
- Step 2 triggers async validation via `POST /api/v1/validate`
- Validation errors displayed to user with `ErrorOutline` icon
- Insert button disabled during validation and when errors exist
- Cleanup via cancelled flag prevents stale state updates

---

## F-4R1-06 [Low] — Unwired common components → FIXED

**Files:** `src/components/ReportBuilder/ReportBuilder.tsx`, `src/App.tsx`

**Fix:** Wired two common components into active paths:

1. **SearchBar** — Replaced inline `TextField` + `InputAdornment` + `IconButton` search in ReportBuilder with `<SearchBar>` component. Removed the local `useEffect`-based debounce (SearchBar handles 300ms debounce internally). Imports adjusted: removed `TextField`, `InputAdornment`, `IconButton`, `SearchIcon`, `CloseIcon`; added `SearchBar`.

2. **StatusBadge** — Replaced raw colored dot (`Box` with `borderRadius: 50%`) + `Typography` in App.tsx footer with `<StatusBadge>` component. Shows "Connected" / "Disconnected" / "Reconnecting" states based on health check status.

**Remaining unwired (by design):**
- `useSseStream.ts` — Alternative SSE hook; primary SSE handled by `useAgentConversation`
- `useExcelConnections.ts` — Ready for `LiveConnectionWizard` integration (deferred item P2-M9)
- `gateway.ts` — Health check already served by `auth.ts`; version check available for future diagnostics
- `SectionHeader`, `LoadingSkeleton`, `EmptyState` — Library components, consumed on demand

---

## Verification

```
tsc --noEmit   → PASS (zero errors)
vitest run     → PASS (4 files, 27 tests)
vite build     → PASS (production bundle)
```

---

*End of fix report.*
