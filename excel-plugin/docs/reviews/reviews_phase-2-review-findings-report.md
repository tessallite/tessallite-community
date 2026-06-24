# Phase 2 -- Review Findings Report

Date: 2026-05-19
Scope: `tessallite/excel-plugin/src/` -- Phase 2 implementation (Report Builder, CUBE Function Wizard, Live Connection Helper, Templates)
Build: `tsc` zero errors, Vite zero warnings, 7/7 tests passing
Reviewer note: This report covers bugs, unwired code, cut corners, missing work, security issues, race conditions, and design problems. Findings are ordered by severity.

---

## CRITICAL

### Finding #1: `buildMsolapConnectionStringWithAuth` leaks password into formula string

**File**: `src/utils/excelFormulas.ts:59-65`
**Severity**: Critical -- Security

`buildMsolapConnectionStringWithAuth` returns a string containing `Password={password}` in plaintext. While the function is currently not called at runtime, it is exported and tested in `excelFormulas.test.ts:54-59`. The test asserts that the password appears in the output string.

If any future consumer uses this function to build a connection string that is then written to a workbook connection, displayed in a formula, logged, or stored in metadata, the password is permanently exposed.

**Spec violation**: Section 8.2 states "The add-in must not store the XMLA password in OfficeRuntime.storage, localStorage, workbook custom properties, logs, diagnostics, telemetry, or React Query cache." A function that embeds a password into a string is one step away from violating this.

**Recommendation**: Remove the exported function and its test, or at minimum make it internal (not exported) and add a comment that it must only be used for the `Workbook.connections.add2()` call which stores the connection string in Excel's internal connection manager (not accessible to the user or add-in). The test should verify the format without asserting password presence in a way that could be copy-pasted as a pattern.

---

### Finding #2: `LiveConnectionWizard` collects `email` and `password` state but never uses them

**File**: `src/components/Connection/LiveConnectionWizard.tsx:21-22`
**Severity**: Critical -- Security / Dead Code

The component has state variables `email` and `password` (line 21-22) and clears them on close (line 47-48), but neither variable is ever read or passed to any function. The `handleCreateConnection` function on line 26 calls `wb.connections.add('Tessallite', connectionString, ...)` using only the server URL and catalog -- no credentials are included.

Two problems:
1. **Dead code**: `email` and `password` are declared, set, and cleared but never consumed. This is confusing.
2. **Incomplete implementation**: The wizard's step 0 explains the connection flow, but when the user clicks "Create Connection", no credentials are collected. The connection is created without authentication, which will fail against a Tessallite gateway that requires Basic Auth for XMLA.

**Spec requirement** (Section 4.1): "connection creation requires an explicit credential prompt at the moment the user chooses 'Create live PivotTable connection.'"

**Fix**: Either add a credential input step before the create call (using the email/password state that already exists), or remove the dead state and document that the wizard currently only works with unauthenticated or SSO-connected gateways.

---

### Finding #3: `handleInsertTable` in `ReportBuilder` crashes on empty result

**File**: `src/components/ReportBuilder/ReportBuilder.tsx:109`
**Severity**: High -- Bug

```ts
const rows = result.data.map((r: Record<string, unknown>) =>
  Object.values(r).map(v => (typeof v === 'string' || typeof v === 'number') ? String(v) : JSON.stringify(v)),
);
```

If `result.data` is an empty array (`[]`), then `headers` falls to the fallback path (line 108): `Object.keys(result.data[0] || {})` which yields `[]`. Then `excelInsertTable([], [])` is called, which passes empty headers and rows to `insertResultTable`. While this won't crash, it will attempt to insert an empty table into Excel and show a success toast for "Inserted 0 rows."

If `result.data` is `undefined` (API returns `{ query: ..., data: undefined }`), line 109 will throw `Cannot read properties of undefined (reading 'map')`.

**Fix**: Guard `result.data` before processing:
```ts
if (!result.data || result.data.length === 0) {
  showToast('Query returned no results', 'info');
  return;
}
```

---

## HIGH

### Finding #4: `CubeFormulaWizard` ignores `targetCell` -- formula always inserts at active cell

