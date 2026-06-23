# Phase 4 Review - Round 2 Findings Report

Date: 2026-05-20

Scope reviewed:
- Completed phase 1, phase 2, phase 3, and phase 4 items in `excel-plugin/docs/execution/active-plan-files.md`.
- Round 1 findings report and round 1 fix report.
- Current Excel plugin source under `excel-plugin/src`.
- Relevant query-router endpoint contracts for `/api/v1/explain`, `/api/v1/validate`, `/api/v1/discover/members`, and `/api/v1/plugin/execute`.

Constraints followed:
- No source code, tests, or configuration files were modified.
- Only this review report was written.

Validation run:
- `npm test -- --run`: passed, 4 files, 27 tests.
- `npm run build`: passed. Vite still warns that the main chunk is larger than 500 kB.

Severity summary:
- Critical: 0
- High: 2
- Medium: 6
- Low: 2
- Info: 0

## Findings

### F-4R2-01 - High - Report Builder filter zone is still UI-only and does not affect executed results

Evidence:
- `src/components/ReportBuilder/DimensionLibrary.tsx:80` maps `filter` and `slicer` assignments into the `filters` zone.
- `src/components/ReportBuilder/ReportBuilder.tsx:343` to `src/components/ReportBuilder/ReportBuilder.tsx:346` adds selected dimensions to the `filters` zone.
- `src/components/ReportBuilder/ZoneMappingGrid.tsx:57` and `src/components/ReportBuilder/ZoneMappingGrid.tsx:82` to `src/components/ReportBuilder/ZoneMappingGrid.tsx:88` display the filter zone to the user.
- `src/components/ReportBuilder/ReportBuilder.tsx:151` to `src/components/ReportBuilder/ReportBuilder.tsx:160` builds the executed semantic query from values, rows, and columns only. It never reads `zoneItems` where `zone === 'filters'`.
- `src/api/queryRouter.ts:27` to `src/api/queryRouter.ts:31` already supports sending filters to `/api/v1/plugin/execute`.
- `services/query-router/src/api/plugin.py:51` to `services/query-router/src/api/plugin.py:56` and `services/query-router/src/api/plugin.py:190` to `services/query-router/src/api/plugin.py:215` show the backend plugin endpoint accepts and binds filters.

Impact:
- A user can add a dimension to Filters and insert a table/chart/pivot that is not filtered.
- The UI creates a false governance signal because the workbook output looks scoped while the query is actually broader.
- This is not just the known missing value-picker item; the current implementation accepts filter-zone selections but drops them during execution.

Recommended fix:
- Add an explicit filter model with dimension, operator, and values/member selection.
- Disable execution when a filter chip lacks required values, or make the UI clear that the chip is not active.
- Populate `SemanticQuery.filters` before calling `executeQuery`.
- Add a component test that adds a filter and asserts `/api/v1/plugin/execute` receives it.

### F-4R2-02 - High - CUBE formula validation is not meaningful and can fail open

Evidence:
- `src/components/CubeFunctions/CubeFormulaWizard.tsx:116` generates an Excel `CUBEVALUE(...)` formula.
- `src/components/CubeFunctions/CubeFormulaWizard.tsx:117` sends that formula to `validateQuery`.
- `src/api/queryRouter.ts:61` to `src/api/queryRouter.ts:70` defaults validation to `protocol: 'jdbc'`.
- `services/query-router/src/api/routes.py:1014` to `services/query-router/src/api/routes.py:1022` parses non-`dax` validation requests as SQL.
- `src/components/CubeFunctions/CubeFormulaWizard.tsx:126` to `src/components/CubeFunctions/CubeFormulaWizard.tsx:130` marks the formula as validated when the validation request throws.

Impact:
- A valid Excel CUBE formula can be rejected because it is sent to a SQL parser.
- A failed network request or unexpected API error is treated as validation success, enabling insertion without validation.
- The plan item `P2-M8 - CUBE formula validation before insert` is only partially implemented.

Recommended fix:
- Do not validate Excel formulas through the default SQL validation path.
- Either validate the underlying semantic tuple before formula generation, or add a backend endpoint/protocol that explicitly accepts CUBE formula inputs.
- Treat validation transport failures as blocking errors, not success.
- Add tests for valid formula, invalid member, API 500, and network failure paths.

### F-4R2-03 - Medium - Query Trace still sends the wrong request shape for plugin semantic queries

