# Handoff: Compact Task Pane (Analyse / KPIs / Ask) + icon swaps

Repo: `tessallite-excel-plugin` (React + MUI, `sx` styling, `tokens` object). Task pane is 320–350px wide.

## Overview
Vertical-space redesign of the Excel add-in task pane. Chrome shrinks from ~640px to ~260px so the working area (fields, KPIs, chat) gets 3–8× more room. All existing controls and states are preserved; nothing is removed, only compacted. Two small icon swaps are included.

## About the design files
The `.dc.html` files in this folder are **HTML design references** (side-by-side "current vs compact" mocks with toggleable states). Do not ship them. Recreate the compact side in the existing React/MUI components, keeping current props, callbacks, strings (`strings.*`, `templates.*`) and `tokens`.

## Fidelity
High-fidelity. Match sizes, colours and spacing below; use existing MUI components and `tokens`. Where the mock shows an SVG path, use the equivalent `@mui/icons-material` icon named.

---

## 0. Icon swaps (do first)
- `NamedSetCard.tsx`: replace the `📋` Typography with `<FilterListOutlined sx={{ fontSize: 16, color: tokens.colorTextSecondary, flexShrink: 0 }} />`.
- `ExcelChatShell.tsx` unavailable EmptyState: replace `SmartToy` with `AutoAwesomeOutlined` (same `fontSize: 48, color: tokens.colorGoldDark`).

---

## 1. Shared shell (all tabs)

### AppHeader — 48px → 36px
- Layout: `display:flex; align-items:center; gap:8px; padding:0 10px; height:36px; border-bottom:1px solid colorBorderLight`.
- Brand mark (existing 4-diamond SVG, 18×16) stays. **Clicking it opens a small popover**: mark 28×24 + "Tessallite" (13px/600) + "Excel plugin · v{version}" (11px colorTextSecondary). Closes on click-outside. Remove the inline title/subtitle text from the header.
- **Scope title** (replaces ProjectBar + Model select): `"{Project} / {Model} ▾"` — project 13px/600 ellipsised, model 12px colorTextSecondary. Click opens a popover containing three small Selects: PROJECT, MODEL, PERSONA (label 10px/600 uppercase, Select 26px). This removes `ProjectBar` and the ReportBuilder model selector from the flow, and moves `PersonaDropdown` out of the footer.
- Icons right (18px, colorTextSecondary, 10px gap): FilterAlt (drill), MenuBook (glossary), Settings. **Settings opens a menu**: email + "Signed in · {env}" header, Switch profile…, Diagnostics, Settings, divider, Sign out (colorRed). `ProfileSwitcher` moves here.

### ModeTabs — 44px → 32px
- Same three tabs; label "Ask Tessallite" → "Ask". 12px, active colorPrimary/600 with 2px bottom border.

### OfflineBanner — 26px row, `#fff8e1`, text 11px colorGoldDark, "Retry" 600.

### AppFooter — 28px → 22px
- `● Connected` (10px, dot colorPrimary) left; `Persona: {name}` right. Email and persona dropdown removed (now in header).

### Scrollbar (all tabs)
- One scroll container per tab; header, tabs and footer never scroll. In Ask, only the chat log scrolls (conversation bar and composer fixed).
- Style: `scrollbar-width:thin; scrollbar-color:transparent transparent` → on `:hover`/`:focus-within` `#c4c4c4 transparent`; WebKit `::-webkit-scrollbar{width:6px}` thumb `#c4c4c4` radius 3px, transparent track, thumb transparent until hover. Transition 0.3s.

---

## 2. Analyse tab (ReportBuilder + ZoneMappingGrid)

Everything below the tabs is one scroll container (`flex:1; min-height:0; overflow:auto`).

### Remove
- "Build report / Pick fields, then insert into Excel." title block.
- "Insert mode" chip row (→ Live checkbox).
- Second button row (Refresh values / Refresh sheet / CUBE / Connect / Trace) → toolbar icons.
- "Available fields" title, hint and icon; "Certified only" chip → checkbox.
- Sort row → sort icon + popover.