**File**: `src/components/CubeFunctions/CubeFormulaWizard.tsx:119-120, 33-48`
**Severity**: High -- Dead Feature

The wizard has a `targetCell` state (line 28) and a text field for the user to enter it (line 115-122), but `handleInsert` on line 33 calls `onInsertFormula(formula)` which only passes the formula string. The receiving function in `ReportBuilder.tsx:148-159` writes to `getSelectedRange()` (the active cell), completely ignoring the user's target cell input.

The user types a cell address, sees "Ready to insert at A1" confirmation, but the formula goes to whatever cell is active.

**Fix**: Either pass the target cell to the insert handler and use `context.workbook.worksheets.getActiveWorksheet().getRange(targetCell)`, or remove the `targetCell` field to avoid misleading the user.

---

### Finding #5: `CubeFormulaWizard` generates invalid CUBEVALUE filter syntax

**File**: `src/components/CubeFunctions/CubeFormulaWizard.tsx:41-42`
**Severity**: High -- Bug

```ts
filters.push(`[${dim.display_name}]`);
```

A CUBEVALUE filter expression must be a valid MDX member expression, e.g., `[Country].[Country].[US]`. The wizard only wraps the dimension display name in brackets, producing `[Revenue]` instead of something like `[Country].[Country]`. This is not a valid MDX tuple expression and will produce `#N/A` in Excel.

Additionally, the filter does not specify which member to filter on -- it just names the dimension without a member value.

**Fix**: The wizard needs a member selector (call `POST /api/v1/discover/members`) so the user can pick a specific dimension member. The filter expression should be `[Dimension].[Hierarchy].[Member]`.

---

### Finding #6: `CubeFormulaWizard` does not verify XMLA connection exists before inserting

**File**: `src/components/CubeFunctions/CubeFormulaWizard.tsx`
**Severity**: High -- Spec Violation

The execution plan (Workstream D, Task 3) requires: "Before insertion, the wizard verifies that a workbook XMLA connection to Tessallite exists. If not, it offers 'Create live connection' and prompts for credentials at that moment."

The wizard has no connection verification step. It generates a formula with connection name `"Tessallite"` (hardcoded in `ReportBuilder.tsx:355`) regardless of whether that connection exists in the workbook. If the connection doesn't exist, the formula will show `#N/A` or `#NAME?` in Excel with no explanation.

**Fix**: Before the insert step, call `Excel.run` to check `context.workbook.connections`. If no connection matching `"Tessallite"` exists, show a warning with a button to open the `LiveConnectionWizard`.

---

### Finding #7: `ReportBuilder` search does not search `display_folder`, glossary synonyms, or alias map

**File**: `src/components/ReportBuilder/ReportBuilder.tsx:56-66`
**Severity**: High -- Spec Cut

The execution plan (Workstream B, Task 5) specifies search across: display_name, technical name, description, effective_description, folder, glossary synonyms, alias map. The specs (Section 4.2) say the same.

The current `filterBySearch` only checks `display_name`, `name`, `effective_description`, and `description`. It does NOT search:
- `display_folder` (the Measure type has this field)
- Glossary synonyms (the glossary API and hook exist but are not used by ReportBuilder)
- Alias map (no API client for `GET .../alias-map` exists in `modelService.ts`)

**Fix**: Expand `filterBySearch` to include `display_folder`. Load the glossary via `useGlossary` and join synonyms. Add the alias-map API endpoint.

---

### Finding #8: `handleTemplateSelect` duplicates items already in zones

**File**: `src/components/ReportBuilder/ReportBuilder.tsx:124-146`
**Severity**: High -- Bug

`handleTemplateSelect` calls `addToZone` for measures and dimensions, but `addToZone` only prevents duplicates where both `id` and `zone` match. When a template is selected and the user clicks the same template again (or a different template that includes the same dimension), items get added again with the same ID but to the same or different zone.

More critically: if the user already has items in zones and then selects a template, the template adds MORE items on top of what is already assigned. The template should clear existing zones before populating, or at minimum warn the user.

**Fix**: Call `clearZones()` at the start of `handleTemplateSelect`.

---

