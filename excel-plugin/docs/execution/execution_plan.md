# Tessallite Excel Plugin -- Execution Plan

Implementation plan for the Excel add-in described in `docs/architecture/architecture_specs.md` and `docs/architecture/architecture_frontend-design.md`.

**Status:** complete | **Verified:** 2026-05-23

> **Audit result:** All core phases delivered. Foundation, Ask MVP, Report
> Builder, CUBE formulas, charts, personas, drill-through, query trace,
> diagnostics, and sideload hardening all confirmed in the codebase. The
> excel-plugin directory contains a full React 18 + Vite + MUI app with
> 20+ components across LoginScreen, ReportBuilder, AskTessallite,
> CubeFunctions, DrillThrough, QueryTrace, Settings, and Connection.
>
> - **Completed:** All 4 delivery phases
> - **Uncompleted:** none (remaining polish items moved to deferred-action-plan)

Last updated: 2026-05-23

---

## 1. Goal

Build a Microsoft Excel add-in that lets business users move from business intent to governed Excel output quickly.

The plugin is not another Tessallite frontend. It is an Excel-side insight workbench:

1. Ask a governed business question.
2. Review a concise answer and result preview.
3. Insert a native Excel artifact.
4. Use glossary, persona scope, CUBE formulas, and drill-through when needed.

The default user journey is **Ask Tessallite -> Insert Table/Chart/Local Pivot/CUBE formulas**. Report Builder is the structured fallback for repeatable reports and power users.

---

## 2. Non-Negotiable Product Constraints

### 2.1 Excel Output Contract

Every insert action must create a native Excel artifact. The task pane is a control surface, not the destination for analysis.

| Output | Allowed | Notes |
|---|---|---|
| Formatted Excel Table | Yes | MVP default. Query results from Tessallite REST APIs. |
| Native Excel Chart | Yes | Backed by an inserted worksheet table/range. |
| Local Excel PivotTable | Yes | Created from inserted worksheet table/range only, where Office.js supports it. |
| CUBE formula range | Yes | Requires an existing XMLA workbook connection. |
| Live XMLA PivotTable | Assisted only | Add-in can create/verify connection and show instructions. Excel native UI completes PivotTable creation. |

Do not implement a button that claims to directly create a live external XMLA PivotTable. Office.js cannot do this.

### 2.2 Security Constraints

- Do not store passwords.
- Do not log passwords.
- Do not put passwords in workbook custom properties, telemetry, diagnostics, React Query cache, localStorage, or OfficeRuntime.storage.
- Login password is used only for `POST /api/v1/auth/login`, then cleared from component state.
- XMLA/MSOLAP credentials are requested only when the user explicitly creates a live connection or inserts CUBE formulas that require a connection.
- Prefer future SSO/device-code auth when available.

### 2.3 Scope Boundaries

Do not build:

- Model health dashboard
- Schema drift screen
- Aggregate coverage dashboard
- Pocket table status UI
- Data quality/admin panels
- Model export/snapshot/version diff
- Permanent query trace workspace
- Full model-builder or admin workflows

Query trace may exist only as an optional support/debug modal under settings.

---

## 3. Delivery Strategy

Build in vertical slices that produce usable Excel outcomes early.

The recommended sequence is:

1. Foundation and Ask MVP
2. Report Builder and CUBE formulas
3. Charts, local PivotTables, personas, and drill-through
4. Production hardening and enterprise distribution

Each phase must end with a workbook-level demo and acceptance checklist. Avoid long platform-only phases that do not insert something useful into Excel.

---

## 4. Phase 0: Project Setup and Technical Spike

**Duration:** 3-5 working days

### Goals

Confirm Office.js capabilities in the target Excel hosts before committing UI behavior.

### Tasks

1. Scaffold `tessallite/excel-plugin` app:
   - `manifest.xml`
   - `package.json`
   - `vite.config.ts`
   - `tsconfig.json`
   - `src/main.tsx`
   - `src/App.tsx`
   - basic task pane route

2. Verify Office host support:
   - Excel Windows Desktop
   - Excel Mac Desktop
   - Excel on the Web

3. Spike these Office.js operations:
   - Write values to active cell range
   - Create formatted Excel Table from result rows
   - Create native chart from table/range
   - Create local PivotTable from worksheet range/table where supported
   - Insert formulas into active cell/range
   - Read active cell formula/value/address
   - Detect workbook connections if available
   - Attempt `Workbook.connections.add2()` only where supported

