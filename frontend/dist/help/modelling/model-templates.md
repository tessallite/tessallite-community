---
title: "Model Templates"
audience: modeller
area: modelling
updated: 2026-05-24
---

## What this covers

Model templates are pre-built starter kits that give you a head start when creating a new model. Instead of defining every dimension, measure, hierarchy, and join from scratch, you pick a template and get a working structure imported automatically. This page explains the available templates, how to use them, and what gets imported.

---

## Available templates

### E-Commerce (Revenue Analytics)

A star schema for online retail: orders fact table with dimensions for product, customer, geography, and date. Includes measures for revenue, order count, average order value, and return rate. Hierarchies for product category and geography.

### SaaS Metrics (ARR/MRR/Churn)

Subscription-business model with a subscriptions fact table and dimensions for plan, customer segment, and cohort. Includes measures for MRR, ARR, churn rate, expansion revenue, and net revenue retention. Date hierarchies for monthly and annual reporting.

### Financial Reporting (GL/PnL/Balance Sheet)

General ledger model with journal entries as the fact table and dimensions for account, cost centre, department, and entity. Includes measures for debit, credit, net amount, and balance. Account hierarchy for chart of accounts roll-up.

---

## How to use a template

1. Open the **Workspace Explorer**.
2. Click **Create Model** on any project card.
3. In the Create Model dialog, click **Choose a template** to open the template gallery.
4. Browse or search templates by category. Each card shows the template name, category, and a preview of what entities will be imported.
5. Click a template to select it, then click **Create**.
6. The model is created first, then the template structure is imported automatically using the existing import/export infrastructure.

If the template import fails for any reason, the empty model is still created. You can manually import the template later using **Project menu > Import**.

---

## What gets imported

Templates import the same structure as a model export bundle:

- Dimensions (with display names, data types, and folders)
- Measures (with aggregation types and format strings)
- Hierarchies (with levels and time units)
- Joins (relationships between fact and dimension tables)
- Personas (if the template includes audience definitions)

Templates do **not** import connections or data. You still need to connect your own data source and map the template's table references to your actual tables.

---

## Related

- [Create a Project](create-a-project.md)
- [Export and Import a Model](export-and-import-a-model.md)
- [Import from dbt](import-from-dbt.md)

---

<- [Import from dbt](import-from-dbt.md) | [Home](../index.md) | [Model Translations ->](model-translations.md)
