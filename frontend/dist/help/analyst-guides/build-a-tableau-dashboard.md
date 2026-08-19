---
title: "Build a Tableau Dashboard"
audience: end user
area: analyst-guides
updated: 2026-08-04
---

## What this covers

A complete walk from a blank Tableau Desktop workbook to a small, polished dashboard on the Tessallite demo model: a sales map-by-country table, a product leaderboard, a monthly trend, and a filter that ties them together — on a **live** connection, so the dashboard reads current governed data every time it opens.

---

## Before you start

- Tableau Desktop.
- Your Tessallite sign-in, the workspace slug, and the gateway address. On the local demo: `admin@acme-demo.com` / `acme-demo`, slug `acme-demo`, gateway `localhost`, port `5433`.
- The connection mechanics are the same as any PostgreSQL source; the reference is [Connect a BI Tool via JDBC](../getting-started/connect-a-bi-tool.md). This page starts once you can see the model in Tableau.

---

## Step 1 — Connect, and stay live

1. Open Tableau Desktop. In the **Connect** pane, choose **PostgreSQL**.
2. **Server**: `localhost`. **Port**: `5433`. **Database**: `acme-demo` (or `acme-demo/modelx` to scope to the model this walkthrough uses).
3. **Username**: your Tessallite email. **Password**: your Tessallite password. Click **Sign In**.
4. On the data source page, drag the **`modelx`** table onto the canvas.
5. At the top right, choose **Live** (not Extract).

**Why live matters here.** An extract freezes a copy of the data into the workbook. It cannot be accelerated by new summaries, it drifts stale until refreshed, and — most quietly dangerous — it is a copy of governed data sitting outside the reach of the rules. A live connection asks Tessallite a fresh, security-checked, accelerated question every time someone opens or filters the dashboard.

---

## Step 2 — Meet the model in the Data pane

Look at the left-hand Data pane. Everything in it comes from the semantic model:

- **Dimensions** — the ways to slice: `country_code`, `product_name`, `customer_segment`, `channel_code`, `business_date`, and friends.
- **Measures** — the governed numbers: `net_sales`, `gross_margin`, `transaction_count`, `Revenue`.

The measures arrive with their definitions already attached. `net_sales` is not "whatever sum someone typed into this workbook" — it is the model owner's definition, the same one Excel and Power BI see. Tableau aggregates them as you drag, and Tessallite computes the aggregation in the database.

> **Tip: let Tableau aggregate, never pre-sum.** Drag the raw measure and let Tableau wrap it (`SUM(net_sales)`). Do not build a custom SQL fragment that sums first — the model's measures are defined to be aggregated at query time, and pre-summing breaks exactly the cases (ratios, distinct counts) the definitions protect.

---

## Step 3 — Sheet one: the country view

1. Double-click **`country_code`**. Tableau reads it as geography and draws a map (if it lands as text instead, drag `country_code` to **Rows** — a tidy table is just as good for this walkthrough).
2. Drag **`net_sales`** onto **Colour** (on the map) or onto **Text** (in the table). Darker or bigger means more sales.
3. Rename the sheet **Sales by Country** (double-click the tab).

That double-click was a query: Tessallite grouped sales by country and returned only the result — one value per country — not the hundred thousand rows behind it.

---

## Step 4 — Sheet two: the product leaderboard

1. New sheet (**Worksheet > New Worksheet**), named **Top Products**.
2. Drag **`product_name`** to **Rows**, **`net_sales`** to **Columns**. Bars appear.
3. Click the **Sort descending** button in the toolbar — longest bar on top.
4. To keep it to the leaders: drag `product_name` to **Filters**, open the **Top** tab, choose **By field > Top 10 by SUM(net_sales)**.

The Top 10 is computed over the full set of products in the database, then the ten winners are drawn — the genuine leaderboard, not the ten biggest of whatever was on screen.

---

## Step 5 — Sheet three: the monthly trend

1. New sheet, named **Monthly Trend**.
2. Drag **`business_date`** to **Columns**. Tableau defaults to YEAR; click the little **+** on the pill (or right-click it and choose **Month**) to walk down to months.
3. Drag **`net_sales`** to **Rows**. The trend line draws itself.

---

## Step 6 — Assemble the dashboard and wire the filter

1. **Dashboard > New Dashboard**. Drag the three sheets onto the canvas: country view top-left, leaderboard top-right, trend along the bottom.
2. On the **Sales by Country** sheet (inside the dashboard), click the small funnel icon — **Use as Filter**. Now clicking a country re-queries the other two sheets for that country alone.
3. Add a quick filter for a second slice if you like: on any sheet, right-click **`channel_code` > Show Filter**, and the tick-box panel appears on the dashboard.

Every click on the dashboard is a fresh live query. Tessallite answers each one through the same routing as every other tool — summary when one fits, governed source path otherwise — and applies your security on the way.

---

## Step 7 — The number-checking habit

Before you circulate the dashboard, do the five-second check Maya does: glance at the grand total of `net_sales` on the leaderboard sheet and compare it with the same figure in the Tessallite web app's query panel (or ask the agent). They match — they must, because both come from the one model — and being able to say "this ties to the governed number" is what separates a dashboard people trust from one people debate.

> **Common trap: blending in a second, ungoverned source.** Tableau makes it easy to drag in a spreadsheet of "adjustments" and blend it with the model. The moment you do, the dashboard shows numbers nobody else can reproduce and the trust check above fails. If an adjustment is real, it belongs in the model — one definition, every tool. See [Why Your Numbers Match](why-your-numbers-match.md).

---

## Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| Cannot sign in | Wrong slug, email, or password | All three are exact; the slug is case-sensitive |
| The model table is not listed | No deployed models, or the connection is scoped to the wrong workspace | Check the Database field; ask your modeller to deploy |
| Everything feels slow on first open | Queries are hitting the source path | Still correct; summaries are created over time as the optimizer learns the workload |
| A date shows as a number or text | The pill is set to the wrong type | Right-click the pill and choose the date granularity you want |
| Numbers differ from a colleague's workbook | They filtered differently, or you are on different personas | Compare filters first; personas can legitimately show different rows |

---

## Related

- [Connect a BI Tool via JDBC](../getting-started/connect-a-bi-tool.md)
- [JDBC Connection Guide](../integrations/jdbc-connection-guide.md)
- [Build a Power BI Report](build-a-power-bi-report.md)
- [Choose Your Connection](choosing-your-connection.md)

---

← [Build a Power BI Report](build-a-power-bi-report.md) | [Home](../index.md) | [Query Tessallite from a Jupyter Notebook →](query-tessallite-from-jupyter.md)
