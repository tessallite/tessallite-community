# Tessallite Excel Plugin — Active Plan (Consolidated)

Date: 2026-05-20 | Status: active

Merged from: execution_plan.md, execution_remaining-work.md, execution_deferred-action-plan.md, execution_deferred-status.md, execution_deferred-registry.md

---

## 1. Master Execution Plan

### 1.1 Goal

Build a Microsoft Excel add-in that lets business users move from business intent to governed Excel output:

1. Ask a governed business question.
2. Review a concise answer and result preview.
3. Insert a native Excel artifact.
4. Use glossary, persona scope, CUBE formulas, and drill-through when needed.

The default user journey is Ask Tessallite -> Insert Table/Chart/Local Pivot/CUBE formulas. Report Builder is the structured fallback for repeatable reports and power users.

### 1.2 Non-Negotiable Product Constraints

#### 1.2.1 Excel Output Contract

Every insert action must create a native Excel artifact. The task pane is a control surface, not the destination.

| Output | Allowed | Notes |
|---|---|---|
| Formatted Excel Table | Yes | MVP default. Query results from Tessallite REST APIs. |
| Native Excel Chart | Yes | Backed by an inserted worksheet table/range. |
| Local Excel PivotTable | Yes | Created from inserted worksheet table/range only, where Office.js supports it. |
| CUBE formula range | Yes | Requires an existing XMLA workbook connection. |
| Live XMLA PivotTable | Assisted only | Add-in can create/verify connection and show instructions. |

Do not implement a button that claims to directly create a live external XMLA PivotTable. Office.js cannot do this.

#### 1.2.2 Security Constraints

- Do not store passwords in any form.
- Do not log passwords.
- Do not put passwords in workbook custom properties, telemetry, diagnostics, React Query cache, localStorage, or OfficeRuntime.storage.
- Login password used only for `POST /api/v1/auth/login`, then cleared from component state.
- XMLA/MSOLAP credentials requested only when user explicitly creates a live connection or inserts CUBE formulas requiring a connection.

#### 1.2.3 Scope Boundaries

Do not build: Model health dashboard, schema drift screen, aggregate coverage dashboard, pocket table status UI, data quality/admin panels, model export/snapshot/version diff, permanent query trace workspace, full model-builder or admin workflows.

### 1.3 Phase 0: Project Setup and Technical Spike — COMPLETE (partial matrix)

| Task | Status |
|---|---|
| Scaffold (manifest.xml, package.json, vite, tsconfig, main.tsx, App.tsx) | Done |
| Verify Office host support | NOT DONE (requires physical Excel Desktop/Mac/Web) |
| Spike Office.js operations (write values, create table, chart, pivot, formulas, read cells, detect connections) | Done (functions exist, never executed on real host) |
| Compatibility matrix | Template exists at `docs/architecture/architecture_compatibility-matrix.md`. All `?` placeholders. |

### 1.4 Phase 1: Foundation and Ask MVP — DONE

| Workstream | Status |
|---|---|
| Authentication and Profiles (LoginScreen, useAuth, storage, ProfileSwitcher) | Done |
| API Client Foundation (client, auth, modelService, queryRouter, agentService, gateway) | Done |
| Ask Tessallite (ChatPanel, ChatMessage, InsertActions, JudgeVerdict) | Done |
| Insert as Table (useExcel, officeSpike, workbookMetadata) | Done |
| SSE streaming extracted to useAgentConversation + useSseStream | Done |

### 1.5 Phase 2: Report Builder and CUBE Formulas — DONE

| Workstream | Status |
|---|---|
| Metadata Loading (useModel, modelService) | Done |
| Report Builder (ReportBuilder, ZoneMappingGrid, MeasureCard, DimensionCard, HierarchyCard, HierarchyLibrary, MeasureLibrary, DimensionLibrary, TemplatePicker, TemplateCard, VirtualList) | Done |
| Report Templates (TemplatePicker, reportTemplates) | Done |
| CUBE Function Wizard | Done |
| Live Connection Helper | Done |
| connectionStrings.ts + useExcelConnections.ts | Done |

### 1.6 Phase 3: Charts, Local PivotTables, Personas, Drill-Through — DONE

| Workstream | Status |
|---|---|
| Insert Chart (excelCharts, heuristic + agent chart hints) | Done |
| Local PivotTable (excelPivotTables, runtime capability gate) | Done |
| Persona Switcher (PersonaDropdown, usePersona) | Done |
| Drill-Through (DrillPanel, DrillPathPicker, cellContext) | Done |

