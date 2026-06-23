# Excel Plugin Deep Review — Phase 1-4 (Round 1)

Date: 2026-05-20
Scope file: `tessallite/excel-plugin/docs/execution/active-plan-files.md`
Scope reviewed: completed parts of Phase 1, 2, 3, 4
Reviewer mode: read-only (no source-code edits)

## Verdict

Not approved as fully production-hardened yet.

I found 6 actionable issues in completed phase scope:
- 0 critical
- 2 high
- 3 medium
- 1 low

I also validated mandatory checks about query-router centralization and dialect branching (details below).

---

## Findings

### F-4R1-01 [High] Report Builder converts numeric results to strings and can misalign headers with row values

**Where**
- `tessallite/excel-plugin/src/components/ReportBuilder/ReportBuilder.tsx:184-189`

**What is wrong**
- Headers are derived from `annotation` title arrays (`:184-186`), while row values are derived from `Object.values(r)` (`:187-189`).
- Numbers are explicitly coerced to string (`String(v)`) (`:188`).

**Impact**
- Native Excel artifacts can receive text instead of numeric values, degrading sort/aggregation/chart behavior.
- Header/value ordering can drift when annotation order and object key order differ, causing silent column mislabeling.

**Why this matters for phase goals**
- Violates the “native Excel artifact” trust expectation for business output quality.

**Recommended fix**
- Use a single authoritative column order (prefer router `columns` or deterministic annotation-to-key mapping).
- Preserve numeric types in row payloads (do not stringify numbers).

---

### F-4R1-02 [High] Query Trace modal posts an invalid payload shape to `/api/v1/explain`

**Where**
- `tessallite/excel-plugin/src/api/queryRouter.ts:38-40`
- `tessallite/excel-plugin/src/components/QueryTrace/TraceModal.tsx:20-25`
- Expected backend contract: `tessallite/services/query-router/src/api/routes.py:144-147, 327-333`

**What is wrong**
- Client sends `{ query }` to `/api/v1/explain`.
- Backend expects `ExecuteRequest` with fields like `model_id` and `raw_query`.

**Impact**
- Trace generation path is nonfunctional or fails frequently with validation errors.
- UI presents “Generate Trace” but cannot reliably provide trace details.

**Recommended fix**
- Send the backend’s expected `ExecuteRequest` shape.
- Include `model_id`, `raw_query` (or transform semantic query through correct plugin explain surface), and `persona_id` where needed.

---

### F-4R1-03 [Medium] Accessibility “done” claim is not met for key interactive controls (missing ARIA labels)

**Where**
- Plan claim: `tessallite/excel-plugin/docs/execution/active-plan-files.md:95`
- Missing labels examples:
  - `tessallite/excel-plugin/src/components/AskTessallite/ChatPanel.tsx:140-147, 235-242`
  - `tessallite/excel-plugin/src/components/LoginScreen/LoginScreen.tsx:119-121`
  - `tessallite/excel-plugin/src/components/Settings/DiagnosticsPanel.tsx:54-56`

**What is wrong**
- Multiple `IconButton` controls are clickable but have no explicit `aria-label`.

**Impact**
- Screen-reader discoverability is reduced for key actions (send message, history, password visibility toggle, close controls).

**Recommended fix**
- Add explicit `aria-label` for all icon-only controls.
- Re-run an accessibility sweep after updates.

---

### F-4R1-04 [Medium] Conversation history selection clears messages instead of loading turn history

**Where**
- `tessallite/excel-plugin/src/hooks/useAgentConversation.ts:250-254`

**What is wrong**
- `loadConversation()` sets the selected conversation id, then calls `setMessages([])`.
- No retrieval of existing turns/messages is performed.

**Impact**
- “Conversation history” UI appears present, but selecting history effectively opens an empty conversation view.
- Functional gap in ask workflow continuity.

**Recommended fix**
- Load and hydrate turns/messages for selected conversations.
- Only clear state when explicitly starting a new conversation.

---

### F-4R1-05 [Medium] CUBE formula “validation” state is stubbed and marks valid without backend validation

**Where**
- `tessallite/excel-plugin/src/components/CubeFunctions/CubeFormulaWizard.tsx:97-102`
- Related available API function: `tessallite/excel-plugin/src/api/queryRouter.ts:42-53`

**What is wrong**
- Step 3 sets `validated=true` without actual validation call.
- Validation UI signals readiness even when semantic/member references may be invalid.

**Impact**
- Users can insert invalid formulas with false confidence from the wizard state.

**Recommended fix**
- Wire real validation to backend before enabling final insert, or clearly mark validation as not performed.

---

### F-4R1-06 [Low] Phase 4 “common components/hook extraction” has unwired/dead artifacts

**Where**
- Exported but unreferenced in current plugin codebase:
  - `tessallite/excel-plugin/src/hooks/useSseStream.ts:23`
  - `tessallite/excel-plugin/src/hooks/useExcelConnections.ts:25`
  - `tessallite/excel-plugin/src/api/gateway.ts:1-13`
  - Common components under `tessallite/excel-plugin/src/components/common/*` (no import hits)
- Usage scan evidence:
  - `rg -n "api/gateway|useSseStream|useExcelConnections|common/SearchBar|common/SectionHeader|common/StatusBadge|common/EmptyState|common/LoadingSkeleton" tessallite/excel-plugin/src -S`

**What is wrong**
- Extracted artifacts exist but are not integrated into active flows.

**Impact**
- Increases maintenance surface and drift risk.
- Creates a false sense of hardening completeness.

**Recommended fix**
- Either wire these artifacts into active paths or remove/defer them explicitly.

---

## Mandatory Full-Sweep Checklist Results

### Plan + implementation reviewed together
- Checked against `active-plan-files.md` done claims for phases 1-4 and corresponding source files.

### Enhancements/bugs/mistakes/unwired code/bad decisions/inefficiencies/incompatibilities/security/edge cases/race conditions/cloud issues/missing frontend pieces
- Checked.
- Actionable findings listed above (F-4R1-01..06).

### Source/target DB queries bypassing query-router
- Checked plugin scope.
- In `excel-plugin/src`, data-query paths are API-driven (not direct DB):
  - `tessallite/excel-plugin/src/api/queryRouter.ts:22,47,60`
- No direct source/target DB execution found in plugin code.

### Conditional SQL processing by source/target DB type (sqlglot requirement)
- Checked plugin scope.
- No SQL dialect branching logic exists in plugin source.

### Compare prior scope and identify omitted work / hidden unresolved items
- Checked.
- Several items are marked done at phase level while functional gaps remain in completed-path UX (trace, conversation history, validation, accessibility).

### Distinguish operational plumbing from user/business query execution
- Checked.
- Findings focus on user-facing plugin behavior and query-contract correctness.

### Include line references
- Provided for each actionable finding.

### Explicitly state absence when no issue found
- Query-router bypass and dialect branching checks in plugin scope: no direct violations found.

---

## Validation Evidence

Executed locally in `tessallite/excel-plugin`:

1. `npm test`
- Result: 4 files, 27 tests, all passed.

2. `npm run build`
- Result: success (`tsc` + `vite build`).
- Warning: single JS chunk ~533.5 kB (chunk-size warning), indicating pending bundle-splitting optimization.

---

## Summary

The phase 1-4 implementation has strong coverage of major surfaces, but production-hardening claims are overstated in key areas. Priority fixes should focus on:
1. data integrity in Report Builder inserts,
2. fixing Query Trace request contract,
3. real CUBE validation,
4. conversation history hydration,
5. accessibility label coverage.