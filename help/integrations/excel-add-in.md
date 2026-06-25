---
title: "Tessallite Excel Add-in"
audience: analyst
area: Integrations
updated: 2026-05-31
---

## What this covers

The Tessallite Excel add-in brings the semantic layer directly into Excel as a task pane. You build reports by dragging governed measures and dimensions, ask the conversational agent questions, insert live `CUBE` formulas, drill through to detail rows, and switch personas — all without leaving the workbook. Every number comes from the same deployed model that powers the web app, so the figures match exactly.

---

## When to use the add-in

Excel can also connect to Tessallite as a native PivotTable over XMLA (see [Excel PivotTable Features](excel-pivottable-features.md)). Use whichever fits the task:

- **Add-in** — guided report building, the conversational agent inside Excel, persona switching, and one-click insert of answers, tables, and charts. Best for analysts who want help assembling a report.
- **Native XMLA PivotTable** — the familiar PivotTable field list with slicers, Show Values As, and timelines. Best when you already think in PivotTables.

Both read the same governed models and honour the same row security and persona rules.

---

## Install the add-in

The add-in is an Office.js task pane served over HTTPS. Office refuses to load task panes over plain HTTP, so the add-in must be reached over `https://` with a certificate Excel trusts.

1. Obtain the add-in manifest (`manifest.xml`) from your administrator. It points at your Tessallite deployment's add-in host (port `3443` in local installs).

   Administrators download the manifest from **Settings → Endpoints** in the web app, where there are two buttons:

   - **Download Deployed Manifest** — the server-generated manifest, already pointed at the live deployment. This is the one to hand out: it is always current, so you do not have to hand-edit any URLs.
   - **Download Custom Manifest** — generates a manifest for a *different* server URL (for example a local-dev host or a custom domain). Use this only when the add-in must point somewhere other than the deployment you are signed in to.

   Prefer the deployed manifest unless you have a specific reason to retarget it.
2. In Excel, go to **Insert → Get Add-ins → My Add-ins → Upload My Add-in**, and select the `manifest.xml` file. (On Excel for the web, use **Insert → Office Add-ins → Upload My Add-in**.)
3. Open the add-in from the **Home** tab. The Tessallite task pane appears on the right.
4. Sign in with your tenant slug, email, and password — the same credentials you use for the web app.

The add-in supports Excel 2016 and later. On older builds it uses a simplified manifest and a local-storage fallback for session state.

---

## Report Builder

Report Builder assembles a query from governed objects and writes the result to the sheet:

1. Pick a model. The measure, dimension, and hierarchy libraries populate with the objects you are allowed to see.
2. Drag objects into the **Rows**, **Columns**, **Values**, and **Filters** zones, or start from a layout in the template picker.
3. Click **Run**. The result range is written to the active sheet with friendly display names as headers.

Because the add-in queries the deployed model, aggregate routing, calculated measures, and time variants all apply automatically.

### Filtering inside Report Builder

An object you drag into the **Filters** zone starts as a plain "equals" match. Click the filter chip to open the small editor and choose an **Operator** that fits what you are trying to do:

- **Equals / Not Equals** — keep (or drop) rows matching the values you type. Separate several values with commas to match any of them.
- **Contains** — keep rows whose text *includes* what you type, anywhere in the value. Typing `VI` keeps both "VISA" and "VIP". Good for partial text matches.
- **Greater Than / Less Than** — compare against a single value (a number or a date). These use one value only: if you type several, the add-in keeps the first and tells you so with a short notice, so you are never left wondering which value it used.
- **Date Range** — keep rows between a start date and an end date. Type the two dates in either order — if you enter them high-to-low, the add-in quietly swaps them so you still get the range you meant, and shows a notice saying it did.

The small label under the value box tells you what kind of value the column expects (a date, a number, or text), so you can pick a sensible operator. If you change your mind, press **Cancel** and the filter snaps back to exactly how it was when you opened the editor. If the model rejects a filter — for example an operator that does not fit the column — the add-in shows the reason in plain words instead of failing silently.

### Using a named list in a zone

A saved **named list** (named set) can be used two ways, and the difference matters:

- **Drag it into a zone** (Rows, Columns, or Filters). The add-in resolves the list's members *at that moment* and writes those exact members into the query. This is a snapshot — simple, but frozen.
- **Insert as formulas.** This writes a live `CUBESET` formula into the sheet instead of a fixed list. The set is re-evaluated every time the workbook refreshes.