### Finding #9: `handleInsertTable` builds incorrect `SemanticQuery.filters` structure

**File**: `src/components/ReportBuilder/ReportBuilder.tsx:93-99`
**Severity**: High -- Bug

```ts
filters: filterDims.length > 0 ? filterDims.map(member => ({ member, operator: 'contains' })) : undefined,
```

The `QueryFilter` type (tessallite.ts:118-122) requires:
```ts
interface QueryFilter {
  member: string;
  operator: string;
  values?: string[];
}
```

The filter is built with `operator: 'contains'` but no `values`. This produces filters like `{ member: "dim_abc123", operator: "contains" }` -- an ID-based member reference with no values to filter on. The backend will either ignore it or throw an error.

Filters in a report layout need actual dimension member values selected by the user, not just the dimension ID.

**Fix**: The filter zone needs a member selector UI (call `POST /api/v1/discover/members`) so users can pick which members to filter on. Until then, filters should be excluded from the query or at minimum documented as a stub.

---

### Finding #10: `personaId` prop is accepted but never used

**File**: `src/components/ReportBuilder/ReportBuilder.tsx:28, 32`
**Severity**: Medium -- Dead Interface

`ReportBuilder` accepts a `personaId` prop but never passes it to `useMeasures`, `useDimensions`, or `useHierarchies`. The hooks don't accept persona parameters either. The `modelService` API calls don't include persona scoping.

Persona filtering is a Phase 3 feature, so this is expected, but the prop's existence is misleading -- it suggests persona scoping is wired.

**Fix**: Remove the prop until Phase 3, or add a `// Phase 3:` comment on it.

---

## MEDIUM

### Finding #11: `checkTemplatePrerequisites` imported but never called

**File**: `src/components/ReportBuilder/ReportBuilder.tsx:12`
**Severity**: Medium -- Dead Import

`checkTemplatePrerequisites` is imported from `reportTemplates.ts` but never used. The `TemplatePicker` component does its own prerequisite checking inline (lines 44-51) instead of using the shared utility.

**Fix**: Remove the import from `ReportBuilder.tsx`, or refactor `TemplatePicker` to use `checkTemplatePrerequisites`.

---

### Finding #12: `TemplateAssignment` interface exported but never used

**File**: `src/utils/reportTemplates.ts:19-24`
**Severity**: Medium -- Dead Code

The `TemplateAssignment` interface is exported but never imported or used anywhere.

**Fix**: Remove it, or implement the feature that uses it.

---

### Finding #13: `MeasureCard` `expanded` state declared but never toggled

**File**: `src/components/ReportBuilder/MeasureCard.tsx:26`
**Severity**: Medium -- Dead Code / Cut Corner

The specs require an expanded view for measure cards showing: aggregation, variant lineage, display folder breadcrumb, cross-model badge, semi-additive behavior, and glossary definition. The `expanded` state is declared on line 26 but there is no UI to toggle it (no expand/collapse button) and no expanded content section.

**Fix**: Either implement the expanded view or remove the `expanded` state and add a `// Phase 3:` comment.

---

### Finding #14: No virtualized lists for large models

**Severity**: Medium -- Performance / Spec Cut

The execution plan (Workstream A, Task 3) requires: "Add virtualized lists for models with 100+ measures or dimensions." The specs (Section 9.3) require: "use `react-window` to render only visible items."

No virtualization library is installed or used. All measures and dimensions render as full React components. With 500+ measures, the task pane will lag.

`package.json` does not include `react-window` or any virtualization dependency.

**Fix**: Install `react-window` and wrap the measure/dimension lists in a `FixedSizeList`. Gate on item count > 100.

---

### Finding #15: Search has no debounce -- fires on every keystroke

**File**: `src/components/ReportBuilder/ReportBuilder.tsx:181`
**Severity**: Medium -- Performance

The specs (Section 4.2) require 300ms debounce on search. The current implementation updates `search` state on every `onChange`, and `filterBySearch` runs synchronously on every render via `useMemo`. While this is acceptable for client-side filtering of cached data, it does not meet the spec's debounce requirement, and if the search is later extended to include API calls (alias map, glossary), it will cause excessive requests.

