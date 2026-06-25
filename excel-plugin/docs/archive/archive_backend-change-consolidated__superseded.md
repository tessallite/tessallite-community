# Tessallite Excel Plugin -- Consolidated Backend Change Requirements

Date: 2026-05-20
Status: Validated -- 2 active backend changes, 3 items require no new backend work
Source: Merged from 5 individual backend change spec files

---

## Verification Summary

| # | Requirement | Backend Change? | Status | Action Plan Ref |
|---|---|---|---|---|
| 1 | P2-M5 Alias Map | Yes | **IMPLEMENTED** | `alias_map.py` deployed, router registered |
| 2 | P2-M8 Formula Validation | No | Endpoint exists — plugin wiring only | N/A |
| 3 | P3-M7 Agent Chart Hints | Yes | **NOT IMPLEMENTED** | See Change 1 below |
| 4 | P3-M8 Drill-Through Set | No | CRUD exists — plugin wiring only | N/A |
| 5 | Persona-Scoped API | Yes | **PARTIALLY IMPLEMENTED** | See Change 2 below |

---

## Change 1: Agent Chart Type in API Response (P3-M7)

**Priority:** Medium | **Blocks:** Excel Plugin P3-M7 (agent-provided chart hints)
**Status:** Not implemented. The backend change is needed before the plugin can consume chart hints.

### Problem

The agent selects a chart type (`bar`, `line`, `pie`, etc.) during query processing but the structured chart type is not returned in the API response. The Excel plugin cannot use the agent's recommendation and must run its own heuristic.

### Current State

- Chart selection: `services/agent-service/src/charts/selector.py` returns a chart type string.
- Pipeline: `services/agent-service/src/pipeline.py` uses chart type for `render_chart()` but does not persist it.
- `AgentTurn` model (line ~1987): no `chart_type` column.
- `TurnResponse` (conversations.py:98-121): no `chart_type` field.

### Files to Change

| File | Change |
|---|---|
| `shared/db/models.py` | Add `chart_type` column to `AgentTurn` |
| `shared/db/migrations/versions/` | New migration: add `chart_type` column (String(32), nullable) |
| `services/agent-service/src/api/conversations.py` | Add `chart_type: Optional[str] = None` to `TurnResponse` |
| `services/agent-service/src/pipeline.py` | Store selected chart type on the turn record |
| `services/agent-service/tests/` | 5 test cases |

### Tests

1. Auto-mode query with time series data returns `chart_type: "line"`
2. Auto-mode query with KPI data returns `chart_type: "kpi"`
3. LLM-mode query returns the LLM-selected `chart_type`
4. Query with no chart returns `chart_type: null`
5. Historical turns without chart_type return `null` (backward compatible)

---

## Change 2: Persona-Scoped API -- Execution Enforcement

**Priority:** High (security) | **Blocks:** Excel Plugin persona enforcement completeness
**Status:** Partially implemented. Metadata scoping (list/detail/drill-options) is done and tested. Execution enforcement is blocked on open question Q1 (see `work/action-plan-excel-plugin-persona-scoped-api.md`).

### What Is Done (Phases 0-2, 4-6, 9)

- Shared helper `_persona_scope.py`: `resolve_effective_persona()` — fail-closed, locked-persona override, conflict rejection, model validation.
- Measure, dimension, and hierarchy list endpoints accept optional `persona_id` and apply allow-list filtering.
- Detail endpoints (`get_measure`, `get_dimension`, `get_hierarchy`, `list_hierarchy_levels`) accept `persona_id` and reject hidden items with 404.
- Drill-options endpoint applies persona filtering (embed locked persona, hierarchy allow-list, hidden measure rejection).
- Excel plugin API client, hooks, cache keys, types, state cleanup all aligned.
- Tests: 20 model-service persona-scope tests, 8 query-router drill tests, 587 gateway tests, 27 Excel plugin tests — all green.

### What Is NOT Done

#### Phase 3: Execution Enforcement (blocked on Q1)

The Excel Report Builder sends `SemanticQuery` objects to `POST /api/v1/execute`, but the query-router `/execute` endpoint expects `ExecuteRequest` with `raw_query`, `protocol`, etc. These contracts are incompatible. Until resolved, Report Builder execution either fails (422) or bypasses persona enforcement.

**Open question Q1:** Which backend endpoint should the Excel Report Builder use for query execution?
- Option A: Headless `/api/v1/headless/query` — accepts JSON semantic query shape. Needs `persona_id` and `apply_persona_gate`.
- Option B: Query-router `/api/v1/execute` — requires plugin to build raw_query SQL. Frontend-heavy but uses fully-enforced pipeline.
- Option C: New dedicated endpoint wrapper.

**Required work after Q1 is resolved:**

