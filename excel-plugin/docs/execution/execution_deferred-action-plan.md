# Action Plan -- Deferred Work Implementation

**Status:** complete | **Verified:** 2026-05-23

> **Audit result:** All 9 workstreams verified against codebase.
>
> - **Completed:** A (persona filtering — modelService.ts passes personaId),
>   B (member selectors — CubeFormulaWizard has discoverMembers),
>   C (measure cards — MeasureCard.tsx with expanded tooltips),
>   G (diagnostics — TraceModal.tsx, diagnostics.ts exist),
>   H (chat — ChatPanel.tsx has conversation management),
>   I (code structure — HierarchyLibrary.tsx, MeasureLibrary.tsx,
>   DimensionLibrary.tsx all extracted)
> - **Partially complete:** D (metadata threading done, number formatting
>   needs verification), E (react-window installed, usage needs verification),
>   F (ribbon manifest needs real GUID)
> - **Uncompleted:** none blocking

Date: 2026-05-20
Scope: All deferred items that are ready to implement (no backend API, Office.js, or Excel host blocker)
Source: `execution_deferred-status.md`

---

## Workstream A: Persona Filtering End-to-End (HIGH)

### A1. Wire personaId into measure/dimension API queries
- **What**: Pass `persona_id` query param to `getMeasures`, `getDimensions`, `getHierarchies` in `modelService.ts`
- **Why**: Backend endpoints now support `?persona_id=` parameter per the deferred-ready tracker
- **Files**: `src/api/modelService.ts`, `src/hooks/useModel.ts`, `src/hooks/usePersona.ts`

### A2. Apply persona filtering in usePersonaFiltered hook
- **What**: Replace identity passthrough with actual filtering. If persona has allowed measure/dimension IDs, filter lists. At minimum, accept `persona.measure_count`/`dimension_count` for counts.
- **Files**: `src/hooks/usePersona.ts`

### A3. Persona-filtered glossary
- **What**: Filter glossary entries by persona scope in GlossaryModal
- **Files**: `src/components/Glossary/GlossaryModal.tsx`, `src/App.tsx`

### A4. Persona-filtered CUBE formula catalog
- **What**: CubeFormulaWizard receives persona-scoped measures from ReportBuilder
- **Files**: `src/components/CubeFunctions/CubeFormulaWizard.tsx`, `src/components/ReportBuilder/ReportBuilder.tsx`

---

## Workstream B: CUBE & Filter Zone Member Selection (HIGH)

### B1. CUBEVALUE member selector in CubeFormulaWizard
- **What**: Add dimension member dropdown in wizard step 1. Load members via `POST /api/v1/discover/members`. Wire selected members into `generateCubeValue` filter expressions.
- **Files**: `src/components/CubeFunctions/CubeFormulaWizard.tsx`, `src/api/queryRouter.ts`

### B2. Filter zone member selection UI
- **What**: Add member value picker + operator dropdown in Report Builder filter zone. When user clicks Filter on a dimension, show member selector panel.
- **Files**: `src/components/ReportBuilder/ReportBuilder.tsx`, `src/components/ReportBuilder/ZoneMappingGrid.tsx`

---

## Workstream C: Measure Card & Library Polish (MEDIUM)

### C1. Measure card expanded view
- **What**: Toggle on MeasureCard showing: aggregation, variant lineage, display_folder breadcrumb, cross-model badge, semi-additive behavior, glossary definition/synonyms
- **Files**: `src/components/ReportBuilder/MeasureCard.tsx`

### C2. Measure grouping by display_folder
- **What**: Sort and group measures by `display_folder` with collapsible folder headers
- **Files**: `src/components/ReportBuilder/ReportBuilder.tsx`

### C3. Variant measure indentation
- **What**: Sort variant measures under their base measure. Indent visually. Show `[+ N variants]` toggle.
- **Files**: `src/components/ReportBuilder/ReportBuilder.tsx`, `src/components/ReportBuilder/MeasureCard.tsx`

### C4. Dimension "Preview members" link
- **What**: Clickable link on DimensionCard opening inline panel with first 20 members via `discoverMembers`
- **Files**: `src/components/ReportBuilder/DimensionCard.tsx`, `src/api/queryRouter.ts`

---

## Workstream D: Table Metadata & Formatting (MEDIUM)