### Zones — 26px single rows
- Container `padding:6px 10px 4px; gap:3px`. Row: `height:26px; display:flex; align-items:center; gap:8px; background:colorSubtleFill; border:1px solid colorBorderLight; border-radius:2px; padding:0 8px`.
- Label 56px wide, 10px/700 colorTextSecondary letter-spacing .03em (VALUES / ROWS / COLUMNS / FILTERS). Zone hint moves to `title` tooltip on the label.
- Chips 18px, 11px, white, 1px colorBorder, radius 2px, "× " remove. Empty zone shows "—" `#9a9a9a`.
- Filter chips: dashed 1px colorPrimary border, colorPrimary text; click opens the existing filter edit Dialog (Operator + Values).
- Incompatible chip: `#ffebee` bg, colorRed border/text, tooltip "Not compatible with {measure}".
- Clear-layout: `DeleteSweepOutlined` 15px colorTextSecondary at far right of VALUES row, only when `hasItems`.

### Toolbar — 30px, `padding:0 10px; gap:4px; border-bottom`
Icon buttons 26×24, radius 2px, tooltip = current button label:
1. Table (`TableChartOutlined`) — contained colorPrimary when enabled.
2. Chart (`InsertChartOutlined`), 3. Pivot (`PivotTableChartOutlined`), 4. Templates (`GridViewOutlined`) — outlined 1px colorBorder.
5. 1px divider.
6. CUBE (`FunctionsOutlined`), 7. Live connection (`AccountTreeOutlined`), 8. Trace (`ManageSearchOutlined`, disabled `#b0b0b0` until `lastQuery`), 9. Refresh values (`RefreshOutlined`). Refresh sheet data can share the Refresh icon with a split menu or stay as a 10th icon.
7. Right-aligned **Live** checkbox (13px box, colorPrimary when checked, label 11px). Checked = `insertMode "live"`, unchecked = `"static"`. Tooltip: "Checked: live formulas · Unchecked: static values".
- Disabled-insert reasons (`addMeasureHint`, `insertDisabledReason`) become tooltips on the disabled buttons.

### Compatibility warning — below toolbar
`#ffebee`, colorRed, 11px: title 700 "Selected fields are not compatible" + 10.5px message and "Compatible dimensions: …".

### RefreshDetailsPanel — below toolbar, colorSubtleFill, 11px: summary + "Hide details" link (colorPrimary), SKIPPED / NEEDS A LOOK groups.

### Search row — 34px
- Search input 24px, radius 2px, placeholder "Search fields", `SearchOutlined` 14px leading.
- **Certified** checkbox (13px, label 11px colorTextSecondary), tooltip "Show certified fields only".
- Sort icon (`SortOutlined` 16px) → popover with SORT BY select + DIRECTION select (only when `sortOptions.length > 0`).

### Field list — starts with `border-top:2px solid colorPrimary`
Five collapsible sections in this order: **Measures, KPIs, Named lists, Dimensions, Hierarchies**.
- Section header 24px: colorSubtleFill, 10px/700 colorTextSecondary uppercase, count right (400), chevron `ExpandMore`/`ChevronRight` 14px. Whole header clickable.
- Row 28px: `padding:0 4px 0 10px; gap:4px; border-bottom:1px solid colorBorderLight`; name 12px ellipsised; chips 14px/9px/600 radius 7px (Certified `#2e7d32` on `rgba(46,125,50,.08)`, Calc/KPI colorGoldDark on `rgba(212,175,55,.12)`, Deprecated `#ed6c02` on `rgba(237,108,2,.08)`, type chips colorTextSecondary on colorSubtleFill). Deprecated names: colorTextSecondary + line-through.
- Action rail: 22×22 icon buttons, 15px icons, colorTextSecondary; primary "add" actions colorPrimary. Hover `colorPrimaryBg`.
  - Measure: Insert as formula (`FunctionsOutlined`), CUBE (italic ƒ / `Functions`), Add to Filter (`FilterAltOutlined`), Add to Values (`Add`, primary). Staged measure: Add → Remove (`Remove`). Row name click toggles details (Description, Aggregation, Format, Folder, Semi-additive, Definition) in a colorSubtleFill block, labels 10px/700 uppercase, values 11.5px.
  - KPI: Details (`InfoOutlined`), Insert options (`MoreHoriz`) → menu with the six existing options (title + 10px description), Add KPI value (`Add`, primary).
  - Named list: Details, Preview members (`Visibility`), Add to Rows (`TableRows`, primary). Type chip Top N / Set.
  - Dimension: Details, Preview members, Add to Filter, Add to Columns (`ViewColumn`, primary), Add to Rows (`TableRows`, primary). Member preview: colorSubtleFill block, "MEMBERS · n" + Close link, member chips 11px white.
  - Hierarchy: leading chevron toggles levels; type chip Calendar / Hierarchy / Segment; Add hierarchy to Rows (primary). Level rows 26px, `padding-left:30px`, `#fafafa`, "└ {level}", Add level to Rows.