| File | Change |
|---|---|
| Selected execution endpoint | Add active persona context to request schema |
| Selected execution endpoint | Resolve effective persona server-side |
| Selected execution endpoint | Reject conflicting persona values with 403 |
| Selected execution endpoint | Ensure execution planner receives effective persona |
| Tests | Prove user cannot execute query against hidden measures/dimensions |

#### Phase 2: Hierarchy-Dimension Filtering (blocked on Q3)

When `included_dimension_ids` is populated but `included_hierarchy_ids` is empty, hierarchy-backed dimensions can bypass the dimension allow-list.

**Open question Q3:** Should hierarchy responses suppress levels whose backing dimension is excluded by `included_dimension_ids`?

### Security Rules (Non-Negotiable)

1. Resolve effective persona on the server. Never trust client-supplied persona_id alone.
2. Never fall back to full catalog when persona_id is invalid, not found, or wrong-model.
3. Metadata scoping is not sufficient — query execution and drill-through must enforce the same persona.
4. The Excel plugin sends active persona context, but the backend is authoritative.

### Files Already Changed

| File | Change |
|---|---|
| `services/model-service/src/api/_persona_scope.py` | New: `resolve_effective_persona()`, `parse_allowed_ids()` |
| `services/model-service/src/api/measures.py` | Add `persona_id` param, persona filtering |
| `services/model-service/src/api/dimensions.py` | Add `persona_id` param, persona filtering |
| `services/model-service/src/api/hierarchies.py` | Add `persona_id` param, persona filtering |
| `tessallite/excel-plugin/src/api/modelService.ts` | Thread `personaId` into metadata calls |
| `tessallite/excel-plugin/src/hooks/useModel.ts` | Persona-aware hooks and query keys |
| `tessallite/excel-plugin/src/types/tessallite.ts` | Persona allow-list fields |

---

## Items Verified -- No Backend Change Required

### P2-M5: Alias Map CRUD Endpoint

**Status: Already implemented.**

File `services/model-service/src/api/alias_map.py` exists with:
- `GET /api/v1/projects/{pid}/models/{mid}/alias-map` — returns alias map (empty if unset)
- `PUT /api/v1/projects/{pid}/models/{mid}/alias-map` — replaces whole map (modeler/admin)
- `POST /api/v1/projects/{pid}/models/{mid}/alias-map/import` — bulk import with merge/replace mode

Router is registered in `services/model-service/src/main.py:166`. The alias map is also cleaned up in cascade delete (`_cascade_delete.py:72`).

**Backend work needed: NONE.** The Excel plugin still needs to call this endpoint from its search logic (`filterBySearch` in ReportBuilder) — that is plugin-side work.

### P2-M8: Formula Validation Endpoint

**Status: Already exists.**

Endpoint `POST /api/v1/validate` in `services/query-router/src/api/routes.py:352-365` is fully implemented with `ValidateResponse` (ok, errors, warnings, requested_measures, etc.). The Excel plugin's `queryRouter.ts` already exports a `validateQuery` function. No UI component calls it yet.

**Backend work needed: NONE.** Plugin just needs to call `validateQuery()` from `CubeFormulaWizard` and disable Insert when `ok === false`.

### P3-M8: Drill-Through Set Column Configuration

**Status: Already exists.**

Full DrillThroughSet CRUD is implemented in `services/model-service/src/api/measures.py:1235-1350`:
- `GET /{measure_id}/drill-through-set`
- `PATCH /{measure_id}/drill-through-set`
- `DELETE /{measure_id}/drill-through-set`
- `POST /{measure_id}/drill-through-set/join-paths`

Schemas exist in `shared/schemas/pydantic_models.py`, and drill-through execution pipeline is in `services/query-router/src/api/drill_routes.py`.

**Backend work needed: NONE.** Plugin just needs to add `getDrillThroughSet()` to `modelService.ts` and consume it in `DrillPanel`.

---

## Execution Plan Linkage

- The master Excel-first roadmap (`work/strategy_excel-first-roadmap.md`) does not explicitly track P2-M5, P2-M8, P3-M7, or P3-M8 — these are sub-tasks of the Excel plugin execution plan (`tessallite/excel-plugin/EXECUTION-PLAN.md`).
- Persona-scoped API is explicitly tracked in `work/action-plan-excel-plugin-persona-scoped-api.md` and linked from the master roadmap.
- The Excel plugin execution plan (`EXECUTION-PLAN.md` §5 Workstream B) lists alias map loading as a Phase 2 metadata task. §6 Workstream D lists formula validation as a Cube Function Wizard task. §7 Workstream A mentions chart hints. §7 Workstream D lists drill-through set integration.

---

*End of consolidated backend change requirements.*