Evidence:
- `src/components/QueryTrace/TraceModal.tsx:22` to `src/components/QueryTrace/TraceModal.tsx:31` calls `explainQuery` with a `SemanticQuery`.
- `src/api/queryRouter.ts:45` to `src/api/queryRouter.ts:58` posts to `/api/v1/explain` and serializes the semantic query into `raw_query` using `JSON.stringify(params.query)`.
- `services/query-router/src/api/routes.py:144` to `services/query-router/src/api/routes.py:168` defines `/explain` input as `ExecuteRequest` with `raw_query`, `protocol`, and SQL/DAX parsing fields.
- `services/query-router/src/api/routes.py:327` to `services/query-router/src/api/routes.py:347` handles `/explain` with the raw-query path.
- `services/query-router/src/api/routes.py:1014` to `services/query-router/src/api/routes.py:1022` parses `raw_query` as DAX only when `protocol === 'dax'`; otherwise it parses as SQL.
- `services/query-router/src/api/plugin.py:177` to `services/query-router/src/api/plugin.py:215` shows plugin semantic execution is a separate endpoint and request shape.

Impact:
- The trace modal can fail to parse the JSON string as SQL.
- If the backend ignores the extra semantic fields, the route explanation is not explaining the query that `/api/v1/plugin/execute` actually ran.
- The round 1 fix changed the TypeScript call shape but did not align the endpoint contract.

Recommended fix:
- Add a semantic-query explain endpoint, for example `/api/v1/plugin/explain`, that accepts the same payload as `/api/v1/plugin/execute`.
- Alternatively return route trace data directly from `/api/v1/plugin/execute` and display that stored trace.
- Add a test that executes a Report Builder query and verifies the trace endpoint uses the same measures, dimensions, filters, persona, and model.

### F-4R2-04 - Medium - Inserted workbook metadata still omits `semanticQuery`

Evidence:
- `src/hooks/useExcel.ts:17` to `src/hooks/useExcel.ts:24` defines `semanticQuery` as supported insert metadata.
- `src/hooks/useExcel.ts:41` to `src/hooks/useExcel.ts:50` writes `semanticQuery` into workbook metadata when provided.
- `src/components/AskTessallite/ChatPanel.tsx:188` to `src/components/AskTessallite/ChatPanel.tsx:201` exposes insert actions for agent result previews, but the message model does not carry the underlying semantic query.
- `src/App.tsx:262` to `src/App.tsx:272` passes project, model, persona, conversation, and format metadata to `excelInsertTable`, but not `semanticQuery`.
- `src/components/ReportBuilder/ReportBuilder.tsx:198` to `src/components/ReportBuilder/ReportBuilder.tsx:211` also passes project, model, persona, and format metadata, but not the `query` built in `executeZoneQuery`.
- `excel-plugin/docs/execution/active-plan-files.md:187` to `excel-plugin/docs/execution/active-plan-files.md:189` lists `P2-#25 - Complete table metadata` as a high-priority item requiring `semanticQuery`.

Impact:
- Inserted Excel tables cannot be reliably refreshed, audited, or re-associated with the exact query shape that produced them.
- Drill-through and support diagnostics lose important context.
- The plan claims table metadata fields exist, but the most important field remains unwired at call sites.

Recommended fix:
- For Report Builder, pass `JSON.stringify(query)` or a stable semantic query envelope through `excelInsertTable`.
- For Ask Tessallite, carry the query/fingerprint returned by the agent or query-router into `Message` and insert metadata.
- Add tests around `insertTable` metadata for both Report Builder and Ask flows.

### F-4R2-05 - Medium - Header/value alignment remains unsafe after the round 1 numeric coercion fix

Evidence:
- `src/components/ReportBuilder/ReportBuilder.tsx:179` to `src/components/ReportBuilder/ReportBuilder.tsx:181` derives display headers from annotation object values.
- `src/components/ReportBuilder/ReportBuilder.tsx:182` to `src/components/ReportBuilder/ReportBuilder.tsx:184` derives row values from `Object.values(r)`.
- `src/hooks/useAgentConversation.ts:167` to `src/hooks/useAgentConversation.ts:172` uses `Object.keys` from the first row for headers and `Object.values` for row values in agent result previews.

Impact:
- Numeric preservation from round 1 is fixed, but column alignment can still break if backend row object key order differs from annotation order.
- This can silently insert values under the wrong Excel headers.
- This is most likely when annotations order measures before dimensions while returned rows are ordered by SQL output or JSON serialization order.