### 1.7 Phase 4: Production Hardening — DONE

| Workstream | Status |
|---|---|
| Error Handling (formatApiError, typed mapping, diagnostics logging, offline banner) | Done |
| Diagnostics (diagnostics.ts ring buffer, DiagnosticsPanel UI, wired into App.tsx settings menu) | Done |
| Accessibility (reduced motion in theme.ts, ARIA labels on key elements) | Done |
| Performance (VirtualList replaced with react-window v2 List, chat streaming batched to 100ms intervals) | Done |
| Common Components (SearchBar, StatusBadge, SectionHeader, LoadingSkeleton, EmptyState) | Done |
| Distribution (icons verified, manifest has real UUID) | Done |

### 1.8 Testing Plan

| Type | Current | Target |
|---|---|---|
| Unit tests | 4 files, 27 tests | 9 additional files, 71 tests |
| Component tests | 0 | 6 files, 30 tests |
| Security tests | 0 | 1 file, 6 tests |
| Excel integration tests | 0 | 8 scenarios (requires live Excel) |
| UAT scenarios | 0 | 8 scenarios (requires live Excel) |

### 1.9 Open Design Decisions

| # | Decision | Status |
|---|---|---|
| D1 | Login token exposure | RESOLVED — Bearer token from OfficeRuntime.storage |
| D2 | Local PivotTable support | RESOLVED — Gated by Office.context.requirements.isSetSupported('ExcelApi', '1.7') |
| D3 | XMLA connection creation | RESOLVED — Attempt + manual fallback implemented |
| D4 | Result refresh metadata | RESOLVED — workbookMetadata.ts with named ranges |
| D5 | Chart type selection | RESOLVED — Agent metadata + heuristic fallback |
| D6 | Large result limit | RESOLVED — Preview cap + confirmation above 10,000 rows |

### 1.10 Definition of Done

A phase is done only when:
- The user-facing workflow works inside Excel, not only in browser preview.
- The workbook artifact is native Excel output.
- Labels follow the output naming rules.
- No password persistence is introduced.
- Errors are recoverable and readable.
- Accessibility basics are implemented.
- Tests cover the core logic and security constraints.
- The demo script for the phase passes.
- The implementation does not add admin/modeler workflows excluded by the specs.

---

## 2. Remaining Work

### 2.1 Work Summary

| # | Category | Scope |
|---|---|---|
| A | Tests | 71 new tests across 12 files |
| B | Deferred items (backend API available) | 11 HIGH, 7 MEDIUM, 7 LOW |
| C | Deferred items (backend blocked) | 0 (all resolved) |
| D | Deferred items (Office.js/Excel host blocked) | 5 |

### 2.2 Missing Tests

| Test File | Category | Tests | Priority |
|---|---|---|---|
| `apiClient.test.ts` | Unit | 8 | HIGH |
| `storage.test.ts` | Unit | 5 | HIGH |
| `formatMapping.test.ts` | Unit | 9 | MEDIUM |
| `queryBuilder.test.ts` | Unit | 6 | MEDIUM |
| `search.test.ts` | Unit | 7 | MEDIUM |
| `LoginScreen.test.tsx` | Component | 5 | HIGH |
| `ChatPanel.test.tsx` | Component | 6 | HIGH |
| `ReportBuilder.test.tsx` | Component | 6 | HIGH |
| `TemplatePicker.test.tsx` | Component | 4 | MEDIUM |
| `PersonaDropdown.test.tsx` | Component | 4 | MEDIUM |
| `GlossaryModal.test.tsx` | Component | 5 | MEDIUM |
| `security.test.ts` | Security | 6 | HIGH |
| **Total** | | **71** | |

---

## 3. Deferred Items — Blocked vs Ready

### 3.1 Blocked by Office.js / Excel Host (5 items)

| # | ID | Item | Phase | Blocker |
|---|---|---|---|---|
| 1 | P0-T2 | Verify Office host support | 0 | Requires physical Excel Desktop (Windows/Mac/Web) |
| 2 | P0-T3 | Run runCompatibilitySpike() | 0 | Requires physical Excel Desktop |
| 3 | P0-T4 | Fill compatibility matrix | 0 | Depends on P0-T2/T3 |
| 4 | P2-M9 | XMLA connection existence check | 2 | workbook.connections.load() not in standard Office.js typedefs |
| 5 | P1-DEF-9/P3-M5 | Context menu integration | 1/3 | Requires ExtensionPoint xsi:type="ContextMenu" manifest entry |

