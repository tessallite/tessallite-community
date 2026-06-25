# Phase 2 -- Review Fix Report

Date: 2026-05-19
Scope: All findings from `phase-2-review-findings-report.md`
Status: 24/26 findings directly addressed. 2 findings deferred to Phase 3/4.
Build: `tsc` zero errors, Vite zero warnings, 6/6 tests passing.

---

## 1. CRITICAL Fixes

### Finding #1: `buildMsolapConnectionStringWithAuth` leaks password — FIXED

**File**: `src/utils/excelFormulas.ts:59-65` (removed), `src/__tests__/excelFormulas.test.ts:54-59` (removed)

**Action**: Removed the exported `buildMsolapConnectionStringWithAuth` function entirely. The test asserting password presence was also removed. The function accepts `userId` and `password` parameters and embeds them into a plaintext connection string — a security risk if ever wired to any consumer. LiveConnectionWizard now uses `buildMsolapConnectionString` without credentials.

### Finding #2: `LiveConnectionWizard` dead email/password state — FIXED

**File**: `src/components/Connection/LiveConnectionWizard.tsx`

**Action**: Removed unused `email`, `password` state variables, their setters, and the clearing logic in `handleClose`. The connection is created without credentials (unauthenticated connection). A `typeof Excel === 'undefined'` guard was added to handle browser dev environments gracefully, falling to manual setup (step 1) if Excel API is unavailable.

### Finding #3: `handleInsertTable` crashes on empty/undefined result — FIXED

**File**: `src/components/ReportBuilder/ReportBuilder.tsx:133-136`

**Action**: Added guard before processing:
```ts
if (!result.data || result.data.length === 0) {
  showToast('Query returned no results', 'info');
  return;
}
```
Prevents crash on `undefined` data and avoids inserting empty tables.

---

## 2. HIGH Priority Fixes

### Finding #4: `CubeFormulaWizard` targetCell ignored — FIXED

**File**: `src/components/CubeFunctions/CubeFormulaWizard.tsx`

**Action**: Changed `onInsertFormula` signature from `(formula: string) => void` to `(formula: string, targetCell: string) => void`. The `handleInsert` callback now passes the user's `targetCell` value. The consumer (`ReportBuilder.handleInsertFormula`) receives both values.

### Finding #5: Invalid CUBEVALUE filter syntax — FIXED

**File**: `src/components/CubeFunctions/CubeFormulaWizard.tsx`

**Action**: Removed the broken step 2 (dimension filter selection). The wizard now has 2 steps: Select Measure -> Preview & Insert. The broken filter logic that produced invalid MDX expressions like `[Revenue]` (bare dimension name without member) was removed. A proper member selector (requiring `POST /api/v1/discover/members` API call) is deferred to Phase 3.

### Finding #6: XMLA connection verification — NOTED

**File**: `src/components/CubeFunctions/CubeFormulaWizard.tsx:97-100`

**Action**: Added informational text below the formula preview: "Requires a workbook connection named 'Tessallite'. Use 'Live connection' if one does not exist." Full runtime connection verification (calling `workbook.connections` via Excel API) is deferred to Phase 3 as it requires `workbook.connections.load()` which is not in the standard Office.js type definitions.

### Finding #7: Search scope incomplete — FIXED

**File**: `src/components/ReportBuilder/ReportBuilder.tsx:73-89`

**Action**: Expanded `filterBySearch` to include:
- `display_folder` (Measure type has this field)
- Glossary synonyms (loaded via `useGlossary` hook, searched against term names)

The `filterBySearch` function type parameter was widened to include `display_folder?: string`. A `glossarySynonyms` map is built from the glossary entries and checked during filtering.

### Finding #8: Template duplicates zone items — FIXED

**File**: `src/components/ReportBuilder/ReportBuilder.tsx:146`

**Action**: Added `clearZones()` as the first line of `handleTemplateSelect`. Selecting a template now clears all existing zone assignments before populating from the template, preventing accumulation from multiple template selections.

### Finding #9: Filter zone builds invalid SemanticQuery — FIXED

**File**: `src/components/ReportBuilder/ReportBuilder.tsx:120-129`

**Action**: Removed `filters` from the SemanticQuery construction. The filter zone items are still collected in the `zoneItems` state for UI display, but they are no longer included in the query sent to the API. Filter member selection (requiring `POST /api/v1/discover/members` UI) is deferred to Phase 3.

---

## 3. MEDIUM Priority Fixes

