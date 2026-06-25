# Deferred Work Review: Blocked vs Ready

Date: 2026-05-20
Scope: All deferred items from Phase 0, 1, 2, 3 across all review rounds
Source: `docs/reviews/` reports, fix reports, `execution_deferred-registry.md`

---

## BLOCKED (require external dependency)

### ~~Blocked by Backend API Change~~ — ALL RESOLVED

All 5 items previously listed as backend-blocked have been verified — the endpoints exist:
- `GET .../alias-map` — `model-service/src/api/alias_map.py` (registered in main.py)
- `POST /api/v1/validate` — `query-router/src/api/routes.py:349`
- SSE `chart_type` — Bug-626 fixed 2026-05-20, `chart_type` now in `turn.completed` payload
- `GET .../drill-through-set` — `model-service/src/api/measures.py:1236` (GET/PUT/POST + join-paths)
- `POST .../drill-options` — `query-router/src/api/drill_routes.py:124`

These items have been moved to the READY section below.

### Blocked by Office.js / Excel Host

| # | ID | Item | Phase | Blocker |
|---|---|---|---|---|
| 6 | P0-T2 | Verify Office host support | 0 | Requires physical Excel Desktop (Windows/Mac/Web) |
| 7 | P0-T3 | Run `runCompatibilitySpike()` | 0 | Requires physical Excel Desktop |
| 8 | P0-T4 | Fill compatibility matrix | 0 | Depends on P0-T2/T3 |
| 9 | P2-M9 | XMLA connection existence check | 2 | `workbook.connections.load()` not in standard Office.js typedefs |
| 10 | P1-DEF-9/P3-M5 | Context menu integration | 1/3 | Requires `ExtensionPoint xsi:type="ContextMenu"` manifest entry + Office.js support |

---

## READY TO IMPLEMENT (no external blocker)

### HIGH Priority (11 items)

| # | ID | Item | Phase | What To Do |
|---|---|---|---|---|
| 1 | P3-M1 | Persona measure/dimension filtering | 3 | Wire personaId into measure/dimension/hierarchy queries. Backend endpoint available. Replace identity passthrough in `usePersonaFiltered`. |
| 2 | P3-M2 | Persona-filtered glossary | 3 | Pass personaId to glossary query or filter client-side. Depends on P3-M1 completion. |
| 3 | P3-M3 | Persona-filtered CUBE formula catalog | 3 | Pass personaId when listing measures in CubeFormulaWizard. Depends on P3-M1. |
| 4 | P2-#5 | CUBEVALUE member selector UI | 2 | Add member picker in CubeFormulaWizard calling `POST /api/v1/discover/members`. |
| 5 | P2-#9 | Filter zone member selection UI | 2 | Add member value picker + operator editor in Report Builder filter zone. |
| 6 | P2-#25 | Complete table metadata | 2 | Pass `projectId`, `modelId`, `personaId`, `semanticQuery` through `insertTable -> doInsertAndTag -> setTableMetadata`. Fields already exist in `TableMetadata` interface. |
| 7 | P2-M5 | Search across alias map | 2 | Call `GET .../alias-map` (exists in model-service). Wire into search filtering logic. |
| 8 | P2-M8 | CUBE formula validation before insert | 2 | Call `POST /api/v1/validate` (exists in query-router). Show validation errors before inserting CUBE formulas. |
| 9 | P3-M7 | Agent-provided chart type hints | 3 | SSE `chart_type` now in payload (Bug-626 fixed). Wire into chart recommendation logic in App.tsx. |
| 10 | P3-M8 | DrillThroughSet column configuration | 3 | Call `GET .../drill-through-set` (exists in model-service). Configure drill-through columns in DrillPanel. |
| 11 | P3-M4 | Breadcrumb up-level drill navigation | 3 | Call `POST .../drill-options` (exists in query-router). Build breadcrumb path with up-level navigation. |

### MEDIUM Priority (7 items)

| # | ID | Item | Phase | What To Do |
|---|---|---|---|---|
| 12 | P2-M6 | Measure card expanded view | 2 | Add toggle on MeasureCard: show aggregation, lineage, folder, cross-model badge, semi-additive, glossary synonyms. |
| 13 | P2-M10 | Virtualized lists | 2 | `react-window` installed but never imported. Replace `VirtualList.tsx` truncation with proper `FixedSizeList` when count > 100. |
| 14 | P2-M13 | Measure grouping by display_folder | 2 | Sort and group measures by `display_folder` with collapsible folder headers. |
| 15 | P2-M14 | Variant measure indentation | 2 | Sort variant measures under their base measure. Indent cards visually. Show `[+ N variants]` toggle. |
| 16 | P3-M6 | Ribbon integration for Phase 3 | 3 | Add ribbon buttons for Chart, Local Pivot, Drill in manifest.xml. |
| 17 | P2-M7 | Dimension "Preview members" link | 2 | Add clickable link on DimensionCard opening inline panel with first 20 members via `discoverMembers`. |
| 18 | P1-DEF-12 | Conversation management UI | 1 | Build conversation list / history panel using `getConversations`/`deleteConversation` APIs already in `agentService.ts`. |

Items removed (already implemented):
- ~~P1-DEF-16~~ — `diagnostics.ts` exists and is complete (ring buffer, redaction, export)
- ~~P2-M11~~ — `useModel.ts` already includes `deployed_version_id` in query keys with 5-min staleTime
- ~~P1-DEF-13~~ — Number formatting already wired through `formatTokens` in `useExcel.ts` → `officeSpike.ts`

### LOW Priority (7 items)

| # | ID | Item | Phase | What To Do |
|---|---|---|---|---|
| 19 | P1-DEF-11 | Follow-up suggestion chips | 1 | Render clickable suggestion chips below agent responses in ChatMessage. |
| 20 | P1-DEF-14 | Loading skeletons | 1 | Replace spinners with MUI `<Skeleton>` components in key loading states. |
| 21 | P1-DEF-15 | Reduced motion support | 1 | Detect `prefers-reduced-motion` media query and disable animations. |
| 22 | P1-DEF-8 | Query trace modal | 1 | Build modal via `POST /api/v1/explain` showing pipeline steps, route decision, SQL diff. |
| 23 | P2-#27 | HierarchyLibrary separate component | 2 | Extract hierarchy rendering from ReportBuilder into standalone `HierarchyLibrary.tsx`. |
| 24 | P2-#28 | MeasureLibrary/DimensionLibrary separate components | 2 | Extract measure and dimension rendering from ReportBuilder into standalone wrapper components. |
| 25 | P3-16 | Multi-measure CUBE drill-through | 3 | Known limitation. Document only -- extract first measure match from multi-measure formulas. |

Items removed (already implemented):
- ~~P1-DEF-10~~ — manifest.xml already has real UUID `e3ae536b-1d2f-44fd-bdc4-e01b5a7597d2`, version, ribbon buttons

---

## SUMMARY

| Category | Count |
|---|---|
| Blocked (backend API) | 0 (all resolved) |
| Blocked (Office.js / Excel host) | 5 |
| Ready -- HIGH | 11 |
| Ready -- MEDIUM | 7 |
| Ready -- LOW | 7 |
| Already implemented (removed) | 4 |
| **Total remaining** | **30** |

**5 items** require a physical Excel Desktop for testing (Office.js host). **25 items** are ready to implement immediately — all backend endpoints exist.