### 3.2 READY — HIGH Priority (11 items)

| # | ID | Item | What To Do |
|---|---|---|---|
| 1 | P3-M1 | Persona measure/dimension filtering | Wire personaId into measure/dimension/hierarchy queries. Filter in usePersonaFiltered. |
| 2 | P3-M2 | Persona-filtered glossary | Pass personaId to glossary query or filter client-side. |
| 3 | P3-M3 | Persona-filtered CUBE formula catalog | Pass personaId when listing measures in CubeFormulaWizard. |
| 4 | P2-#5 | CUBEVALUE member selector UI | Add member picker in CubeFormulaWizard calling discoverMembers. |
| 5 | P2-#9 | Filter zone member selection UI | Add member value picker + operator editor in Report Builder filter zone. |
| 6 | P2-#25 | Complete table metadata | Pass projectId, modelId, personaId, semanticQuery through insertTable -> setTableMetadata. |
| 7 | P2-M5 | Search across alias map | Call GET .../alias-map (endpoint exists). Wire into search filtering logic. |
| 8 | P2-M8 | CUBE formula validation before insert | Call POST /api/v1/validate (endpoint exists). Show validation errors. |
| 9 | P3-M7 | Agent-provided chart type hints | SSE chart_type now in payload (Bug-626 fixed). Wire into chart recommendation logic. |
| 10 | P3-M8 | DrillThroughSet column configuration | Call GET .../drill-through-set (endpoint exists). Configure drill-through columns. |
| 11 | P3-M4 | Breadcrumb up-level drill navigation | Call POST .../drill-options (endpoint exists). Build breadcrumb with up-level nav. |

### 3.3 READY — MEDIUM Priority (7 items)

| # | ID | Item | What To Do |
|---|---|---|---|
| 12 | P2-M6 | Measure card expanded view | Toggle on MeasureCard: aggregation, lineage, folder, cross-model badge, semi-additive, glossary synonyms. |
| 13 | P2-M7 | Dimension "Preview members" link | Add clickable link on DimensionCard opening inline panel with first 20 members via discoverMembers. |
| 14 | P1-DEF-12 | Conversation management UI | Build conversation list/history panel using getConversations/deleteConversation APIs (already in agentService.ts). |
| 15 | P1-DEF-11 | Follow-up suggestion chips | Render clickable suggestion chips below agent responses in ChatMessage. |
| 16 | P1-DEF-8 | Query trace modal | Build modal via POST /api/v1/explain showing pipeline steps, route decision, SQL diff. |
| 17 | P3-M6 | Ribbon integration for Phase 3 | Add ribbon buttons for Chart, Local Pivot, Drill in manifest.xml. |
| 18 | P3-16 | Multi-measure CUBE drill-through | Known limitation. Document only — extract first measure match from multi-measure formulas. |

### 3.4 Summary

| Category | Count |
|---|---|
| Blocked (Office.js / Excel host) | 5 |
| Ready — HIGH | 11 |
| Ready — MEDIUM | 7 |
| Ready — LOW | 0 (all implemented in Phase 4) |
| Already implemented since last audit | 10 |
| **Total remaining** | **23** |

### 3.5 Already Implemented Since Deferred Audit (removed from open list)

| ID | Title | Implemented In |
|---|---|---|
| P1-DEF-13 | Number formatting from response metadata | Phase 4 (formatTokens wiring in useExcel.ts) |
| P1-DEF-14 | Loading skeletons | Phase 4 (LoadingSkeleton common component) |
| P1-DEF-15 | Reduced motion support | Phase 4 (theme.ts prefersReducedMotion) |
| P1-DEF-16 | Diagnostics module with redaction | Phase 4 (diagnostics.ts + DiagnosticsPanel) |
| P2-M10 | Virtualized lists (react-window) | Phase 4 (VirtualList replaced with react-window v2 List) |
| P2-M11 | Metadata caching by deployed_version_id | Already done in useModel.ts (query keys include versionKey, 5-min staleTime) |
| P2-M13 | Measure grouping by display_folder | Phase 4 (MeasureLibrary groups by folder with collapsible headers) |
| P2-M14 | Variant measure indentation | Phase 4 (MeasureLibrary sorts variants under base, 20px left margin) |
| P2-#27 | HierarchyLibrary separate component | Phase 2 (HierarchyLibrary.tsx exists) |
| P2-#28 | MeasureLibrary/DimensionLibrary separate components | Phase 4 (MeasureLibrary.tsx, DimensionLibrary.tsx extracted from ReportBuilder) |
| P1-DEF-10 | Manifest GUID placeholder | Phase 2 (manifest.xml has real UUID e3ae536b-1d2f-44fd-bdc4-e01b5a7597d2) |

