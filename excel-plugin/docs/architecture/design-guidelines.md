For an Excel data-analysis plugin, the target is **“native productivity panel”**, not **“mini website in a task pane.”** Microsoft’s current Office Add-in guidance points to **Fluent UI**, **Segoe typography**, **Office theming**, narrow task-pane layouts, restrained colour, simple iconography, and ribbon/task-pane coordination as the design foundation. ([Microsoft Learn][1])

## 1. The design language to follow

Use this as the core design direction:

> **Excel-native Fluent control surface: compact, contextual, spreadsheet-first, low-chrome, data-dense, keyboard-friendly.**

This means:

| Web-page-looking plugin  | Modern Excel-native plugin                              |
| ------------------------ | ------------------------------------------------------- |
| Big hero banner          | Compact task-pane header                                |
| Marketing copy           | Action labels and status text                           |
| Large cards everywhere   | Small functional panels                                 |
| Website navigation       | Command bar, tabs, back button                          |
| Brand-heavy colours      | Mostly neutral UI, Excel/brand accent used sparingly    |
| Long scrolling page      | Short workflows, collapsible sections                   |
| Big CTA buttons          | Contextual commands: Insert, Refresh, Analyse, Validate |
| Decorative illustrations | Small monoline icons and data previews                  |

Microsoft describes Office’s design language as clean and simple, with common typography, colours, icons, motion and components; Fluent UI is the official framework for making add-ins feel like a natural extension of Office. ([Microsoft Learn][1])

---

## 2. The most important UX trend: **task pane as a tool panel, not a page**

Excel users are already working in the workbook. The plugin should feel like a **side instrument panel**.

Recommended panel structure:

```text
┌──────────────────────────────┐
│ Tessallite          Connected│  ← compact header
├──────────────────────────────┤
│ [Data] [Analyse] [Output]    │  ← 2–4 tabs only
├──────────────────────────────┤
│ Selected range               │
│ Sheet1!A1:H120   8 cols      │  ← context card
├──────────────────────────────┤
│ Measures                     │
│ ☐ Revenue                    │
│ ☐ Cost                       │
│ ☐ Margin %                   │
├──────────────────────────────┤
│ Filters                      │
│ Region      [All ▾]          │
│ Period      [2025 ▾]         │
├──────────────────────────────┤
│ Preview                      │
│ Revenue  £12.4m              │
│ Margin   18.2%               │
├──────────────────────────────┤
│ [Insert result] [Refresh]    │  ← sticky action bar
└──────────────────────────────┘
```

The task pane should be narrow-first. Microsoft specifically warns that Office Add-ins have constrained task pane widths, commonly around 320–350 px depending on host/platform, so navigation and controls must be designed for narrow layouts. ([Microsoft Learn][2])

---

## 3. Navigation pattern

For your Excel data plugin, avoid website-style left menus, sidebars inside sidebars, or top nav bars.

Use one of these:

### Best pattern for data-analysis plugin

**Top command bar + 3 tabs**

Example:

```text
Tessallite                         ⚙
[ Search model...              🔍 ]

Data | Analyse | Output

```

Use:

| Section     | Purpose                                        |
| ----------- | ---------------------------------------------- |
| **Data**    | connection, table/range/model selection        |
| **Analyse** | measures, dimensions, filters, query plan      |
| **Output**  | insert results, refresh, formulas, audit trail |

Microsoft recommends command bars for add-ins with four or more sections, search/filter needs, or narrow task panes; tab bars work when there are two to four major sections. ([Microsoft Learn][2])

### Avoid

```text
Home | Products | Pricing | Docs | About | Contact
```

That screams “website”.

---

## 4. Use Fluent UI, not generic Bootstrap/Tailwind web styling

Use **Fluent UI React v9** components where possible:

