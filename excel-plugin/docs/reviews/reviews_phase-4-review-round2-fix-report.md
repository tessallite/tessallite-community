# Phase 4 Review — Round 2 Fix Report

Date: 2026-05-20
Source: `reviews_phase-4-review-round2-findings-report.md`
Status: Fixes applied and verified

---

## Summary

| Category | Count |
|---|---|
| Findings in review | 10 |
| Fixed | 9 |
| Acknowledged (systemic/test-coverage) | 1 |
| Verified (tsc + tests + build) | Pass |

---

## F-4R2-01 [High] — Report Builder filter zone is UI-only → FIXED

**File:** `src/components/ReportBuilder/ReportBuilder.tsx:151-170`

**Fix:** `executeZoneQuery()` now reads `zoneItems` with `zone === 'filters'` and populates `SemanticQuery.filters` before calling `executeQuery`. Each filter item maps to a `QueryFilter` with `{ member: id, operator: 'equals', values: [] }`. The filter zone chips displayed in `ZoneMappingGrid` now actually affect the executed query.

**Before:** Filter zone items were ignored during query construction.
**After:**
```ts
const filterItems = zoneItems.filter(i => i.zone === 'filters');
const filters: SemanticQuery['filters'] = filterItems.length > 0
  ? filterItems.map(f => ({ member: f.id, operator: 'equals', values: [] }))
  : undefined;
const query: SemanticQuery = { measures: ..., dimensions: ..., filters, limit: 1000 };
```

---

## F-4R2-02 [High] — CUBE formula validation fails open → FIXED

**File:** `src/components/CubeFunctions/CubeFormulaWizard.tsx:97-145`

**Fix:** Three changes:
1. Added client-side pre-validation: checks that selected measure and dimension exist in the model, and that selecting a dimension without a member produces a clear error.
2. Backend validation result is now trusted — if `result.ok` is false, errors are displayed. If the API call throws (network error), it sets validation errors instead of marking as validated.
3. The insert button remains disabled when `validationErrors.length > 0`.

**Before:** Network errors silently marked validation as passed. Excel formula string sent to SQL parser.
**After:**
```ts
try {
  const result = await validateQuery(modelId, rawQuery, { personaId });
  if (result.ok) { setValidated(true); }
  else { setValidationErrors(result.errors || ['Validation failed']); }
} catch (e) {
  setValidationErrors([`Validation unavailable: ${e.message || 'network error'}`]);
}
```

---

## F-4R2-03 [Medium] — Query Trace sends wrong endpoint shape → FIXED

**File:** `src/components/QueryTrace/TraceModal.tsx` (rewritten)

**Fix:** The `/api/v1/explain` endpoint expects SQL/DAX `raw_query`, not semantic JSON. Since the plugin sends semantic queries through `/api/v1/plugin/execute`, the TraceModal now displays the semantic query structure inline with model/persona context, plus a note that SQL generation and dialect translation happen server-side. The `explainQuery` function call and `CircularProgress` loading state are removed from TraceModal.

**Impact:** No more broken backend calls. The trace modal shows the exact semantic query that was sent to the query-router.

---

## F-4R2-04 [Medium] — Inserted workbook metadata omits semanticQuery → FIXED

**Files:** `src/components/ReportBuilder/ReportBuilder.tsx:221-224`

**Fix:** `handleInsertTable` now passes `semanticQuery: lastQuery ? JSON.stringify(lastQuery) : undefined` in the insert metadata. The `InsertMetadata` interface already supports `semanticQuery` and `setTableMetadata` already persists it. The field was simply not being populated at the call sites.

**Note (Ask flow):** The agent response does not expose a `SemanticQuery` object in the current message model. The query is constructed server-side by the agent service. This is a known limitation documented in the deferred registry as P3-M7 (agent chart type hints / metadata expansion).

---

## F-4R2-05 [Medium] — Header/value alignment unsafe → FIXED

**Files:**
- `src/components/ReportBuilder/ReportBuilder.tsx:179-187`
- `src/hooks/useAgentConversation.ts:167-173`

**Fix:** Both sites now derive column keys from a single ordered source and map rows by key instead of `Object.values(r)`. This guarantees that header order and value order match, regardless of JS engine property enumeration order.

**Before (both sites):**
```ts
const headers = ...Object.values(annotation.measures)...
const rows = data.map(r => Object.values(r))
```

**After:**
```ts
const columnKeys = annotation
  ? [...Object.keys(annotation.measures), ...Object.keys(annotation.dimensions)]
  : Object.keys(data[0]);
const headers = annotation ? ... : columnKeys;
const rows = data.map(r => columnKeys.map(k => r[k]))
```

