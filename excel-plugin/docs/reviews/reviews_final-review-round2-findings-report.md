# Final Review - Round 2 Findings Report

Date: 2026-05-22

Scope reviewed:
- `docs/reviews/reviews_final-review-round1-findings-report.md`
- `docs/reviews/reviews_final-review-round1-fix-report.md`
- `docs/execution/active-plan-files.md`
- `docs/execution/execution_plan.md`
- Relevant architecture specs under `docs/architecture/`
- Current Excel plugin implementation under `src/`

Constraints followed:
- No source code, tests, or configuration files were modified.
- Only this review report was written.

Validation run:
- `npx tsc --noEmit`: clean.
- `npm test -- --run`: passed, 14 files and 111 tests.
- Test run still emits React `act(...)` warnings from `LoginScreen.test.tsx`; see finding F-FINAL-R2-04.
- No live Excel Desktop/Web host smoke test was run in this review.

Severity summary:
- Critical: 0
- High: 1
- Medium: 2
- Low: 1
- Info: 0

## Findings

### F-FINAL-R2-01 - High - Plugin-inserted table drill-through still cannot load

Status relative to round 1:
- Carried forward from F-FINAL-R1-02. The range-containment part was improved, but the table-cell path still does not resolve the measure required by `DrillPanel`.

Evidence:
- `docs/execution/execution_plan.md:632` to `docs/execution/execution_plan.md:655` require selected-cell context and drill-through for plugin-inserted result rows.
- `src/hooks/useExcel.ts:41` to `src/hooks/useExcel.ts:50` stores only workbook metadata such as project, model, persona, conversation, turn, and semantic query. It does not store selected-column measure identity, row filters, or a mapping from inserted table columns back to measure ids.
- `src/utils/cellContext.ts:59` to `src/utils/cellContext.ts:69` resolves plugin-table context from metadata, but returns no `measureId`, no `measureName`, and no row/cell filters.
- `src/App.tsx:415` to `src/App.tsx:423` opens the drill panel with `setDrillMeasureId(ctx.measureId || '')`, so plugin-table cells open with an empty measure id.
- `src/components/DrillThrough/DrillPanel.tsx:47` to `src/components/DrillThrough/DrillPanel.tsx:49` exits early when `measureId` is empty. Because `optionsLoaded` is never set in that path, the panel opens without loading drill options or showing a useful unsupported-cell message.

Impact:
- Selecting a non-CUBE plugin-inserted result cell still cannot drill through, even though the plan explicitly requires plugin-inserted result rows to work.
- The round 1 fix report says drill-through now works for plugin table and CUBE formula cells, but only table range lookup was fixed. The core measure-resolution requirement remains missing.

Recommended fix:
- Persist enough result metadata to resolve selected cells: column key/title to measure id, dimension column mappings, and row filter values for the selected row.
- In `resolvePluginTableContext`, derive `measureId`, `measureName`, and filters from that metadata and the selected cell address.
- If the selected cell cannot map to a drillable measure, stop before opening `DrillPanel` and show the existing unsupported-context toast.
- Add tests for a selected plugin table data cell, not just CUBE formula parsing.

### F-FINAL-R2-02 - Medium - Live XMLA connection creation passes the connection string in the wrong argument slot

Status relative to round 1:
- New regression in the F-FINAL-R1-03 fix path.

Evidence:
- `docs/architecture/architecture_specs.md:170` requires the add-in to help create an XMLA workbook connection where Office.js supports workbook connections.
- `src/hooks/useExcelConnections.ts:64` to `src/hooks/useExcelConnections.ts:69` models the workbook connection API as `add(name, description, connectionString, commandText, commandType?)` and passes the connection string as the third argument.
- `src/components/Connection/LiveConnectionWizard.tsx:37` to `src/components/Connection/LiveConnectionWizard.tsx:42` defines a different signature, `add(name, cs, command, desc)`, and calls `wb.connections.add('Tessallite', connectionString, 'Command text', 'Tessallite XMLA')`.

Impact:
- On hosts where the workbook connection API matches the existing `useExcelConnections` abstraction, the wizard will put the MSOLAP connection string into the description field and the literal string `Command text` into the connection-string field.
- The UI can advance to "Connection created" while Excel has an invalid or unusable Tessallite connection. CUBE formulas and the native live PivotTable flow then fail later.

Recommended fix:
- Use one shared connection creation helper instead of duplicating the unsafe Office.js cast in `LiveConnectionWizard`.
- At minimum, align the argument order with the existing `useExcelConnections.createConnection` path and verify the host API signature against the Office.js compatibility spike.
- Add an Office.js mock test that asserts the exact arguments passed to workbook connection creation include the generated `Provider=MSOLAP.8` string in the connection-string parameter.

### F-FINAL-R2-03 - Medium - XMLA password remains in component state and clipboard fallback after automatic connection creation fails

Status relative to round 1:
- New issue in the F-FINAL-R1-03 fix path.

