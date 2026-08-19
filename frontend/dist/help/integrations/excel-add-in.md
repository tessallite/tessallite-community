---
title: "Tessallite Excel Add-in"
audience: analyst
area: Integrations
updated: 2026-07-09
---

## What This Covers

The Tessallite Excel add-in brings the semantic layer directly into Excel as a task pane. You build reports by dragging governed measures and dimensions, ask the conversational agent questions, insert values, charts, and PivotTables, drill through to detail rows, and switch personas without leaving the workbook. Every number comes from the same deployed model that powers the web app, so the figures match exactly.

---

## When To Use The Add-in

Excel can also connect to Tessallite as a native PivotTable over XMLA (see [Excel PivotTable Features](excel-pivottable-features.md)). Use whichever fits the task:

- **Add-in** - guided report building, the conversational agent inside Excel, persona switching, connectionless value functions, local PivotTables, and one-click insert of answers, tables, and charts. Best for analysts who want help assembling a report without creating a workbook data connection.
- **Native XMLA PivotTable** - the familiar PivotTable field list with slicers, Show Values As, and timelines. Best when you already think in PivotTables.

Both read the same governed models and honour the same row security and persona rules.

---

## Install The Add-in

The add-in is an Office.js task pane served over HTTPS. Office refuses to load task panes over plain HTTP, so the add-in must be reached over `https://` with a certificate Excel trusts.

1. Obtain the add-in manifest (`manifest.xml`) from your administrator. It points at your Tessallite deployment's add-in host (port `3443` in local installs).
2. In Excel, go to **Insert > Get Add-ins > My Add-ins > Upload My Add-in**, and select the `manifest.xml` file. On Excel for the web, use **Insert > Office Add-ins > Upload My Add-in**.
3. Open the add-in from the **Home** tab. The Tessallite task pane appears on the right.
4. Sign in with your tenant slug, email, and password. These are the same credentials you use for the web app.

Administrators download the manifest from **Settings > Endpoints** in the web app, where there are two buttons:

- **Download Deployed Manifest** - the server-generated manifest, already pointed at the live deployment. This is the one to hand out because it is always current.
- **Download Custom Manifest** - generates a manifest for a different server URL, for example a local-dev host or a custom domain. Use this only when the add-in must point somewhere other than the deployment you are signed in to.

Prefer the deployed manifest unless you have a specific reason to retarget it.

### Install From A Trusted Shared-folder Catalogue

For local development or an internal pilot, your administrator may give you a shared-folder catalogue URL instead of asking you to upload a manifest file manually. The catalogue folder contains the add-in manifest. Excel must trust that folder before it can show the add-in.

Use this flow on Excel for Windows, including Excel 2019 Home and Student:

1. Copy the shared-folder URL from your administrator. It should look like a Windows shared folder path, for example `\\SERVER\Share\TessalliteExcelAddin`.
2. In Excel, open **File > Options > Trust Center**.
3. Click **Trust Center Settings**.
4. Open **Trusted Add-in Catalogs**.
5. Paste the shared-folder URL into **Catalog URL**.
6. Click **Add catalog**.
7. In the list of trusted catalogues, tick **Show in Menu** for the catalogue you just added.
8. Click **OK**, then close and restart Excel.
9. In Excel, open the blue **Add-ins** button on the ribbon. In many Excel builds the tooltip says **Insert Add-ins**.
10. Open the **Shared Folder** tab.
11. Click **Refresh** in the top-right corner of the add-ins window.
12. Select the Tessallite add-in tile, then click **Add** or **Close** when Excel confirms it.
13. Look for the Tessallite ribbon button. It is usually on **Home**, **Insert**, or **Developer**, depending on the Excel build and ribbon layout.
14. Click the Tessallite ribbon button. The Tessallite task pane opens on the right side of the workbook.

If the Shared Folder tab says there are no add-ins available, verify that the catalogue folder contains a valid `manifest.xml` file and that Excel was restarted after the catalogue was added. If the task pane opens but shows the wrong server URL, the catalogue is pointing at the wrong manifest.

---

## Report Builder

Report Builder assembles a query from governed objects and writes the result to the sheet:

1. Pick a model. The measure, dimension, and hierarchy libraries populate with the objects you are allowed to see.
2. Drag objects into the **Rows**, **Columns**, **Values**, and **Filters** zones, or start from a layout in the template picker.
3. Click **Run**. The result range is written to the active sheet with friendly display names as headers.