Recommended fix:
- Use a single ordered list of stable backend column keys for both headers and row values.
- Map rows by key, for example `columnKeys.map(key => r[key])`, and separately map those keys to display labels.
- Prefer a backend response `columns` array if available; otherwise derive keys from the semantic query order and annotation keys.
- Add a test where row object order intentionally differs from annotation order.

### F-4R2-06 - Medium - Persona-scoped glossary and alias metadata are still not filtered by persona

Evidence:
- `src/components/ReportBuilder/ReportBuilder.tsx:39` and `src/components/ReportBuilder/ReportBuilder.tsx:40` load glossary and alias map without `personaId`.
- `src/hooks/useModel.ts:73` to `src/hooks/useModel.ts:85` defines `useGlossary` and `useAliasMap` without a persona argument or persona-scoped query key.
- `src/api/modelService.ts:49` to `src/api/modelService.ts:55` calls glossary and alias-map endpoints without `persona_id`.
- `src/App.tsx:78` and `src/App.tsx:647` to `src/App.tsx:651` show the Glossary modal also uses unscoped glossary entries.
- `excel-plugin/docs/execution/active-plan-files.md:183` to `excel-plugin/docs/execution/active-plan-files.md:185` lists persona filtering for metadata, glossary, and CUBE catalog as high-priority work.

Impact:
- Users in a restricted persona can still see glossary terms, synonyms, or aliases that may refer to hidden measures/dimensions.
- Search can match hidden business terminology even when the measure/dimension list itself is persona-scoped.
- The UI creates inconsistent persona behavior across the footer persona selector, Report Builder metadata lists, glossary modal, and search.

Recommended fix:
- Thread `personaId` through glossary and alias-map hooks and API calls.
- If backend endpoints do not support persona filtering, filter client-side against the persona-filtered measure and dimension collections until backend support exists.
- Include `personaId` in React Query keys to avoid cache bleed between personas.

### F-4R2-07 - Medium - Diagnostics clear action does not clear diagnostics and redaction is too narrow

Evidence:
- `src/components/Settings/DiagnosticsPanel.tsx:38` to `src/components/Settings/DiagnosticsPanel.tsx:43` implements Clear Log by adding `Diagnostics cleared` and re-reading the report.
- `src/utils/diagnostics.ts:19` and `src/utils/diagnostics.ts:20` keep events in a module-level array, but no exported clear function exists.
- `src/api/client.ts:71` to `src/api/client.ts:74` logs serialized API error bodies to diagnostics.
- `src/utils/diagnostics.ts:103` to `src/utils/diagnostics.ts:106` only redacts bearer tokens and `Password=...` connection-string fragments.

Impact:
- The Clear Log button is misleading and retains all previous diagnostics until the ring buffer naturally trims.
- API error bodies containing JSON keys such as `password`, `token`, `access_token`, `refresh_token`, `secret`, `client_secret`, `api_key`, `connectionString`, or `Authorization` can still be copied into support diagnostics.
- This violates the phase 4 diagnostics hardening goal and the security constraint that secrets must not be logged.

Recommended fix:
- Add and use `clearDiagnostics()` that empties the event array.
- Redact common secret keys in JSON-like strings and URL query parameters, not only bearer-token text.
- Add security tests for diagnostics redaction.

### F-4R2-08 - Medium - Conversation history selection does not hydrate historical messages

Evidence:
- `src/components/AskTessallite/ChatPanel.tsx:151` to `src/components/AskTessallite/ChatPanel.tsx:168` renders a conversation history menu and calls `onSelectConversation`.
- `src/hooks/useAgentConversation.ts:250` to `src/hooks/useAgentConversation.ts:254` handles selection by calling `getConversation` and setting only `conversationId`.
- `src/hooks/useAgentConversation.ts:39` keeps messages in local component state, and selected conversation data is never mapped back into `messages`.
- `src/api/agentService.ts:39` to `src/api/agentService.ts:40` returns only `AgentConversation` for `getConversation`; no message/turn fetch is wired here.

Impact:
- Selecting a prior conversation changes the active ID but leaves the visible transcript unchanged.
- A follow-up typed after selecting history can be appended to a conversation whose previous turns are not shown.
- This can confuse users and create bad audit/support context.

Recommended fix:
- Either fetch and hydrate historical turns when a conversation is selected, or make the menu a metadata-only selector and clear the current message list with a clear status message.
- Add test coverage for selecting history after an active conversation exists.

