---
title: "Query Panel"
audience: modeller
area: modelling
updated: 2026-08-11
---

## What this covers

The **Query** panel is a SQL workbench inside Model Builder. Paste a SQL statement, choose a dialect, validate it, explain the route, or execute it against the model. Use it when you need to test the query router directly instead of building a pivot in the Measure Query Panel.

---

## Main controls

| Control | Purpose |
|---|---|
| SQL editor | Holds the raw query sent to the router. |
| Dialect | Tells the validator how to parse the query: PostgreSQL, BigQuery, JDBC/Hadoop, or Spark SQL. |
| Force route | Optional override for source, aggregate, or pocket routing during investigation. |
| Persona | Runs the query as a selected persona so security and parameter filters can be checked. |
| Validate | Parses and checks the query without execution. |
| Explain | Shows the route and compiler trace without returning result rows. |
| Execute | Runs the query and displays rows, timing, route, and trace output. |

---

## When to use it

Use the Query panel to reproduce a BI-tool query, debug routing, compare forced routes, inspect generated SQL, and confirm row-security behavior for a persona. Use the Measure Query Panel when the question starts from one model measure and a few dimensions.

---

## Reading the route

The route trace explains whether Tessallite used an aggregate, a pocket table, or the live source. If the route is unexpected, run **Explain** first, then use **Force route** only for diagnosis. A forced route is not a model setting and should not be used to hide incomplete modelling.

---

## Saving and reusing a query

A query you run often can be saved to the model's **Saved Queries** so you — and your teammates — can re-run it without retyping it. Give the query a name and an optional description and save it. Saved queries belong to the **model**, not to one person, so anyone with access to the model sees the same list and can run any of them.

Because the list is shared, *changing* or *deleting* a saved query is governed:

- You may always edit or delete a query **you** created.
- A **Modeller** (or higher) may edit or delete **any** saved query on the model.
- A Viewer who did not create a query cannot change or delete it — the panel shows the server's message ("Only the query owner or a modeler can modify this saved query") rather than failing silently.

Deleting always asks you to **confirm first**, because a delete cannot be undone and the query is shared with everyone on the model.

---

## Scratchpad measures

The **Scratchpad** panel, next to the Query panel in the Toolbelt, holds your own private calculations. A scratchpad measure is just a name and a SQL expression — `SUM(amount) / COUNT(DISTINCT customer_id)`, say — that you can then drop into the Measure Query Panel like any other measure. Nobody else sees your scratchpad measures, and they are not part of the deployed model, so this is the safe place to try an idea out before proposing it as a real measure.

### What happens when you click Save

Before the measure is stored, Tessallite runs your expression past the model to check that everything it mentions actually exists. Three things can happen:

| What you see | What it means | What to do |
|---|---|---|
| The measure appears in the list | The expression checked out against the model | Nothing — go and use it |
| A message naming something in your expression, e.g. *unknown column `custmer_id`* | A real verdict: the expression refers to something the model does not have | Fix the expression. Saving again without changing it will fail the same way |
| *"Your measure was not saved because the query validator could not be reached... Try saving again in a moment."* | Nothing is wrong with your expression — the service that does the checking was momentarily unavailable and could not look at it | Wait a few seconds and click **Save** again |

That third case is worth understanding, because it looks like a failure but it is not a judgement on your work.

### Why Tessallite refuses instead of saving it anyway

It would be easy for Tessallite to shrug and save your measure unchecked. It deliberately does not, and the reason is what happens next if it did.

An expression that does not really work does not announce itself. It does not produce an error in the pivot. It produces a **column full of blanks** — and a column of blanks looks like a genuine answer ("there were no sales in that region"). You would not know to doubt it, because there is nothing on screen to doubt. That is the single worst outcome in a reporting tool: a wrong number wearing the costume of a right one.

Compare the two costs honestly:

- **Save it unchecked:** you are inconvenienced by nothing today, and possibly misled next month, permanently and silently.
- **Refuse to save it:** you are inconvenienced for a few seconds today, you can see exactly why, and you fix it by clicking again.

The second is a much better trade, so that is the one Tessallite makes. Note that only *saving a new measure* is affected — the outage does not touch measures you saved earlier, and it does not stop anyone querying or reporting. Nothing is broken for people reading your dashboards; you simply cannot add a new scratchpad measure until the checker is back.

If the message keeps appearing for more than a couple of minutes, it is not your model — tell your administrator that the query service is unreachable.

---

## Related

- [Measure Query Panel](measure-query-panel.md)
- [Live vs Aggregate](../querying/live-vs-aggregate.md)
- [Configure Row Security](configure-row-security.md)

---

← [Pivot Conditional Formatting](pivot-conditional-formatting.md) | [Home](../index.md) | [Live vs Aggregate →](../querying/live-vs-aggregate.md)