| Need        | Fluent-style component      |
| ----------- | --------------------------- |
| Main action | Button / SplitButton        |
| Search      | SearchBox / Input with icon |
| Settings    | Menu / Drawer / Popover     |
| Tabs        | TabList                     |
| Status      | Badge                       |
| Forms       | Field, Dropdown, Combobox   |
| Lists       | List, Tree, Table           |
| Warnings    | MessageBar / Toast          |
| Dialogs     | Dialog                      |

Microsoft explicitly recommends Fluent UI React for Office Add-ins and describes it as designed to fit Microsoft 365 applications. ([Microsoft Learn][3])

---

## 5. Typography: small, calm, Office-like

Use **Segoe UI**. Do not use web fonts like Inter, Roboto, Montserrat, Poppins, or big SaaS display fonts unless you want the pane to feel non-native.

Recommended type scale:

| Element         |     Size | Style                    |
| --------------- | -------: | ------------------------ |
| App title       | 16–18 px | Semibold                 |
| Page title      | 18–21 px | Light/Semibold, rare     |
| Section heading | 14–17 px | Semibold                 |
| Body text       |    14 px | Regular                  |
| Labels/captions | 11–12 px | Regular                  |
| KPI/result      | 18–24 px | Semibold, used sparingly |

Microsoft’s Office typography guidance says Segoe is the standard Office typeface, with body text commonly at 14 px, subtitles around 17 px, and title text around 21 px. ([Microsoft Learn][4])

---

## 6. Colour: mostly neutral, Excel green sparingly

A modern Excel plugin should not be dark, glossy, gradient-heavy, or brand-heavy by default. It should look like it belongs beside the spreadsheet.

Use:

```text
Background:        #FFFFFF / Office theme background
Panel border:      #E1E1E1
Text primary:      #242424
Text secondary:    #616161
Subtle fill:       #F5F5F5
Excel accent:      #217346 or Fluent green equivalent
Danger:            Fluent red
Warning:           Fluent yellow/orange
```

Guideline:

| Use colour for        | Do not use colour for   |
| --------------------- | ----------------------- |
| active state          | decoration              |
| primary command       | big gradients           |
| success/error/warning | large background blocks |
| selected range/model  | brand flooding          |
| status badges         | every icon/card/title   |

Microsoft’s colour guidance says Office uses colour purposefully and minimally, and add-ins should avoid overwhelming customer content. It also recommends testing Office themes and maintaining accessible contrast. ([Microsoft Learn][5])

---

## 7. Layout: 20 px margin, 4 px rhythm, no web-page whitespace

Good Excel plugin UX is **dense but not cramped**.

Recommended rules:

| Rule               | Recommendation                         |
| ------------------ | -------------------------------------- |
| Outer pane margin  | 16–20 px                               |
| Internal spacing   | multiples of 4 px                      |
| Section gap        | 12–16 px                               |
| Control height     | 32–40 px                               |
| Touch/click target | ideally 44 px where possible           |
| Footer action bar  | sticky bottom                          |
| Long lists         | virtualised/scrollable inside section  |
| Forms              | stacked labels, not two-column layouts |

Microsoft’s add-in layout guidance recommends 20 px default margins, 4 px grid spacing, consistent layouts, responsive design, avoiding overcrowding, and consolidating controls to reduce unnecessary mouse movement. ([Microsoft Learn][6])

---

## 8. Icon style: monoline, flat, minimal

Use Office/Fluent monoline-style icons.

For data analysis, icon examples:

| Function        | Icon metaphor           |
| --------------- | ----------------------- |
| Connect         | plug/database           |
| Model           | cube/table relationship |
| Analyse         | chart/search            |
| Refresh         | circular arrow          |
| Insert          | table plus              |
| Validate        | shield/check            |
| Warning         | triangle                |
| Query route     | branching arrows        |
| Cache/aggregate | layered table           |

Avoid 3D icons, colourful illustrations, emoji-style icons, filled app-store icons, and over-detailed SVGs. Microsoft’s icon guidance recommends simple, clear, monoline icons with only necessary details, flat colour, and usually no more than two elements. ([Microsoft Learn][7])

