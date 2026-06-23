# Tessallite Excel Plugin -- Specification

Detailed specification for a Microsoft Excel add-in that brings Tessallite's governed semantic layer into Excel as an insight workbench. The plugin enables business users to ask business questions, understand definitions, and insert governed Excel-native outputs -- tables, charts, CUBE formulas, and supported PivotTable workflows -- without leaving Excel.

Status: active
Last updated: 2026-05-18

---

## Table of Contents

1. [Background and Motivation](#1-background-and-motivation)
2. [Architecture Overview](#2-architecture-overview)
3. [Plugin Technology Stack](#3-plugin-technology-stack)
4. [Feature Specifications](#4-feature-specifications)
   - 4.1 [Authentication and Connection Management](#41-authentication-and-connection-management)
   - 4.2 [Report Builder](#42-report-builder)
   - 4.3 [Ask Tessallite (Conversational Analytics)](#43-ask-tessallite-conversational-analytics)
   - 4.4 [Cube Function Generator](#44-cube-function-generator)
   - 4.5 [Drill-Through Panel](#45-drill-through-panel)
   - 4.6 [Glossary Lookup](#46-glossary-lookup)
   - 4.7 [Persona Switcher](#47-persona-switcher)
   - 4.8 [Excel Output Contract](#48-excel-output-contract)
5. [API Surface Required](#5-api-surface-required)
6. [UI Layout and Interaction Design](#6-ui-layout-and-interaction-design)
7. [Installation and Distribution](#7-installation-and-distribution)
8. [Security Model](#8-security-model)
9. [Caching and Performance Strategy](#9-caching-and-performance-strategy)
10. [Development Phases](#10-development-phases)
11. [Glossary](#11-glossary)

---

## 1. Background and Motivation

### 1.1 What Exists Today

Tessallite already provides XMLA/SOAP connectivity through the gateway service (`services/gateway/src/dax/xmla_server.py`). Excel connects using the MSOLAP provider (`Provider=MSOLAP.8`) to `POST /api/v1/xmla/` and performs DISCOVER (metadata browsing) and EXECUTE (MDX/DAX queries). This gives Excel users:

- PivotTable creation against Tessallite models
- Dimension and measure browsing via the field list
- Hierarchy drill-down in PivotTables
- Cross-filtering and slicing
- Multi-catalog (multi-model) support

### 1.2 What XMLA Cannot Do

The XMLA protocol is limited to OLAP metadata (cubes, dimensions, measures, hierarchies) and MDX/DAX query execution. It cannot expose:

- **Conversational analytics**: the agent-service chat endpoint -- natural language to data
- **Semantic descriptions**: glossary definitions attached to measures and dimensions (`effective_description`)
- **Variant measures**: time-intelligence variants (YoY, YTD, trailing N) appear as flat measures in XMLA with no grouping or lineage
- **Persona-scoped views**: switching between business and technical views without reconnecting
- **Drill-through to detail**: Tessallite's hierarchy-aware drill-through API (`/api/v1/measures/{id}/drill-through`)
- **Format strings**: Tessallite's format tokens (`currency`, `percent_2dp`, etc.) are not passed through XMLA
- **Report composition**: XMLA has no concept of building a PivotTable layout by selecting measures and dimensions from a curated palette

### 1.3 Why an Excel Plugin

An Office Add-in (web extension) runs inside Excel as a task pane. It has access to:

1. The Tessallite REST APIs (direct HTTP from the task pane)
2. The Excel JavaScript API (read/write cells, create local tables and charts, insert formulas, create local PivotTables from worksheet ranges, and bind worksheet events)
3. The existing XMLA connection (for PivotTable operations)

This hybrid approach means the plugin enhances the XMLA experience with Tessallite-specific capabilities for report building, rather than replacing PivotTable functionality or recreating the Tessallite web frontend.

**The plugin's job**: Help business users move from business intent to governed Excel output as quickly as possible. The primary path is natural language (Ask Tessallite). The structured fallback is guided composition (Report Builder). The plugin does not monitor model health, debug query routing, export model definitions, or replicate admin workflows -- those belong in the web frontend.

### 1.4 Product Boundaries

The add-in must optimize for Excel users, not Tessallite administrators.

**Do:**
- Make Ask Tessallite the fastest route from question to inserted spreadsheet output.
- Use Excel as the working canvas: the final answer should be a worksheet table, chart, CUBE formula range, or native PivotTable workflow.
- Add semantic help where Excel is weak: business definitions, measure lineage, approved terminology, templates, persona-aware model scope, and governed drill-through.
- Keep advanced diagnostics behind settings or support flows.

**Do not:**
- Build another Tessallite web frontend inside the task pane.
- Add model health dashboards, schema drift views, aggregate coverage panels, export/version-diff tools, or administrative controls.
- Compete with Excel's native PivotTable field list for open-ended drag-and-drop exploration.
- Promise programmatic creation of live external XMLA PivotTables where Office.js does not support it.

---

## 2. Architecture Overview

```
+----------------------------------------------------------+
| Microsoft Excel                                          |
|                                                          |
|  +-------------------+    +---------------------------+  |
|  | XMLA / MSOLAP     |    | Office Add-in (Task Pane) |  |
|  | Connection        |    |                           |  |
|  |                   |    |  +---------------------+  |  |
|  |  PivotTables      |    |  | React SPA           |  |  |
|  |  CUBE functions   |    |  |  - Report Builder   |  |  |
|  |  Field List       |    |  |  - Ask Tessallite   |  |  |
|  |  PivotCharts      |    |  |  - Glossary lookup  |  |  |
|  |                   |    |  |  - Drill-Through    |  |  |
|  +--------+----------+    |  |  - Persona Switcher |  |  |
|           |               |  +----------+----------+  |  |
|           |               |             |              |  |
|  +--------+----------+    |  +----------+----------+  |  |
|  | Excel JS API       |<--+  | Auth + API Client    |  |  |
|  | (cell read/write,  |    +--+----------+----------+  |  |
|  |  table creation,   |                   |              |  |
|  |  chart creation,   |                   |              |  |
|  |  event binding)    |                   |              |  |
|  +--------------------+                   |              |  |
|                                           |              |  |
+-------------------------------------------|--------------+
                                            |
                           HTTPS (JWT)      |  HTTPS + Basic Auth
                                            |
                     +-----------------------+-------------------+
                     |                                           |
           +---------v----------+                    +-----------v-----------+
           | Gateway :8080      |                    | Model Service :8001   |
           |  /api/v1/xmla/*    |                    |  /api/v1/auth/*       |
           |  (XMLA/SOAP)       |                    |  /api/v1/projects/*   |
           +---------+----------+                    |  /api/v1/tenants/*    |
                     |                               +-----------+-----------+
                     | HTTPS (JWT)                               |
           +---------v----------+                    +-----------v-----------+
           | Query Router :8002  |                    | Agent Service :8005   |
           |  /api/v1/execute    |                    |  .../agent/           |
           |  /api/v1/explain    |                    |  conversations/*      |
           |  /api/v1/headless/* |                    +-----------------------+
           +---------------------+
```

**Key design principle**: Live XMLA PivotTables remain an Excel/MSOLAP workflow. The plugin task pane handles intent capture, semantic guidance, query execution through Tessallite APIs, CUBE formula generation, local result-table/chart creation, glossary lookup, drill-through, and persona-scoped metadata. The two communicate through the shared Excel workbook: CUBE formulas, worksheet tables, cell selections, and existing PivotTable state.

---

## 3. Plugin Technology Stack

| Component | Technology | Rationale |
|---|---|---|
| Add-in type | Office Add-in (Task Pane) | Runs on Windows, Mac, and Web; uses modern web standards |
| Manifest | XML manifest (v1.1) | Required for Office Add-in registration. XML manifests are widely tested for sideloading and on-premises deployment. Shared runtime and SSO features are not required by this architecture, so the older JSON unified manifest is not needed. |
| Frontend | React 18 + TypeScript + MUI 5 | Matches Tessallite frontend stack for code sharing |
| API client | Fetch API with JWT bearer tokens | Direct REST calls to model-service and query-router |
| Excel integration | Excel JavaScript API (Office.js) | Cell read/write, table creation, chart creation, local PivotTables from worksheet ranges where supported, formula insertion, event binding |
| Build tool | Vite | Fast builds, matches Tessallite frontend |
| Package manager | npm | Matches Tessallite frontend |

**Platform support**: Windows Desktop (Excel 2016+), Mac Desktop, Excel on the Web. Result-table insertion, chart creation, glossary lookup, chat, and CUBE formula generation are cross-platform subject to Office.js requirement-set availability. Live XMLA PivotTable connectivity is Windows/Mac only and is completed through Excel's native external-data/PivotTable UI. Programmatic local PivotTables can only be created from worksheet ranges, not directly from external XMLA connections.

---

## 4. Feature Specifications

### 4.1 Authentication and Connection Management

**Description**: The plugin provides a login screen and connection profile manager. Users authenticate against the Tessallite model-service and save non-secret connection profile metadata (server URL, tenant, email, display name). Passwords are never persisted by the add-in.

**Implementation**:
- Login via `POST /api/v1/auth/login` with `{tenant_id, email, password}`
- JWT stored in `OfficeRuntime.storage` (persistent, encrypted on-device)
- Connection profiles stored as JSON in `OfficeRuntime.storage` with fields: `name, server_url, tenant_id, email`
- Auto-login on plugin open if a default profile exists
- Profile switcher dropdown in the task pane header
- Password entered on the login form is used only for the login request and is immediately discarded from React state after the request completes
- If a future SSO/device-code flow is available, it should replace password login for enterprise deployments

**Excel JS integration**: After login, the plugin can help create an XMLA workbook connection where Office.js and the host platform support `Workbook.connections.add2()`. Because XMLA/MSOLAP uses Basic Authentication, connection creation requires an explicit credential prompt at the moment the user chooses "Create live PivotTable connection." The add-in must not silently reuse or persist the login password for this purpose.

**Limitation -- OLAP PivotTable creation**: The Excel JavaScript API cannot programmatically create an OLAP PivotTable connected to an external data source. `Workbook.pivotTables.add()` only supports creating PivotTables from local worksheet ranges. To create an XMLA-connected PivotTable, the user must use Excel's native Insert > PivotTable dialog after the plugin has established the XMLA connection. The plugin can automate everything up to and including the connection creation, but the final PivotTable insertion step requires user action. The plugin should show a brief instruction: "Connection created. Go to Insert > PivotTable > Use an external data source > Choose the Tessallite connection."

---

### 4.2 Report Builder

**Description**: The structured surface for users who know the measures and dimensions they want. Users select measures, dimensions, and hierarchies from annotated libraries, assign them to a report layout, and insert the result into Excel as a query-backed worksheet table, chart, local PivotTable from inserted results, or CUBE formulas. This replaces the traditional "model browser tree" pattern with a composition-oriented layout designed for report construction.

**Positioning**: Report Builder is not the default "insight at speed" path; Ask Tessallite is. Report Builder exists for repeatable finance/operations layouts, power users who prefer explicit field selection, and cases where the user wants CUBE formulas or template-driven report scaffolding.

**API calls**:
- `GET /api/v1/projects` -- list projects
- `GET /api/v1/projects/{id}/models` -- list models in project
- `GET /api/v1/projects/{id}/models/{model_id}/measures` -- list measures with `effective_description`
- `GET /api/v1/projects/{id}/models/{model_id}/measures/{mid2}` -- measure detail
- `GET /api/v1/projects/{id}/models/{model_id}/measures/{mid2}/available-variants` -- time variant enumeration
- `GET /api/v1/projects/{id}/models/{model_id}/dimensions` -- list dimensions with `effective_description`
- `GET /api/v1/projects/{id}/models/{model_id}/hierarchies` -- list hierarchies with levels
- `POST /api/v1/discover/members` -- get distinct members for a dimension

**Building block libraries**:

The Report Builder presents three collapsible libraries:

#### Measure Library

Each measure card shows a compact default view:
- `display_name` (primary label)
- `effective_description` (one-line truncation, expandable on click)
- Primary semantic badges only: `format` token and `measure_type`
- `[+ Values]` quick action

Expanded details, opened by clicking the card title or info icon, show:
- `default_agg`
- Variant lineage (e.g., "variant of: Revenue")
- `display_folder` breadcrumb
- Cross-model badge: `[cross-model: {source_model_name}]`
- Semi-additive behavior badge: `[semi-additive: {behavior}]`
- glossary definition and synonyms when available

Measures are grouped by `display_folder` when folders are defined, with collapsible folder headers. Variant measures are indented under their base measure with a `[+ N variants]` expand/collapse toggle.

Each measure card has a checkbox for multi-select and a `[+ Values]` quick-action button.

#### Dimension Library

Each dimension card shows:
- `display_name` (primary label)
- `effective_description` (glossary text)
- Data type chip (`string`, `time`, `numeric`)
- Source type badge (`dim`, `calculated`)
- Calendar type badge for time dimensions (e.g., `[fiscal: Jul]`)

Each dimension card has quick-action buttons: `[-> Rows]`, `[-> Columns]`, `[-> Filter]`, `[-> Slicer]`.
A `[Preview members]` link expands an inline panel showing the first 20 distinct member values, loaded via `POST /api/v1/discover/members`.

#### Hierarchy Library

Each hierarchy card shows:
- Hierarchy `name` and type badge (`date`, `explicit`, `segment`)
- Level chain: `Year > Quarter > Month > Day`
- Clicking a hierarchy expands to show individual levels, each with a `[-> Rows]` quick-action button

**Report layout mapping**:

Above the building block libraries, a collapsible layout grid shows a PivotTable-like layout because users understand Filters, Columns, Rows, and Values. This grid is a report intent model; it does not imply the plugin can always create a live external XMLA PivotTable programmatically.

```
+------------------------------------------+
| REPORT LAYOUT                   [clear]  |
+------------------------------------------+
| FILTERS                                  |
| [no filters assigned]                    |
+------------------------------------------+
| COLUMNS           | VALUES               |
| [no columns]      | [no measures]        |
+--------------------+---------------------+
| ROWS                                     |
| [no rows assigned]                       |
+------------------------------------------+
```

When the user clicks a quick-action button on a measure or dimension card, the item's name appears in the corresponding zone slot. The zone mapping updates in real time as items are added or removed. A `[clear]` button resets all zones.

Each zone slot shows the assigned items as removable chips. For example, after assigning Revenue and Order Count to Values:

```
| VALUES                                    |
| [Revenue x]  [Order Count x]             |
```

**Insertion actions and technical behavior**:

When at least one item is assigned, action buttons appear below the zone mapping:

| Button | Behavior |
|---|---|
| **Insert Results Table** | Builds a semantic query from the layout, calls `/api/v1/execute`, and writes the result rows as a formatted Excel Table |
| **Insert Chart** | Inserts the results table if needed, then creates a native Excel chart from that worksheet range |
| **Insert Local PivotTable** | Inserts the result rows as a worksheet table, then creates a local Excel PivotTable from that range where Office.js supports it |
| **Insert CUBE Formulas** | Writes CUBE formulas for assigned measures and filters at the active cell range; requires an XMLA workbook connection |
| **Create Live PivotTable Connection** | Creates or verifies the XMLA connection and shows step-by-step native Excel instructions for Insert > PivotTable > Use an external data source. The add-in does not create the external OLAP PivotTable itself |

**Do not implement** an "Insert live XMLA PivotTable" button that claims to complete the entire live PivotTable insertion. The correct live path is connection helper plus clear Excel-native instructions.

**Search**: A search bar at the top filters all three libraries simultaneously. Searches across `display_name`, `name`, `description`, `effective_description`, `display_folder`, glossary synonyms, and alias map entries. Debounced at 300ms. Libraries with no matching items are hidden.

**Report templates**:

A "Templates" button opens a grid of pre-configured report patterns:

| Template | Default Layout |
|---|---|
| **Time Series** | Date hierarchy on rows, selected measures in values |
| **Top N Breakdown** | Single dimension on rows, one measure in values, top-10 value filter |
| **Period Comparison** | Time dimension on columns (two members), measures in values |
| **Geographic Breakdown** | Geography dimension on rows, measures in values |
| **Variance Analysis** | Two measures in values (actual vs target), dimension on rows |

Clicking a template populates the zone mapping. The user can adjust assignments before inserting.

---

### 4.3 Ask Tessallite (Conversational Analytics)

**Description**: A chat interface embedded in the task pane that connects to the Tessallite agent-service. Users type natural-language questions and receive answers with governed data. Answers can be inserted into the worksheet as formatted tables, charts, local PivotTables from inserted results, or CUBE formula scaffolds. This is the primary and default path from question to Excel output.

**Default landing**: After successful login, the task pane opens in Ask Tessallite unless the user has explicitly chosen and persisted another mode. The empty state should encourage immediate business questions, not browsing model metadata.

**API calls**:
- `GET /api/v1/projects/{id}/agent/config` -- verify agent is configured, check LLM provider
- `GET /api/v1/projects/{id}/agent/personas` -- list agent personas for behaviour tailoring
- `POST /api/v1/projects/{id}/agent/conversations` -- create conversation
- `GET /api/v1/projects/{id}/agent/conversations` -- list conversation history
- `GET /api/v1/projects/{id}/agent/conversations/{cid}` -- load conversation
- `POST /api/v1/projects/{id}/agent/conversations/{cid}/messages` -- send message (sync)
- `POST /api/v1/projects/{id}/agent/conversations/{cid}/messages/stream` -- send message (SSE streaming)
- `POST /api/v1/projects/{id}/agent/conversations/{cid}/messages/feedback` -- submit feedback

**Chat behavior**:

1. On first opening, call `GET .../agent/config` to verify the agent is configured. If not configured, show: "Conversational analytics is not configured for this project. Contact your Tessallite administrator." with a disabled input bar.
2. If configured, show the provider and model name as a badge in the conversation header (e.g., `[Claude 3.5 Sonnet]`).
3. User selects an agent persona from `GET .../agent/personas` (a dropdown in the header -- e.g., "Explains in simple terms" or "Technical detail").
4. User types a question (e.g., "What was revenue by country last quarter?").
5. Plugin sends to agent-service via SSE streaming.
6. Agent processes: plans, queries semantic layer, narrates answer.
7. Response includes: narration text, query result rows, semantic query, and judge verdict (if configured).
8. Plugin displays narration text and a data preview table with action buttons.
9. User clicks an insertion action to write results to the sheet.

**Insertion actions from agent responses**:

| Button | Behavior |
|---|---|
| **Insert as Table** | Writes the result rows as a formatted Excel Table on a new or existing sheet |
| **Insert Chart** | Writes the result rows as a table if needed, then creates a native Excel chart from the inserted range |
| **Insert Local PivotTable** | Writes the result rows as a table, then creates a local PivotTable from that table where Office.js supports local PivotTable creation |
| **Insert CUBE Formulas** | Converts the response metadata into a CUBE formula scaffold when the answer maps cleanly to model members and a workbook XMLA connection exists |
| **Create Live Connection** | Creates or verifies the XMLA connection and guides the user through Excel's native external PivotTable flow |
| **Show Query** | Expands a code block showing the semantic query and physical SQL |

**Dimensional structure detection**: When the agent response includes metadata about which columns are measures vs dimensions, the plugin automatically detects the report pattern (e.g., "time series by region") and pre-selects the most appropriate insertion action. If the response is a single value, "Insert as Table" is the default action label in Ask. If it is a multi-column result set with clear dimensional structure, "Insert Local PivotTable" or "Insert Chart" is highlighted. If the response maps to model members and measures, "Insert CUBE Formulas" is also available.

**Additional chat features**:
- Conversation history (persisted by agent-service, loaded on reopen)
- Agent persona selector (dropdown from `GET .../agent/personas`)
- LLM provider badge showing configured provider and model
- Feedback buttons (thumbs up/down, mapped to `POST .../feedback`)
- Judge verdict display (when available -- confidence score and evaluation)
- Follow-up question suggestions based on context, shown as clickable chips below the response
- Streaming: text appears token by token via SSE `narration.delta` events; data table appears with skeleton placeholder until `query.rows` arrives
- Suggested prompts should be business-facing ("Show this by region", "Compare to last quarter", "Show top 10 customers") and should not expose semantic-query syntax

---

### 4.4 Cube Function Generator

**Description**: A wizard that generates CUBE formulas based on user selections. This is a secondary interaction for power users who need specific formulas rather than full PivotTable reports.

**Formula types supported**:
- `CUBEMEMBER(connection, member_expression)` -- single member
- `CUBEVALUE(connection, member_expression, [filter1], [filter2], ...)` -- aggregated value
- `CUBESET(connection, set_expression, [caption], [sort_order], [sort_by])` -- set of members
- `CUBERANKEDMEMBER(connection, set_expression, rank)` -- nth member of a set
- `CUBEKPIMEMBER(connection, kpi_name, kpi_property)` -- KPI member

**Wizard steps**:

1. **Select Measure**: Dropdown listing all measures in the active model, pre-populated if triggered from a specific measure card
2. **Select Dimensions**: Row dimension selector, optional filter rows with dimension + member pickers (member selector loads asynchronously via `POST /api/v1/discover/members` with search as the user types)
3. **Preview**: Generated formula displayed in monospace, target cell auto-populated from `Workbook.getActiveCell()`

**Connection check**: Before insertion, the wizard verifies that a workbook XMLA connection to Tessallite exists. If not, it offers "Create live connection" and prompts for credentials at that moment. Formula correctness is guaranteed by the wizard's structured generation from model metadata (measures, dimensions, members) rather than by backend parsing.

**Triggered by**: "Insert formula" button on measure cards in Report Builder, "Tessallite: Insert measure" context menu, or keyboard shortcut `Ctrl+Shift+M`.

---

### 4.5 Drill-Through Panel

**Description**: When a user double-clicks a PivotTable value cell or selects a CUBEVALUE cell, the plugin opens a drill-through panel showing the underlying detail rows via Tessallite's hierarchy-aware drill-through API.

**API calls**:
- `POST /api/v1/measures/{measure_id}/drill-options` -- get available drillable hierarchies for the cell
- `POST /api/v1/measures/{measure_id}/drill-through` -- execute drill-through with cell context

**Behavior**:
1. User selects a cell in a PivotTable that shows a measure value
2. Plugin reads the cell context via Excel JS API (PivotTable name, cell address)
3. Plugin resolves the measure and dimension members from the PivotTable layout
4. Calls `drill-options` to show available drill-down paths
5. User selects a path (or the default is used)
6. Calls `drill-through` with the cell context, retrieves detail rows
7. Displays rows in a scrollable table in a slide-in panel
8. "Insert to Sheet" writes the detail rows as an Excel Table on a new sheet

**DrillThroughSet integration**: The plugin reads the measure's `drill_through_set` configuration (`GET .../measures/{id}/drill-through-set`) to know which columns to display in the detail view.

**Breadcrumb navigation**: The drill-through panel header shows the cell context as clickable breadcrumbs: `Revenue > Country: US > Date: 2025-Q4`. Clicking a breadcrumb level navigates back up to that summarisation level.

**Note on Excel's native Show Details**: The plugin supplements, not replaces, Excel's native Show Details double-click action. Both will fire on a double-click. Users can disable Excel's native Show Details in PivotTable Options > Data > "Enable show details" if they prefer the plugin's governed drill-through exclusively.

---

### 4.6 Glossary Lookup

**Description**: Contextual, on-demand lookup of business glossary definitions. When the user clicks a measure or dimension card in the Report Builder, or selects a cell containing a CUBE formula, the plugin shows the glossary definition in a popover.

**API calls**:
- `GET /api/v1/projects/{id}/models/{model_id}/glossary` -- list glossary entries
- `GET /api/v1/glossary/public/{token}` -- public glossary (no auth, when shared)

**Contextual lookup triggers**:
- Clicking a measure/dimension card in Report Builder -- popover anchored to the card
- Selecting a cell containing a CUBE formula -- popover anchored to the footer or shown as a slide-in
- Right-click > "Tessallite: Look up in glossary" -- popover at cursor position
- Clicking the glossary icon in the header bar -- opens a searchable glossary list as a modal overlay

**Popover content**:
- Term name
- Definition text
- Context notes (if available)
- Source badge (User-written / LLM-generated / LLM-approved)
- Sample values (formatted according to measure format token)
- Synonyms list
- Status badge (approved / pending / rejected)
- "View in Report Builder" link that navigates to the corresponding measure/dimension

**Searchable glossary list** (accessible from header icon): A modal overlay with full-text search across `term`, `definition`, and `synonyms`. Filtered by type (All / Measures / Dimensions) and source (All / User / LLM / LLM Approved). Clicking an entry navigates to and highlights the corresponding item in the Report Builder.

---

### 4.7 Persona Switcher

**Description**: A dropdown in the task pane footer that shows available personas for the active model and allows switching between them. This provides role-scoped views of the semantic model directly in Excel.

**API calls**:
- `GET /api/v1/projects/{id}/models/{model_id}/personas` -- list personas
- `GET /api/v1/projects/{id}/agent/personas` -- list project-level personas

**Behavior**:
1. User selects a persona from the footer dropdown
2. Plugin filters Report Builder metadata, glossary lookup, Ask Tessallite context, and CUBE formula generation to the persona scope
3. Existing plugin-inserted result tables and charts are not silently changed; they remain workbook artifacts until the user refreshes/re-runs them
4. For CUBE formulas, newly generated formulas use the persona catalog string (`{model_slug}_{persona_slug}`) when applicable
5. For native XMLA PivotTables, the plugin offers a "Create persona connection" action and instructs the user to use Excel's native connection/PivotTable flow. Existing live PivotTables may require reconnecting or switching to the persona catalog through Excel's connection UI
6. Persona description is shown below the dropdown

**Persona info displayed**:
- Name, slug, description
- Measure count / Dimension count
- Audience roles (which user roles this persona targets)

**Business-user display rule**: The default persona dropdown must not expose internal/security implementation flags such as `includes_hidden_columns` or `bypass_row_security`. If a selected persona has a security-sensitive warning, show a generic admin-facing warning only in the settings/detail popover: "This persona has elevated model visibility. Contact your Tessallite administrator if this is unexpected."

**Info bar**: When a non-default persona is active, an info bar appears above the content area: "Viewing as 'Executive'. 23 of 45 measures shown. [Switch to Default]".

**Note on the `_technical` persona**: The gateway automatically seeds a `_technical` persona per model. This persona should appear in the switcher and is labelled as "[Technical]" with the audience badge.

---

### 4.8 Excel Output Contract

All insert actions must create native Excel artifacts. The task pane is a control surface, not the destination for analysis.

| Output Type | Created By | Data Source | Refresh Behavior | Notes |
|---|---|---|---|---|
| **Formatted Excel Table** | Ask response, Report Builder layout, drill-through | Tessallite REST query result rows | Re-run through plugin action; optional "Refresh this result" metadata can be stored in worksheet custom properties | MVP default output because it is reliable across platforms |
| **Native Excel Chart** | Ask response or Report Builder chart action | Inserted worksheet table/range | Refresh when the backing range/table is refreshed | Do not render charts only inside the task pane |
| **Local Excel PivotTable** | Ask response or Report Builder layout | Inserted worksheet table/range | Refreshes from the local result table | This is not a live XMLA PivotTable; label UI as "Local PivotTable" |
| **CUBE Formula Range** | Cube Function Wizard, Report Builder formula action, eligible Ask response | Existing XMLA workbook connection | Live against XMLA connection when workbook recalculates/refreshes | Requires an XMLA connection and correct member expressions |
| **Live XMLA PivotTable** | Excel native Insert > PivotTable flow after plugin creates/verifies connection | MSOLAP connection to Tessallite gateway | Live against Tessallite XMLA on PivotTable refresh | Plugin provides connection helper and instructions; it does not create the external PivotTable directly |

**Implementation rules:**
- Every insertion action must state whether the output is live against Tessallite or a local snapshot from a Tessallite query result.
- Use "Local PivotTable" for PivotTables created from inserted worksheet ranges.
- Use "Live PivotTable connection" for XMLA/MSOLAP workflows.
- Store enough metadata for plugin-created tables to support future refresh: project id, model id, persona id, semantic query, source conversation id/turn id when applicable, creation timestamp, and plugin version.
- Do not overwrite active cells without an explicit replace confirmation.
- For large result sets, cap preview rows in the task pane and ask confirmation before inserting more than 10,000 rows.
- If the user's selected insertion target overlaps existing data, show a range-conflict dialog with "Insert on new sheet" as the default action.

---

## 5. API Surface Required

The plugin consumes the following Tessallite APIs. All calls use HTTPS with JWT bearer authentication.

### 5.1 Model Service (:8001)

| Endpoint | Method | Used By Feature |
|---|---|---|
| `/api/v1/auth/login` | POST | Authentication |
| `/api/v1/auth/logout` | POST | Authentication |
| `/api/v1/auth/users/me` | GET | Authentication |
| `/api/v1/tenants/me` | GET | Connection management |
| `/api/v1/projects` | GET | Report Builder |
| `/api/v1/projects/{id}` | GET | Report Builder |
| `/api/v1/projects/{id}/models` | GET | Report Builder |
| `/api/v1/projects/{id}/models/{mid}` | GET | Report Builder (deployment status) |
| `/api/v1/projects/{id}/models/{mid}/measures` | GET | Report Builder |
| `/api/v1/projects/{id}/models/{mid}/measures/{mid2}` | GET | Report Builder (measure detail) |
| `/api/v1/projects/{id}/models/{mid}/measures/{mid2}/available-variants` | GET | Report Builder (variant display) |
| `/api/v1/projects/{id}/models/{mid}/dimensions` | GET | Report Builder |
| `/api/v1/projects/{id}/models/{mid}/dimensions/{did}` | GET | Report Builder (dimension detail) |
| `/api/v1/projects/{id}/models/{mid}/hierarchies` | GET | Report Builder |
| `/api/v1/projects/{id}/models/{mid}/hierarchies/{hid}` | GET | Report Builder (hierarchy detail) |
| `/api/v1/projects/{id}/models/{mid}/personas` | GET | Persona Switcher |
| `/api/v1/projects/{id}/models/{mid}/personas/{pid}` | GET | Persona Switcher (detail) |
| `/api/v1/projects/{id}/models/{mid}/personas/{pid}/tag-restrictions` | GET | Persona Switcher (tag filters) |
| `/api/v1/projects/{id}/models/{mid}/glossary` | GET | Glossary Lookup |
| `/api/v1/glossary/public/{token}` | GET | Glossary Lookup (public) |
| `/api/v1/projects/{id}/models/{mid}/measures/{mid2}/drill-through-set` | GET | Drill-Through |
| `/api/v1/projects/{id}/models/{mid}/measures/{mid2}/drill-through-set/join-paths` | GET | Drill-Through |
| `/api/v1/projects/{id}/models/{mid}/alias-map` | GET | Report Builder (search) |
| `/api/v1/projects/{id}/models/{mid}/data-tags` | GET | Report Builder (tag display) |
| `/api/v1/system/settings` | GET | Version compatibility check |

### 5.2 Query Router (:8002)

| Endpoint | Method | Used By Feature |
|---|---|---|
| `/api/v1/execute` | POST | Ask/Report Builder result table insertion, Cube Function Generator preview |
| `/api/v1/explain` | POST | On-demand query trace (settings menu) |
| `/api/v1/validate` | POST | Cube Function Generator |
| `/api/v1/discover/members` | POST | Report Builder (member preview) |
| `/api/v1/measures/{id}/drill-options` | POST | Drill-Through |
| `/api/v1/measures/{id}/drill-through` | POST | Drill-Through |
| `/api/v1/diagnostics/query-rewrites` | GET | On-demand query trace |

### 5.3 Agent Service (:8005)

| Endpoint | Method | Used By Feature |
|---|---|---|
| `/api/v1/projects/{id}/agent/config` | GET | Ask Tessallite (config check, LLM provider) |
| `/api/v1/projects/{id}/agent/models` | GET | Ask Tessallite (model scope) |
| `/api/v1/projects/{id}/agent/personas` | GET | Ask Tessallite (agent persona selector) |
| `/api/v1/projects/{id}/agent/conversations` | POST | Ask Tessallite |
| `/api/v1/projects/{id}/agent/conversations` | GET | Ask Tessallite (history) |
| `/api/v1/projects/{id}/agent/conversations/{cid}` | GET | Ask Tessallite |
| `/api/v1/projects/{id}/agent/conversations/{cid}` | DELETE | Ask Tessallite |
| `/api/v1/projects/{id}/agent/conversations/{cid}/turns` | GET | Ask Tessallite (history) |
| `/api/v1/projects/{id}/agent/conversations/{cid}/messages` | POST | Ask Tessallite (sync) |
| `/api/v1/projects/{id}/agent/conversations/{cid}/messages/stream` | POST | Ask Tessallite (SSE) |
| `/api/v1/projects/{id}/agent/conversations/{cid}/messages/feedback` | POST | Ask Tessallite (feedback) |

### 5.4 Gateway (:8080)

| Endpoint | Method | Used By Feature |
|---|---|---|
| `/api/v1/xmla/` | POST | PivotTable operations (via MSOLAP, not plugin) |
| `/health` | GET | Connection health check |

No new APIs need to be built. The plugin is a pure consumer of existing Tessallite endpoints.

---

## 6. UI Layout and Interaction Design

### 6.1 Task Pane Layout (360px wide)

```
+------------------------------------------+
| [T] Tessallite     [model v]  [?] [gear] |  <-- header (48px)
+------------------------------------------+
| [ Ask Tessallite ]  [ Report Builder ]   |  <-- mode switcher (36px)
+------------------------------------------+
|                                          |
|  (Active mode content fills this area)   |
|                                          |
|  Ask Tessallite mode (default):          |
|  +------------------------------------+  |
|  | Ask a question about your data...  |  |
|  | [What drove revenue last quarter?] |  |
|  |                                    |  |
|  | Answer + result preview            |  |
|  | [Insert Table] [Chart] [Local PT]  |  |
|  +------------------------------------+  |
|                                          |
|  Report Builder mode:                    |
|  +------------------------------------+  |
|  | REPORT LAYOUT          [templates] |  |
|  | FILTERS: [none]                    |  |
|  | COLUMNS: [none]   VALUES: [none]   |  |
|  | ROWS: [none]                       |  |
|  +------------------------------------+  |
|  | Search: [________________]         |  |
|  +------------------------------------+  |
|  | MEASURES (12)              [fold]  |  |
|  | [x] Revenue (sum, currency)        |  |
|  |     "Total revenue..."  [+ Values] |  |
|  | [x] Revenue YoY % [variant]        |  |
|  |     [+ Values]                     |  |
|  | [ ] Order Count (count) [+ Values] |  |
|  +------------------------------------+  |
|  | DIMENSIONS (5)             [fold]  |  |
|  | Country [string]                    |  |
|  |   [->Rows] [->Cols] [->Filt] [->Sl]|  |
|  | Order Date [time] [fiscal: Jul]     |  |
|  +------------------------------------+  |
|  | HIERARCHIES (2)            [fold]  |  |
|  | [>] business_date_h                |  |
|  |     Year > Qtr > Month > Day       |  |
|  +------------------------------------+  |
|                                          |
+------------------------------------------+
| Persona: [Executive v]  | Connected [.] |  <-- footer (28px)
+------------------------------------------+
```

### 6.2 Mode Switcher

Two modes instead of five tabs:

| Mode | Icon | Content |
|---|---|---|
| **Ask Tessallite** | Chat bubble | Conversational agent chat interface with Insert Table/Chart/Local PivotTable/CUBE Formula actions |
| **Report Builder** | Table chart | Report layout mapping + measure/dimension/hierarchy libraries + templates |

### 6.3 Contextual Overlays

These appear on demand, not as permanent tabs:

| Overlay | Trigger | Content |
|---|---|---|
| **Glossary popover** | Click measure/dimension card, CUBE cell, or header icon | Definition, synonyms, sample values, source/status badges |
| **Drill-Through panel** | Double-click PivotTable value cell, right-click menu | Detail rows with breadcrumb navigation and hierarchy path selector |
| **Cube Function Wizard** | "Insert formula" button, context menu, Ctrl+Shift+M | Step-by-step formula generation with validation |
| **Query Trace modal** | Settings > "View last query trace" | Pipeline steps, route decision, original/rewritten SQL, performance stats |

### 6.4 Context Menu Additions

| Context Menu Item | Appears When | Action |
|---|---|---|
| "Tessallite: Drill through" | Cell is a PivotTable value cell | Opens Drill-Through panel |
| "Tessallite: Look up in glossary" | Cell contains CUBEMEMBER or is PivotTable header | Opens glossary popover for the term |
| "Tessallite: Insert measure" | Any cell | Opens Cube Function Wizard |

### 6.5 Ribbon Integration

The plugin adds a ribbon group "Tessallite" with three buttons:

| Button | Action |
|---|---|
| **Connect** | Open login screen (if not authenticated) or profile switcher |
| **Ask** | Open task pane on Ask Tessallite mode |
| **Report Builder** | Open task pane on Report Builder mode |

---

## 7. Installation and Distribution

### 7.1 Sideloading (Development)

1. Build the React SPA (`npm run build`)
2. Host on a reachable HTTPS URL (Vite dev server with `@vitejs/plugin-basic-ssl` on `localhost:3000`, or nginx reverse proxy)
3. Update `manifest.xml` `SourceLocation` to point to the hosted URL
4. Place `manifest.xml` on a network share or SharePoint catalog
5. In Excel: File > Options > Trust Center > Trust Center Settings > Trusted Add-in Catalogs > add the network share path
6. Load via Insert > Add-ins > My Add-ins > Shared Folder > "Tessallite"

### 7.2 Centralized Deployment (Enterprise)

- Publish to a corporate catalog (SharePoint, network share)
- Admin deploys via Microsoft 365 Admin Center > Integrated Apps
- Users see "Tessallite" in their Insert > Add-ins ribbon

### 7.3 AppSource (Public)

- Submit to Microsoft AppSource for public distribution
- Requires Microsoft Partner Center registration

### 7.4 File Structure

```
tessallite/excel-plugin/
  manifest.xml
  package.json
  tsconfig.json
  vite.config.ts
  src/
    main.tsx
    App.tsx
    api/
      auth.ts
      modelService.ts
      queryRouter.ts
      agentService.ts
      gateway.ts
      discover.ts
      diagnostics.ts
    components/
      LoginScreen.tsx
      ProfileSwitcher.tsx
      common/
        StatusBadge.tsx
        SearchBar.tsx
        SectionHeader.tsx
        CodeBlock.tsx
        LoadingSkeleton.tsx
        EmptyState.tsx
      ReportBuilder/
        ReportBuilder.tsx
        ZoneMappingGrid.tsx
        MeasureLibrary.tsx
        DimensionLibrary.tsx
        HierarchyLibrary.tsx
        MeasureCard.tsx
        DimensionCard.tsx
        HierarchyCard.tsx
        TemplatePicker.tsx
        TemplateCard.tsx
      AskTessallite/
        ChatPanel.tsx
        ChatMessage.tsx
        JudgeVerdict.tsx
        InsertActions.tsx
      DrillThrough/
        DrillPanel.tsx
        DrillPathPicker.tsx
      Glossary/
        GlossaryPopover.tsx
        GlossarySearchModal.tsx
      PersonaSwitcher/
        PersonaDropdown.tsx
      CubeFunctions/
        CubeFormulaWizard.tsx
      QueryTrace/
        TraceModal.tsx
    hooks/
      useAuth.ts
      useModel.ts
      useExcel.ts
      useConnectionStatus.ts
      useExcelEvents.ts
    utils/
      excelFormulas.ts
      cubeMemberParser.ts
      connectionStrings.ts
      diagnostics.ts
    types/
      tessallite.ts
  public/
    assets/
      icon-16.png
      icon-32.png
      icon-80.png
      icon-128.png
```

---

## 8. Security Model

### 8.1 Authentication Flow

1. User enters email, password, tenant_id, and server URL in the login form
2. Plugin calls `POST /api/v1/auth/login` with `{tenant_id, email, password}`
3. Plugin clears the password value from component state after the request completes
4. Plugin extracts the JWT (from `Set-Cookie` header or response body, depending on backend option)
5. JWT is stored in `OfficeRuntime.storage` (encrypted at rest by Office)
6. All subsequent API calls include `Authorization: Bearer {jwt}` header

**Note**: The `csrf_token` cookie is not needed when using Bearer token auth. The plugin does not rely on cookie-based session management.

### 8.2 XMLA Authentication

The XMLA connection (for live PivotTables and CUBE formulas) uses Basic Authentication over HTTPS. The plugin may construct a connection string with `User ID={email};Password={password}` only after the user explicitly enters credentials in a connection-creation dialog. The add-in must not store the XMLA password in `OfficeRuntime.storage`, `localStorage`, workbook custom properties, logs, diagnostics, telemetry, or React Query cache.

### 8.3 Cross-Origin Considerations

Tessallite services must include CORS headers:
- `Access-Control-Allow-Origin: {plugin_origin}`
- `Access-Control-Allow-Methods: GET, POST, PUT, PATCH, DELETE, OPTIONS`
- `Access-Control-Allow-Headers: Authorization, Content-Type`
- `Access-Control-Allow-Credentials: true`

### 8.4 Credential Storage

- JWT: `OfficeRuntime.storage` (encrypted, persisted per user per device)
- Connection profiles: `OfficeRuntime.storage`
- Password storage: forbidden. There is no "Remember password" option in the add-in.

### 8.5 Credential Cleanup on Sign-Out

On sign-out, the plugin removes the JWT from `OfficeRuntime.storage`. If an auto-created XMLA connection exists, it is removed from the workbook via `Workbook.connections.getItem(connectionId).delete()`. If the user manually created the connection, a prompt appears: "Remove the Tessallite workbook connection? [Yes] [No]".

### 8.6 Row Security

The plugin does not bypass Tessallite's row security. All API calls include the JWT, which drives row security rules. PivotTable queries through the XMLA gateway also enforce row security.

---

## 9. Caching and Performance Strategy

### 9.1 Metadata Caching

Model metadata (measures, dimensions, hierarchies, personas, glossary) is cached with version-based invalidation:
1. On first load, fetch all metadata and store in `localStorage` with `deployed_version_id` as the cache key
2. On subsequent loads, fetch only the model summary to check `deployed_version_id`
3. If version matches, use cached data; otherwise, fetch fresh and update cache
4. Glossary entries are cached separately with a 5-minute TTL

### 9.2 API Call Deduplication

Use TanStack Query (React Query) for:
- Automatic request deduplication across components
- Stale-while-revalidate pattern for background refreshes
- Retry with exponential backoff on transient failures (3 retries, 1s/2s/4s intervals)

### 9.3 Virtualized Lists

For models with 100+ measures or dimensions, use `react-window` to render only visible items.

### 9.4 Request Retry and Resilience

- All API calls retry on 5xx and network errors (3 retries, exponential backoff)
- SSE reconnection for chat streaming: exponential backoff (1s, 2s, 4s, max 30s)
- Stale JWT detection: on any 401, redirect to login
- Offline banner: "Connection lost. [Retry]" when health check fails 3 consecutive times

---

## 10. Development Phases

### Phase 1: Foundation + Ask MVP -- 3-4 weeks

**Goal**: Users can authenticate, ask a governed question, and insert the answer into Excel as a native table.

**Features**:
- 4.1 Authentication and Connection Management
- 4.3 Ask Tessallite with SSE streaming, response preview, Insert as Table, and feedback
- Metadata loading for active project/model/persona
- Basic glossary lookup from Ask response columns/measures
- Excel output metadata stored for inserted result tables
- No password persistence

### Phase 2: Report Builder + CUBE Formulas -- 2-3 weeks

**Goal**: Power users can compose structured outputs from semantic building blocks and generate live CUBE formulas.

**Features**:
- 4.2 Report Builder with compact measure/dimension/hierarchy libraries, search, quick actions, and templates
- 4.4 Cube Function Generator for CUBEMEMBER/CUBEVALUE
- XMLA connection helper with explicit credential prompt and native Excel PivotTable instructions
- Contextual glossary popover and searchable glossary modal

### Phase 3: Charts, Local PivotTables, Persona, Drill-Through -- 2-3 weeks

**Goal**: Insert richer Excel-native artifacts and support governed exploration from existing workbook cells.

**Features**:
- Insert Chart from Ask and Report Builder results
- Insert Local PivotTable from inserted worksheet result tables where Office.js supports it
- 4.7 Persona Switcher
- 4.5 Drill-Through Panel
- Context menu integration
- Ribbon integration

### Phase 4: Polish and Distribution -- 3-4 weeks

**Goal**: Production readiness, enterprise deployment.

**Features**:
- Error handling and retry logic
- Offline detection and resilience (SSE reconnection, stale JWT detection)
- Performance optimization (metadata caching, virtualized lists, React Query)
- Accessibility (keyboard navigation, screen reader, WCAG 2.1 AA)
- On-demand query trace modal (settings menu)
- Version compatibility check (plugin version vs server version)
- Client-side diagnostics log ("Copy Diagnostics" for support)
- Enterprise deployment documentation
- AppSource submission (optional)

---

## 11. Glossary

| Term | Definition |
|---|---|
| **XMLA** | XML for Analysis. A SOAP protocol for OLAP data access. Used by Excel to connect to Analysis Services and Tessallite. |
| **MSOLAP** | Microsoft OLE DB Provider for Analysis Services. The provider Excel uses to speak XMLA. |
| **CUBE functions** | Excel functions (CUBEMEMBER, CUBEVALUE, CUBESET, etc.) that query OLAP data sources. |
| **Task Pane** | A side panel in Office applications that hosts web content (Office Add-in). |
| **Office Add-in** | A web application hosted inside Office using the Office JavaScript API. |
| **Persona** | A scoped view of a Tessallite model that filters visible measures, dimensions, and hierarchies based on user role. |
| **Glossary** | A collection of business definitions attached to measures and dimensions in the semantic model. |
| **Drill-through** | The ability to see underlying detail rows behind an aggregated value. |
| **Variant measure** | A time-intelligence measure derived from a base measure (e.g., Year-over-Year growth, Year-to-Date). |
| **PivotTable zone** | One of four areas in a PivotTable: Filters, Columns, Rows, Values. |
| **Report template** | A pre-configured PivotTable layout pattern for common analytical scenarios. |
