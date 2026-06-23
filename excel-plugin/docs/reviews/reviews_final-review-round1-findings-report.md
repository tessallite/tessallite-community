# Final Review - Round 1 Findings Report

Date: 2026-05-22

Scope reviewed:
- `docs/execution/active-plan-files.md`
- `docs/execution/execution_plan.md`
- `docs/architecture/*`
- Current Excel plugin implementation under `src/`

Constraints followed:
- No source code, tests, or configuration files were modified.
- Only this review report was written.

Validation run:
- `npm test` initially failed under the sandbox with `spawn EPERM`.
- Re-run with approval: failed, 3 files failed and 7 tests failed; 11 files and 104 tests passed.
- The failing tests are in `apiClient.test.ts`, `storage.test.ts`, and `security.test.ts`.

Severity summary:
- Critical: 0
- High: 2
- Medium: 5
- Low: 1
- Info: 0

## Findings

### F-FINAL-R1-01 - High - Active persona is not passed into Ask conversations

Evidence:
- `docs/execution/execution_plan.md:597` to `docs/execution/execution_plan.md:600` require persona filtering for Ask context, Report Builder libraries, and glossary lookup.
- `src/App.tsx:422` to `src/App.tsx:428` stores the selected persona and shows the non-default persona state.
- `src/App.tsx:430` to `src/App.tsx:440` sends Ask messages with `sendMessage(content, projectId, modelId)` and drops `activePersonaId`.
- `src/hooks/useAgentConversation.ts:73` to `src/hooks/useAgentConversation.ts:98` supports a `personaId` argument and passes it when creating a conversation.
- `src/api/agentService.ts:24` to `src/api/agentService.ts:32` serializes `persona_id` into the conversation create request when provided.

Impact:
- A user can select a restricted persona and see the UI say they are viewing as that persona, but Ask Tessallite creates the conversation without that scope.
- Agent answers can use the default persona instead of the selected persona, which can broaden data visibility and make inserted workbook outputs misleading.

Recommended fix:
- Pass `activePersonaId` from `App.tsx` into `sendMessage(content, projectId, modelId, activePersonaId)`.
- Add a regression test that selects a persona, sends an Ask message, and asserts the conversation create request includes `persona_id`.

### F-FINAL-R1-02 - High - Drill-through cannot work for the required CUBE/table contexts

Evidence:
- `docs/execution/execution_plan.md:632` to `docs/execution/execution_plan.md:655` require selected-cell detection, measure id resolution, and drill-through for CUBE formula cells and plugin-inserted result rows.
- `src/utils/cellContext.ts:36` to `src/utils/cellContext.ts:44` extracts only `measureName` from a CUBE formula and never resolves `measureId`.
- `src/utils/cellContext.ts:47` to `src/utils/cellContext.ts:57` returns plugin table metadata but also never returns a `measureId` or filters.
- `src/App.tsx:393` to `src/App.tsx:407` sets `drillMeasureId` from `ctx.measureId || ''`.
- `src/components/DrillThrough/DrillPanel.tsx:47` to `src/components/DrillThrough/DrillPanel.tsx:49` exits early when `measureId` is empty, so no drill options load.
- `src/utils/workbookMetadata.ts:40` to `src/utils/workbookMetadata.ts:49` stores metadata under the inserted table start cell, while `src/utils/workbookMetadata.ts:85` to `src/utils/workbookMetadata.ts:94` looks up metadata using the selected cell address. Selecting a data cell such as `B2` will not find metadata stored under `A1`.

Impact:
- The toolbar drill-through action will usually show "unsupported cell" or an empty drill panel for the exact contexts the plan says must work.
- Plugin-inserted result rows are especially affected because row-level context and measure identity are not stored or reconstructed.

Recommended fix:
- Store enough result metadata to map selected table columns/cells back to measure ids and filters.
- Resolve selected cells by containing table/range, not by exact start-cell metadata key.
- For CUBE formulas, map the parsed measure name to model metadata before opening `DrillPanel`.
- Add tests for CUBE formula cells and non-top-left inserted table cells.

### F-FINAL-R1-03 - Medium - Live XMLA connection flow does not request credentials

Evidence:
- `docs/execution/execution_plan.md:471` to `docs/execution/execution_plan.md:475` require detecting a missing XMLA connection, prompting for credentials only in that flow, and never persisting the XMLA password.
- `docs/architecture/architecture_frontend-design.md:797` to `docs/architecture/architecture_frontend-design.md:799` also require a credential prompt for creating the connection.
- `src/components/Connection/LiveConnectionWizard.tsx:22` builds a connection string from only `serverUrl` and `catalog`.
- `src/utils/excelFormulas.ts:50` to `src/utils/excelFormulas.ts:55` returns `Provider=MSOLAP.8;Data Source=...;Initial Catalog=...` with no user credential fields.
- `src/components/Connection/LiveConnectionWizard.tsx:30` to `src/components/Connection/LiveConnectionWizard.tsx:35` creates the workbook connection directly from that unauthenticated string, and `src/components/Connection/LiveConnectionWizard.tsx:80` to `src/components/Connection/LiveConnectionWizard.tsx:90` displays the same unauthenticated string for manual setup.

Impact:
- CUBE formulas and assisted live PivotTable setup can appear configured while Excel's MSOLAP provider has no credentials to authenticate against Tessallite XMLA.
- Users may reach a broken native Excel connection flow with no recovery path beyond manually guessing connection details.