### F-4R2-09 - Low - Test coverage still contradicts the phase Definition of Done

Evidence:
- `excel-plugin/docs/execution/active-plan-files.md:100` to `excel-plugin/docs/execution/active-plan-files.md:108` states current coverage is 4 unit test files, 0 component tests, 0 security tests, 0 Excel integration tests, and 0 UAT scenarios.
- `excel-plugin/docs/execution/active-plan-files.md:147` to `excel-plugin/docs/execution/active-plan-files.md:163` lists 71 missing tests.
- `excel-plugin/docs/execution/active-plan-files.md:121` to `excel-plugin/docs/execution/active-plan-files.md:131` says a phase is done only when core logic and security constraints are covered by tests and the demo script passes.
- Current validation confirms only 27 unit tests run.

Impact:
- The implementation can pass current validation while major phase 1-4 behaviors remain untested: login/security, Report Builder filtering and metadata, query trace, component interaction, diagnostics redaction, and Office host workflows.
- The plan marks phases 1-4 as done while its own test and Definition of Done sections say key validation is absent.

Recommended fix:
- Do not treat phases 1-4 as production-complete until the missing high-priority component/security tests are implemented or explicitly reclassified.
- Prioritize tests for findings F-4R2-01 through F-4R2-08.

### F-4R2-10 - Low - Common hardening components remain partly unused

Evidence:
- `src/components/common/LoadingSkeleton.tsx`, `src/components/common/SectionHeader.tsx`, and `src/components/common/EmptyState.tsx` are defined.
- Search found only `SearchBar` and `StatusBadge` used in current source.
- `excel-plugin/docs/execution/active-plan-files.md:97` marks Common Components as done.

Impact:
- This is not a runtime bug by itself, but it shows phase 4 common-component extraction was only partially adopted.
- Screens still use local skeleton/empty/header implementations, increasing inconsistent UI and accessibility behavior.

Recommended fix:
- Either wire the common components where they were intended to standardize phase 4 UX, or remove/reclassify them as a component library foundation rather than completed hardening.

## Mandatory Review Sweep Coverage

Plan and implementation comparison:
- Checked the active plan against current source and round 1 fixes.
- Found incomplete or inconsistent items around filter execution, CUBE validation, query trace, metadata, persona glossary/alias filtering, diagnostics, conversation history, and tests.

Source/target database access:
- No direct source or target database query execution was found in `excel-plugin/src`.
- User/business query execution in the Excel plugin is routed through `/api/v1/plugin/execute`.
- Member discovery is routed through `/api/v1/discover/members`.
- Live XMLA helper text and connection generation are Excel provider plumbing, not application-side SQL execution.

SQL dialect branching:
- No SQL dialect branching or target/source database conditional SQL processing was found in `excel-plugin/src`.
- No SQL generation in the Excel plugin was found that should be replaced with `sqlglot`.

Regressions from round 1 fixes:
- Numeric values are now preserved in Report Builder rows, but header/value alignment remains unsafe.
- Query Trace was partially rewired but still targets an incompatible backend endpoint shape.
- CUBE validation now calls an API but does not validate the generated Excel formula correctly and fails open on request errors.
- Conversation history no longer clears the transcript, but it still does not hydrate selected history.

Security sweep:
- Password persistence was not found in storage.
- `LoginScreen` clears password state after `onLogin` returns.
- Diagnostics redaction remains incomplete and the clear button does not clear retained events.
- Persona filtering is incomplete for glossary and alias-map metadata.

Performance and deployment sweep:
- `npm run build` succeeds.
- Vite warns that the main bundle is larger than 500 kB. This is a production performance concern but not a blocking build failure.

Accessibility sweep:
- Key icon buttons checked in the reviewed paths have ARIA labels.
- Some common accessibility hardening components remain unused, but no new concrete ARIA regression was identified in this round.

Testing sweep:
- Current tests pass.
- Coverage remains below the plan's target and below the plan's own Definition of Done for completed phases.

## Overall Assessment

Round 2 found no critical issues, but the completed phase status is still too optimistic. The most important remaining risks are user-visible correctness and trust issues:
- Filters shown in the Report Builder are not executed.
- CUBE validation can validate the wrong thing or fail open.
- Query Trace still does not explain the semantic plugin query path.
- Inserted workbook metadata still cannot reconstruct the query that produced the output.
- Persona-scoped metadata is inconsistent.

The plugin builds and existing unit tests pass, but those tests do not cover the areas where the highest-risk issues were found.