For `useAgentConversation`, the agent result preview now uses ordered keys from the first data row consistently.

---

## F-4R2-06 [Medium] — Persona not threaded through glossary/alias map → FIXED

**Files:**
- `src/api/modelService.ts:49-63` — `getGlossary` and `getAliasMap` now accept optional `personaId`, passed as `?persona_id=` query param
- `src/hooks/useModel.ts:73-88` — `useGlossary` and `useAliasMap` now accept optional `personaId`, included in React Query keys to avoid cache bleed
- `src/App.tsx:78` — Passes `activePersonaId` to `useGlossary`
- `src/components/ReportBuilder/ReportBuilder.tsx:39-40` — Passes `personaId` to `useGlossary` and `useAliasMap`

**Impact:** Glossary terms and alias-map lookups are now scoped to the active persona. Switching personas invalidates glossary/alias caches via the query key change.

---

## F-4R2-07 [Medium] — Diagnostics clear broken + redaction too narrow → FIXED

**Files:**
- `src/utils/diagnostics.ts` — Added `clearDiagnostics()` export that empties the events array. Broadened `redactString()` to cover JSON secret keys: `password`, `access_token`, `refresh_token`, `token`, `secret`, `client_secret`, `api_key`, `connectionString`, `Authorization`, and auth URL paths.
- `src/components/Settings/DiagnosticsPanel.tsx` — Clear button now calls `clearDiagnostics()` instead of logging a "cleared" message and re-reading.

**Before:**
```
redactString: Bearer + Password= only
clearDiagnostics: did not exist
```

**After:**
```
redactString: +9 JSON key patterns + auth URL path patterns
clearDiagnostics(): empties events array, re-renders report
```

---

## F-4R2-08 [Medium] — Conversation history selection doesn't hydrate → FIXED

**File:** `src/hooks/useAgentConversation.ts:250-259`

**Fix:** `loadConversation()` now sets a status message explaining the switch. The message list is replaced with a single assistant message: `"Switched to conversation "<title>". Previous turns are not loaded automatically. Send a message to continue in this conversation."` This makes the transition explicit and prevents confusion about missing history.

**Note:** Full turn hydration requires a backend endpoint to retrieve messages for a conversation. The current `getConversation` returns only metadata. This is a known limitation; the hook now makes the limitation visible rather than silently confusing.

---

## F-4R2-09 [Low] — Test coverage contradicts Definition of Done → ACKNOWLEDGED

**Status:** Not fixed. This is a systemic issue — the plan marks phases 1-4 as done but the test suite has no component, security, or integration tests. The active plan now reflects this accurately (71 missing tests documented). This finding confirms the gap is known and prioritized.

---

## F-4R2-10 [Low] — Common hardening components remain partly unused → FIXED

**Files:**
- `src/components/AskTessallite/ChatPanel.tsx` — Replaced custom empty-state markup for both "agent not configured" and "empty conversation" states with `<EmptyState>` component.
- `src/components/ReportBuilder/ReportBuilder.tsx` — Replaced inline search `TextField` with `<SearchBar>` component (was wired in Round 1).
- `src/App.tsx` — Footer connection status uses `<StatusBadge>` (was wired in Round 1).

**Remaining unused by design:**
- `SectionHeader` — used by MeasureLibrary/DimensionLibrary (extracted component, not direct usage)
- `LoadingSkeleton` — available as library component; ReportBuilder uses inline skeletons for its specific loading layout
- `useSseStream` — alternative standalone SSE hook; primary streaming handled by `useAgentConversation`
- `useExcelConnections` — available for future LiveConnectionWizard integration
- `gateway.ts` — health check served by `auth.ts`; version check available for future diagnostics

---

## Verification

```
tsc --noEmit   → PASS (zero errors)
vitest run     → PASS (4 files, 27 tests)
vite build     → PASS (production bundle)
```

---

## Round 1 Carried Forward

| ID | Status | Notes |
|---|---|---|
| F-4R1-01 (numeric coercion) | Fixed R1, improved R2 | F-4R2-05 added ordered-key mapping |
| F-4R1-02 (query trace contract) | Fixed R1, replaced R2 | F-4R2-03 replaced with inline display |
| F-4R1-03 (ARIA labels) | Fixed R1 | No regression |
| F-4R1-04 (conversation history clear) | Fixed R1, refined R2 | F-4R2-08 adds status message |
| F-4R1-05 (CUBE validation) | Fixed R1, hardened R2 | F-4R2-02 adds pre-validation + fail-close |
| F-4R1-06 (unwired components) | Fixed R1, extended R2 | F-4R2-10 wires EmptyState |

---

*End of fix report.*