Recommended fix:
- Add an explicit XMLA credential prompt inside the live connection flow.
- Use those credentials only to create/copy the connection string in that flow, then clear component state immediately.
- Add tests that the login password is never reused and that the live connection string includes the expected user-supplied XMLA credentials only when the user entered them.

### F-FINAL-R1-04 - Medium - CUBE formula insertion overwrites cells without confirmation

Evidence:
- `docs/execution/execution_plan.md:477` to `docs/execution/execution_plan.md:480` require active-cell defaulting and replace confirmation when the target cell is non-empty.
- `docs/architecture/architecture_frontend-design.md:801` to `docs/architecture/architecture_frontend-design.md:805` repeat the replace-confirmation requirement.
- `src/components/CubeFunctions/CubeFormulaWizard.tsx:265` to `src/components/CubeFunctions/CubeFormulaWizard.tsx:270` lets the user choose a target cell.
- `src/components/CubeFunctions/CubeFormulaWizard.tsx:337` to `src/components/CubeFunctions/CubeFormulaWizard.tsx:340` calls insert without any local overwrite guard.
- `src/hooks/useExcel.ts:110` to `src/hooks/useExcel.ts:118` writes `range.formulas = [[formula]]` without loading the target cell value or asking for confirmation.

Impact:
- Inserting a CUBE formula can silently overwrite existing workbook values or formulas.
- This violates the Excel-first safety behavior already implemented for table active-cell insertion.

Recommended fix:
- Load the target range `values` and `formulas` before writing.
- If the target cell is non-empty, prompt through the same confirmation guard pattern used by table insertion.
- Add a unit or Office.js mock test for empty and non-empty target cells.

### F-FINAL-R1-05 - Medium - Report Builder table metadata records the previous query

Evidence:
- `docs/execution/execution_plan.md:282` to `docs/execution/execution_plan.md:290` require inserted result metadata to include the semantic query.
- `src/components/ReportBuilder/ReportBuilder.tsx:179` to `src/components/ReportBuilder/ReportBuilder.tsx:188` builds a query and calls `setLastQuery(query)`.
- `src/components/ReportBuilder/ReportBuilder.tsx:228` to `src/components/ReportBuilder/ReportBuilder.tsx:242` immediately writes metadata using `lastQuery ? JSON.stringify(lastQuery) : undefined`.
- The callback dependency list at `src/components/ReportBuilder/ReportBuilder.tsx:249` does not include `lastQuery`, and React state updates are asynchronous.

Impact:
- The first inserted table after opening Report Builder can have no `semanticQuery`.
- Later inserted tables can be tagged with the previous query instead of the query that produced the rows.
- Refresh, audit, and drill-through metadata become unreliable.

Recommended fix:
- Return the `query` from `executeZoneQuery()` alongside headers/rows/annotation.
- Use that returned query directly in `handleInsertTable` instead of reading `lastQuery` state.
- Add a regression test that two different queries insert metadata for their own query shapes.

### F-FINAL-R1-06 - Medium - Connection health polling uses the wrong client path

Evidence:
- `src/App.tsx:18` imports `healthCheck` from `./api/auth`.
- `src/App.tsx:156` to `src/App.tsx:190` uses that health check to set connected/reconnecting status.
- `src/api/auth.ts:27` to `src/api/auth.ts:30` calls `fetch('/health')`, bypassing the configured profile base URL and JWT handling.
- `src/api/gateway.ts:8` to `src/api/gateway.ts:10` already defines a profile-aware `apiClient.get('/health')` health check, but it is not used by `App.tsx`.

Impact:
- The footer status can report the add-in host or Vite dev server health instead of the selected Tessallite server profile.
- Remote profiles and local development can show false connected/disconnected states.

Recommended fix:
- Import the profile-aware health check from `api/gateway.ts`, or move the correct implementation into one canonical health module.
- Add a test that configuring `serverUrl` causes health polling to request `${serverUrl}/health`.

### F-FINAL-R1-07 - Medium - Storage backend is captured at module load, causing test failures and brittle runtime fallback

Evidence:
- `src/utils/storage.ts:21` to `src/utils/storage.ts:30` chooses either `OfficeRuntime.storage` or `localStorage`.
- `src/utils/storage.ts:32` stores that backend in a module-level `const storage = getBackend()`.
- The approved `npm test` run failed storage/security/api-client tests because test stubs for `OfficeRuntime.storage` did not affect the already-captured backend.

Impact:
- If `OfficeRuntime` is not available at module evaluation time but becomes available after Office bootstrapping, the add-in will continue using `localStorage` for JWT/profile data.
- The current automated suite is red: 7 tests fail across storage, security, and API auth behavior.

Recommended fix:
- Resolve the backend lazily per operation or expose a test-only injection/reset hook.
- Prefer `OfficeRuntime.storage` whenever it is available at call time.
- Keep the fallback to `localStorage` only for genuinely unsupported Office hosts.

### F-FINAL-R1-08 - Low - Conversation deletion has no confirmation

Evidence:
- `src/components/AskTessallite/ChatPanel.tsx:170` to `src/components/AskTessallite/ChatPanel.tsx:179` calls `onDeleteConversation(c.id)` directly from the delete icon.
- `src/App.tsx:654` to `src/App.tsx:663` immediately calls `deleteConversation(projectId, id)`.

Impact:
- A single click in the history menu permanently deletes a conversation with no undo or confirmation.
- This is an avoidable destructive-action rough edge in the primary Ask workflow.

Recommended fix:
- Add a confirmation dialog before deleting a conversation.
- Keep the history menu open or provide clear cancellation affordance until the user confirms.