---

## 3. KPIs tab (KpiPanel)

One scroll container below tabs.

### Row 1 — 34px: Search (24px, "Search KPIs") · **Certified** checkbox (same as Analyse) · Refresh (`RefreshOutlined`, outlined 24×24) · Insert scorecard (`DashboardOutlined`, contained colorPrimary 24×24, tooltip "Insert all KPIs as a scorecard table"). Replaces the title row and All/Certified chips.
### Row 2 — 26px status strip: "{n} KPIs" 11px/600 · divider · status pills (14px, 9px/600, radius 7px): "{n} Good" `#2e7d32`/`rgba(46,125,50,.08)`, "{n} Warning" `#ed6c02`/`rgba(237,108,2,.08)`, "{n} Poor" `#d32f2f`/`rgba(211,47,47,.08)`. **Pills are click-to-filter**; selected pill is filled (white text) with " ×". Right caption "Click a status to filter" 10px.
### Eval-error banner — `rgba(237,108,2,.06)`, 10.5px `#ed6c02` (existing string).
### List
- Folder headers = same 24px collapsible section header as Analyse. Ungrouped KPIs follow with no header.
- Row 30px: status dot 8px (STATUS_COLORS; `#9a9a9a` when unevaluated) · name 12px/600 ellipsised · Certified/Deprecated chip · value 12px/700 in status colour · "/ {goal}" 10px colorTextSecondary · trend arrow 13px/700 (TREND_COLORS) · rail: Insert table (`TableChartOutlined`), Insert chart (`BarChartOutlined`, disabled `#c4c4c4` with tooltip evalError when value/goal missing). Unevaluated shows "not evaluated" 11px `#9a9a9a` instead of value.
- Keep loading (skeleton rows 30px), load-error (red text + outlined "Try again"), empty ("No KPIs yet" + description) and no-match states.

---

## 4. Ask tab (ExcelChatShell / ChatCanvas / AssistantTurn / InsertActions)

Fixed: conversation bar and composer. Scrolling: the log only.

### Conversation bar — 30px
- Left: `History` 15px + **active conversation title** 12px/600 ellipsised + ▾; click opens the history menu (28px items, selected `colorPrimaryBg`, `DeleteOutline` 14px colorRed per item → existing confirm dialog). Untitled → "New conversation".
- Middle-right: provider model caption 10px colorTextSecondary.
- Right: New conversation (`Add` 15px colorPrimary, outlined 24×24).