Evidence:
- `docs/execution/execution_plan.md:43` to `docs/execution/execution_plan.md:47` state that passwords must not be stored and XMLA/MSOLAP credentials are requested only for the explicit live connection/CUBE flow.
- `docs/architecture/architecture_frontend-design.md:799` says the add-in must not reuse or store the login password for XMLA/MSOLAP.
- `src/components/Connection/LiveConnectionWizard.tsx:21` to `src/components/Connection/LiveConnectionWizard.tsx:29` keeps the XMLA username/password in React state and builds a connection string containing the password on every render.
- `src/components/Connection/LiveConnectionWizard.tsx:31` to `src/components/Connection/LiveConnectionWizard.tsx:49` clears credentials only after the automatic connection succeeds. The `typeof Excel === 'undefined'` path and the catch path both move to manual setup without clearing `xmlaPassword`.
- `src/components/Connection/LiveConnectionWizard.tsx:119` to `src/components/Connection/LiveConnectionWizard.tsx:122` renders the full connection string in the dialog, and `src/components/Connection/LiveConnectionWizard.tsx:59` to `src/components/Connection/LiveConnectionWizard.tsx:61` copies it to the OS clipboard.

Impact:
- If automatic creation fails, the XMLA password remains in React component state and is exposed in clear text in the manual setup view.
- The "Copy" action can place the password into the OS clipboard, which is outside the add-in's controlled storage and can be read by other local applications or later paste actions.

Recommended fix:
- Make the manual setup path an explicit credential-disclosure step: warn that the copied string contains the XMLA password, copy only after confirmation, and clear `xmlaPassword` immediately after copying or leaving the step.
- Prefer a manual setup flow that asks the user to enter the password directly into Excel's native credential prompt instead of copying a password-bearing connection string.
- Add a security test for failed automatic connection creation: credentials should not remain visible or reusable after the fallback transition unless the user explicitly chooses a one-time copy action.

### F-FINAL-R2-04 - Low - Test suite passes but still emits React `act(...)` warnings

Status relative to round 1:
- New test-quality finding observed during round 2 validation.

Evidence:
- `npm test -- --run` passed 14 files and 111 tests, but emitted repeated React warnings that updates to `LoginScreen` and MUI `FormControl` were not wrapped in `act(...)`.
- `src/__tests__/LoginScreen.test.tsx:29` to `src/__tests__/LoginScreen.test.tsx:35`, `src/__tests__/LoginScreen.test.tsx:53` to `src/__tests__/LoginScreen.test.tsx:65`, and `src/__tests__/LoginScreen.test.tsx:67` to `src/__tests__/LoginScreen.test.tsx:73` use immediate assertions after rendering MUI text fields and async profile-prefill behavior.

Impact:
- The suite is green, but these warnings mean some assertions may run before all user-visible state updates have settled.
- Future changes to login/profile prefill behavior can produce flaky tests or hide real regressions behind warning noise.

Recommended fix:
- Update the affected `LoginScreen` tests to use async queries or `waitFor` where component effects and MUI form-control state updates are expected.
- Treat React act warnings as cleanup debt before relying on these component tests as production-hardening coverage.

## Fix Verification Notes

- F-FINAL-R1-01, active persona in Ask: verified. `src/App.tsx:446` to `src/App.tsx:456` passes `activePersonaId` to `sendMessage`.
- F-FINAL-R1-04, CUBE formula overwrite confirmation: verified for the direct insert path. `src/hooks/useExcel.ts:110` to `src/hooks/useExcel.ts:129` loads target values/formulas and prompts before writing.
- F-FINAL-R1-05, Report Builder metadata stale query: verified. `src/components/ReportBuilder/ReportBuilder.tsx:165` to `src/components/ReportBuilder/ReportBuilder.tsx:216` returns the current query, and `src/components/ReportBuilder/ReportBuilder.tsx:239` to `src/components/ReportBuilder/ReportBuilder.tsx:242` stores `JSON.stringify(result.query)`.
- F-FINAL-R1-06, health polling path: verified. `src/App.tsx:18` imports `healthCheck` from `./api/gateway`, and `src/api/gateway.ts:8` to `src/api/gateway.ts:10` uses the configured `apiClient`.
- F-FINAL-R1-07, lazy storage backend: verified. `src/utils/storage.ts:32` to `src/utils/storage.ts:34` resolves the backend at call time, and the previously failing storage/security/api-client tests now pass.
- F-FINAL-R1-08, delete confirmation: verified. `src/components/AskTessallite/ChatPanel.tsx:171` to `src/components/AskTessallite/ChatPanel.tsx:180` opens a confirmation state instead of deleting directly, and `src/components/AskTessallite/ChatPanel.tsx:227` to `src/components/AskTessallite/ChatPanel.tsx:252` renders the confirmation dialog.

## Residual Risk

- No live Excel host validation was performed. Connection creation, workbook metadata named-item behavior, and drill-through cell-context detection still need real Excel Desktop/Web verification because they depend on Office.js behavior that unit tests currently only mock lightly or do not cover.