4. Produce a short compatibility matrix:

| Feature | Windows | Mac | Web | Notes |
|---|---|---|---|---|
| Insert Table |  |  |  |  |
| Insert Chart |  |  |  |  |
| Local PivotTable |  |  |  |  |
| CUBE formulas |  |  |  |  |
| Create XMLA connection |  |  |  |  |
| Read selected CUBE formula |  |  |  |  |

### Acceptance Criteria

- Add-in loads in at least one desktop Excel host and one browser/dev host.
- A test button inserts a table into the active worksheet.
- A test button inserts a chart from that table.
- The team has written evidence of which PivotTable/connection operations are actually supported by the target hosts.

---

## 5. Phase 1: Foundation and Ask MVP

**Duration:** 3-4 weeks

### Goal

A user can sign in, ask Tessallite a business question, preview the governed result, and insert the answer into Excel as a formatted table.

### Workstream A: Authentication and Profiles

Files/modules:

- `src/api/auth.ts`
- `src/hooks/useAuth.ts`
- `src/components/LoginScreen.tsx`
- `src/components/ProfileSwitcher.tsx`
- `src/utils/storage.ts`

Tasks:

1. Implement login form:
   - Server URL
   - Tenant
   - Email
   - Password
   - Remember this profile

2. Implement profile persistence:
   - Store only `name`, `server_url`, `tenant_id`, `email`
   - Never store password

3. Implement JWT storage:
   - Use `OfficeRuntime.storage`
   - Redirect to login on 401
   - Clear password from component state after login attempt

4. Implement connection health check:
   - Poll `/health` every 30 seconds
   - Show connected/disconnected/reconnecting status in footer

Acceptance criteria:

- User can sign in and reload the task pane without re-entering profile metadata.
- Saved profile pre-fills server, tenant, and email but not password.
- Sign out removes JWT and returns to login.
- No password appears in storage, logs, diagnostics, or browser devtools application storage.

### Workstream B: API Client Foundation

Files/modules:

- `src/api/client.ts`
- `src/api/modelService.ts`
- `src/api/queryRouter.ts`
- `src/api/agentService.ts`
- `src/api/gateway.ts`
- `src/types/tessallite.ts`

Tasks:

1. Build shared fetch client:
   - Base URL per profile
   - Bearer token injection
   - JSON error normalization
   - Retry policy for safe GETs
   - 401 handling

2. Add typed clients for:
   - `GET /api/v1/projects`
   - `GET /api/v1/projects/{id}/models`
   - `GET /api/v1/projects/{id}/models/{mid}`
   - `GET /api/v1/projects/{id}/agent/config`
   - `POST /api/v1/projects/{id}/agent/conversations`
   - `POST /api/v1/projects/{id}/agent/conversations/{cid}/messages/stream`
   - `POST /api/v1/projects/{id}/agent/conversations/{cid}/messages/feedback`

3. Add TanStack Query:
   - Query keys include server URL, tenant, project id, model id, persona id
   - Clear queries on sign out/profile switch

Acceptance criteria:

- API errors render a user-readable message and preserve diagnostic detail for support.
- Profile switch invalidates old tenant/project/model data.
- No stale data from another tenant/profile appears after switching profiles.

### Workstream C: Ask Tessallite

Files/modules:

- `src/components/AskTessallite/ChatPanel.tsx`
- `src/components/AskTessallite/ChatMessage.tsx`
- `src/components/AskTessallite/InsertActions.tsx`
- `src/components/AskTessallite/JudgeVerdict.tsx`
- `src/hooks/useAgentConversation.ts`
- `src/hooks/useSseStream.ts`

Tasks:

1. Make Ask Tessallite the default mode after first login.

2. Implement conversation header:
   - Active model name
   - Provider/model badge
   - Agent persona dropdown
   - New conversation action
   - Conversation history action, if API is available

3. Implement chat flow:
   - Empty state with business prompts
   - User message bubble
   - Streaming agent response
   - Result preview table
   - Judge verdict when present
   - Feedback buttons
   - Follow-up suggestion chips

4. Implement agent-not-configured state:
   - Disabled input
   - Clear instruction to contact administrator

Acceptance criteria:

- User can ask a question and see streamed response text.
- Result rows appear as a compact preview table.
- Feedback calls the feedback endpoint.
- Agent-not-configured projects fail gracefully.