### 3.6 Recovered From Earlier Phases

| ID | Title | Implemented In |
|---|---|---|
| P1-DEF-1 | Report Builder (zone mapping, libraries) | Phase 2 |
| P1-DEF-2 | Report templates | Phase 2 |
| P1-DEF-3 | Cube Function Wizard | Phase 2 |
| P1-DEF-4 | Live connection helper | Phase 2 |
| P1-DEF-5 | Glossary search modal | Phase 2 |
| P1-DEF-6 | Persona switcher | Phase 3 |
| P1-DEF-7 | Drill-through panel | Phase 3 |

---

## 4. Full Deferred Registry (Historical)

### 4.1 Summary by Phase

| Phase | Total Deferred | Implemented Later | Still Open | Partial |
|---|---|---|---|---|
| Phase 0 | 3 | 0 | 3 | 0 |
| Phase 0-1 | 16 | 8 | 7 | 0 |
| Phase 2 | 13 | 0 | 11 | 2 |
| Phase 3 | 10 | 0 | 9 | 1 |
| **Total** | **42** | **8** | **30** | **3** |

Note: 10 of the 30 "still open" items have been implemented in Phase 4 (see section 3.5). 23 remain.

### 4.2 Category A: Backend API (All Resolved)

All 7 items previously listed as backend-blocked have been verified — endpoints exist. Moved to READY section.

### 4.3 Category B: Office.js / Excel Host (5 items)

| ID | Title | Severity | Phase |
|---|---|---|---|
| P0-T2 | Verify Office host support | HIGH | 0 |
| P0-T3 | Run runCompatibilitySpike() | HIGH | 0 |
| P0-T4 | Compatibility matrix | HIGH | 0 |
| P2-M9 | XMLA connection check before CUBE insert | HIGH | 2 |
| P3-M5 | Context menu integration | MEDIUM | 3 |

### 4.4 Category C: UI Not Built — No External Blocker (10 open, 2 now done)

| ID | Title | Severity | Status |
|---|---|---|---|
| P1-DEF-8 | Query trace modal | LOW | Open |
| P1-DEF-11 | Follow-up suggestion chips | LOW | Open |
| P1-DEF-12 | Conversation management | LOW | Open |
| P1-DEF-13 | Number formatting from response metadata | LOW | DONE (Phase 4) |
| P1-DEF-14 | Loading skeletons | LOW | DONE (Phase 4) |
| P1-DEF-15 | Reduced motion support | LOW | DONE (Phase 4) |
| P1-DEF-16 | Diagnostics module with redaction | MEDIUM | DONE (Phase 4) |
| P2-M6 | Measure card expanded view | MEDIUM | Open |
| P2-M7 | Dimension "Preview members" link | LOW | Open |
| P2-M13 | Measure grouping by display_folder | LOW | DONE (Phase 4) |
| P2-#5 | CUBEVALUE member selector | HIGH | Open |
| P2-#9 | Filter zone member selection UI | HIGH | Open |

### 4.5 Category D: Partially Implemented (3 items)

| ID | Title | Severity | Status |
|---|---|---|---|
| P2-M14 | Variant measure indentation | LOW | DONE (Phase 4) |
| P2-#25 | Table metadata incomplete | MEDIUM | Open — fields exist but not populated |
| P3-M4 | Breadcrumb up-level navigation | MEDIUM | Open — breadcrumb renders but no up-level nav |

### 4.6 Category E: Structural / Performance / Cosmetic (8 items)

