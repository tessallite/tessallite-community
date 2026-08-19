---
title: "Build a Report with the Excel Add-in"
audience: end user
area: analyst-guides
updated: 2026-08-04
---

## What this covers

The Tessallite Excel add-in is a task pane that lives inside Excel and builds reports from governed measures and dimensions — no workbook connection to configure, no SQL to write. This page walks through a full reporting session with it: signing in, assembling a sales table by dragging fields, filtering it, asking the built-in agent a question, inserting a chart and a KPI card, drilling to the rows behind a number, and refreshing everything the next day.

---

## Before you start

- The add-in must be installed. If you cannot see the Tessallite task pane in Excel, follow the install steps in [Tessallite Excel Add-in](../integrations/excel-add-in.md) — your administrator hands you the manifest or a catalogue link.
- The walkthrough uses the demo workspace (`acme-demo`) and the `modelx` model, as with every page in this section.
- You need your Tessallite sign-in — the same email and password you use for the web app.

---

## The session we are following

Maya (from the dashboard walkthrough) has a new request: a quick regional sales summary for tomorrow's ops call, plus a straight answer to "which channel grew fastest last month?", plus the three KPIs on one sheet. She does not want to build a PivotTable for this — she wants to drag a few fields, ask one question, and be done. That is exactly what the add-in is for.

---

## Step 1 — Sign in and pick the model

1. Open Excel, find the Tessallite ribbon button (usually on **Home** or **Insert**), and click it. The task pane opens on the right.
2. Sign in with your tenant slug (`acme-demo`), email, and password.
3. At the top of the pane, choose the **`modelx`** model. The measure, dimension, and hierarchy libraries fill in — with only the objects you are allowed to see. If your list looks shorter than a colleague's, that is your persona doing its job, not a missing feature.

---

## Step 2 — Drag a report together in Report Builder

The **Report Builder** tab is where tables get assembled.

1. Drag **`region_code`** into the **Rows** zone.
2. Drag **`net_sales`** and **`gross_margin`** into the **Values** zone.
3. Click **Run**.

A grouped table lands on the sheet: one row per region, net sales and gross margin beside each, with friendly headers. Underneath, the add-in asked Tessallite for exactly that grouping — and Tessallite routed the question to a pre-computed summary if one covers it, so the answer comes back fast even over a hundred thousand rows.

### Tighten it with a filter

Drag **`channel_code`** into the **Filters** zone, then click the little filter chip that appears. The editor offers operators that read like plain English:

- **Equals** `online` — just the online channel. Type several values with commas to match any of them.
- **Contains** `VI` — would keep both "VISA" and "VIP".
- **Greater Than / Less Than** — one value, number or date.
- **Date Range** — two dates, in either order; the add-in swaps them if you type them backwards and tells you it did.

The small label under the value box says what kind of value the column expects, and if the model cannot accept a filter, the add-in explains why in plain words rather than showing an error code.

> **Tip: named lists as filters.** If your modeller saved a named list — "Focus Products", say — you can drag it into a zone two ways. A plain drag writes the list's members as they are right now (a snapshot). **Insert as formulas** writes a live `CUBESET` that re-evaluates on every refresh. For anything that changes over time — a dynamic Top N, a filtered list — choose the formula version so next month's members flow in by themselves.

---

## Step 3 — Ask the question out loud

The **Ask Tessallite** panel is the conversational agent, inside Excel.

1. Type: *which channel grew fastest last month?*
2. The answer streams back as it is generated — a sentence, the supporting query, and a judge verdict on the answer's quality.
3. Click the **insert** action to drop the answer text, the result table, or a chart straight onto the sheet.

The agent plays by the same rules as everything else: it honours the model's glossary (so "channel" means what the model says it means), your row security, and your persona. It cannot show you data you are not permitted to see, no matter how the question is phrased.

---

## Step 4 — KPI cards and a chart

Switch to the **KPIs** tab. Every KPI on the model — Net Sales, Revenue, Gross Margin % — appears as a live card with its current value, status, and trend.

Each card has two buttons:

- **Insert Table** — a mini-table at the active cell: name, value, goal, status, trend.
- **Insert Chart** — a column chart of current value against target.

Three clicks and Maya's KPI strip is on the sheet, with the same governed verdicts the scorecard shows. For a chart of the regional table instead, the Report Builder's insert actions can drop a chart of the query result next to it.

---

## Step 5 — Drill through to the rows behind a number

The ops call will ask "what is inside this 412,000 for North?" Maya is ready:

1. Select the region's net sales cell in the inserted result.
2. Open the **Drill-through** panel.
3. Pick the detail columns to include from the drill-path picker — the model's curated set, so the choice list is tidy.
4. The contributing fact rows land on the sheet.

The detail always reconciles to the cell it came from, and it respects row security and persona scope — if some rows are not yours to see, they are not in the list, and the visible total still matches the visible detail.

---

## Step 6 — Tomorrow: refresh, do not rebuild

Save the workbook. Next morning:

- **Refresh** (in the Report Builder footer) re-runs everything Tessallite inserted and recalculates every `TESSALLITE.*` formula.
- **Refresh sheet data** re-runs just the inserted tables on the active sheet — handy when one sheet matters and the rest can wait.

Two courtesies the refresh extends, worth knowing about:

- **It never overwrites your own work.** If a table needs to grow and the cells below it hold your notes, the refresh leaves the table exactly as it was and tells you which one and why, under **Show details**. Clear the space and refresh again.
- **The attribution line follows.** Each inserted table carries a small grey "Source: Tessallite …" line with the refresh time, which moves with the table — so a circulated workbook always says when its numbers were fetched.

> **A cell can look empty and not be empty.** A formula like `=IF(B1>0,B1,"")`
> shows nothing on screen when the condition is false, but the formula is still
> there, and deleting it loses work you would have to rebuild. The add-in checks
> for the formula, not just for what the cell displays — so if you insert over a
> block like that, you get the "this will overwrite existing content" question
> even though the area looks blank. Say yes only if you meant to replace it.

> **Common trap: sharing the hidden backing sheet.** Local PivotTables the add-in inserts are built over a hidden `_tsl_data_*` backing sheet. Hidden is not secured: anyone who can edit the workbook can unhide it. If the row-level detail should stay private, do not share the workbook — share the report another way.

---

## Add-in or PivotTable? A quick rule of thumb

| You want... | Reach for |
|---|---|
| Guidance, the agent, persona switching, one-click KPI cards | The add-in |
| Slicers, timelines, Show Values As, double-click drill-through in a classic grid | A native XMLA PivotTable |
| A single governed number inside a sentence or a hand-built schedule | `TESSALLITE.VALUE` formulas (see [Excel Formulas That Stay Live](excel-formulas-that-stay-live.md)) |

They coexist happily in one workbook, and they never disagree — same model, same security, same numbers.

---

## Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| No Tessallite button in the ribbon | Add-in not installed | Follow the install steps in [Tessallite Excel Add-in](../integrations/excel-add-in.md) |
| Task pane opens but will not sign in | Wrong tenant slug or password | The slug is case-sensitive; check all three fields |
| A table was skipped on refresh | Something of yours sits in the cells it needs, or its structure changed in the model | Open **Show details** for the exact reason; clear the cells or re-insert the table |
| The agent's answer names the wrong thing | The model glossary does not cover that term yet | Ask the modeller to add the alias; see [Author the glossary alias map](../agent/glossary-alias-map.md) |
| Fewer measures than expected in the library | Persona or column-level security | Working as designed — ask your administrator if you need wider access |

---

## Related

- [Tessallite Excel Add-in](../integrations/excel-add-in.md)
- [Build Your First Excel Dashboard](build-your-first-excel-dashboard.md)
- [Excel Formulas That Stay Live](excel-formulas-that-stay-live.md)
- [Agent Chat](../agent/agent-chat.md)

---

← [Build Your First Excel Dashboard](build-your-first-excel-dashboard.md) | [Home](../index.md) | [Excel Formulas That Stay Live →](excel-formulas-that-stay-live.md)