Because the add-in queries the deployed model, aggregate routing, calculated measures, and time variants all apply automatically.

### Connectionless Values And Local PivotTables

The normal add-in insert path does not require a workbook-level OLAP connection named `Tessallite`.

- Clicking the measure value insert action writes a `TESSALLITE.VALUE` custom function, for example `=TESSALLITE.VALUE("inventory","shipping_cost")`. The function uses the same sign-in session and selected model as the task pane.
- Single-cell KPI value and status inserts write `TESSALLITE.KPI`, for example `=TESSALLITE.KPI("inventory","Shipping Cost","status")`.
- Local PivotTable inserts run the Report Builder query through Tessallite, write a flat grouped result into a hidden backing sheet, create a namespaced `_tsl_data_*` Excel Table, and build a native Excel PivotTable over that table using the Rows, Columns, Filters, and Values zones.
- Local PivotTable inserts only accept additive standard measures. Use **Insert Table** or the advanced CUBE formula path for calculated, variant, semi-additive, or non-additive measures so Excel does not re-aggregate a value that must stay at its governed grain.

If a workbook is opened while a different model is selected in the task pane, the `TESSALLITE.*` functions fail with a clear model-mismatch message instead of returning a number from the wrong model. Select the model named in the formula and refresh values.

Hidden PivotTable backing sheets are still workbook data. Anyone with workbook edit access can unhide them, so do not share a workbook with people who should not see the data behind the pivot.

### Filtering Inside Report Builder

An object you drag into the **Filters** zone starts as a plain "equals" match. Click the filter chip to open the small editor and choose an **Operator** that fits what you are trying to do:

- **Equals / Not Equals** - keep or drop rows matching the values you type. Separate several values with commas to match any of them.
- **Contains** - keep rows whose text includes what you type, anywhere in the value. Typing `VI` keeps both "VISA" and "VIP".
- **Greater Than / Less Than** - compare against a single value, such as a number or a date. If you type several values, the add-in keeps the first and tells you so.
- **Date Range** - keep rows between a start date and an end date. Type the two dates in either order; if they are high-to-low, the add-in swaps them and tells you.

The label under the value box tells you what kind of value the column expects. If the model rejects a filter, the add-in shows the reason in plain words.

### Using A Named List In A Zone

A saved **named list** can be used two ways:

- **Drag it into a zone**. The add-in resolves the list's members at that moment and writes those exact members into the query. This is a snapshot.
- **Insert as formulas**. This writes a live `CUBESET` formula into the sheet instead of a fixed list. The set is re-evaluated every time the workbook refreshes.

Prefer **Insert as formulas** whenever the list is large or changes over time. A large list may be truncated in preview, and a dynamic Top N or Filtered list changes membership over time.

For a fixed, short list that never changes, a plain drop is fine. See [Named-list MDX composition](named-list-mdx-composition.md) for how the formulas are built.

---

## KPIs Tab

The **KPIs** tab is the middle tab in the task pane. It shows all KPIs defined in the active model, evaluated live with their current value, status, and trend direction.

Every KPI card has two buttons:

- **Insert Table** - writes a mini-table at the active cell showing the KPI name, value, goal, status, and trend.
- **Insert Chart** - creates a column chart comparing the current value against the target.

Single-cell KPI value and status inserts in Report Builder use `TESSALLITE.KPI` by default. Multi-cell KPI layouts and named-list formula inserts may still use the advanced CUBE path when they deliberately need workbook OLAP formulas.

---

## You Always See The Published Model

The add-in shows you the **published** version of the model — the version someone deployed on purpose. It never shows work that is still in progress.

This matters most when a modeller is halfway through changing something. Imagine a colleague is rewriting the "Top 10 Customers" list and has it down to three names while they experiment. Nothing in your workbook changes. You keep seeing the ten names from the published version, because a half-finished edit is not a decision anyone has made yet. When they finish and deploy, your next refresh picks up the new list.

The same rule covers KPI names, descriptions, and definitions, and it applies everywhere the add-in reads: the KPIs tab, the named lists in the Report Builder, and the `TESSALLITE.*` formulas in your cells. It is the same rule Power BI, Tableau, and any SQL tool already follow, which is why the same number matches across all of them.

**If you are the modeller**, this is worth knowing, because your own drafts will not appear in Excel either. That is deliberate — the add-in is where people consume the model, not where you build it. Two things to do instead:

- To check your work in progress, use the **model builder** in the Tessallite web app. It shows live drafts, which is exactly what it is for.
- When the change is ready for everyone, **Save and then Deploy** the model. It appears in Excel on the next refresh.

There is a practical reason beyond tidiness. A formula for something unpublished cannot return a number anyway: the gateway that resolves it also serves the published version only, so a draft KPI offered in the pane would insert a formula that shows `#N/A` forever. Showing you only what actually works is the point.

### If you see "the published version of this model is unavailable"

This message means the add-in could not read the model's published version — not that your connection is down. The add-in stops rather than showing you unpublished figures, because a number nobody approved is worse than no number.

Ask a modeller to open the model in the web app and deploy it again, then refresh the pane. If it keeps happening, send them the model name and the exact message.

---

## Ask Tessallite

The **Ask Tessallite** panel is the conversational agent inside Excel. Type a question in plain language; the answer streams back as it is generated, with the supporting query and a judge verdict on answer quality. Use the **insert** action to drop the answer text, the result table, or a chart onto the sheet.

The agent honours the model's glossary, row security, and the persona you have selected, so it will not surface data you are not permitted to see.

---

## CUBE Formula Wizard

The CUBE formula wizard is the advanced OLAP path. It generates live-connection cube formulas, such as `CUBEVALUE` and `CUBEMEMBER`, for a measure sliced by chosen dimension members. These formulas require the workbook to have an Analysis Services connection named `Tessallite`. Use this mode when you deliberately want Excel's native XMLA engine. For normal add-in inserts, use the connectionless value and local PivotTable paths.

---

## Drill-through

Select a result cell and open the **Drill-through** panel to see the contributing fact rows. The drill-path picker lets you choose which detail columns and joined dimensions to include, then writes the detail rows to the sheet. Drill-through respects row security and persona scope.

---

## Persona Switcher

The persona switcher sets the active persona for everything the add-in does. Selecting a persona applies that audience's allowed measures and dimensions, default filters, and row-level security. Queries run through the persona-scoped execution path.

---

## Custom Excel Functions

The add-in registers connectionless worksheet functions under the `TESSALLITE` namespace, plus older compatibility functions under the `TESS` namespace. These functions use the add-in sign-in session, so the task pane must be signed in and a model selected before custom functions will work.

### TESSALLITE.VALUE

```
=TESSALLITE.VALUE("model_slug", "measure_name")
=TESSALLITE.VALUE("model_slug", "measure_name", "region_code", "EU")
```

Returns a governed measure value, optionally filtered by dimension member values. Calls are batched for recalculation, so a sheet with many value cells does not send one request per cell.

### TESSALLITE.KPI

```
=TESSALLITE.KPI("model_slug", "kpi_name", "value")
=TESSALLITE.KPI("model_slug", "kpi_name", "goal")
=TESSALLITE.KPI("model_slug", "kpi_name", "status")
```

Returns one KPI property. Status returns `1`, `0`, or `-1`, so Excel icon-set formatting can use it.

### TESSALLITE.MEMBERVALUE

```
=TESSALLITE.MEMBERVALUE("model_slug", "measure_name", "dimension_name", "member_value")
```

Returns one measure for one dimension member. Use it for small hand-built schedules where a full PivotTable would be too much.

### Older TESS Namespace

- `=TESS.LISTBYID("named_set_id")` returns the members of a named set as a spilled array where the Excel version supports dynamic arrays.
- `=TESS.KPIVALUE("kpi_id")`, `=TESS.KPIGOAL("kpi_id")`, and `=TESS.KPISTATUS("kpi_id")` are retained for older workbooks.

### Caching And Refresh

Custom functions cache results for 60 seconds. To force a fresh evaluation, click **Refresh** in the Report Builder footer. This clears all caches and triggers a full workbook recalculation so every `TESSALLITE.*` formula fetches a fresh value immediately. Switching personas, switching connection profiles, and signing out also clear the caches automatically.

### Refresh Sheet Data

The **Refresh sheet data** button (next to the Refresh button in the Report Builder footer) re-runs every Tessallite-inserted table on the active worksheet. It reads each table's stored query and re-executes it against the current session and persona. Tables that belong to a different project or model than the one currently selected are skipped with a reason. Tables whose column structure has changed since insertion are also skipped to prevent data corruption.

When a table grows or shrinks, its attribution line (the small grey "Source: Tessallite ..." row underneath it) moves with it and picks up the new refresh time.