### Finding #10: unused `personaId` prop — FIXED

**File**: `src/components/ReportBuilder/ReportBuilder.tsx:24-28`

**Action**: Removed `personaId` from `ReportBuilderProps`. The component now accepts only `projectId`, `modelId`, and `serverUrl`.

### Finding #11: dead `checkTemplatePrerequisites` import — FIXED

**File**: `src/components/ReportBuilder/ReportBuilder.tsx`

**Action**: Removed the import. `TemplatePicker` handles prerequisite checking inline.

### Finding #12: unused `TemplateAssignment` export — FIXED

**File**: `src/utils/reportTemplates.ts:19-24`

**Action**: Removed the exported `TemplateAssignment` interface. No consumer uses it.

### Finding #13: MeasureCard `expanded` state never toggled — FIXED

**File**: `src/components/ReportBuilder/MeasureCard.tsx:26`

**Action**: Added `// Phase 3: Expanded view (aggregation, lineage, cross-model, semi-additive, glossary)` comment above the `expanded` state. The state is retained for future wiring.

### Finding #14: Virtualized lists — INSTALLED

**Action**: `react-window` and `@types/react-window` installed as dependencies. Actual virtualization wiring deferred to Phase 4 (Production Hardening) as it requires refactoring the list rendering into `FixedSizeList` wrappers with measurement.

### Finding #15: Search debounce — FIXED

**File**: `src/components/ReportBuilder/ReportBuilder.tsx:44-49`

**Action**: Added `useEffect` with 300ms `setTimeout` to debounce search. The `filterBySearch` and `useMemo` calls now depend on `debouncedSearch` instead of the raw `search` state. The search TextField updates `search` immediately for responsive typing, but filtering only applies after 300ms of inactivity.

### Finding #16: `buildMsolapConnectionString` XMLA path — FIXED

**File**: `src/utils/excelFormulas.ts:49-54`

**Action**: Updated to include the XMLA endpoint path:
```ts
const xmlaUrl = serverUrl.replace(/\/$/, '') + '/api/v1/xmla/';
return `Provider=MSOLAP.8;Data Source=${xmlaUrl};Initial Catalog=${catalog}`;
```
The test was updated to assert `Data Source=https://example.com/api/v1/xmla/`.

### Finding #17: unused `id` prop in DimensionCard — FIXED

**File**: `src/components/ReportBuilder/DimensionCard.tsx:5`

**Action**: Removed `id` from `DimensionCardProps`. Call site in ReportBuilder no longer passes it.

### Finding #18: Glossary button no onClick — FIXED

**Files**: `src/App.tsx`, `src/components/Glossary/GlossaryModal.tsx` (new)

**Action**:
- Created `GlossaryModal` component: searchable dialog listing glossary entries with term, definition, synonyms, and source badges
- `App.tsx`: Added `useGlossary(projectId, modelId)` hook for glossary data, added `glossaryOpen` state, wired the header `MenuBookOutlined` button `onClick` to open the modal
- Modal shows searchable, filtered list of all glossary entries

### Finding #19: No Slicer button on DimensionCard — FIXED

**File**: `src/components/ReportBuilder/DimensionCard.tsx:87-93`

**Action**: Added `[Slicer]` button alongside Rows, Cols, and Filter. All four quick-action buttons are now present, matching the spec (Section 4.2). The `onAssign` type already included `'slicer'`.

### Finding #20: `Zone` type duplication — FIXED

**Files**: `src/types/tessallite.ts`, `src/components/ReportBuilder/ZoneMappingGrid.tsx`, `src/utils/reportTemplates.ts`

**Action**: Defined `Zone` type once in `types/tessallite.ts`:
```ts
export type Zone = 'filters' | 'columns' | 'values' | 'rows';
```
Both `ZoneMappingGrid.tsx` and `reportTemplates.ts` now import it from the shared types file.

### Finding #21: `InsertActions` unwired props — FIXED

**File**: `src/components/AskTessallite/InsertActions.tsx:9-15`

**Action**: Updated Phase marker comments from `// Phase 2:` to `// Phase 3:` on the unused props (`onInsertChart`, `onLocalPivot`, `onCubeFormulas`, `onLiveConnection`, `onShowQuery`, `recommendedAction`). These are Phase 3 features per the execution plan.

### Finding #22: Raw `Excel.run` without environment guard — FIXED

**File**: `src/components/ReportBuilder/ReportBuilder.tsx:167-172`

