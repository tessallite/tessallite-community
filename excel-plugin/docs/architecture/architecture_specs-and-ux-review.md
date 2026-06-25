# Tessallite Excel Plugin -- Purpose & UX Review

Re-evaluation of `architecture_specs.md` and `architecture_frontend-design.md` against the correct product purpose: **an analytics workbench that brings Tessallite's data capabilities into Excel, not a recreation of the Tessallite web frontend.**

Status: active  
Date: 2026-05-18

---

## Table of Contents

1. [Purpose Correction](#1-purpose-correction)
2. [The Current Specs: What Is a Web Frontend Clone](#2-the-current-specs-what-is-a-web-frontend-clone)
3. [What the Plugin Should Do](#3-what-the-plugin-should-do)
4. [Feature-by-Feature Reassessment](#4-feature-by-feature-reassessment)
5. [Structural Changes Needed](#5-structural-changes-needed)
6. [New: Feature Proposals for the Analytics Workbench](#6-new-feature-proposals-for-the-analytics-workbench)
7. [What to Remove or Demote](#7-what-to-remove-or-demote)
8. [Revised Tab/Flow Architecture](#8-revised-tabflow-architecture)
9. [Impact on SPECS.md Sections](#9-impact-on-specsmd-sections)
10. [Impact on FRONTEND-DESIGN.md Sections](#10-impact-on-frontend-designmd-sections)
11. [Summary of Required Changes](#11-summary-of-required-changes)

---

## 1. Purpose Correction

The current specs describe a plugin that replicates Tessallite's web frontend capabilities inside Excel: a model browser tree, a health dashboard, a query trace debugger, an export panel, and a chat tab -- all arranged in a 5-tab task pane. This is architecturally a **miniature web frontend running in an Excel sidebar.**

The correct purpose is narrower and sharper:

> **The Excel plugin is an analytics workbench.** It lets Excel users leverage Tessallite's semantic layer to build reports, PivotTables, and PivotCharts using natural language (the conversational agent) and point-and-click selection from measure/dimension/hierarchy libraries. It provides glossary definitions so users understand what they're looking at. It does not monitor model health, debug query routing, export model definitions, or replicate admin/modeler workflows.

### Why this distinction matters

The strategy document (`docs/strategy/strategy_excel-strategy.md`) explicitly states: the native add-in is for **"guided analytics and AI-assisted querying -- the job PivotTables were not designed for."** It also warns:

> "Users will compare it to PivotTable for pivot-style work and find it lacking. Must position it for a different job (guided analytics, not ad-hoc slicing)."

Building a 5-tab mini-frontend with health dashboards and query traces positions the plugin as a "Tessallite admin console in Excel" -- which competes with the web frontend, bloats the task pane, and distracts from the core analytics workflow.

### The correct job to be done

| User | Job | Plugin's Role |
|---|---|---|
| Business analyst | "Build a quarterly revenue report by region" | Provide the measure/dimension/hierarchy building blocks, help compose them, insert results as PivotTable/PivotChart |
| Executive | "What were our top products last month?" | Let them ask the conversational agent, get an answer with data, and insert it into a sheet |
| Finance user | "I need a P&L view with YoY comparison" | Present measures organized by folder, let them compose a multi-measure layout with time variants |
| Anyone | "What does 'base_amount' mean?" | Show the glossary definition instantly, from the cell they're looking at |

None of these jobs require: a model health dashboard, a query trace panel, a version diff tool, or a model export dialog.

---

## 2. The Current Specs: What Is a Web Frontend Clone

The following elements in the current specs directly mirror the Tessallite web frontend and serve admin/modeler personas, not the Excel analytics user:

| Spec Element | Web Frontend Equivalent | Admin/Modeler? |
|---|---|---|
| Model Health Dashboard (SPECS 4.11, DESIGN sec 10) | `ModelHealthPanel.tsx` in Model Builder | Yes -- monitoring/ops |
| Query Trace (SPECS 4.10, DESIGN sec 11) | QueryPanel + DiagnosticsPanel | Yes -- debugging |
| Export & Snapshot (SPECS 4.12, DESIGN sec 15) | ExportPanel, VersionDiff | Yes -- modeler tool |
| 5-tab bar (Browse/Ask/Glossary/Health/Trace) | Tab-based navigation in web app | N/A -- structural mirroring |
| "Model Browser" tree view with project > model > measures/dimensions/hierarchies | Explorer page + Model Builder panels | Partially -- the tree structure mirrors the web app's navigation hierarchy |
| Source Statistics table showing row counts and sizes | StatisticsPanel | Yes -- ops/admin |
| Schema Changes list | SchemaChangesPanel | Yes -- modeler |
| Hierarchy Health section | HierarchyHealthPanel | Yes -- modeler |
| Version diff view | VersionsDialog | Yes -- modeler |
| Pocket Table Status section | PocketTablesPanel | Yes -- ops |
| Data Quality violations section | DataQualityPanel | Yes -- modeler/ops |

These collectively represent approximately **40% of the specified tabs and sections**. They belong in the web frontend, not in an Excel sidebar designed for analytics consumption.

---

## 3. What the Plugin Should Do

The plugin should support three core workflows:

### Workflow A: Natural Language to Report
```
User types question → Agent answers with data → User clicks "Insert to Sheet"
                                                        ↓
                                              PivotTable or Excel Table created
```
The conversational agent is the **primary entry point**. It is the fastest path from question to report.

### Workflow B: Manual Report Composition
```
User browses measures + dimensions + hierarchies → Selects what they want → Generates PivotTable/PivotChart/Formulas
```
Building block libraries (measures, dimensions, hierarchies) serve as a palette for report construction. Users pick items and the plugin inserts them into Excel as a structured report.

### Workflow C: Understanding Data
```
User clicks a cell with a CUBE formula or PivotTable value → Sees the glossary definition → Understands what the number means
```
Contextual glossary lookup eliminates confusion about opaque measure names.

### Supporting capabilities
- **Persona switching** (SPECS 4.9) -- scoped views are essential for analytics. Keep as-is.
- **Drill-through** (SPECS 4.7) -- seeing detail rows behind an aggregate is a core analytics action. Keep as-is.
- **Cube Function Wizard** (SPECS 4.5) -- generates formulas for advanced users. Keep as a secondary interaction.

---

## 4. Feature-by-Feature Reassessment

### 4.1 Authentication and Connection Management
**Verdict: KEEP AS-IS.** Authentication is table stakes. The current spec is thorough and technically accurate (JWT extraction from httpOnly cookies, `OfficeRuntime.storage`, profile management). No changes needed for purpose alignment.

### 4.2 Semantic Model Browser
**Verdict: RESTRUCTURE.** The current spec describes a full tree view (Project > Model > Measures | Dimensions | Hierarchies sections). This mirrors the web frontend's Explorer page.

**What to change:** Replace the tree with a **report builder layout**:
- A model selector at the top (single dropdown: "Revenue Model")
- Below it, three horizontally-scrollable or vertically-stacked building block lists:
  - **Measures** -- the "what to show" palette
  - **Dimensions** -- the "how to slice it" palette
  - **Hierarchies** -- the "how to drill" palette
- Each list is searchable and shows `display_name` + `effective_description` + relevant badges
- No project selector (assume one active project, or make it a secondary setting)
- No tree expansion/collapse of project nodes (that's file-system navigation, not report building)

The current tree view is useful for exploration but the primary pattern should be: "pick from these lists to build your report."

### 4.3 Measure Library with Drag-and-Drop
**Verdict: KEEP, with UX simplification.** The current spec correctly describes a searchable, annotated measure list. The two-click workflow (click measure, then click cell) is technically pragmatic. Variant grouping, cross-model badges, and semi-additive chips are useful.

**What to add:**
- **Multi-select**: checkboxes on measure cards so users can select multiple measures at once, then insert them all as a column set in one action
- **"Insert as PivotTable fields" action**: instead of writing individual CUBE formulas, add selected measures to the active PivotTable's Values area via Excel JS API
- **"Insert as PivotChart" action**: create a PivotChart alongside the PivotTable

### 4.4 Dimension Explorer with Hierarchy Navigation
**Verdict: KEEP, with UX simplification.** Dimension and hierarchy browsing is essential for report building. Member preview is valuable.

**What to add:**
- **"Use as Row/Column/Filter" action**: add the selected dimension to a specific area of the active PivotTable
- **"Use as Slicer" action**: insert the dimension as a slicer
- **Hierarchy expansion into PivotTable**: when a user expands a hierarchy and picks a level, insert it as a row/column hierarchy in the PivotTable

### 4.5 Cube Function Generator
**Verdict: KEEP as secondary feature.** The wizard is useful for power users who need specific formulas. However, it should not be the primary interaction pattern. Most users should build reports via the agent or the manual composition workflow.

### 4.6 Ask Tessallite (Conversational Analytics)
**Verdict: PROMOTE to primary feature.** This is the single most important tab. The current spec is good but positions it as "one of five tabs." It should be the **first and most prominent tab**, or even the default landing view after login.

**What to enhance:**
- Make it the default tab after login
- Add PivotTable/Chart generation: when the agent returns data, offer "Insert as PivotTable" and "Insert as PivotChart" alongside "Insert as Table"
- Add follow-up question suggestions based on the current context
- Show a prominent "New Report" button in the empty state: "Ask a question to generate a report instantly"

### 4.7 Drill-Through Panel
**Verdict: KEEP AS-IS.** This is a genuine analytics feature that XMLA cannot provide. The current spec is thorough.

### 4.8 Glossary Lookup
**Verdict: KEEP, with tighter integration.** Glossary is not a "reference tab" you browse idly -- it's a contextual lookup tool. The current spec already supports contextual lookup (click a cell, see the glossary entry), which is the right pattern.

**What to change:**
- Merge the standalone Glossary tab into a **tooltip/overlay pattern**: clicking a measure/dimension in the report builder or clicking a CUBE cell shows an inline glossary card, not a full-tab navigation
- Keep the searchable glossary list accessible but not as a primary tab -- perhaps as a button in the header bar or a slide-out from the report builder
- This eliminates one tab and keeps the user in their analytics flow

### 4.9 Persona Switcher
**Verdict: KEEP AS-IS.** Scoped views are essential for analytics consumption. The footer placement is correct. The info bar when a non-default persona is active is a good pattern.

### 4.10 Query Explanation and Trace
**Verdict: REMOVE as a permanent tab. Demote to optional debug overlay.** Query tracing is a diagnostic tool for data engineers, not a feature business analysts need when building reports. The strategy doc explicitly calls this out: the plugin is for "guided analytics, not ad-hoc slicing."

**What to do instead:**
- Remove the Trace tab from the main tab bar
- Add a "Why was my query slow?" button in the settings menu (gear icon) that opens a one-time trace overlay
- Keep the auto-trace mechanism (listens for PivotTable refresh, fetches rewrite) but surface it only on explicit request
- This eliminates a tab and removes the most "admin console" element from the main UI

### 4.11 Model Health Dashboard
**Verdict: REMOVE entirely from the plugin.** This is a monitoring tool for modelers and admins. The web frontend (`ModelHealthPanel.tsx`) is the correct surface for it. Excel users building reports do not need to know about stale aggregates, schema drift, hierarchy health issues, or data quality violations. If data is wrong, they will notice and report it -- they do not need a dashboard to pre-diagnose.

**Justification:**
- Adds 7 sections to the UI (deployment status, alerts, data quality, aggregate coverage, pocket tables, hierarchy health, schema changes, source statistics)
- Each section requires API calls, increases load time, and adds UI complexity
- The target user (business analyst) cannot act on any of this information -- they can't dismiss alerts, revalidate models, or fix schema drift from Excel
- This is the single largest source of "web frontend clone" bloat in the current specs

**What to keep:** Nothing from this feature. If a future version needs to surface a lightweight status indicator (e.g., "Model deployed" vs "Model not deployed"), that can be a single chip in the header bar, not a full tab.

### 4.12 Export and Snapshot
**Verdict: REMOVE from the plugin.** Model export (JSON/YAML), version history browsing, and version diff are modeler/admin tools. They belong in the web frontend's Model Builder, not in an Excel sidebar.

**Justification:** The strategic doc mentions model export from Excel as a workflow: "analysts working in Excel can share the model definition with data engineers." This is a theoretical workflow that adds 3 dialogs and multiple API calls for a use case that will occur rarely, if ever. The web frontend already handles this better.

---

## 5. Structural Changes Needed

### From 5 tabs to 2 primary surfaces + contextual overlays

| Current | Proposed | Rationale |
|---|---|---|
| Tab: Browse | **Surface: Report Builder** (default landing) | Measures, dimensions, hierarchies as building blocks in a composition-oriented layout |
| Tab: Ask | **Surface: Ask Tessallite** (alternate landing) | Conversational agent as the fast path to reports |
| Tab: Glossary | **Overlay: Glossary lookup** (contextual, not a full tab) | Glossary is a reference tool, not a workspace. Show on demand. |
| Tab: Health | **REMOVED** | Admin/modeler function |
| Tab: Trace | **Overlay: Query Trace** (settings menu item) | Diagnostic tool, accessed on demand |

### Proposed layout

```
+----------------------------------------------+
| HEADER BAR (48px -- reduced from 56px)        |
| [T logo] Tessallite    [model v] [glossary?] [gear] |
+----------------------------------------------+
| MODE SWITCHER (36px)                          |
| [ Report Builder ]  [ Ask Tessallite ]        |
+----------------------------------------------+
| CONTENT AREA                                  |
|  (Report Builder or Chat, depending on mode)  |
|                                               |
+----------------------------------------------+
| FOOTER BAR (28px -- reduced)                  |
| Persona: [Executive v] | Connected [dot]      |
+----------------------------------------------+
```

**Key changes from the current spec:**
- 5-tab bar replaced with a 2-mode switcher (Report Builder / Ask Tessallite)
- Header height reduced (48px instead of 56px) since there's no profile dropdown occupying space -- profile accessible via gear
- Footer reduced (28px instead of 32px)
- No dedicated Glossary, Health, or Trace tabs -- these become contextual overlays or are removed
- Model selector moved to the header bar (compact `Select` showing active model)

### Total saved: 1 tab bar row removed, multiple admin sections eliminated, ~40% less UI surface area

---

## 6. New: Feature Proposals for the Analytics Workbench

These features are not in the current specs but directly support the analytics workbench purpose:

### 6.1 Report Builder: Multi-Measure Selection and Batch Insert

**Description:** Users check multiple measure cards, then choose an insertion action: "Add to PivotTable Values", "Create PivotChart", or "Insert as CUBE formulas."

**UI:** Checkboxes on each measure card. A floating action bar appears at the bottom when 1+ measures are selected: `[2 selected] [Add to PivotTable] [Create PivotChart]`.

### 6.2 Report Builder: PivotTable Zone Mapping

**Description:** The Report Builder shows the four PivotTable zones (Filters, Columns, Rows, Values) as drop targets. Users select measures and dimensions, then assign them to zones. When done, the plugin generates or updates the PivotTable layout.

**UI:**
```
+----------------------------------------------+
| PIVOTTABLE LAYOUT                             |
| +------------------------------------------+ |
| | FILTERS                                  | |
| | [drop dimension here]                    | |
| +------------------------------------------+ |
| | COLUMNS              | VALUES            | |
| | [drop dim here]      | [drop measures]   | |
| +-----------------------+-------------------+ |
| | ROWS                                      | |
| | [drop dimensions/hierarchies here]        | |
| +------------------------------------------+ |
|                                              |
| [Insert PivotTable]  [Insert PivotChart]      |
+----------------------------------------------+
```

This is the core "report building" interaction. Users visualise the PivotTable structure before it exists, then generate it with one click.

**API:** Uses Excel JS API `PivotTable.layout` to configure zones, or falls back to creating CUBE formulas if the PivotTable already exists.

### 6.3 PivotChart Generation from Agent Responses

**Description:** When the conversational agent returns data, offer "Insert as PivotChart" alongside "Insert as Table." The plugin creates a PivotChart connected to the same data.

**UI:** Add a third button in the agent response actions: `[Insert to Sheet] [Insert as PivotChart] [Show Query]`.

**API:** Uses `Worksheet.charts.add()` with the inserted data table as the source range, or creates a PivotChart from the PivotTable if one was generated.

### 6.4 Report Templates

**Description:** Common report patterns pre-configured with typical measure/dimension assignments. Users pick a template, the plugin populates the layout.

**Templates:**
- "Time Series" -- date hierarchy on rows, 1-3 measures in values
- "Top N Breakdown" -- dimension on rows, 1 measure in values, top-10 filter
- "Comparison" -- two time periods side by side (time in columns with two members)
- "Geographic" -- geography dimension on rows, measures in values, map-capable
- "Variance" -- actual vs budget/target measures in values, dimension on rows

**UI:** A "Templates" button in the Report Builder header that opens a grid of template cards. Clicking one populates the PivotTable zone mapping.

### 6.5 Quick-Insert from Agent Responses

**Description:** When the agent returns data with a clear dimensional structure (e.g., revenue by country by quarter), detect the dimensional pattern and offer to build the corresponding PivotTable/PivotChart layout.

**Implementation:** The agent response includes metadata about which columns are measures vs dimensions. The plugin reads this and offers: "This looks like a time series by country. [Insert as PivotTable] [Insert as PivotChart]".

### 6.6 Contextual Glossary Overlay (not a tab)

**Description:** Instead of a full Glossary tab, show a compact glossary card as an overlay when:
1. The user clicks a measure/dimension in the Report Builder
2. The user selects a cell containing a CUBE formula
3. The user right-clicks and selects "Look up in glossary"

**UI:** A popover/tooltip anchored to the clicked element, showing: term, definition, source badge, synonyms, and sample values. Max width 280px, auto-dismiss on click outside. Includes a "View all glossary" link that opens a searchable list overlay.

### 6.7 Quick Actions from Report Builder

**Description:** Each dimension card in the Report Builder has quick-action buttons:
- `[-> Rows]` -- add to PivotTable rows
- `[-> Columns]` -- add to PivotTable columns
- `[-> Filter]` -- add to PivotTable filter area
- `[-> Slicer]` -- insert as a standalone slicer

Each measure card has:
- `[+ Values]` -- add to PivotTable values
- `[fx]` -- insert as CUBE formula at active cell

These replace the drag-and-drop interaction (which is technically problematic) with explicit, single-click actions.

---

## 7. What to Remove or Demote

| Element | Current Location | Action | Reason |
|---|---|---|---|
| Model Health tab | Tab 4 (Health) | **Remove entirely** | Admin/modeler function; not an analytics feature |
| Deployment Status card | Health tab | **Remove** | Admin function |
| Alerts section | Health tab | **Remove** | Admin/monitoring function |
| Data Quality section | Health tab | **Remove** | Modeler function |
| Aggregate Coverage section | Health tab | **Remove** | Ops function |
| Pocket Tables section | Health tab | **Remove** | Ops function |
| Hierarchy Health section | Health tab | **Remove** | Modeler function |
| Schema Changes section | Health tab | **Remove** | Modeler function |
| Source Statistics section | Health tab | **Remove** | Admin function |
| Query Trace tab | Tab 5 (Trace) | **Demote** to optional overlay (settings menu) | Diagnostic tool, not analytics |
| Export/Snapshot overlay | Overlay | **Remove entirely** | Modeler/admin tool |
| Version diff overlay | Overlay | **Remove entirely** | Modeler tool |
| Glossary as standalone tab | Tab 3 (Glossary) | **Demote** to contextual overlay + searchable list accessible from header | Reference tool, not a workspace |
| Project selector in Model Browser | Browse tab header | **Remove** from main UI, move to settings | Most users work with one project at a time |
| 5-tab bar | Global layout | **Replace** with 2-mode switcher | Reduces structural complexity |

### What stays

| Element | Location | Status |
|---|---|---|
| Login screen | Screen 1 | Keep as-is |
| Profile selector | Header dropdown | Keep as-is |
| Report Builder (measures + dimensions + hierarchies) | Primary surface | Restructured from "Model Browser" |
| Ask Tessallite (conversational agent) | Primary surface | Enhanced (see 6.3, 6.5) |
| Persona Switcher | Footer bar | Keep as-is |
| Cube Function Wizard | Overlay | Keep as secondary feature |
| Drill-Through Panel | Overlay | Keep as-is |
| Connection status indicator | Footer bar | Keep as-is |
| Context menus | Right-click menu | Keep as-is, remove "Explain this cell" |
| Ribbon buttons | Ribbon | Simplify to 3: Connect, Report Builder, Ask |

---

## 8. Revised Tab/Flow Architecture

### Primary surfaces (2)

1. **Report Builder** -- default landing. Shows:
   - Model selector (dropdown in header)
   - PivotTable zone mapping grid (top, collapsible)
   - Measure library (searchable, checkable, with quick-add buttons)
   - Dimension library (searchable, with zone-assignment buttons)
   - Hierarchy library (collapsible, with zone-assignment buttons)
   - "Insert PivotTable" / "Insert PivotChart" action buttons
   - Template picker (collapsed by default, accessible via button)

2. **Ask Tessallite** -- conversational agent. Shows:
   - Chat interface (current DESIGN section 8, with enhancements from 6.3 and 6.5)
   - Agent persona selector
   - LLM provider badge

### Contextual overlays (accessed on demand)

3. **Glossary lookup** -- popover/popup showing term definition, synonyms, sample values. Triggered by clicking a measure/dimension card, clicking a CUBE cell, or right-click > "Look up in glossary." Includes a "Search all glossary" link that opens a full searchable list modal.

4. **Drill-Through Panel** -- slide-in overlay, triggered by double-clicking a PivotTable value cell or context menu (keep as-is from current spec).

5. **Cube Function Wizard** -- modal overlay, triggered by "Insert formula" button on measure cards or context menu (keep as-is from current spec).

6. **Query Trace** -- modal overlay, triggered by Settings > "View last query trace." Shows the trace data from the most recent PivotTable refresh (keep the visual design from current spec but as a one-time modal, not a persistent tab).

### Removed

7. ~~Model Health~~ -- removed entirely
8. ~~Export/Snapshot~~ -- removed entirely
9. ~~Version Diff~~ -- removed entirely

### Ribbon buttons (simplified)

| Current | Proposed |
|---|---|
| Connect | **Connect** (keep) |
| Browse | **Report Builder** (renamed) |
| Ask | **Ask** (keep) |
| Refresh Trace | **REMOVED** |
| Settings | **Settings** (keep) |

### Context menu items (simplified)

| Current | Proposed |
|---|---|
| "Tessallite: Explain this cell" | **REMOVED** (trace is on-demand, not contextual) |
| "Tessallite: Drill through" | **KEEP** |
| "Tessallite: Look up in glossary" | **KEEP** |
| "Tessallite: Insert measure" | **KEEP** |
| "Tessallite: Refresh and trace" | **REMOVED** |

---

## 9. Impact on SPECS.md Sections

### Section 4: Feature Specifications

| Section | Action | Notes |
|---|---|---|
| 4.1 Authentication | Keep as-is | No changes needed |
| 4.2 Semantic Model Browser | **Rewrite** as "Report Builder" | Replace tree-with-sections with zone mapping + building block libraries |
| 4.3 Measure Library | **Rewrite** with multi-select, PivotTable insertion | Keep search, badges, variant grouping. Add checkboxes, quick-add buttons, batch insert |
| 4.4 Dimension Explorer | **Rewrite** with zone-assignment actions | Add "-> Rows", "-> Columns", "-> Filter", "-> Slicer" buttons |
| 4.5 Cube Function Wizard | Keep as-is | Secondary feature, fine as spec'd |
| 4.6 Ask Tessallite | **Enhance** | Add PivotTable/Chart generation, follow-up suggestions, default landing consideration |
| 4.7 Drill-Through Panel | Keep as-is | Fine as spec'd |
| 4.8 Glossary Lookup | **Rewrite** as contextual overlay | Replace standalone tab with popover/popup + searchable list modal |
| 4.9 Persona Switcher | Keep as-is | Fine as spec'd |
| 4.10 Query Trace | **Replace** with "On-demand query trace" | Modal overlay in settings, not a persistent tab |
| 4.11 Model Health | **Remove** | Remove entire section |
| 4.12 Export and Snapshot | **Remove** | Remove entire section |

### Section 5: API Surface Required

Remove endpoints related to removed features:
- Model Health endpoints (alerts, data quality, schema changes, hierarchy health, aggregate coverage, pockets, metrics, refresh runs, source statistics) -- approximately 20 endpoints removed
- Export/version/diff endpoints -- approximately 6 endpoints removed
- Agent KPIs endpoint (was for Health tab) -- 1 endpoint removed

Total: ~27 endpoints removed from the required API surface. This simplifies the plugin's dependency footprint significantly.

### Sections 6-10

| Section | Action |
|---|---|
| 6. UI Layout | **Rewrite** to show 2-mode layout with contextual overlays |
| 7. Installation | Keep as-is |
| 8. Security | Keep as-is |
| 9. Caching | Keep as-is (caching is still needed for measures, dimensions, hierarchies) |
| 10. Development Phases | **Restructure** (see below) |

### Revised Development Phases

**Phase 1: Foundation (3-4 weeks)** -- same as current
- Authentication
- Report Builder (measure + dimension + hierarchy libraries with quick-add buttons)
- Basic Cube Function Wizard

**Phase 2: Conversational Analytics (2-3 weeks)** -- enhanced
- Ask Tessallite with PivotTable/Chart generation from responses
- Contextual glossary overlay
- Report templates

**Phase 3: PivotTable Integration (2-3 weeks)** -- new, replaces current Phases 3a/3b
- Persona Switcher
- PivotTable zone mapping (multi-select + zone assignment)
- PivotChart generation
- Drill-Through Panel

**Phase 4: Polish (2-3 weeks)** -- replaces current Phase 4
- Context menu integration
- Ribbon integration
- On-demand query trace overlay
- Error handling, caching, accessibility

**Phase 5: Distribution (1-2 weeks)** -- replaces current Phase 5
- Enterprise deployment documentation
- AppSource submission (optional)

**Total: 10-15 weeks** (vs current 12-17 weeks). The simplification eliminates the most complex features (Health, Export, full Trace tab) and replaces them with analytics-focused additions that are smaller in scope.

---

## 10. Impact on FRONTEND-DESIGN.md Sections

### Sections to keep (with modifications noted)

| Section | Action |
|---|---|
| 1. Design Principles | **Rewrite** to reflect analytics workbench purpose |
| 2. Design Tokens | Keep as-is |
| 3. Global Layout | **Rewrite** for 2-mode layout |
| 4. Login | Keep as-is |
| 5. Profile Selector | Keep as-is |
| 6. Main Task Pane | **Rewrite** -- replace 5-tab bar with 2-mode switcher |
| 7. Model Browser | **Rewrite** as "Report Builder" (see section 6 of this review) |
| 8. Ask Tessallite | **Enhance** (see section 6.3, 6.5 of this review) |
| 12. Cube Function Wizard | Keep as-is |
| 13. Drill-Through Panel | Keep as-is |
| 14. Persona Switcher | Keep as-is |
| 16. Footer Bar | Keep as-is |
| 17. Context Menus | **Simplify** (remove "Explain this cell" and "Refresh and trace") |
| 18. Ribbon Buttons | **Simplify** (remove "Refresh Trace") |
| 19. Toast Notifications | Keep as-is |
| 20. Loading/Empty States | **Adjust** (remove empty states for removed features) |
| 21. Error States | Keep as-is |
| 22. Keyboard Shortcuts | **Simplify** (remove Trace/Health shortcuts, add Report Builder shortcut) |
| 23. Accessibility | Keep as-is |
| 24. Animation and Motion | Keep as-is |
| 25. Responsive Behavior | Keep as-is |

### Sections to remove or replace

| Section | Action |
|---|---|
| 9. Glossary (as a tab) | **Replace** with "Glossary Overlay" design (popover + searchable modal) |
| 10. Health | **Remove entirely** |
| 11. Trace (as a tab) | **Replace** with "Query Trace Overlay" design (modal accessed from settings) |
| 15. Export and Snapshot | **Remove entirely** |

### New sections to add

| Section | Content |
|---|---|
| **Report Builder: PivotTable Zone Mapping** | Design for the drag-and-drop-free zone assignment grid (Section 6.2 of this review) |
| **Report Builder: Templates** | Design for the template picker grid (Section 6.4 of this review) |
| **Report Builder: Quick Actions** | Design for the quick-add buttons on measure/dimension cards (Section 6.7 of this review) |
| **Glossary Popover** | Design for the contextual glossary popover/card (Section 6.6 of this review) |
| **Query Trace Modal** | Design for the on-demand trace modal (simplified from current Section 11) |

---

## 11. Summary of Required Changes

### What to add to the specs

1. **PivotTable zone mapping** -- visual layout builder for assigning measures/dimensions/hierarchies to PivotTable zones
2. **Multi-measure selection** with batch insert actions
3. **PivotChart generation** -- from agent responses and from manual selection
4. **Report templates** -- common patterns pre-configured for quick report creation
5. **Quick-add buttons** on measure/dimension/hierarchy cards ("-> Rows", "+ Values", etc.)
6. **Contextual glossary popover** -- replacing the standalone Glossary tab
7. **Agent response-to-PivotTable workflow** -- detecting dimensional structure and offering structured insertion

### What to remove from the specs

1. **Model Health Dashboard** (SPECS 4.11, DESIGN section 10) -- entirely
2. **Export and Snapshot** (SPECS 4.12, DESIGN section 15) -- entirely
3. **Query Trace as a permanent tab** (SPECS 4.10, DESIGN section 11) -- demote to optional overlay
4. **Glossary as a standalone tab** (SPECS 4.8, DESIGN section 9) -- demote to contextual overlay
5. **Project selector in main UI** -- move to settings (most users work with one project)
6. **5-tab bar** -- replace with 2-mode switcher

### What to restructure

1. **Model Browser** → **Report Builder** (building blocks + zone mapping, not a tree viewer)
2. **Measure Library** → enhanced with multi-select, batch insert, quick-add buttons
3. **Dimension Explorer** → enhanced with zone-assignment buttons
4. **Layout** → 2-mode (Report Builder / Ask Tessallite) instead of 5-tab
5. **Development phases** → simplified, removing admin features, adding analytics features

### Estimated net change

- **SPECS.md**: ~30% reduction in total content (removing ~300 lines of admin features, adding ~200 lines of analytics features)
- **FRONTEND-DESIGN.md**: ~35% reduction in total content (removing ~600 lines of Health/Trace/Export/Glossary tab designs, adding ~300 lines of Report Builder designs)
- **API surface**: ~27 endpoints removed from required list
- **Component count**: ~15 components removed (Health*8, Export*3, Glossary tab, Trace tab components), ~8 components added (zone mapping, templates, quick actions, popover)
- **Development timeline**: 10-15 weeks (vs 12-17 weeks), with higher-value features delivered earlier

---

*End of review.*