### Workstream D: Insert as Table

Files/modules:

- `src/hooks/useExcel.ts`
- `src/utils/excelTables.ts`
- `src/utils/workbookMetadata.ts`
- `src/components/AskTessallite/InsertActions.tsx`

Tasks:

1. Implement `insertResultTable()`:
   - Create new sheet by default, with safe unique name
   - Support active-cell insertion with overwrite confirmation
   - Write headers and rows
   - Create formatted Excel Table
   - Apply number formatting where response metadata provides format tokens

2. Store metadata for inserted result:
   - Project id
   - Model id
   - Persona id
   - Conversation id
   - Turn id
   - Semantic query
   - Insert timestamp
   - Plugin version

3. Add large result guard:
   - Preview capped in task pane
   - Confirm before inserting more than 10,000 rows

Acceptance criteria:

- "Insert Table" creates a valid formatted Excel Table.
- Sheet/range collision prompts before overwrite.
- Inserted table includes reasonable column widths and formats.
- Metadata is attached for future refresh/debug.

### Phase 1 Demo

Demo script:

1. Open Excel.
2. Open Tessallite add-in.
3. Sign in.
4. Ask: "What was revenue by country last quarter?"
5. Watch streamed response.
6. Insert as Table.
7. Show resulting Excel Table and stored connection/profile behavior after reload.

---

## 6. Phase 2: Report Builder and CUBE Formulas

**Duration:** 2-3 weeks

### Goal

Power users can build structured outputs from semantic measures/dimensions and create live CUBE formula ranges when an XMLA connection exists.

### Workstream A: Metadata Loading

Files/modules:

- `src/api/modelService.ts`
- `src/hooks/useModel.ts`
- `src/types/tessallite.ts`

Tasks:

1. Load model metadata:
   - Measures
   - Measure details
   - Available variants
   - Dimensions
   - Hierarchies
   - Personas
   - Glossary
   - Alias map
   - Data tags

2. Cache by deployed version:
   - `deployed_version_id` is cache key
   - Invalidate on model version change
   - Glossary gets 5-minute TTL

3. Add virtualized lists for models with 100+ measures or dimensions.

Acceptance criteria:

- Report Builder loads without blocking Ask.
- Switching model/persona updates available metadata.
- Large models remain responsive.

### Workstream B: Compact Report Builder

Files/modules:

- `src/components/ReportBuilder/ReportBuilder.tsx`
- `src/components/ReportBuilder/ZoneMappingGrid.tsx`
- `src/components/ReportBuilder/MeasureLibrary.tsx`
- `src/components/ReportBuilder/DimensionLibrary.tsx`
- `src/components/ReportBuilder/HierarchyLibrary.tsx`
- `src/components/ReportBuilder/MeasureCard.tsx`
- `src/components/ReportBuilder/DimensionCard.tsx`
- `src/components/ReportBuilder/HierarchyCard.tsx`

Tasks:

1. Implement report layout grid:
   - Filters
   - Columns
   - Values
   - Rows
   - Removable chips
   - Clear action

2. Implement compact measure cards:
   - Default view: display name, one-line description, format, type, quick action
   - Expanded view: aggregation, folder, lineage, cross-model, semi-additive, glossary synonyms

3. Implement dimension cards:
   - Display name
   - One-line description
   - Type badges
   - Rows/Columns/Filter/Slicer intent actions
   - Preview members

4. Implement hierarchy cards:
   - Collapsed level chain
   - Expand levels
   - Rows action by hierarchy level

5. Implement search:
   - Display name
   - Technical name
   - Description/effective description
   - Folder
   - Glossary synonyms
   - Alias map
   - 300ms debounce

Acceptance criteria:

- User can assign fields to report layout without drag-and-drop.
- Compact cards fit comfortably in a 360px pane.
- Search filters all libraries consistently.
- No admin/modeler-only metadata dominates the default view.

### Workstream C: Report Templates

Files/modules:

- `src/components/ReportBuilder/TemplatePicker.tsx`
- `src/components/ReportBuilder/TemplateCard.tsx`
- `src/utils/reportTemplates.ts`

Templates:

- Time Series
- Top N Breakdown
- Period Comparison
- Geographic Breakdown
- Variance Analysis
- KPI Snapshot

Tasks:

1. Define template prerequisites:
   - Required measure count
   - Required time dimension/hierarchy
   - Required categorical dimension
   - Optional comparison measure