---

## 9. Data-analysis-specific UX trends

For Excel data plugins, the best modern pattern is **contextual assistance around the sheet selection**.

### Good patterns

**1. Selection-aware panel**

When the user selects a range/table/pivot:

```text
Selected: Sheet1!A1:H120
Detected: transaction table
Columns: 8
Rows: 120
Quality: 2 warnings
```

**2. “Explain before execute”**

Before inserting data:

```text
Query will use:
✓ Revenue measure
✓ Region dimension
✓ 2025 filter
✓ Aggregated table: sales_monthly_region
Estimated rows: 48
```

**3. Preview-first workflow**

Show a tiny result preview before writing to Excel.

```text
Preview
Region      Revenue
UK          £4.2m
US          £3.8m
UAE         £1.1m
```

**4. One-click insert**

Actions should sound like Excel actions:

```text
Insert as table
Insert formula
Create pivot
Refresh selection
Validate workbook
```

Not:

```text
Submit
Continue
Launch
Generate output
Explore solution
```

**5. Workbook-native trust**

Show provenance:

```text
Source: BigQuery / Finance Gold Layer
Measure: Approved Revenue
Last refreshed: 09:42
Definition: Net booked revenue after adjustments
```

This is especially important for your kind of plugin because business users need confidence that inserted numbers are governed, traceable and consistent.

---

## 10. Ribbon + task pane split

Do not put everything in the task pane.

Use the **Excel ribbon** for quick commands:

| Ribbon command    | Opens task pane view       |
| ----------------- | -------------------------- |
| Connect           | Data                       |
| Analyse selection | Analyse                    |
| Insert KPI        | Output                     |
| Refresh results   | Output / background action |
| Validate workbook | Audit view                 |
| Settings          | Settings dialog            |

Microsoft’s navigation guidance says ribbon commands are best for primary entry points, context-specific actions, quick actions and feature discovery; task-pane navigation is better for multistep workflows, settings, browsing and persistent state. ([Microsoft Learn][2])

---

## 11. First-run experience: small, not a landing page

Avoid:

```text
Welcome to Tessallite
The future of data analytics is here...
[Get started]
```

Use:

```text
Connect to your governed data model

1. Choose a source
2. Select approved measures
3. Insert trusted results into Excel

[Connect]
```

Microsoft recommends onboarding inside the app, but also warns against making registration/signup the first blocker before users experience functionality. ([Microsoft for Developers][8])

---

## 12. What makes it look “modern Excel” immediately

Use these visual cues:

| Cue                      | Effect                                      |
| ------------------------ | ------------------------------------------- |
| Segoe UI                 | Feels native to Office                      |
| Fluent components        | Matches Microsoft 365                       |
| Light neutral background | Keeps workbook dominant                     |
| Small command bar        | Feels like a tool, not a site               |
| Compact tabs             | Fits task-pane mental model                 |
| Status badges            | Good for data quality, refresh, connection  |
| Sticky bottom actions    | Reduces scrolling                           |
| Collapsible sections     | Handles complex data workflows              |
| Monoline icons           | Matches Office ribbon language              |
| Theme support            | Works with dark/high-contrast Office themes |

---

## 13. Design rules for your plugin panel

Use these as implementation rules:

1. **No hero sections.** The spreadsheet is the hero.
2. **No website navigation.** Use command bar, tabs, back button.
3. **No oversized cards.** Use compact panels and grouped controls.
4. **No decorative gradients.** Use neutral surfaces and Excel green sparingly.
5. **No long paragraphs.** Use labels, helper text, status, preview.
6. **Every screen must answer:** “What is selected, what can I do, what will happen?”
7. **Primary actions must be Excel verbs:** Insert, Refresh, Create, Validate, Explain.
8. **Keep brand quiet.** Logo small; brand colour only as accent.
9. **Show trust metadata.** Source, model, definition, refresh time, row count.
10. **Design for 320–350 px width first.** Anything that only works wide is wrong.

---