Prefer **Insert as formulas** whenever the list is large or changes over time:

- A **large** list may be *truncated* in the preview the add-in fetched. Dropping it would silently use only the members that came back, under-counting your result. A `CUBESET` formula keeps the full membership.
- A **dynamic** list — Top N or Filtered — decides its members each time it runs. Dropping it freezes today's members into the sheet, so it would stop updating. A formula re-evaluates on every refresh and stays correct.

For a fixed, short list that never changes, a plain drop is fine. See [Named-list MDX composition](named-list-mdx-composition.md) for how the formulas are built.

---

## KPIs tab

The **KPIs** tab is the middle tab in the task pane. It shows all KPIs defined in the active model, evaluated live with their current value, status (green/amber/red), and trend direction (up/flat/down).

### What you see

- **Summary bar** — total KPI count with coloured chips showing how many are Good, Warning, and Poor.
- **Filter row** — switch between "All" and "Certified only", or type a search term.
- **KPI cards** — one card per KPI, grouped by display folder. Each card shows the KPI name, colour status dot, formatted value, target, and trend arrow.

### Inserting KPIs to the worksheet

Every KPI card has two buttons:

- **Insert Table** — writes a mini-table at the active cell showing the KPI name, value, goal, status, and trend.
- **Insert Chart** — creates a column chart comparing the current value against the target.

The scorecard button in the header inserts **all** KPIs as a live formula-based scorecard table with traffic-light status icons and trend arrows.

### When data refreshes

KPI values refresh each time you switch to the KPIs tab or press the refresh button. Behind the scenes, the add-in calls the model's batch evaluation endpoint and caches the results for the session.

---

## Ask Tessallite

The **Ask Tessallite** panel is the conversational agent inside Excel. Type a question in plain language; the answer streams back as it is generated, with the supporting query and a judge verdict on answer quality. Use the **insert** action to drop the answer text, the result table, or a chart onto the sheet.

The agent honours the model's glossary, row security, and the persona you have selected, so it will not surface data you are not permitted to see.

---

## CUBE formula wizard

The CUBE formula wizard generates live-connection cube formulas (the `CUBEVALUE` / `CUBEMEMBER` family) for a measure sliced by chosen dimension members. Live formulas recalculate on refresh, so a workbook built this way stays current against the deployed model. Pick the measure and members in the wizard and it writes the formulas into the selected cells.

---

## Drill-through

Select a result cell and open the **Drill-through** panel to see the contributing fact rows. The drill-path picker lets you choose which detail columns and joined dimensions to include, then writes the detail rows to the sheet. Drill-through respects row security and persona scope — you only ever see rows you are authorised to see.

---

## Persona switcher

The persona switcher sets the active persona for everything the add-in does. Selecting a persona applies that audience's allowed measures and dimensions, default filters, and row-level security. Queries run through the persona-scoped execution path, so an excluded measure or dimension is hidden or returns a clear authorisation error rather than leaking data.

---

## Trace, diagnostics, and glossary

- **Query trace** shows the route a query took (aggregate, pocket, or source) and the rewritten SQL, so you can confirm where a number came from.
- **Diagnostics** reports the add-in's connection and authentication state — useful when a query fails.
- **Glossary** looks up the business definition of any measure or dimension without leaving Excel.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Add-in icons appear black or the pane is blank | Loaded over plain HTTP, or the certificate is not trusted | Reach the add-in over `https://` (port `3443` locally) and trust the development certificate, then reload |
| "Sign in failed" | Wrong tenant slug, or the deployment URL in the manifest is unreachable | Confirm the tenant slug and that the add-in host URL in the manifest resolves from your machine |
| A measure is missing from the library | Your persona or role excludes it | Switch to a persona that includes the measure, or ask your modeller to grant access |
| Numbers differ from the web app | An older sheet result predates a model change | Re-run the report; the add-in always queries the currently deployed model |

---

## Related

- [Excel PivotTable Features](excel-pivottable-features.md)
- [Connect Excel via XMLA](../getting-started/connect-excel.md)
- [Agent Chat](../agent/agent-chat.md)
- [Configure Personas](../modelling/configure-personas.md)

---

← [Excel PivotTable Features](excel-pivottable-features.md) | [Home](../index.md) | [Power BI Connection Guide →](powerbi-connection-guide.md)