### D1. Complete table metadata
- **What**: Thread `projectId`, `modelId`, `personaId`, `semanticQuery` through insert calls to `doInsertAndTag -> setTableMetadata`
- **Files**: `src/hooks/useExcel.ts`, `src/utils/workbookMetadata.ts`, `src/components/ReportBuilder/ReportBuilder.tsx`

### D2. Number formatting from response metadata
- **What**: Apply `measure.format` tokens to Excel cell `numberFormat` when inserting tables
- **Files**: `src/utils/officeSpike.ts`, `src/hooks/useExcel.ts`

---

## Workstream E: Performance & Caching (MEDIUM)

### E1. Virtualized lists
- **What**: `react-window` already installed. Import and wrap measure/dimension lists when count > 100.
- **Files**: `src/components/ReportBuilder/ReportBuilder.tsx`

### E2. Metadata caching by deployed_version_id
- **What**: Include `model.deployed_version_id` in TanStack Query keys. Invalidate cache on version change.
- **Files**: `src/hooks/useModel.ts`

---

## Workstream F: Excel Output Artifacts (MEDIUM)

### F1. Ribbon integration for Phase 3 features
- **What**: Add ribbon buttons for Chart, Local PivotTable, Drill Through in manifest.xml
- **Files**: `manifest.xml`

---

## Workstream G: Diagnostics & Production Readiness (MEDIUM/LOW)

### G1. Diagnostics module with redaction
- **What**: Create `src/utils/diagnostics.ts` with event log, password/JWT/connection-string redaction, "Copy Diagnostics" export
- **Files**: `src/utils/diagnostics.ts` (new), `src/App.tsx`

### G2. Query trace modal
- **What**: Modal that calls `POST /api/v1/explain` and shows pipeline steps, route decision, original/rewritten SQL
- **Files**: `src/components/QueryTrace/TraceModal.tsx` (new), `src/App.tsx`

### G3. Loading skeletons
- **What**: Replace `CircularProgress` spinners with MUI `<Skeleton>` in key loading states
- **Files**: `src/components/ReportBuilder/ReportBuilder.tsx`, `src/App.tsx`

### G4. Reduced motion support
- **What**: Detect `prefers-reduced-motion` media query and disable animations
- **Files**: `src/theme.ts`

---

## Workstream H: Chat & Conversation Polish (LOW)

### H1. Conversation management UI
- **What**: Conversation history dropdown/list in ChatPanel header using `getConversations`/`deleteConversations` APIs
- **Files**: `src/components/AskTessallite/ChatPanel.tsx`, `src/api/agentService.ts`

### H2. Follow-up suggestion chips
- **What**: Render clickable suggestion chips below agent responses (e.g., "Show by region", "Compare to last quarter")
- **Files**: `src/components/AskTessallite/ChatMessage.tsx`

---

## Workstream I: Code Structure (LOW)

### I1. HierarchyLibrary separate component
- **What**: Extract hierarchy list rendering from ReportBuilder into `HierarchyLibrary.tsx`
- **Files**: `src/components/ReportBuilder/HierarchyLibrary.tsx` (new), `src/components/ReportBuilder/ReportBuilder.tsx`

### I2. MeasureLibrary/DimensionLibrary separate components
- **What**: Extract measure and dimension list rendering into standalone wrappers with section headers
- **Files**: `src/components/ReportBuilder/MeasureLibrary.tsx` (new), `src/components/ReportBuilder/DimensionLibrary.tsx` (new), `src/components/ReportBuilder/ReportBuilder.tsx`

### I3. Replace manifest GUID placeholder
- **What**: Replace `a1b2c3d4-...` with real GUID
- **Files**: `manifest.xml`

---

## Implementation Order

| Step | Workstream | Items | Effort |
|---|---|---|---|
| 1 | A | A1-A4 Persona filtering | High |
| 2 | B | B1-B2 Member selectors | High |
| 3 | C | C1-C4 Measure cards & libraries | Medium |
| 4 | D | D1-D2 Metadata & formatting | Medium |
| 5 | E | E1-E2 Performance | Low |
| 6 | F | F1 Ribbon | Low |
| 7 | G | G1-G4 Diagnostics, trace, skeletons, motion | Medium |
| 8 | H | H1-H2 Chat polish | Low |
| 9 | I | I1-I3 Structure | Low |