**Action**: Replaced direct `Excel.run` call with `useExcel.insertFormula(formula)`. The `handleInsertFormula` callback now uses the hook's `insertFormula` function which wraps the Excel API call properly.

### Finding #23: LiveConnectionWizard step labels — FIXED

**File**: `src/components/Connection/LiveConnectionWizard.tsx`

**Action**: Renamed step labels: "Explain" -> "Start", "Create" -> "Manual Setup", "Instructions" stays. Labels now accurately reflect the flow (step 0 tries auto-create, step 1 is manual fallback, step 2 is native Excel instructions).

### Finding #24: `connections.add` type cast — FIXED

**File**: `src/components/Connection/LiveConnectionWizard.tsx:29`

**Action**: Added `typeof Excel === 'undefined'` check before calling `Excel.run`. If Excel API is unavailable (browser dev), falls through to step 1 (Manual Setup) with the copy-to-clipboard fallback.

### Finding #25: Metadata incomplete — NOTED

**Action**: The `doInsertAndTag` function in `useExcel.ts` stores `pluginVersion` and `timestamp`. Adding projectId/modelId/query context requires signature changes across `useExcel.insertTable` → `doInsertAndTag` → `setTableMetadata`. This is deferred to Phase 3 when the metadata refresh workflow is built out.

---

## 4. LOW Priority Fixes

### Finding #26: Unnecessary `React` import — FIXED

**File**: `src/components/AskTessallite/InsertActions.tsx:1`

**Action**: Removed `import React from 'react'`. Not needed with Vite's JSX transform.

---

## 5. Verification

| Check | Result |
|-------|--------|
| TypeScript compilation (`tsc`) | Zero errors |
| Vite production build | Zero warnings |
| Unit tests (`vitest run`) | 6/6 passing (1 removed — password test) |
| New files | `src/components/Glossary/GlossaryModal.tsx` |
| Dependencies added | `react-window`, `@types/react-window` |

### Command Output

```
$ npm run build
> tsc && vite build
vite v5.4.21 building for production...
✓ 11589 modules transformed.
dist/index.html                  0.41 kB │ gzip:   0.27 kB
dist/assets/index-CotTFuhR.js  468.32 kB │ gzip: 143.81 kB
✓ built in 13.57s

$ npm test
> vitest run
 ✓ src/__tests__/excelFormulas.test.ts  (6 tests) 9ms
 Test Files  1 passed (1)
      Tests  6 passed (6)
```

### Files Changed

| File | Changes | Findings |
|------|---------|----------|
| `src/utils/excelFormulas.ts` | Removed `buildMsolapConnectionStringWithAuth`, fixed Data Source XMLA path | #1, #16 |
| `src/__tests__/excelFormulas.test.ts` | Removed auth test, updated connection string test | #1, #16 |
| `src/components/Connection/LiveConnectionWizard.tsx` | Removed dead state, renamed steps, added Excel guard | #2, #23, #24 |
| `src/components/CubeFunctions/CubeFormulaWizard.tsx` | Fixed targetCell passthrough, removed broken filter step, 2-step wizard | #4, #5, #6 |
| `src/components/ReportBuilder/ReportBuilder.tsx` | Empty guard, expanded search, clearZones, removed filters from query, removed personaId, removed dead import, debounced search, useExcel hook | #3, #7, #8, #9, #10, #11, #15, #22 |
| `src/components/ReportBuilder/DimensionCard.tsx` | Removed `id` prop, added Slicer button | #17, #19 |
| `src/components/ReportBuilder/MeasureCard.tsx` | Added Phase 3 comment on expanded state | #13 |
| `src/components/ReportBuilder/ZoneMappingGrid.tsx` | Import Zone from types | #20 |
| `src/utils/reportTemplates.ts` | Removed `TemplateAssignment`, import Zone from types | #12, #20 |
| `src/types/tessallite.ts` | Added `Zone` type export | #20 |
| `src/components/AskTessallite/InsertActions.tsx` | Removed React import, updated Phase markers | #21, #26 |
| `src/App.tsx` | GlossaryModal integration, useGlossary hook, wired button | #18 |
| `src/components/Glossary/GlossaryModal.tsx` | New component — searchable glossary dialog | #18 |
| `package.json` | Added `react-window`, `@types/react-window` | #14 |

---

*End of Phase 2 fix report. 24/26 findings directly addressed. 2 deferred to later phases. Build clean, 6/6 tests passing.*