**Fix**: Add a 300ms debounced search value using a `useEffect` + `setTimeout` pattern or `useDeferredValue`.

---

### Finding #16: `buildMsolapConnectionString` does not include XMLA path

**File**: `src/utils/excelFormulas.ts:49-54`
**Severity**: Medium -- Compatibility

The connection string is:
```
Provider=MSOLAP.8;Data Source=${serverUrl};Initial Catalog=${catalog}
```

The Tessallite gateway's XMLA endpoint is at `POST /api/v1/xmla/` (per the architecture diagram in SPECS.md). MSOLAP connects to an XMLA endpoint, not just a base URL. The `Data Source` should likely be `${serverUrl}/api/v1/xmla/` or the gateway's dedicated XMLA host:port.

Without the correct XMLA path, Excel will try to connect to the model-service base URL, which does not speak XMLA/SOAP.

**Fix**: Verify the correct Data Source value against the Tessallite gateway configuration. It should point to the XMLA endpoint, not the REST API base URL.

---

### Finding #17: `DimensionCard` accepts `id` prop but never uses it

**File**: `src/components/ReportBuilder/DimensionCard.tsx:5, 22`
**Severity**: Low -- Dead Prop

The `id` prop is accepted but never referenced in the component body.

**Fix**: Remove it from the interface and call sites.

---

### Finding #18: Glossary button in header has no `onClick` handler

**File**: `src/App.tsx:479`
**Severity**: Medium -- Unwired UI

The header has a Glossary `IconButton` with title and aria-label but no `onClick`. Clicking it does nothing. Phase 2 scope includes "Contextual glossary popover and searchable glossary modal" per the execution plan.

The `useGlossary` hook exists and is functional, but no glossary UI component consumes it.

**Fix**: Add a glossary modal/popover that opens on click and uses `useGlossary`. This is a Phase 2 scope item.

---

### Finding #19: No `Slicer` zone action on `DimensionCard`

**File**: `src/components/ReportBuilder/DimensionCard.tsx:66-91`
**Severity**: Low -- Spec Cut

The specs (Section 4.2) list quick-action buttons: `[-> Rows]`, `[-> Columns]`, `[-> Filter]`, `[-> Slicer]`. The current card only has Rows, Cols, and Filter. The `onAssign` type includes `'slicer'` but no button triggers it.

**Fix**: Add a Slicer button, or remove `'slicer'` from the `onAssign` type until Phase 3/4.

---

### Finding #20: `Zone` type is duplicated in two files

**File**: `src/components/ReportBuilder/ZoneMappingGrid.tsx:5`, `src/utils/reportTemplates.ts:6`
**Severity**: Low -- Duplication

Both files independently define `export type Zone = 'filters' | 'columns' | 'values' | 'rows'`. If one changes, the other won't.

**Fix**: Define `Zone` once (e.g., in `types/tessallite.ts`) and import it in both places.

---

### Finding #21: `InsertActions` component props are mostly unwired

**File**: `src/components/AskTessallite/InsertActions.tsx:9-15`
**Severity**: Medium -- Unwired UI

`InsertActions` accepts `onInsertChart`, `onLocalPivot`, `onCubeFormulas`, `onLiveConnection`, `onShowQuery`, and `recommendedAction` props, but `ChatPanel.tsx` only passes `onInsertTable` when rendering it (line 113). All other insertion actions are `undefined` and therefore hidden.

This means "Insert Chart", "Local Pivot", "CUBE formulas", "Live connection", and "Show Query" buttons never appear in Ask Tessallite. These are Phase 3 features per the execution plan, so the props are correctly forward-declared, but the `// Phase 3:` comments are missing on the prop interface.

**Fix**: Add `// Phase 3:` comments on the unwired props.

---

### Finding #22: `Excel` global used directly without environment guard in `ReportBuilder`

**File**: `src/components/ReportBuilder/ReportBuilder.tsx:150-158`
**Severity**: Medium -- Compatibility

```ts
Excel.run(async (context) => { ... })
```