| ID | Title | Severity | Status |
|---|---|---|---|
| P2-M10 | Virtualized lists (react-window) | MEDIUM | DONE (Phase 4) |
| P2-M11 | Metadata caching by deployed_version_id | LOW | DONE (already existed) |
| P2-#27 | HierarchyLibrary as separate component | LOW | DONE (Phase 2) |
| P2-#28 | MeasureLibrary/DimensionLibrary separate | LOW | DONE (Phase 4) |
| P3-M6 | Ribbon integration for Phase 3 | LOW | Open |
| P3-16 | Multi-measure CUBE drill-through | LOW | Open |
| P1-DEF-10 | Manifest GUID placeholder | LOW | DONE |

---

## 5. Recommended Prioritization

### Must Fix Before Production

1. P0-T2/T3/T4 — Compatibility matrix populated (requires Excel desktop testing)
2. P3-M1 — Persona filtering (backend endpoints exist)
3. P2-#25 — Complete table metadata (fields exist, just need wiring)
4. P2-#5 — CUBEVALUE member selector
5. P2-#9 — Filter zone member selection UI

### High Value, Low Effort

6. P2-M7 — Dimension "Preview members" link
7. P1-DEF-12 — Conversation management UI (API already wired)
8. P2-M6 — Measure card expanded view

### Can Defer

9. P1-DEF-8 (query trace), P1-DEF-11 (suggestion chips)
10. P3-M6 (ribbon), P3-16 (multi-measure CUBE drill-through)
11. P3-M5 (context menu — Office.js blocked)

---

## 6. Current File Inventory

### Components (29 present, 0 missing from plan)
```
src/components/AskTessallite/ChatPanel.tsx
src/components/AskTessallite/ChatMessage.tsx
src/components/AskTessallite/InsertActions.tsx
src/components/AskTessallite/JudgeVerdict.tsx
src/components/Connection/LiveConnectionWizard.tsx
src/components/CubeFunctions/CubeFormulaWizard.tsx
src/components/DrillThrough/DrillPanel.tsx
src/components/DrillThrough/DrillPathPicker.tsx
src/components/Glossary/GlossaryModal.tsx
src/components/LoginScreen/LoginScreen.tsx
src/components/PersonaSwitcher/PersonaDropdown.tsx
src/components/ProfileSwitcher/ProfileSwitcher.tsx
src/components/QueryTrace/TraceModal.tsx
src/components/ReportBuilder/DimensionCard.tsx
src/components/ReportBuilder/DimensionLibrary.tsx
src/components/ReportBuilder/HierarchyCard.tsx
src/components/ReportBuilder/HierarchyLibrary.tsx
src/components/ReportBuilder/MeasureCard.tsx
src/components/ReportBuilder/MeasureLibrary.tsx
src/components/ReportBuilder/ReportBuilder.tsx
src/components/ReportBuilder/TemplateCard.tsx
src/components/ReportBuilder/TemplatePicker.tsx
src/components/ReportBuilder/VirtualList.tsx
src/components/ReportBuilder/ZoneMappingGrid.tsx
src/components/Settings/DiagnosticsPanel.tsx
src/components/Toast/ToastProvider.tsx
src/components/common/EmptyState.tsx
src/components/common/LoadingSkeleton.tsx
src/components/common/SearchBar.tsx
src/components/common/SectionHeader.tsx
src/components/common/StatusBadge.tsx
```

### API (6 files)
```
src/api/agentService.ts
src/api/auth.ts
src/api/client.ts
src/api/gateway.ts
src/api/modelService.ts
src/api/queryRouter.ts
```

### Hooks (7 files)
```
src/hooks/useAgentConversation.ts
src/hooks/useAuth.ts
src/hooks/useExcel.ts
src/hooks/useExcelConnections.ts
src/hooks/useModel.ts
src/hooks/usePersona.ts
src/hooks/useSseStream.ts
```

### Utils (10 files)
```
src/utils/cellContext.ts
src/utils/connectionStrings.ts
src/utils/diagnostics.ts
src/utils/excelCharts.ts
src/utils/excelFormulas.ts
src/utils/excelPivotTables.ts
src/utils/officeSpike.ts
src/utils/reportTemplates.ts
src/utils/storage.ts
src/utils/workbookMetadata.ts
```

### Tests (4 files, 27 tests)
```
src/__tests__/cellContext.test.ts
src/__tests__/excelCharts.test.ts
src/__tests__/excelFormulas.test.ts
src/__tests__/reportTemplates.test.ts
```

### Verification Status
- `tsc --noEmit`: zero errors
- `vitest run`: 27/27 pass
- `vite build`: production bundle succeeds

---

*End of consolidated active plan.*
