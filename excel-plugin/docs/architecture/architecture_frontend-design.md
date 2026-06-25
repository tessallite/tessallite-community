# Tessallite Excel Plugin -- Frontend Design Document

Detailed UX and visual design specification for the Tessallite Excel plugin task pane. This document defines every screen, component, interaction pattern, state transition, and visual treatment. It is the single source of truth for implementation.

Companion to `SPECS.md`.
Status: active
Last updated: 2026-05-18

---

## Table of Contents

1. [Design Principles](#1-design-principles)
2. [Design Tokens](#2-design-tokens)
3. [Global Layout](#3-global-layout)
4. [Screen 1: Login](#4-screen-1-login)
5. [Screen 2: Profile Selector](#5-screen-2-profile-selector)
6. [Main Task Pane -- Mode Switcher](#6-main-task-pane----mode-switcher)
7. [Mode 1: Report Builder](#7-mode-1-report-builder)
8. [Mode 2: Ask Tessallite](#8-mode-2-ask-tessallite)
9. [Overlay: Cube Function Wizard](#9-overlay-cube-function-wizard)
10. [Overlay: Drill-Through Panel](#10-overlay-drill-through-panel)
11. [Overlay: Persona Switcher](#11-overlay-persona-switcher)
12. [Overlay: Glossary Popover](#12-overlay-glossary-popover)
13. [Overlay: Glossary Search Modal](#13-overlay-glossary-search-modal)
14. [Overlay: Query Trace Modal](#14-overlay-query-trace-modal)
15. [Footer Bar](#15-footer-bar)
16. [Context Menus](#16-context-menus)
17. [Ribbon Buttons](#17-ribbon-buttons)
18. [Toast Notifications](#18-toast-notifications)
19. [Loading and Empty States](#19-loading-and-empty-states)
20. [Error States](#20-error-states)
21. [Keyboard Shortcuts](#21-keyboard-shortcuts)
22. [Accessibility](#22-accessibility)
23. [Animation and Motion](#23-animation-and-motion)
24. [Responsive Behavior](#24-responsive-behavior)

---

## 1. Design Principles

| Principle | Guideline |
|---|---|
| **Insight workbench** | The plugin is an Excel-side insight accelerator, not a Tessallite frontend clone. Every UI element must help users ask a business question, understand the governed definition, and insert an Excel-native output. |
| **Tessallite-native** | Match the main Tessallite web frontend visually: same colors, typography, chip patterns, and status system from `frontend/src/theme/tokens.ts`. |
| **Task pane compact** | The task pane is 360px wide. Every screen is designed for this constraint: vertical stacking, collapsible sections, tight padding. |
| **Excel-first interaction** | The plugin uses Excel as the analysis canvas. Outputs are formatted tables, charts, local PivotTables from inserted result ranges, CUBE formulas, or a guided live XMLA connection flow. |
| **Ask-first workflow** | Ask Tessallite is the default path because it gives business users the shortest route from intent to worksheet output. Report Builder is the structured fallback for repeatable layouts and power users. |
| **Progressive disclosure** | Default views show the most useful elements. Advanced features (Cube Function Wizard, query trace) are accessed on demand, not in the primary flow. |
| **No false PivotTable promises** | The UI must distinguish "Local PivotTable" from "Live PivotTable connection". Office.js cannot directly create an external XMLA OLAP PivotTable, so the add-in provides a connection helper and native Excel instructions for that path. |
| **Consistent status system** | The green/gold/red/muted status color system from `tokens.ts` is used everywhere. |

---

## 2. Design Tokens

All tokens are inherited from the Tessallite main frontend (`frontend/src/theme/tokens.ts`).

### 2.1 Colors

```
--color-primary:           #006C35
--color-primary-dark:      #004E25
--color-primary-light:     #E8F5E9
--color-primary-bg:        rgba(0,108,53,0.08)

--color-charcoal:          #333333
--color-text-secondary:    #5A6577
--color-mint:              #F2F7F4
--color-white:             #FFFFFF
--color-border:            #CBD5E1
--color-border-light:      #E0E0E0

--color-gold:              #D4AF37
--color-gold-dark:         #A67C00
--color-gold-bg:           rgba(164,124,0,0.08)
--color-gold-light:        #FFF8E1

--color-red:               #B33A3A
--color-red-bg:            #FFEBEE

--color-purple:            #6B4C8A
--color-purple-bg:         rgba(107,76,138,0.08)

--color-muted:             #5A6577
--color-muted-bg:          #F2F7F4
```

### 2.2 Typography

```
--font-sans:   'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif
--font-mono:   'JetBrains Mono', 'Fira Code', monospace

--fs-title:    16px  / 700  (panel titles)
--fs-section:  13px  / 700  (section headers)
--fs-body:     13px  / 400  (body text)
--fs-caption:  11px  / 400  (metadata, secondary text)
--fs-chip:     11px  / 600  (status badges)
--fs-data:     11px  / 400  (table data cells, monospace)
--fs-code:     12px  / 400  (SQL display, monospace)
--fs-button:   13px  / 600  (button labels)
--fs-mode:     11px  / 600  (mode switcher labels)
```

### 2.3 Spacing

```
--space-xs:    4px
--space-sm:    8px
--space-md:    12px
--space-lg:    16px
--space-xl:    24px
--space-xxl:   32px
```

### 2.4 Radii

```
--radius-sm:   4px   (chips, badges)
--radius-md:   8px   (cards, inputs, panels)
--radius-lg:   12px  (modals, overlays)
--radius-full: 50%   (avatars, icon circles)
```

### 2.5 Shadows

```
--shadow-panel:    0 1px 3px rgba(0,0,0,0.08)
--shadow-overlay:  0 4px 16px rgba(0,0,0,0.12)
```

Tessallite's main frontend uses flat surfaces with borders instead of shadows. The plugin follows the same convention.

### 2.6 Status Colors (from tokens.ts `statusColor()`)

| Severity | Background | Foreground |
|---|---|---|
| `active`, `success`, `completed`, `approved`, `validated` | `#E8F5E9` | `#006C35` |
| `warning`, `stale`, `creating`, `running` | `#FFF8E1` | `#A67C00` |
| `error`, `failed`, `invalid` | `#FFEBEE` | `#B33A3A` |
| `retired`, default | `#F2F7F4` | `#5A6577` |

---

## 3. Global Layout

### 3.1 Task Pane Dimensions

- Fixed width: **360px** (Office Add-in task pane default)
- Height: fills the Excel window height (minus the formula bar)
- Resizable by dragging its left edge (Excel handles this natively)

### 3.2 Layout Structure

```
+----------------------------------------------+
| HEADER BAR (48px fixed)                      |
|  [T logo] Tessallite  [model v]  [?] [gear] |
+----------------------------------------------+
| MODE SWITCHER (36px fixed)                   |
|  Ask Tessallite  |  Report Builder           |
+----------------------------------------------+
| CONTENT AREA (fills remaining height)         |
|  scrollable, contains active mode content     |
+----------------------------------------------+
| FOOTER BAR (28px fixed)                      |
|  Persona: [Executive v] | Connected [dot]    |
+----------------------------------------------+
```

Total fixed chrome: 48 + 36 + 28 = 112px.

### 3.3 Header Bar (48px)

```
+--------------------------------------------------+
| [T icon] Tessallite   [Revenue Model v] [?] [gear] |
+--------------------------------------------------+
```

| Element | Size | Behavior |
|---|---|---|
| Tessallite icon | 20x20px | Green T-logo |
| "Tessallite" text | `--fs-title` (16px/700) | `--color-charcoal` |
| Model selector | 140px wide `Select size="small"` | Filters content to selected model |
| Glossary search icon | 20x20px `MenuBookOutlinedIcon` | Opens glossary search modal (Section 13) |
| Settings gear icon | 20x20px | Opens settings: server URL, profiles, theme, "View last query trace", sign out. Has `margin-right: 40px` to avoid Office personality menu collision. |

Background: `--color-white` with `border-bottom: 1px solid var(--color-border-light)`.

### 3.4 Mode Switcher (36px)

```
+--------------------------------------------------+
|  Ask Tessallite           |  Report Builder       |
+--------------------------------------------------+
```

Two-option toggle, 50% width each.

- Height: 36px, font: `--fs-mode` (11px/600), uppercase, centered
- Active: `color: --color-primary`, `background: --color-primary-bg`, `border-bottom: 2px solid --color-primary`
- Inactive: `color: --color-text-secondary`, no border
- Hover: `color: --color-charcoal`, `background: --color-mint`
- Default: Ask Tessallite on first open. If the user explicitly switches modes, persist the last-used mode in `localStorage`.

### 3.5 Footer Bar (28px)

```
+--------------------------------------------------+
| Persona: [Default v]        Connected [green dot] |
+--------------------------------------------------+
```

- Height: 28px, background: `--color-mint`, border-top: `1px solid var(--color-border-light)`
- Font: `--fs-caption` (11px)
- Persona dropdown: `Select size="small"` with transparent background
- Connection status: 8px dot. Green when connected, red when disconnected, gold pulse when reconnecting

### 3.6 Output Naming Rules

The UI must use precise labels so users understand whether the workbook artifact is live or local.

| Label | Use For | Do Not Use For |
|---|---|---|
| **Insert Table** | Query results inserted as a formatted Excel Table | Live XMLA output |
| **Chart** | Native Excel chart backed by an inserted table/range | Task-pane-only visualizations |
| **Local Pivot** | PivotTable created from an inserted worksheet table/range | Live XMLA/MSOLAP PivotTable |
| **CUBE formulas** | Formula grid backed by an existing XMLA connection | Static query result tables |
| **Live connection** | XMLA connection helper plus native Excel instructions | A completed external PivotTable insertion |

Never label a range-based PivotTable as a live PivotTable. Never show "Insert PivotTable" without qualifying whether it is local or live.

---

## 4. Screen 1: Login

### 4.1 When Shown

- No JWT stored (first use)
- Stored JWT expired (401 from any API call)
- User clicks "Sign out"

### 4.2 Layout

```
+----------------------------------------------+
|          (64px top padding)                   |
|                                               |
|            [T icon 32x32]                     |
|            Tessallite                         |
|                                               |
|   +--------------------------------------+    |
|   | Server URL                           |    |
|   | [https://cloud.tessallite.io       ] |    |
|   +--------------------------------------+    |
|   +--------------------------------------+    |
|   | Tenant                                |    |
|   | [acme-demo                          ] |    |
|   +--------------------------------------+    |
|   +--------------------------------------+    |
|   | Email                                 |    |
|   | [admin@acme-demo.com               ] |    |
|   +--------------------------------------+    |
|   +--------------------------------------+    |
|   | Password                              |    |
|   | [.................] [eye icon]       ] |    |
|   +--------------------------------------+    |
|                                               |
|   [x] Remember this profile                   |
|                                               |
|   +--------------------------------------+    |
|   |        Sign In                        |    |
|   +--------------------------------------+    |
|                                               |
|   (80px bottom padding)                       |
+----------------------------------------------+
```

### 4.3 Field Specifications

| Field | Type | Placeholder | Validation |
|---|---|---|---|
| Server URL | TextField | `https://your-tessallite.io` | Must start with `https://` or `http://` |
| Tenant | TextField | `Tenant slug` | Non-empty, lowercase alphanumeric + hyphens |
| Email | TextField | `Email address` | Valid email format |
| Password | TextField (password) | `Password` | Non-empty. Eye toggle shows/hides |
| Remember this profile | Checkbox | -- | Stores email, tenant, server URL, and profile name in `OfficeRuntime.storage`; never stores password |

### 4.4 Sign In Button

- Full width, `variant="contained"`, primary color
- Font: `--fs-button` (13px/600), height: 40px, radius: `--radius-md`
- Loading: `CircularProgress` (20px, white)
- Disabled: 50% opacity when fields empty/invalid

### 4.5 Error States

| Condition | Display |
|---|---|
| Wrong credentials (401) | Red alert: "Invalid email or password." |
| Server unreachable | Red alert: "Cannot connect to Tessallite at {url}." |
| Tenant not found | Red alert: "Tenant '{tenant}' not found." |
| Network timeout | Red alert: "Connection timed out." |

Error alerts use `Alert severity="error" variant="outlined"`, `--fs-caption` text.

### 4.6 Transitions

- Login screen fades out (150ms), main task pane fades in (150ms)
- Auto-login on open if a valid JWT is stored: "Connecting..." spinner. If only a saved profile is stored, pre-fill server, tenant, and email but require password entry.

---

## 5. Screen 2: Profile Selector

### 5.1 Trigger

Clicking the settings gear > profile section, or if no profile, the header shows "Add profile".

### 5.2 Dropdown Layout

```
+----------------------------------+
| Profile: admin@acme-demo         |
+----------------------------------+
| [check] Production               |
|         acme-demo / :8001        |
| ---                             |
| [     ] Staging                  |
|         acme-demo / :8001        |
| ---                             |
| [     ] Development              |
|         dev-tenant / localhost    |
| ---                             |
| [+ Add New Connection]           |
| ---                             |
| [Sign Out]                       |
+----------------------------------+
```

### 5.3 Profile Item

- Row 1: Profile name (`fontWeight: 600`, `--fs-body` 13px)
- Row 2: `{tenant} / {host}` (`--fs-caption` 11px, `--color-text-secondary`)
- Check icon (16px) on active profile
- Selected: `background: --color-primary-bg`, hover: `background: --color-mint`
- Height: 48px per item

### 5.4 Actions

| Item | Behavior |
|---|---|
| Click a profile | Switch profile, re-authenticate, reload model data |
| "Add New Connection" | Opens simplified login dialog |
| "Sign Out" | Clears JWT, shows login screen |

---

## 6. Main Task Pane -- Mode Switcher

After login, the mode switcher provides navigation between two work modes.

| Mode | Icon (MUI) | Label |
|---|---|---|
| Ask Tessallite | `ChatBubbleOutlineIcon` | ASK TESSALLITE |
| Report Builder | `TableViewIcon` | REPORT BUILDER |

**Default**: Ask Tessallite on first open. Last-used mode persists in `localStorage` only after the user manually switches mode.

---

## 7. Mode 1: Report Builder

### 7.1 Layout

```
+----------------------------------------------+
| REPORT LAYOUT                        [clear]  |
| +------------------------------------------+ |
| | FILTERS                                  | |
| | [no filters assigned]                    | |
| +------------------------------------------+ |
| | COLUMNS           | VALUES               | |
| | [no columns]      | [no measures]        | |
| +--------------------+---------------------+ |
| | ROWS                                     | |
| | [no rows assigned]                       | |
| +------------------------------------------+ |
| [Templates]     [Table] [Chart] [Local Pivot]|
+----------------------------------------------+
| SEARCH                                        |
| [Search measures, dimensions...          ] [x]|
+----------------------------------------------+
|                                               |
| SECTION: Measures (12)                [fold]  |
| +------------------------------------------+ |
| | FOLDER: Financial (4)             [fold] | |
| | +--------------------------------------+ | |
| | | [x] Revenue                          | | |
| | |     sum  |  currency  |  [standard]  | | |
| | |     "Total revenue including taxes"  | | |
| | |     [+ Values]                       | | |
| | +--------------------------------------+ | |
| | +--------------------------------------+ | |
| | | [ ] Revenue YoY Growth %  [variant]  | | |
| | |     variant of Revenue               | | |
| | |     [+ Values]                       | | |
| | +--------------------------------------+ | |
| +------------------------------------------+ |
|                                               |
| SECTION: Dimensions (5)               [fold]  |
| +------------------------------------------+ |
| | Country              [string]  [dim]     | |
| |   "ISO 3166-1 alpha-2 country code"     | |
| |   [->Rows] [->Cols] [->Filt] [->Slicer] | |
| |   [preview 12 members >]                | |
| +------------------------------------------+ |
| | Order Date           [time]  [dim]       | |
| |   [fiscal: Jul]                          | |
| |   "Business calendar date"               | |
| |   [->Rows] [->Cols] [->Filt] [->Slicer] | |
| +------------------------------------------+ |
|                                               |
| SECTION: Hierarchies (2)              [fold]  |
| +------------------------------------------+ |
| | [>] business_date_h         [date]       | |
| |     Year > Quarter > Month > Day         | |
| |     [->Rows]                             | |
| +------------------------------------------+ |
| | [>] geography_h           [explicit]     | |
| |     Region > Country > City              | |
| |     [->Rows]                             | |
| +------------------------------------------+ |
|                                               |
+----------------------------------------------+
```

### 7.2 Zone Mapping Grid

Visual layout builder for report intent. Users assign measures and dimensions to familiar Filters, Columns, Values, and Rows zones, then insert an Excel-native output. This grid does not imply the add-in can directly create a live external XMLA PivotTable.

**Empty state** (no items assigned): Shows placeholder text in each zone: `[no filters assigned]`, `[no columns]`, `[no measures]`, `[no rows assigned]`. Placeholder uses `--fs-caption`, `--color-text-secondary`, italic.

**Populated state** (items assigned): Each assigned item appears as a removable chip: `{item_name} x`. Chips use `--fs-chip` (11px/600), `--color-primary-bg` background, `--color-primary` text. Clicking `x` removes the item and unchecks it in the library.

**Zone layout**:
- FILTERS: stacked vertically, left-aligned
- COLUMNS and VALUES: side by side in a two-column row
- ROWS: full-width below the two-column row

**Action bar** (visible when 1+ items assigned):
- `[Templates]` button: opens template picker (Section 7.7)
- `[Insert Table]` button: `variant="contained" size="small"`, primary. Executes the semantic query and writes a formatted Excel Table
- `[Insert Chart]` button: `variant="contained" size="small"`, primary. Creates a native Excel chart from the inserted result table
- `[Local Pivot]` button: `variant="contained" size="small"`, primary. Creates a local PivotTable from the inserted worksheet table where Office.js supports it
- `[CUBE formulas]` button: `variant="outlined" size="small"`. Writes CUBE formulas for assigned measures; requires an XMLA workbook connection
- `[Live connection]` button: `variant="outlined" size="small"`. Creates/verifies the XMLA connection and opens a short instruction panel for Excel's native Insert > PivotTable > Use an external data source flow
- `[clear]` link: `variant="text" size="small"`, `--color-text-secondary`. Resets all zones

### 7.3 Search Bar

- Fixed below zone grid, height: 36px
- Input: `TextField variant="outlined" size="small" fullWidth`
- Placeholder: "Search measures, dimensions..."
- Debounced 300ms, searches across `display_name`, `name`, `description`, `effective_description`, `display_folder`, glossary synonyms, alias map entries
- Sections with no matching items are hidden

### 7.4 Measure Library

Each measure is a selectable card:

```
+----------------------------------------------+
| [x]  Revenue                                 |
|      sum  |  currency  |  [standard]         |
|      "Total revenue including taxes"          |
|      [+ Values]                              |
+----------------------------------------------+
```

**Row structure:**

| Row | Content | Style |
|---|---|---|
| Row 1 | Checkbox (16px) + `display_name` | `--fs-body` (13px/600) |
| Row 2 | `default_agg` tag + `format` tag + type badge | `--fs-caption` (11px) |
| Row 3 | `effective_description` (truncated 2 lines) | `--fs-caption`, `--color-text-secondary` |
| Row 4 | `[+ Values]` quick-action button | `variant="text" size="small"`, `--color-primary` |

Default cards must stay compact. Only `display_name`, one-line description, `format`, `measure_type`, and the primary action are visible by default. Aggregation, lineage, cross-model, semi-additive, folder breadcrumb, and glossary synonyms appear in an expanded details area opened from an info icon or the card title.

**Tags on Row 2:**

| Tag | Style |
|---|---|
| Aggregation (`sum`, `count`, `avg`) | `--color-muted-bg` bg, `--color-muted` text |
| Format (`currency`, `percent`) | `--color-purple-bg` bg, `--color-purple` text |
| Type: `standard` | `--color-primary-bg` bg, `--color-primary` text |
| Type: `calculated` | `--color-gold-bg` bg, `--color-gold-dark` text |
| Type: `variant` | `--color-purple-bg` bg, `--color-purple` text + base measure name |
| Cross-model | `--color-gold-bg` bg, `--color-gold-dark` text, `[cross-model: {model}]` |
| Semi-additive | `--color-purple-bg` bg, `--color-purple` text, `[semi-additive: {behavior}]` |

**Card dimensions**: Variable height (56-72px), padding: 8px 12px, border: `1px solid transparent`. Hover: `background: --color-mint`, `border-color: --color-border-light`. Checked: `border-left: 3px solid --color-primary`, `background: --color-primary-bg`.

**Variant measures**: Grouped under base measure with 8px indent. `[+ N variants]` toggle.

**Multi-select**: Check multiple measures, then `[Insert Table]`, `[Local Pivot]`, or `[CUBE formulas]` uses all assigned items. `[CUBE formulas]` writes all checked measures as a column set.

**Clicking the measure name**: Opens glossary popover (Section 12).

### 7.5 Dimension Library

```
+----------------------------------------------+
| Country                   [string]  [dim]     |
| "ISO 3166-1 alpha-2 country code"             |
| [->Rows] [->Cols] [->Filt] [->Slicer]        |
| [preview 12 members >]                        |
+----------------------------------------------+
```

**Row structure:**

| Row | Content | Style |
|---|---|---|
| Row 1 | `display_name` | `--fs-body` (13px/600) |
| Row 2 | Data type chip + source badge + calendar badge | `--fs-caption` (11px) |
| Row 3 | `effective_description` | `--fs-caption`, `--color-text-secondary` |
| Row 4 | Quick-action buttons | `--fs-caption`, `--color-primary` |
| Row 5 | "Preview N members" link (hidden for time dims) | `--fs-caption`, `--color-primary` |

**Data type chips**: `string` in muted, `time` in gold, `numeric` in primary.

**Source badges**: `[dim]` in muted, `[calculated]` in gold.

**Calendar badge**: `[fiscal: Jul]` or `[ISO]` in muted, for time dimensions.

**Quick-action buttons**: `[->Rows]`, `[->Cols]`, `[->Filt]`, `[->Slicer]`. Each is `variant="text" size="small"`, `--color-primary`. Active assignment shows bold. Clicking `[->Slicer]` adds the dimension to the report layout as a slicer intent. If the output is a local table/PivotTable and Office.js supports slicer creation for that object, the add-in inserts a slicer; otherwise it inserts the dimension as a filter column and shows a note.

**"Preview N members"**: Expands inline panel showing first 20 distinct members. Loads via `POST /api/v1/discover/members`. Each member clickable: inserts `CUBEMEMBER` formula.

### 7.6 Hierarchy Library

```
+----------------------------------------------+
| [>] business_date_h              [date]       |
|     Year > Quarter > Month > Day              |
|     [->Rows]                                 |
+----------------------------------------------+
```

**Collapsed**: Chevron + name + type badge + level chain + `[->Rows]` button.

**Expanded**: Shows individual levels, each with `[->Rows]` and optional `[preview members >]`. Time unit badges on levels (e.g., `[quarter]` in gold).

Clicking a level assigns the hierarchy to Rows at that depth.

### 7.7 Report Templates

Triggered by `[Templates]` button in zone mapping action bar. Opens a modal grid of cards:

| Template | Layout |
|---|---|
| **Time Series** | Date hierarchy on rows, measures in values |
| **Top N Breakdown** | Dimension on rows, measure in values, top-10 filter |
| **Period Comparison** | Time on columns (two members), measures in values |
| **Geographic Breakdown** | Geography dimension on rows, measures in values |
| **Variance Analysis** | Two measures in values (actual vs target), dimension on rows |

Each card: ~140x90px, border: `1px solid --color-border-light`, radius: `--radius-md`. Icon: 24px `--color-primary`. Title: `--fs-body` (13px/600). Description: `--fs-caption`, 2 lines.

Clicking a template populates the zone mapping grid. Modal closes. User adjusts before inserting.

### 7.8 Sections

Measures, Dimensions, and Hierarchies in collapsible sections:

```
SECTION: Measures (12)                    [fold]
```

- Header: `--fs-section` (13px/700), `--color-charcoal`, uppercase
- Count badge: `(12)` in `--color-text-secondary`
- Collapse toggle: `[-] / [+]` icon, 16px
- Default: Measures and Dimensions expanded, Hierarchies collapsed
- Divider: 1px line, `--color-border-light`, 16px above header

**Folder grouping**: Measures with `display_folder` values grouped under collapsible folder sub-headers. Unassigned measures under "(ungrouped)".

---

## 8. Mode 2: Ask Tessallite

### 8.1 Layout

```
+----------------------------------------------+
| CONVERSATION HEADER                           |
| Revenue Model Analysis  [Claude 3.5 Sonnet]   |
| Agent persona: [Default v]  [+New] [history]  |
+----------------------------------------------+
| MESSAGE LIST (scrollable)                     |
|                                               |
| +------------------------------------------+ |
| | [user] What was revenue by country       | |
| |         last quarter?                    | |
| +------------------------------------------+ |
|                                               |
| +------------------------------------------+ |
| | [T] Revenue by country for Q4 2025:     | |
| |                                          | |
| | Revenue was $4.2M, with the US           | |
| | contributing 62%, UK 18%, Germany 12%.   | |
| |                                          | |
| | +--------------------------------------+ | |
| | | Country   | Revenue   | % of Total  | | |
| | |-----------|-----------|-------------| | |
| | | US        | $2,604K   | 62.0%       | | |
| | | UK        | $756K     | 18.0%       | | |
| | | Germany   | $504K     | 12.0%       | | |
| | +--------------------------------------+ | |
| |                                          | |
| | [Insert Table] [Chart] [Local Pivot]      | |
| | [CUBE formulas] [Live connection] [Query] | |
| |                                          | |
| | [thumbs up]  [thumbs down]               | |
| +------------------------------------------+ |
|                                               |
+----------------------------------------------+
| INPUT BAR (fixed bottom)                      |
| [Ask a question about your data...     ] [>] |
+----------------------------------------------+
```

### 8.2 Conversation Header

- **Title**: `--fs-body` (13px/600), ellipsis truncated
- **LLM badge**: `[Claude 3.5 Sonnet]` chip, `--fs-chip`, muted bg. From `GET .../agent/config`. Shows `[Not configured]` in red if unavailable
- **Agent persona**: `Select size="small"`, from `GET .../agent/personas`
- **"+New"**: `variant="text" size="small"`, primary
- **History icon**: dropdown listing last 10 conversations with title and timestamp

### 8.3 User Message Bubble

- Background: `--color-primary-bg`, border-left: 3px solid `--color-primary`
- Padding: 8px 12px, font: `--fs-body` (13px/400)
- Right-aligned

### 8.4 Agent Response Bubble

- Background: `--color-white`, border: `1px solid --color-border-light`, radius: `--radius-md`
- Padding: 12px, margin: 8px 0

**Narration text**: `--fs-body` (13px/400). Bold for measure/dimension names. Inline code for SQL snippets (`--font-mono`, `--fs-code`, `background: --color-muted-bg`).

**Data table**: `Table size="small"` inside `Paper variant="outlined"`. Max height: 200px, scrollable. Header: `fontWeight: 600`, `background: --color-mint`, 11px. Data: `--fs-data` (11px), monospace for numerics, right-aligned. Striped rows. Column limit: 6 columns with "+N more" if wider.

**Streaming state**: Bubble appears with typing indicator (three dots). Text streams token by token. Data table shows skeleton until `query.rows` arrives. "Generating..." badge in gold in top-right corner.

### 8.5 Insertion Action Buttons

| Button | Style | Behavior |
|---|---|---|
| **Insert Table** | `variant="contained" size="small"`, primary | Writes data as Excel Table on new sheet |
| **Chart** | `variant="contained" size="small"`, primary | Writes the result table if needed, then creates a native Excel chart from that range |
| **Local Pivot** | `variant="contained" size="small"`, primary | Writes the result table, then creates a local PivotTable from that worksheet table where supported |
| **CUBE formulas** | `variant="outlined" size="small"` | Generates CUBE formulas when response metadata maps cleanly to model measures/members and an XMLA connection exists |
| **Live connection** | `variant="outlined" size="small"` | Opens the XMLA connection helper and native Excel PivotTable instructions |
| **Show Query** | `variant="outlined" size="small"` | Expands code block with semantic + physical SQL |

**Dimensional structure detection**: When response metadata flags columns as measures vs dimensions, the recommended action gets a gold outline pulse (500ms, once). Single value: only Insert Table. Time series: recommend Chart. Multi-dimensional result: recommend Local Pivot. Wide flat result: only Insert Table. Never label a local result-range PivotTable as a live PivotTable.

### 8.6 Judge Verdict Block

```
+----------------------------------------------+
| [Judge] Confidence: 92%  |  Rubric: 4.2/5.0  |
| "Answer is accurate and well-supported by     |
|  the data."                                    |
+----------------------------------------------+
```

- Background: `--color-primary-bg`, border-left: 3px solid `--color-primary`
- Padding: 8px 12px
- Scores: `--fs-chip`, primary text
- Narration: `--fs-caption`, `--color-text-secondary`
- Shown only when `judge_verdict` present

### 8.7 Follow-Up Suggestions

```
+----------------------------------------------+
| Suggested follow-ups:                         |
| [Compare to previous quarter] [By region]     |
+----------------------------------------------+
```

- Chips: `Chip variant="outlined" size="small"`, primary border/text
- Click populates input bar

### 8.8 Feedback Buttons

Thumbs up/down as `IconButton size="small"` (32px). Toggle behavior. Calls `POST .../feedback`.

### 8.9 Input Bar

- Fixed at bottom, background: `--color-white`, border-top: `1px solid --color-border-light`
- Padding: 8px 12px
- `TextField variant="outlined" size="small" fullWidth multiline maxRows=3`
- Send button: `SendIcon`, `color="primary"`, 32px. Shows `CircularProgress` (16px) while waiting
- Enter sends, Shift+Enter newline

### 8.10 Empty State

```
+----------------------------------------------+
|                                               |
|            [chat bubble icon 48px]            |
|                                               |
|          Ask Tessallite anything              |
|                                               |
|   Try: "What was revenue last quarter?"       |
|   Try: "Show me top 10 customers"             |
|   Try: "Compare YoY growth by region"         |
|                                               |
+----------------------------------------------+
```

Icon: `ChatBubbleOutlineIcon`, 48px, `color: --color-border`. Title: `--fs-section` (13px/700). Chips: `Chip variant="outlined" size="small"`, primary.

### 8.11 Agent Not Configured State

```
+----------------------------------------------+
|                                               |
|         [SmartToyIcon 48px]                   |
|                                               |
|  Conversational analytics unavailable         |
|                                               |
|  Contact your Tessallite administrator        |
|  to configure an LLM provider.                |
|                                               |
+----------------------------------------------+
```

Icon: `SmartToyIcon`, 48px, `--color-gold`. Input bar is visible but disabled (greyed out).

---

## 9. Overlay: Cube Function Wizard

### 9.1 Trigger

- "Insert formula" button on measure card, context menu, or `Ctrl+Shift+M`
- `[Insert Formulas]` button in zone mapping action bar for batch formulas

### 9.2 Layout (Modal Dialog)

Overlay covers content area, leaves header and footer visible.

```
+--CUBE FUNCTION WIZARD-------------------+
|                                         |
| Step 1 of 3: Select Measure            |
|                                         |
| Measure: [Revenue................. v]   |
|                                         |
| Step 2 of 3: Select Dimensions         |
|                                         |
| Row dimension: [Country........... v]   |
| Filter: [+ Add filter]                 |
|                                         |
| Step 3 of 3: Preview                   |
|                                         |
| +-------------------------------------+|
| | =CUBEVALUE("Tessallite",            ||
| |   "[Measures].[Revenue]",           ||
| |   "[Country].[Country].[US]")       ||
| +-------------------------------------+|
|                                         |
| Target cell: [A1.................]      |
| [check] Ready to insert                 |
|                                         |
| [Cancel]              [Insert Formula]  |
+-----------------------------------------+
```

### 9.3 Wizard Steps

**Step 1**: `Select` dropdown of measures with display name + aggregation. Pre-populated if triggered from card.

**Step 2**: Row dimension `Select`. Optional filters with `Autocomplete` member search via `POST /api/v1/discover/members`.

**Step 3**: Formula in monospace code block. Target cell auto-populated from `Workbook.getActiveCell()`. Formula correctness is guaranteed by the wizard's structured generation from model metadata.

**Connection check**: Before formula generation, verifies workbook connection exists. If not, offers "Create live connection". That action opens a credential prompt for XMLA/MSOLAP; the add-in must not reuse or store the login password.

### 9.4 Formula Insertion

1. Write formula via `Excel.run` to target cell
2. Success toast: "Formula inserted at A1"
3. If cell has value, confirmation dialog: "Replace?"

---

## 10. Overlay: Drill-Through Panel

### 10.1 Trigger

- Double-click PivotTable value cell (supplements, not replaces, Excel's native Show Details)
- Right-click > "Tessallite: Drill through"

### 10.2 Layout (Side Overlay)

Slides in from right edge over 200ms `ease-out`.

```
+----------------------------------------------+
| DRILL THROUGH                    [X close]    |
| Revenue > Country: US > Date: 2025-Q4         |
+----------------------------------------------+
| Drill-down path:                              |
| [business_date_h v]                           |
+----------------------------------------------+
| DETAIL ROWS (47)              [Insert Sheet]  |
| +------------------------------------------+ |
| | Order ID | Date       | Amount | Method  | |
| |----------|------------|--------|---------| |
| | ORD-4821 | 2025-10-03 | $1,240 | Card    | |
| | ORD-4822 | 2025-10-04 | $890   | PayPal  | |
| | ORD-4823 | 2025-10-05 | $2,100 | Card    | |
| +------------------------------------------+ |
|                                               |
| [< Prev]          Page 1 of 3        [Next >] |
+----------------------------------------------+
```

### 10.3 Header

Clickable breadcrumbs: `Measure > Dimension: Member > ...`. Each level navigates back up the drill hierarchy.

### 10.4 Path Selector

`Select size="small" fullWidth` from `drill-options` API.

### 10.5 Detail Rows

- `Table size="small"` with `maxHeight: 300px`
- Columns from `drill_through_set.detail_columns`
- Cursor pagination, 50 rows/page
- "Insert Sheet" plus "Copy" (tab-separated to clipboard) buttons

---

## 11. Overlay: Persona Switcher

### 11.1 Trigger

- Footer persona dropdown or `Ctrl+Shift+P`

### 11.2 Layout (Dropdown)

```
+----------------------------------------------+
| [check] Default                    [business] |
|         All measures and dimensions.          |
| ---                                           |
| [     ] Executive                 [business] |
|         Key KPIs, top-level aggregates only.  |
| ---                                           |
| [     ] Technical                [technical] |
|         Technical model view.                 |
+----------------------------------------------+
```

### 11.3 Persona Item

| Row | Content | Style |
|---|---|---|
| Row 1 | Check + name + audience badge | `--fs-body` (13px/600) |
| Row 2 | Description | `--fs-caption`, `--color-text-secondary` |

Audience: `[business]` in primary, `[technical]` in purple.
Extra business-facing badges: `[recommended]`, `[default]`, `[limited view]`, `[technical]`. Do not show raw internal flags such as `bypass_row_security` or `includes_hidden_columns` in the dropdown. If elevated visibility needs to be surfaced, show it only inside a settings/details popover as: "This persona has elevated model visibility. Contact your Tessallite administrator if this is unexpected."

### 11.4 Persona Info Bar

When non-default persona active:

```
+----------------------------------------------+
| [i] Viewing as "Executive". 23/45 measures   |
| shown.  [Switch to Default]                   |
+----------------------------------------------+
```

Background: `--color-mint`, border-left: 3px solid `--color-primary`, font: `--fs-caption`.

---

## 12. Overlay: Glossary Popover

### 12.1 Trigger

- Click measure/dimension name in Report Builder
- Select CUBE-formula cell
- Right-click > "Look up in glossary"

### 12.2 Layout (Popover)

Floating card with arrow pointing to anchor:

```
+------------------------------------+
| Revenue                  [measure] |
|                                    |
| "Total revenue including taxes"    |
|                                    |
| Status: [approved]                 |
| Source: [LLM Approved]             |
|                                    |
| Synonyms: turnover, total sales    |
|                                    |
| Sample: $1,240, $890, $2,100       |
|                                    |
| [View in Report Builder]           |
+------------------------------------+
```

- Width: 280px, max height: 300px, scrollable
- Background: `--color-white`, border: `1px solid --color-border`, radius: `--radius-md`
- Shadow: `--shadow-overlay`, arrow: 8px triangle

**Content rows**:

| Row | Content | Style |
|---|---|---|
| Row 1 | Term + type badge | `--fs-body` (13px/600) |
| Row 2 | Definition | `--fs-body` (13px/400) |
| Row 3 | Status + source badges | `--fs-chip` (11px/600) |
| Row 4 | Synonyms | `--fs-caption`, `--color-text-secondary` |
| Row 5 | Sample values (formatted) | `--fs-caption`, monospace |
| Row 6 | "View in Report Builder" link | `--color-primary` |

**Source badges**: `user` in primary, `llm` in gold, `llm_approved` in primary with checkmark.

**Status badges**: `approved` in primary-light/primary, `pending` in gold-light/gold-dark, `rejected` in red-bg/red.

**Dismissal**: Click outside, `Escape`, or `X` button.

**Navigation**: "View in Report Builder" scrolls to and highlights card with gold border (fades after 2s).

---

## 13. Overlay: Glossary Search Modal

### 13.1 Trigger

- Glossary icon in header bar (`MenuBookOutlinedIcon`)
- "Search all terms" link in glossary popover

### 13.2 Layout (Modal)

```
+----------------------------------------------+
| GLOSSARY                         [X close]    |
+----------------------------------------------+
| [Search glossary terms...               ] [x] |
+----------------------------------------------+
| [All] [Measures] [Dimensions]  Source: [All v]|
+----------------------------------------------+
|                                               |
| +------------------------------------------+ |
| | Revenue                          [measure]| |
| | "Total revenue including taxes"           | |
| | Source: LLM Approved | Status: approved   | |
| | Synonyms: turnover, total sales           | |
| | Sample: $1,240, $890, $2,100             | |
| +------------------------------------------+ |
|                                               |
+----------------------------------------------+
```

### 13.3 Search and Filters

- Search debounced 300ms across `term`, `definition`, `synonyms`
- Type filter: toggle `All | Measures | Dimensions` (primary active, mint inactive)
- Source filter: `Select size="small"` with All, User, LLM, LLM Approved

### 13.4 Card Interaction

Clicking a card dismisses modal and navigates Report Builder to highlight the corresponding item.

---

## 14. Overlay: Query Trace Modal

### 14.1 Trigger

- Settings gear > "View last query trace"
- Available only after a PivotTable refresh has been captured

### 14.2 Layout (Modal)

```
+----------------------------------------------+
| QUERY TRACE                      [X close]    |
| Last refresh: 14:32:05                        |
+----------------------------------------------+
| PIPELINE                                      |
| +------------------------------------------+ |
| | [1] Parse MDX                     [ok]    | |
| | [2] Bind to semantic model        [ok]    | |
| | [3] Match aggregate               [hit]   | |
| | [4] Rewrite SQL                   [ok]    | |
| | [5] Execute                       [ok]    | |
| +------------------------------------------+ |
|                                               |
| ROUTE DECISION                                |
| | Route: aggregate                           | |
| | Table: agg_daily_revenue                  | |
| | Grain: [country_code, month]               | |
| | Hit rate: 94.2%                            | |
|                                               |
| ORIGINAL QUERY (MDX)             [copy]       |
| +------------------------------------------+ |
| | SELECT NON EMPTY ...                      | |
| +------------------------------------------+ |
|                                               |
| REWRITTEN SQL                    [copy]       |
| +------------------------------------------+ |
| | SELECT country_code, SUM(revenue) ...     | |
| +------------------------------------------+ |
|                                               |
| PERFORMANCE                                   |
| | Execution time: 23ms  | Rows: 47           | |
| | Route: aggregate      | Table: agg_daily   | |
+----------------------------------------------+
```

### 14.3 Pipeline Steps

Numbered circles (18px) with connecting vertical line. Completed: primary green bg, in-progress: gold bg, not started: border color. Status badges: `[ok]` green, `[miss]` gold, `[error]` red.

### 14.4 Code Blocks

- `Paper variant="outlined"` with `background: --color-mint`, font: `--font-mono`, `--fs-code`
- Max height: 120px, scrollable, copy button top-right

### 14.5 Empty State

```
No query trace available
Refresh a PivotTable to capture the latest execution trace.
```

---

## 15. Footer Bar

```
+--------------------------------------------------+
| Persona: [Default v]        |   Connected [green] |
+--------------------------------------------------+
```

- Height: 28px, background: `--color-mint`, border-top: `1px solid --color-border-light`
- Font: `--fs-caption` (11px)
- Connection status: green/red/gold 8px dot. Polls `/health` every 30s

---

## 16. Context Menus

### 16.1 Items

| Menu Item | Condition | Action |
|---|---|---|
| **Tessallite: Drill through** | PivotTable value cell | Opens Drill-Through overlay |
| **Tessallite: Look up in glossary** | CUBE formula or PivotTable header | Opens glossary popover |
| **Tessallite: Insert measure** | Any cell | Opens Cube Function Wizard |

### 16.2 Visual

- "Tessallite" submenu group with green T-icon (12px)
- Items disabled (greyed) when condition not met

---

## 17. Ribbon Buttons

### 17.1 Ribbon Group: "Tessallite"

```
+------------------------------------------------+
| Tessallite                                      |
|  [Connect]     [Ask]     [Report Builder]       |
+------------------------------------------------+
```

| Button | Icon | Action |
|---|---|---|
| **Connect** | Plug | Login screen or profile switcher |
| **Ask** | Chat | Task pane on Ask Tessallite mode |
| **Report Builder** | Table | Task pane on Report Builder mode |

### 17.2 Button Style

- 32x32px icon with text label below
- Active: icon background `--color-primary-bg`

---

## 18. Toast Notifications

### 18.1 Position

Bottom of task pane, above footer.

### 18.2 Types

| Type | Icon | Background | Duration |
|---|---|---|---|
| Success | Check, green | `--color-primary-light` | 3s |
| Error | Error, red | `--color-red-bg` | 5s or dismissed |
| Warning | Warning, gold | `--color-gold-light` | 4s |
| Info | Info, primary | `--color-primary-bg` | 3s |

Single line, `--fs-caption` (11px). Stack upward. Click to dismiss.

### 18.3 Triggers

| Event | Toast |
|---|---|
| Local PivotTable created | "Local PivotTable created on sheet '{name}'" (success) |
| Chart created | "Chart created on sheet '{name}'" (success) |
| Table inserted | "Inserted {N} rows on sheet '{name}'" (success) |
| Formula inserted | "Formula inserted at {cell}" (success) |
| Live connection created | "Live Tessallite connection created. Use Excel Insert > PivotTable to build a live PivotTable." (success) |
| Persona switched | "Switched to {persona} view" (info) |
| Login failed | "Invalid credentials" (error) |
| API failed | "Failed to load. Retrying..." (error) |
| Connection lost | "Connection lost. Retrying in 30s..." (warning) |

---

## 19. Loading and Empty States

### 19.1 Loading Spinner

Centered `CircularProgress` (24px, primary) with `--fs-caption` text.

### 19.2 Skeleton Loading

`Skeleton variant="rectangular" animation="pulse"`, matching real content dimensions.

### 19.3 Empty States

| Context | Icon (48px) | Title | Subtitle |
|---|---|---|---|
| No projects | `FolderOpenIcon` | "No projects found" | "Create a project in Tessallite, then refresh." |
| No models | `LayersIcon` | "No models in this project" | "Build a semantic model in Tessallite first." |
| Model not deployed | `PublishOffIcon` | "Model not deployed" | "Deploy this model to enable Excel connectivity." |
| No measures | `FunctionsIcon` | "No measures defined" | "Add measures to your model in Tessallite." |
| No dimensions | `LabelIcon` | "No dimensions defined" | "Add dimensions to your model in Tessallite." |
| No hierarchies | `AccountTreeIcon` | "No hierarchies defined" | "Add hierarchies to your model in Tessallite." |
| No glossary entries | `MenuBookIcon` | "Glossary is empty" | "Bootstrap glossary from the model builder." |
| Agent not configured | `SmartToyIcon` | "Conversational analytics unavailable" | "Contact your administrator to configure an LLM." |
| No trace available | `TrackChangesIcon` | "No query trace available" | "Refresh a PivotTable to capture the trace." |
| No search results | `SearchOffIcon` | "No results found" | "Try different search terms." |
| No drill-through paths | `UnfoldLessIcon` | "No drill-through paths" | "This measure does not have drill-through configured." |

Icons: `color: --color-border`. Title: `--fs-section`, `--color-charcoal`. Subtitle: `--fs-caption`, `--color-text-secondary`. Centered with 48px top padding.

---

## 20. Error States

### 20.1 API Error Banner

```
+----------------------------------------------+
| [!] Failed to load measures. [Retry] [Dismiss]|
+----------------------------------------------+
```

Background: `--color-red-bg`, border-left: 3px solid `--color-red`. Retry and dismiss buttons inline.

### 20.2 Connection Lost

```
+----------------------------------------------+
| [!] Connection lost                           |
| Tessallite is unreachable.                    |
| [Retry Connection]     Retrying in 24s...     |
+----------------------------------------------+
```

Full-width banner, auto-retry every 30s with countdown.

### 20.3 Session Expired

Full-width banner: "Session expired. [Sign In Again]". Preserves profile.

---

## 21. Keyboard Shortcuts

| Shortcut | Action |
|---|---|
| `Ctrl+Shift+M` | Open Cube Function Wizard |
| `Ctrl+Shift+P` | Open Persona Switcher |
| `Ctrl+Shift+R` | Switch to Report Builder mode |
| `Ctrl+Shift+A` | Switch to Ask Tessallite (focus input) |
| `Ctrl+Shift+G` | Open glossary search modal |
| `Escape` | Close current overlay |
| `Enter` | Send chat message |
| `Shift+Enter` | New line in chat input |

Note: All `Ctrl+Shift` combos verified against Excel defaults. Shortcuts only work when task pane has focus.

---

## 22. Accessibility

### 22.1 ARIA Requirements

| Element | ARIA |
|---|---|
| Mode switcher | `role="tablist"`, each mode: `role="tab"`, `aria-selected` |
| Section headers | `role="button"`, `aria-expanded` |
| Chat message list | `role="log"`, `aria-live="polite"` |
| Zone mapping grid | `role="region"`, `aria-label="PivotTable layout"` |
| Toast notifications | `role="alert"`, `aria-live="assertive"` |

### 22.2 Focus Management

- Mode switch: focus to first interactive element
- Overlay open: focus to first step/selector
- Overlay close: focus returns to trigger
- Focus ring: 2px solid `--color-primary`

### 22.3 Color Contrast

- `#333333` on white: 12.6:1 (AAA)
- `#5A6577` on white: 5.5:1 (AA)
- `#006C35` on white: 5.7:1 (AA)
- `#FFFFFF` on `#006C35`: 6.5:1 (AA)

### 22.4 Screen Reader

- Icons have `aria-label`
- Chat: "User asked: {text}. Tessallite answered: {text}"
- Data tables: `<th>` with `scope`
- Zone grid: "Filters: 1 item. Values: 2 items"

### 22.5 High Contrast

Supports `prefers-contrast: more`. System colors replace backgrounds. Status uses shape (not color alone).

---

## 23. Animation and Motion

### 23.1 Motion Tokens

| Token | Duration | Easing | Use |
|---|---|---|---|
| `--motion-fast` | 150ms | `ease-out` | Hover, clicks, chips |
| `--motion-normal` | 200ms | `ease-out` | Mode switch, collapse, dropdown |
| `--motion-slow` | 300ms | `ease-in-out` | Overlays, wizard transitions |
| `--motion-skeleton` | 1500ms | `ease-in-out` | Skeleton pulse (loop) |

### 23.2 Specific Animations

| Element | Animation |
|---|---|
| Mode switch | Fade out/in (150ms). No slide |
| Section collapse | Height auto to 0 (200ms), content fade |
| Card hover | Background transition (150ms) |
| Drill-through | Slide from right (200ms) |
| Chat streaming | Text token by token. Typing dots pulse |
| Connection dot | Pulse 1s scale loop on "Reconnecting" |
| Toast | Slide up from footer (200ms), fade-out (150ms) |
| Skeleton | Opacity 0.4-1.0-0.4 over 1500ms loop |
| Popover | Fade in + scale 0.95 to 1.0 (150ms) |
| Zone chip add | Scale 0.8 to 1.0 with bounce (200ms) |

### 23.3 Reduced Motion

`prefers-reduced-motion: reduce` disables all animations (0ms). Skeleton shows static gray. Streaming shows full text at once.

---

## 24. Responsive Behavior

### 24.1 Width Thresholds

| Width | Behavior |
|---|---|
| 360px (default) | Full layout |
| 320-359px | Mode labels abbreviate: BUILDER / ASK. Footer persona narrows |
| 280-319px | Mode icons only. Footer text hides. Measure cards hide description. Zone grid collapses to summary: "3 items. [Expand]" |
| < 280px | Not supported |

### 24.2 Content Height

- `overflow-y: auto` on content area
- Fixed elements: zone grid, search bar, footer, chat input do not scroll
- Zone grid is collapsible via section header
- Virtual scrolling for 100+ item lists
- Drill-through: pagination (50 rows/page)

---

*End of Frontend Design Document.*