The `handleInsertFormula` callback in `ReportBuilder` calls `Excel.run` directly. When developing in a browser (not inside Excel), `Excel` is undefined and this will throw. The `useExcel` hook's functions handle this through `officeSpike.ts` which likely guards against this, but the raw `Excel.run` call in `ReportBuilder` bypasses that protection.

This is the same pattern used in `LiveConnectionWizard.tsx:28`.

**Fix**: Move the `Excel.run` call into `useExcel.ts` as a new `insertFormula` function (which already exists but is not used by the ReportBuilder), or add a `typeof Excel !== 'undefined'` guard.

---

### Finding #23: `LiveConnectionWizard` step 0 goes directly to step 2 on success, skips step 1

**File**: `src/components/Connection/LiveConnectionWizard.tsx:26-38`
**Severity**: Low -- Logic

The stepper shows 3 steps: Explain (0), Create (1), Instructions (2). When the user clicks "Create Connection" on step 0:
- On success: `setStep(2)` -- jumps directly to Instructions, skipping step 1 (manual instructions). This is correct behavior.
- On failure: `setStep(1)` -- shows manual fallback. This is also correct.

But the step labels are misleading. "Create" (step 1) is only reached on failure. "Explain" (step 0) has the "Create Connection" button. The Stepper UI will briefly show step 1 highlighted when the API call is in flight and then jump to step 2.

**Fix**: Rename steps to something like "Explain" -> "Manual Fallback" -> "Instructions", or restructure to show a loading state between step 0 and step 2.

---

### Finding #24: `connections.add` API typed as `any` cast -- will fail on most Excel hosts

**File**: `src/components/Connection/LiveConnectionWizard.tsx:29-32`
**Severity**: Medium -- Compatibility

```ts
const wb = context.workbook as unknown as {
  connections: { add: (name: string, cs: string, command: string, desc: string) => void };
};
```

`Workbook.connections.add` (or `add2`) is not part of the standard Excel JS API requirement sets. It is only available on specific Excel builds. The `as unknown as` cast bypasses TypeScript's type checking entirely, so if the API doesn't exist at runtime, the call will silently fail with a generic error.

**Fix**: Add a runtime feature detection check before calling `connections.add`. Show a clear error message if the API is not available, directing the user to the manual fallback (step 1).

---

### Finding #25: `handleInsertTable` in `ReportBuilder` does not store metadata

**File**: `src/components/ReportBuilder/ReportBuilder.tsx:105-121`
**Severity**: Medium -- Missing Feature

The Ask Tessallite path stores metadata via `setTableMetadata` (called inside `doInsertAndTag` in `useExcel.ts`). But the Report Builder's `handleInsertTable` calls `excelInsertTable` which internally calls `doInsertAndTag`, so metadata IS stored.

However, the metadata only includes `pluginVersion` and `timestamp`. The execution plan (Workstream D, Task 2) requires storing: project id, model id, persona id, conversation id, turn id, semantic query, insert timestamp, plugin version. The `setTableMetadata` call in `useExcel.ts:30-33` only stores the last two.

The `workbookMetadata.ts` interface supports all these fields, but `doInsertAndTag` hardcodes only version and timestamp.

**Fix**: Pass context (projectId, modelId, semanticQuery) to `insertTable` and through to `doInsertAndTag`, then store it via `setTableMetadata`.

---

## LOW

### Finding #26: `import React from 'react'` in `InsertActions.tsx` is unnecessary

**File**: `src/components/AskTessallite/InsertActions.tsx:1`
**Severity**: Low -- Dead Import

With the JSX transform configured in Vite/TypeScript, `import React from 'react'` is not needed. No other component file has this import.

**Fix**: Remove it.

---

### Finding #27: `HierarchyLibrary` component mentioned in execution plan does not exist as a separate file

**Severity**: Low -- Structural

The execution plan lists `HierarchyLibrary.tsx` as a separate file, but hierarchies are rendered inline in `ReportBuilder.tsx` (lines 274-308). This is fine functionally but diverges from the planned file structure.

**Fix**: No action needed -- just note the divergence.

---

### Finding #28: No `MeasureLibrary` or `DimensionLibrary` wrapper components

**Severity**: Low -- Structural