2. Implement template picker.

3. On selection:
   - Populate report layout
   - Show missing-field warnings
   - Let user adjust before insertion

Acceptance criteria:

- Templates reduce setup work for common finance/operations reports.
- If a model lacks required fields, the template explains what is missing.

### Workstream D: CUBE Function Wizard

Files/modules:

- `src/components/CubeFunctions/CubeFormulaWizard.tsx`
- `src/utils/excelFormulas.ts`
- `src/utils/connectionStrings.ts`
- `src/api/queryRouter.ts`

Tasks:

1. Generate:
   - `CUBEMEMBER`
   - `CUBEVALUE`
   - basic multi-measure formula ranges

2. Validate formula:
   - Call `POST /api/v1/validate`
   - Show pass/fail state

3. Verify XMLA connection:
   - Detect existing workbook connection when possible
   - If missing, show "Create live connection"
   - Prompt for credentials only in this flow
   - Never persist XMLA password

4. Insert formula:
   - Active cell default
   - Replace confirmation if target cell is non-empty

Acceptance criteria:

- User can generate and insert a valid `CUBEVALUE`.
- Missing connection produces a clear, recoverable flow.
- Password is not stored.

### Workstream E: Live Connection Helper

Files/modules:

- `src/components/Connection/LiveConnectionWizard.tsx`
- `src/utils/connectionStrings.ts`
- `src/hooks/useExcelConnections.ts`

Tasks:

1. Build a small wizard:
   - Explain live XMLA connection vs local results
   - Prompt for XMLA credentials
   - Attempt connection creation where supported
   - Show native Excel instructions:
     - Insert > PivotTable
     - Use an external data source
     - Choose the Tessallite connection

2. Add clear fallback if Office.js connection creation is unavailable:
   - Copy connection string button
   - Manual steps

Acceptance criteria:

- UI never claims to create the live PivotTable itself.
- User can create or manually configure the connection with clear steps.

### Phase 2 Demo

Demo script:

1. Open Report Builder.
2. Search for Revenue.
3. Add Revenue to Values.
4. Add Country to Rows.
5. Insert Table.
6. Generate CUBE formula for Revenue filtered by Country.
7. Show live connection helper without storing credentials.

---

## 7. Phase 3: Charts, Local PivotTables, Personas, Drill-Through

**Duration:** 2-3 weeks

### Goal

Users can turn answers into richer Excel artifacts, switch business views, and inspect governed detail rows.

### Workstream A: Insert Chart

Files/modules:

- `src/utils/excelCharts.ts`
- `src/components/AskTessallite/InsertActions.tsx`
- `src/components/ReportBuilder/ReportBuilder.tsx`

Tasks:

1. Detect chart recommendation:
   - Time series -> line chart
   - Category + measure -> bar/column chart
   - Part-of-whole with limited categories -> pie/doughnut only if appropriate

2. Insert backing table if not already inserted.

3. Create native Excel chart from range.

Acceptance criteria:

- Chart is native Excel, not task-pane-only.
- Chart references inserted data range/table.
- User gets clear failure message if chart insertion is unsupported.

### Workstream B: Local PivotTable

Files/modules:

- `src/utils/excelPivotTables.ts`
- `src/components/AskTessallite/InsertActions.tsx`
- `src/components/ReportBuilder/ReportBuilder.tsx`

Tasks:

1. Insert result table.

2. Create local PivotTable from inserted table/range where supported.

3. Map dimensions and measures from response/layout metadata.

4. Label all UI and toasts as **Local PivotTable**.

Acceptance criteria:

- Local PivotTable is created from inserted data only.
- UI does not imply the PivotTable is live against Tessallite.
- If local PivotTable API is unavailable, action is disabled with explanation.

### Workstream C: Persona Switcher

Files/modules:

- `src/components/PersonaSwitcher/PersonaDropdown.tsx`
- `src/hooks/usePersona.ts`

Tasks:

1. Load model personas.

2. Filter:
   - Ask context
   - Report Builder libraries
   - Glossary lookup
   - CUBE formula catalog

3. Show business-facing persona metadata:
   - Name
   - Description
   - Measure/dimension counts
   - Audience badge

4. Do not show raw internal flags in dropdown.

5. Add info bar for non-default persona:
   - "Viewing as Executive. 23 of 45 measures shown. Switch to Default."

Acceptance criteria:

- Switching persona changes available measures/dimensions.
- Existing inserted workbook outputs are not silently changed.
- Internal security flags are not shown in normal business UX.

### Workstream D: Drill-Through

Files/modules:

- `src/components/DrillThrough/DrillPanel.tsx`
- `src/components/DrillThrough/DrillPathPicker.tsx`
- `src/api/queryRouter.ts`
- `src/api/modelService.ts`
- `src/utils/cellContext.ts`

Tasks:

1. Detect selected cell context:
   - CUBE formula cell
   - PivotTable value cell where Office.js exposes enough context
   - Plugin-inserted result table cell with stored metadata

2. Resolve:
   - Measure id
   - Filters/dimension members
   - Persona/model/project

3. Call:
   - `POST /api/v1/measures/{id}/drill-options`
   - `POST /api/v1/measures/{id}/drill-through`

4. Render detail rows:
   - Breadcrumb context
   - Path selector
   - 50-row pagination
   - Insert to Sheet
   - Copy TSV

Acceptance criteria:

- Drill-through works for at least CUBE formula cells and plugin-inserted result rows.
- Unsupported cell contexts show a clear explanation.
- Detail rows insert as a formatted Excel Table.

### Phase 3 Demo

Demo script:

1. Ask for revenue by country over time.
2. Insert Chart.
3. Insert Local Pivot.
4. Switch persona and re-run a question.
5. Select a CUBE/result value and drill through.
6. Insert detail rows to a new sheet.

---

## 8. Phase 4: Production Hardening and Distribution

**Duration:** 3-4 weeks

### Goal

Make the add-in production-ready for enterprise deployment.

### Workstream A: Error Handling and Resilience

Tasks:

1. Normalize API errors:
   - Auth expired
   - Permission denied
   - Model not deployed
   - Agent not configured
   - Query failed
   - Network timeout

2. Add retry policy:
   - GET retries: 3 attempts with backoff
   - SSE reconnect: 1s, 2s, 4s, max 30s
   - No blind retry for mutation/insert actions without idempotency

3. Add offline banner:
   - Health check fails 3 times
   - Retry button
   - Countdown

Acceptance criteria:

- Failures do not leave the task pane blank.
- User can recover from expired session.
- Network failures do not duplicate inserted worksheet artifacts.

### Workstream B: Diagnostics

Files/modules:

- `src/utils/diagnostics.ts`
- `src/components/Settings/DiagnosticsPanel.tsx`

Tasks:

1. Keep last 100 client events:
   - API route and status
   - Timing
   - Excel operation attempted
   - Error codes
   - Current profile host/tenant, not secrets

2. Add "Copy Diagnostics" in settings.

3. Redact:
   - Passwords
   - JWTs
   - Authorization headers
   - Connection strings containing passwords
   - Result row values if tenant policy requires it

Acceptance criteria:

- Support can diagnose failures without secrets.
- Redaction is covered by unit tests.

### Workstream C: Accessibility

Tasks:

1. Keyboard navigation:
   - Mode switcher
   - Search
   - Chat input
   - Insert buttons
   - Modals
   - Dropdowns

2. ARIA:
   - `role="tablist"` for mode switcher
   - `role="log"` for chat messages
   - `aria-live` for streaming/status/toasts
   - Focus trap in modals

3. Reduced motion:
   - Disable animations when `prefers-reduced-motion: reduce`

Acceptance criteria:

- Core workflows are keyboard-operable.
- Screen reader labels are meaningful.
- No color-only status indicators.

### Workstream D: Performance

Tasks:

1. Virtualize long lists.

2. Debounce search.

3. Avoid re-rendering chat history on streaming every token if it causes lag.

4. Cache metadata by `deployed_version_id`.

5. Cap task pane previews.

Acceptance criteria:

- 500 measures and 500 dimensions remain usable.
- Search responds within 300ms after debounce for cached metadata.
- Chat streaming remains smooth.

### Workstream E: Distribution

Tasks:

1. Finalize manifest:
   - Icons
   - Ribbon group
   - Required permissions
   - SourceLocation

2. Document sideloading:
   - Local HTTPS dev server
   - Network share/SharePoint catalog
   - Excel Desktop steps

3. Document enterprise deployment:
   - Microsoft 365 Admin Center
   - Integrated Apps
   - Required service origins/CORS

4. Optional AppSource readiness:
   - Privacy/security notes
   - Support URL
   - Branding assets

Acceptance criteria:

- Dev team can sideload locally.
- Admin can deploy through an enterprise catalog.
- Required CORS origins and service URLs are documented.

---

## 9. Testing Plan

### 9.1 Unit Tests

Cover:

- API client error normalization
- Storage helpers never persisting password fields
- Formula generation
- Format token mapping
- Report layout to semantic query conversion
- Output metadata serialization/redaction
- Persona filtering
- Search matching

### 9.2 Component Tests

Cover:

- Login form validation
- Ask empty/configured/not-configured states
- Chat response rendering
- Insert action availability by response shape
- Report Builder assignment/removal
- Template missing-field warnings
- Glossary popover/modal
- Persona dropdown

### 9.3 Excel Integration Tests

Use Office.js test harness or manual scripted checks where automation is limited.

Must verify:

- Insert Table
- Insert Chart
- Local PivotTable support/fallback
- Formula insertion
- Active cell overwrite confirmation
- New sheet naming collision handling
- Workbook metadata write/read
- Sign out cleanup

### 9.4 Security Tests

Must verify:

- Password is not stored after login.
- Password is not present in diagnostics.
- JWT is not copied by diagnostics.
- XMLA password is not persisted after live connection flow.
- Profile switch clears tenant-specific cached data.
- Row-security behavior is enforced by backend for REST and XMLA paths.

### 9.5 UAT Scenarios

1. Executive asks for top-line KPI and inserts table.
2. Finance analyst asks for revenue by country and inserts chart.
3. Analyst uses Report Builder to create revenue by country/month table.
4. Analyst inserts CUBE formula for a single KPI.
5. User creates live connection and completes native Excel PivotTable flow.
6. User switches persona and sees reduced measure catalog.
7. User drills through from a value and inserts detail rows.
8. User loses connection and recovers without duplicate inserts.

---

## 10. Definition of Done

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

## 11. Open Decisions

These should be resolved during Phase 0 or early Phase 1.

| Decision | Options | Recommendation |
|---|---|---|
| Login token exposure | Use existing cookie flow, add `include_token`, or same-origin proxy | Prefer the least invasive option that works consistently in Office Desktop and Web. Document final choice. |
| Local PivotTable support | Enable per host, or hide until verified | Gate by runtime capability. Do not show broken actions. |
| XMLA connection creation | Office.js `connections.add2()` vs manual instructions | Attempt where supported; always provide manual fallback. |
| Result refresh metadata | Worksheet custom properties vs hidden metadata sheet | Prefer custom properties if reliable across hosts; fallback to hidden metadata sheet. |
| Chart type selection | Heuristic only vs agent-provided chart hint | Use agent metadata when present; otherwise apply deterministic heuristics. |
| Large result limit | Hard cap vs confirmation | Preview cap plus confirmation above 10,000 rows. Backend limits still apply. |

---

## 12. Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Office.js host differences | Features work on one Excel host but not another | Phase 0 compatibility matrix; runtime feature gating; clear fallbacks |
| PivotTable confusion | Users think local PivotTables are live | Strict naming: Local Pivot vs Live connection |
| Password leakage | Security incident | No password persistence; diagnostics redaction; security tests |
| Agent response lacks enough metadata | Insert actions are weak or wrong | Require response metadata contract; fallback to Insert Table |
| Large models slow the pane | Poor UX | Versioned metadata cache, virtualized lists, debounced search |
| Large result insertion freezes Excel | Poor UX/data loss | Preview cap, confirmation, row limits, progress indicator |
| Persona switch changes user expectations | Users expect existing workbook outputs to update automatically | Info bar and explicit re-run/refresh actions |
| Drill-through cell context unavailable | Feature unreliable in some contexts | Support CUBE/plugin-inserted contexts first; clear unsupported messages |

---

## 13. Implementation Order Summary

1. Scaffold app and manifest.
2. Verify Office.js output capabilities.
3. Build auth/profile/storage with no password persistence.
4. Build API client and model/agent config loading.
5. Build Ask Tessallite streaming.
6. Build Insert Table.
7. Add glossary lookup from Ask results.
8. Build Report Builder metadata libraries and layout grid.
9. Build templates.
10. Build CUBE formula wizard and live connection helper.
11. Add Chart insertion.
12. Add Local Pivot insertion with runtime gating.
13. Add persona switcher.
14. Add drill-through.
15. Add diagnostics, accessibility, performance, and distribution hardening.

---

*End of execution plan.*