If a table needs to grow but the cells directly beneath it are not empty - your own note, a subtotal, or a second block of data - Tessallite does **not** refresh that table. It leaves everything exactly as it was and tells you which table was affected, so nothing of yours is overwritten. Clear or move those cells and refresh again.

**Show details.** If any table was skipped, or refreshed with a warning, a **Show details** link appears under the buttons. Open it to see exactly which table was affected and why. Some reasons are temporary - for example, if something else in Excel was still working on the same cells, or if Excel would not hand back a table's saved query details that time, the table is left untouched and simply clicking **Refresh sheet data** again will usually pick it up. A temporary problem is always named in the list; a table is never dropped from the count without a reason. A table listed under **Skipped** was not changed. A table listed under **Refreshed with warnings** needs a look: it may have its new numbers with something else left incomplete, or - rarely, if Excel reported an error part-way through - a mix of old and new rows. The reason next to each table says which.

### Insert Mode (Live vs Static)

A **Live / Static** toggle in the Report Builder footer controls how single-value inserts behave:

- **Live** (default): inserts a `TESSALLITE.VALUE(...)` or `TESSALLITE.KPI(...)` formula. The value refreshes automatically on workbook recalculation.
- **Static**: fetches the current value once and writes it as a plain number. The cell does not update automatically.

This setting applies only to the default single-value insert actions (the sigma icon on measures, and the value/status KPI inserts). Table inserts, chart inserts, CUBE formula inserts, and scorecard inserts are not affected by the toggle.

### Error Messages

| Cell text | Meaning |
|---|---|
| `#ERROR: Not signed in...` | Open the Tessallite task pane and sign in first. |
| `#ERROR: No model selected...` | Select a project and model in the task pane. |
| `#ERROR: Formula model does not match...` | The formula names one model while the task pane has another model selected. Select the formula's model and refresh. |
| `#ERROR: Session expired...` | Your JWT has expired. Re-open the task pane and sign in again. |
| `#N/A` | The measure or KPI property is not available for that cell. |

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Add-in icons appear black or the pane is blank | Loaded over plain HTTP, or the certificate is not trusted | Reach the add-in over `https://` and trust the development certificate, then reload. |
| "Sign in failed" | Wrong tenant slug, or the deployment URL in the manifest is unreachable | Confirm the tenant slug and that the add-in host URL in the manifest resolves from your machine. |
| A measure is missing from the library | Your persona or role excludes it | Switch to a persona that includes the measure, or ask your modeller to grant access. |
| Numbers differ from the web app | An older sheet result predates a model change, or a formula was calculated while another model was selected | Re-run the report, or select the model named in the formula and click **Refresh values**. |
| `#NAME?` for every TESSALLITE formula | The custom functions runtime did not register this Excel session (common on Office 2019/2021 perpetual) | Re-insert the add-in from **Insert > My Add-ins > Shared Folder**. A plain Excel restart is not always enough. |
| The pane works but every formula shows `#VALUE!` after a few seconds, and the server is on `localhost` | The custom functions sandbox is blocked from local network access, or it does not trust the server certificate | Ask your administrator to apply the one-time machine setup described in the plugin README (loopback exemption plus installing the certificate authority into the machine store). |
| The add-in half-works: formulas respond but the ribbon button is gone, or the reverse | The Office add-in cache has become inconsistent after repeated add and remove cycles | Close Excel, delete the contents of `%LOCALAPPDATA%\Microsoft\Office\16.0\Wef`, reopen Excel, and re-insert the add-in. Sign in again afterwards — clearing the cache also clears the saved session. |
| A KPI or named list you just created is not in the pane | It has not been deployed yet. The add-in shows the published model only | Save and Deploy the model in the web app, then refresh the pane. To check the draft itself, use the model builder. |
| A cell shows "Published model unavailable" | The model's published version could not be read | Ask a modeller to deploy the model again, then refresh. The add-in stops here on purpose rather than showing unpublished figures. |

Tip: type `=TESSALLITE.DIAG()` in any cell to see the add-in's own health
report — whether storage works, which model is selected, which server it
talks to, and whether the server answers. Include this text when you contact
support.

---

## Related

- [Excel PivotTable Features](excel-pivottable-features.md)
- [Connect Excel via XMLA](../getting-started/connect-excel.md)
- [Agent Chat](../agent/agent-chat.md)
- [Configure Personas](../modelling/configure-personas.md)

---

<- [Excel PivotTable Features](excel-pivottable-features.md) | [Home](../index.md) | [Named List MDX Composition ->](named-list-mdx-composition.md)