## 14. Recommended Tessallite-style panel language

For Tessallite specifically, I would use this UI personality:

> **Governed analytics inside Excel — calm, precise, native, enterprise-grade.**

Not:

> “AI-powered revolutionary analytics platform.”

Better labels:

| Instead of     | Use                |
| -------------- | ------------------ |
| Ask AI         | Ask governed model |
| Generate       | Insert result      |
| Data product   | Approved model     |
| Execute query  | Preview result     |
| Magic insights | Explain measure    |
| Optimise       | Route query        |
| Upload         | Connect source     |
| Dashboard      | Output view        |
| Explore        | Analyse selection  |

---

## 15. Ideal screen map

For an Excel analysis plugin, I would keep it to five main views:

| View        | Purpose                                                    |
| ----------- | ---------------------------------------------------------- |
| **Home**    | connection state, selected workbook context, recent models |
| **Data**    | source/model/table selection                               |
| **Analyse** | measures, dimensions, filters, preview                     |
| **Output**  | insert table/formula/pivot, refresh, formatting            |
| **Audit**   | definition, lineage, source, query route, warnings         |

Settings should not be a main tab unless used constantly. Put it behind a gear icon.

---

## 16. Simple visual specification

```css
font-family: "Segoe UI", system-ui, sans-serif;

pane {
  background: OfficeTheme.background;
  color: OfficeTheme.bodyText;
  padding: 20px;
}

section {
  border: 1px solid #E1E1E1;
  border-radius: 4px;
  background: #FFFFFF;
  padding: 12px;
  margin-bottom: 12px;
}

primaryAction {
  background: #217346;
  color: white;
}

secondaryAction {
  background: transparent;
  border: 1px solid #D1D1D1;
}

caption {
  font-size: 11px;
  color: #616161;
}

body {
  font-size: 14px;
}

sectionTitle {
  font-size: 14px;
  font-weight: 600;
}
```

Keep the radius modest. Big 16–24 px SaaS card radii make it look like a web dashboard, not an Office tool.

---

## Bottom line

The plugin should look like **Excel gained a governed analytics side panel**, not like you embedded your website into Excel.

The design direction should be:

> **Fluent UI + Segoe + compact task-pane layout + minimal Excel-green accent + contextual workbook actions + data preview + trust/provenance metadata.**

That combination will make the plugin feel modern, native, and serious enough for enterprise Excel users.
**make sure to embed every referenced component in the code base ; this application will run atrbapped**

[1]: https://learn.microsoft.com/en-us/office/dev/add-ins/design/add-in-design-language "Office Add-in design language - Office Add-ins | Microsoft Learn"
[2]: https://learn.microsoft.com/en-us/office/dev/add-ins/design/navigation-patterns "Navigation patterns for Office Add-ins - Office Add-ins | Microsoft Learn"
[3]: https://learn.microsoft.com/en-us/office/dev/add-ins/quickstarts/fluent-react-quickstart "Fluent UI React in Office Add-ins - Office Add-ins | Microsoft Learn"
[4]: https://learn.microsoft.com/en-us/office/dev/add-ins/design/add-in-typography "Typography guidelines for Office Add-ins - Office Add-ins | Microsoft Learn"
[5]: https://learn.microsoft.com/en-us/office/dev/add-ins/design/add-in-color "Color guidelines for Office Add-ins - Office Add-ins | Microsoft Learn"
[6]: https://learn.microsoft.com/en-us/office/dev/add-ins/design/add-in-layout "Layout guidelines for Office Add-ins - Office Add-ins | Microsoft Learn"
[7]: https://learn.microsoft.com/en-us/office/dev/add-ins/design/add-in-icons "Icon guidelines for Office Add-ins - Office Add-ins | Microsoft Learn"
[8]: https://devblogs.microsoft.com/microsoft365dev/best-practices-for-designing-word-excel-and-powerpoint-add-ins/ "Best practices for designing Word, Excel, and PowerPoint add-ins"