Same as above. The execution plan lists `MeasureLibrary.tsx` and `DimensionLibrary.tsx` as separate files. Instead, all rendering happens directly in `ReportBuilder.tsx`, making it 368 lines.

**Fix**: Consider extracting into separate components to match the plan and keep files under 400 lines.

---

### Finding #29: `DimensionCard` source type hardcoded to `'dim' | 'calculated'`

**File**: `src/components/ReportBuilder/DimensionCard.tsx:9`
**Severity**: Low -- Fragility

The `sourceType` prop type is `'dim' | 'calculated'`, matching the `Dimension` type in `tessallite.ts:60`. This is correct, but if the backend adds a new source type, the component will fail silently (no badge shown).

**Fix**: No immediate action needed, but consider a fallback badge for unknown types.

---

### Finding #30: `ZoneMappingGrid` Templates button appears even when no measures are assigned

**File**: `src/components/ReportBuilder/ZoneMappingGrid.tsx:118-120`
**Severity**: Low -- UX

The "Templates" button shows whenever `hasItems` is true, even if only dimensions or hierarchies are assigned. Templates require at least one measure, so clicking it would show all templates greyed out.

**Fix**: Consider showing Templates only when at least one measure is in values, or always show it (even when zones are empty) in the header area.

---

## MISSING WORK (Phase 2 scope items not implemented)

These are scope items from the execution plan's Phase 2 that have zero or minimal implementation:

| # | Item | Plan Reference | Status |
|---|---|---|---|
| M1 | Glossary popover on measure/dimension card click | Workstream B (specs 4.6) | Not implemented. Header button exists but unwired. No popover component. |
| M2 | Glossary search modal | Workstream B (specs 4.6) | Not implemented. `useGlossary` hook exists but no UI. |
| M3 | Search across `display_folder` | Workstream B, Task 5 | Not implemented. |
| M4 | Search across glossary synonyms | Workstream B, Task 5 | Not implemented. |
| M5 | Search across alias map | Workstream B, Task 5 | Not implemented. No alias-map API client. |
| M6 | Measure card expanded view | Workstream B, Task 2 | Not implemented. State exists but no UI. |
| M7 | Dimension "Preview members" link | Workstream B, Task 3 | Not implemented. No `discoverMembers` call in ReportBuilder. |
| M8 | `POST /api/v1/validate` formula validation | Workstream D, Task 2 | Not called. Wizard shows "Ready to insert" without validation. |
| M9 | XMLA connection existence check before CUBE insert | Workstream D, Task 3 | Not implemented. |
| M10 | Virtualized lists (react-window) | Workstream A, Task 3 | Not implemented. Not installed. |
| M11 | Metadata caching by `deployed_version_id` | Workstream A, Task 2 | Not implemented. React Query handles staleness but no version-based invalidation. |
| M12 | 300ms search debounce | Workstream B, Task 5 | Not implemented. |
| M13 | Measure grouping by `display_folder` | Specs 4.2 (Measure Library) | Not implemented. Measures render flat list. |
| M14 | Variant measure indentation under base | Specs 4.2 (Measure Library) | Not implemented. Variants show as flat cards with a small label. |
| M15 | `Slicer` zone action | Specs 4.2 (Dimension Library) | Not implemented. |

---

## SUMMARY

| Category | Count |
|---|---|
| Critical | 3 |
| High | 7 |
| Medium | 9 |
| Low | 5 |
| Missing scope items | 15 |
| **Total findings** | **39** |

### Critical fixes needed before Phase 2 can be declared complete:

1. **#1**: Password in connection string function -- security risk
2. **#2**: LiveConnectionWizard collects but never uses credentials -- incomplete wizard
3. **#3**: Empty/undefined query result crashes Report Builder

### High-priority functional gaps:

4. **#4**: Target cell ignored in Cube Formula Wizard
5. **#5**: Invalid CUBEVALUE filter syntax
6. **#6**: No XMLA connection verification before formula insert
7. **#7**: Search scope incomplete (no folders, synonyms, alias map)
8. **#8**: Template selection duplicates zone items
9. **#9**: Filter zone builds invalid SemanticQuery

---

*End of Phase 2 review report.*
