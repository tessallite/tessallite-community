---
title: "Model Details"
audience: modeller
area: modelling
updated: 2026-06-04
---

## What this covers

The **Model Details** panel is a quick reference for everything inside the open
model. It has two tabs: **Model**, which lists the model's attributes and shows
the SQL that produces them, and **Documents**, which holds the auto-generated
model documentation. Open it from the toolbelt on the right of the Model
Builder (the document icon).

---

## The Model tab

The Model tab answers one question: *what does this model expose, and to whom?*

### Persona selector

At the top of the tab is a **Persona** dropdown.

- Leave it on **Full model** to see every attribute in the model.
- Pick a persona to see only the attributes that persona is allowed to see.
  Personas govern which measures, dimensions, and hierarchies a group of users
  can query; choosing one here filters the table to exactly those columns.

### Attribute table

Each row is one attribute (a physical column or a user-defined attribute).

| Column | Meaning |
|---|---|
| **#** | Row number in the current view |
| **Attribute** | The canonical (technical) name of the column |
| **Data type** | The column's data type, e.g. `varchar`, `numeric` |
| **Display name** | The friendly name shown to business users |
| **Description** | The business description. If the attribute is linked to a Business Glossary term, the glossary definition is shown; otherwise the attribute's own description is used |
| **Type** | **Physical** for a real source column, **UDA** for a user-defined attribute |
| **Source table** | The table the attribute belongs to |
| **Formula** | For UDAs, the expression that defines the attribute; blank for physical columns |

### Export

The **Export** button downloads the table exactly as shown (respecting the
persona filter) in your choice of format:

- **Excel (.xlsx)** — a formatted spreadsheet
- **CSV** — comma-separated values
- **JSON** — structured data for integrations
- **Text** — a plain, column-aligned table

### Source SELECT statement

Below the table is a read-only, formatted **SELECT statement**. This is the
query the platform runs for `SELECT * FROM <model>`, rewritten for the selected
persona — so it projects exactly the columns the persona can see. Use the copy
button to put it on your clipboard, for example to paste into the Query Panel or
a BI tool. If the statement cannot be produced, a short notice appears instead.

---

## The Documents tab

The Documents tab shows generated documentation for the model — its tables,
measures, dimensions, and relationships. It is produced automatically when you
open the tab; there is no button to press. Use the **copy** and **download**
icons to take the documentation as Markdown.

---

## Tips

- The persona filter on the Model tab and the Source SELECT statement always
  agree: both reflect the same persona, so the SQL never references a column the
  table is hiding.
- Descriptions come from the **Business Glossary** when a term matches the
  attribute name. Curating the glossary improves this panel automatically.

---

## Related

- [Configure Personas](configure-personas.md)
- [Business Glossary](business-glossary.md)
- [Query Panel](query-panel.md)

---

<- [Model Translations](model-translations.md) | [Home](../index.md) | [Agent Chat ->](../agent/agent-chat.md)
