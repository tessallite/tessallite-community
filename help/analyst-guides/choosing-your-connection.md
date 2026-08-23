---
title: "Choose Your Connection"
audience: end user
area: analyst-guides
updated: 2026-08-04
---

## What this covers

There are six ways to get numbers out of Tessallite, and they all read the same governed models. This page helps you pick the right one for the job you are about to do — building an Excel dashboard, writing a Power BI report, exploring in Tableau, or pulling data into a Python notebook — before the walkthrough pages that follow show you each one step by step.

---

## The short version

| If you are about to... | Use this | Walkthrough |
|---|---|---|
| Build or refresh a dashboard inside Excel | Tessallite Excel add-in, or a native PivotTable over XMLA | [Build Your First Excel Dashboard](build-your-first-excel-dashboard.md) and [Build a Report with the Excel Add-in](build-a-report-with-the-excel-add-in.md) |
| Drop single governed numbers into Excel cells | `TESSALLITE.*` worksheet functions | [Excel Formulas That Stay Live](excel-formulas-that-stay-live.md) |
| Build a shareable report in Power BI | PostgreSQL connector with DirectQuery | [Build a Power BI Report](build-a-power-bi-report.md) |
| Explore visually and build dashboards in Tableau | PostgreSQL connector, live connection | [Build a Tableau Dashboard](build-a-tableau-dashboard.md) |
| Analyse data in Python or train a model in Jupyter | `psycopg2` over the JDBC endpoint | [Query Tessallite from a Jupyter Notebook](query-tessallite-from-jupyter.md) |
| Put governed metrics inside your own app | Headless API (JSON in, rows out) | [Headless API](../integrations/headless-api.md) |

Whichever you pick, three things are always true:

1. **The numbers match.** Every connection reads the same semantic model, so Excel, Power BI, and a notebook all return the same figure for the same question.
2. **The security follows you.** Row security, column restrictions, and your persona apply on every connection. You cannot accidentally see more than you are allowed to.
3. **The speed follows you too.** Tessallite routes each query to a pre-computed summary when one exists, on every connection, with no setting to turn on.

---

## The two Excel paths, side by side

Excel gets two entries in the table because they suit different habits. Both are fully supported and both read the same models.

| | Excel add-in | Native PivotTable (XMLA) |
|---|---|---|
| What it feels like | A guided task pane inside Excel: drag measures and dimensions, ask questions in plain language, insert results | The familiar PivotTable field list connected to a cube |
| Best for | Analysts who want help assembling a report, or who want the agent inside Excel | Analysts who already think in PivotTables and want slicers, timelines, and Show Values As |
| Needs | The add-in manifest from your administrator | A workbook data connection to the XMLA endpoint |
| Extra tricks | `TESSALLITE.VALUE` cell formulas, KPI inserts, persona switcher, one-click charts | Top 10 value filters, drill-through by double-click, `GETPIVOTDATA`, CUBE formulas |

If you are not sure, start with the add-in: it is the gentler on-ramp, and you can add a native PivotTable later in the same workbook.

> **Tip.** You do not have to choose forever. A workbook can hold an add-in report on one sheet and an XMLA PivotTable on another, and they will agree with each other, because both are views of the same model.

---

## Power BI and Tableau: live, not imported

Both tools connect to Tessallite through the PostgreSQL connector on port `5433`. The one setting that matters is the connection mode:

- **Power BI: choose DirectQuery.** Every visual then asks Tessallite a live question when the report refreshes, so aggregate routing keeps working and you never report on a stale extract.
- **Tableau: choose Live Connection.** Same idea — each worksheet queries Tessallite as you build, instead of freezing a copy into an extract.

An imported extract still works, but it is a photograph of the data at one moment: it cannot be accelerated by new aggregates, and it drifts out of date until someone refreshes it. Live connections stay current and stay fast.

---

## Jupyter and Python: when a BI tool is not the destination

Sometimes the report is not the end of the work. You might be building features for a machine-learning model, running a statistical check, or feeding a pandas pipeline. For that, Tessallite looks like an ordinary PostgreSQL database on port `5433`, and the standard `psycopg2` driver connects with your normal Tessallite sign-in. The walkthrough at [Query Tessallite from a Jupyter Notebook](query-tessallite-from-jupyter.md) goes from `pip install` to a plotted chart and a trained model in one sitting.

The reason to go through Tessallite rather than straight to the source database is the same as everywhere else: the measure definitions, the security rules, and the row filtering are applied before the data reaches your notebook, so the numbers in your experiment reconcile with the numbers on the executive dashboard.

---

## The headless API: for builders, not readers

If you are embedding metrics in your own application — a mobile screen, a customer portal, a scheduled job — the headless API lets your code ask for measures and dimensions by name and get JSON rows back, with the same routing and security as every other path. It is documented for developers at [Headless API](../integrations/headless-api.md). If your goal is a chart you look at rather than a product you ship, one of the five paths above is simpler.

---

## What you need for any of them

- **Your Tessallite sign-in**: your email address and password.
- **The workspace slug**: the short identifier for your workspace, such as `acme-demo`. It is case-sensitive. Your administrator can tell you yours.
- **The gateway address**: `localhost` for a local install, or a hostname from your administrator for a cloud install.
- **At least one deployed model.** If you connect and see no tables, no model has been published yet — ask your modeller to deploy one.

> **Trying this on the demo workspace?** A local demo install uses workspace slug `acme-demo`, sign-in `admin@acme-demo.com` with password `acme-demo`, and gateway `localhost`. The walkthroughs in this section use those values and the demo `modelx` model, so you can follow along click for click.

---

## Related

- [Connect a BI Tool via JDBC](../getting-started/connect-a-bi-tool.md)
- [Connect Excel via XMLA](../getting-started/connect-excel.md)
- [Tessallite Excel Add-in](../integrations/excel-add-in.md)
- [BI Tool Compatibility Matrix](../integrations/bi-compatibility.md)

---

← [Tessallite Features](../getting-started/tessallite-features.md) | [Home](../index.md) | [Build Your First Excel Dashboard →](build-your-first-excel-dashboard.md)