### Messages — log `padding:8px 10px; gap:8px`
- User bubble: colorPrimary bg, white, **13px/600**, `padding:5px 10px`, radius `8px 8px 2px 8px`, max-width 88%, right-aligned.
- Assistant card: 1px colorBorderLight, radius 2px, no inner padding; sections separated by 1px borders:
  1. **Meta line** 10px colorTextSecondary `padding:4px 8px`: `● Completed` (`#2e7d32`/600) · latency · route pill (uppercase 600, aggregate `#2e7d32`/`#e8f5e9`, pocket `#7b1fa2`/`#f3e5f5`, source `#f57f17`/`#fff8e1`) · "{n} rows" · provider right. Replaces the four MetadataBadges chips.
  2. **Visual**: chart ~118px with axes/legend, no title bar; `Fullscreen` 14px maximize icon top-right (opens existing dialog).
  3. **Answer** 12px/1.4 `padding:4px 8px 6px`.
  4. **Calculation steps · n** 24px collapsible row → 11px table (#, description + 10px formula, mono value).
  5. **Diagnostics** 24px collapsible row (when config enables any) → THINKING / SEMANTIC QUERY / SQL blocks, 10.5px, colorSubtleFill, mono 10px.
  6. **Data · n rows** 24px collapsible row, caption of column names right → inline table 11px (header colorSubtleFill 10px/600; mono numbers).
  7. **Action footer** 30px `padding:0 6px; gap:4px`, 26×24 buttons: Chart (`BarChart`), Table (`TableChartOutlined`), Local pivot (`PivotTableChart`) — contained colorPrimary; the `recommendedAction` one is contained **colorGoldDark**; Pop-out (`OpenInNew`, outlined, when supported) · divider · Citations count (outlined, number; click toggles a chip row: "{name} · {type}" 10px pills → existing provenance dialog) · Trace (`ManageSearchOutlined`, outlined) · **Judge verdict**: `VerifiedOutlined` 15px `#2e7d32` on `rgba(46,125,50,.1)`, tooltip "Verified by quality judge · {metric} x/5 · …"; pending → `CircularProgress` 12; failed verdicts use colorGoldDark/colorRed. Replaces JudgeVerdictStrip · right: 👍 👎 (`ThumbUpAltOutlined`/`ThumbDownAltOutlined` 14px) when `feedbackEnabled`.
- Suggested questions: 20px outlined chips, radius 2px, 11px.
- Refused / judge-blocked turn: `#fff8e1` bg, 1px colorGold border, title "Request refused" colorGoldDark/600 with chevron, body 11px, row: "Try rephrasing:" + outlined colorGoldDark **⟲ Rephrase** 22px + "View trace" text.
- Error turn: `#ffebee`, 1px colorRed, 11px colorRed.
- Streaming: card with 22px "Thinking — …" header (colorSubtleFill, chevron toggles thought text), step list (✓ `#2e7d32` done, spinner running), narration 12px with blinking `|` in colorPrimary.
- Empty chat: `AutoAwesomeOutlined` 40px colorGoldDark, "Ask about your data" 14px/600, hint 12px, two example-question chips.
- Agent unavailable: existing EmptyState with `AutoAwesomeOutlined` 48px.
- Scroll-to-bottom: 28px round white button, 1px colorBorder, shadow, `ExpandMore` colorPrimary, sticky bottom-right of log.
- Send-failed toast: colorRed filled, 11px, "Retry" underlined + message + ✕, sticky bottom of log.

### Composer — `padding:6px 10px`, border-top
- Textarea min-height 30px, 12px, `padding:6px 8px`, radius 2px, 1px colorBorder (grows to 144px as today).
- Send: 30×30, radius 2px, colorPrimary, `Send` 15px; while streaming → colorRed `Stop`. Char counter/too-long text unchanged.

---

## Design tokens used
colorPrimary `#217346` · colorPrimaryDark `#185a33` · colorPrimaryBg `rgba(33,115,70,.07)` · colorSubtleFill `#F5F5F5` · colorBorderLight `#E1E1E1` · colorBorder `#D1D1D1` · colorCharcoal `#242424` · colorTextSecondary `#616161` · colorRed `#B33A3A` / bg `#FFEBEE` · colorGold `#D4AF37` · colorGoldDark `#A67C00` · muted `#9a9a9a` · disabled icon `#b0b0b0`/`#c4c4c4` · status good `#2e7d32`, warning `#ed6c02`, poor `#d32f2f`.
Type: Segoe UI stack; 13/12/11/10/9px steps; mono Cascadia Code/Consolas. Radius 2px everywhere (pills 7–10px). 4px spacing rhythm.

## Files
- `CompactReportBuilder.dc.html` — Analyse tab, current vs compact, all states.
- `CompactKpiPanel.dc.html` — KPIs tab.
- `CompactAskTab.dc.html` — Ask tab.
- `IconSwapPreview.dc.html` — the two icon replacements.
Open each in a browser; use the "Show states" strip under the compact pane to see every state; hover icons for the exact tooltip copy.
