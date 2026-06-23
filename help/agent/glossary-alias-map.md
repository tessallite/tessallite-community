---
title: "Author the glossary alias map"
audience: modeller
area: agent
updated: 2026-04-26
---

## What this covers

The glossary alias map is the per-model context the agent reads alongside the project brief. It teaches the answer LLM how a model is shaped, what its measures actually mean, and how to disambiguate the abbreviations users type. This article explains the parts of the glossary, why each part matters for answer quality, and how to write entries that hold up under real questions.


---

## Why a glossary

Two facts shape the design. First, an LLM is a pattern matcher; without grounding it will pick the most-likely-looking measure even when wrong. Second, every business invents its own shorthand — *gross*, *NMV*, *take rate* — and analysts type that shorthand into the chat box.

The glossary closes both gaps. For each allow-listed model, the modeller writes a short overview, lists the analytical questions the model is good at, and supplies a disambiguation table for the abbreviations real users send. The agent sees this on every turn that touches the model. Without a glossary the agent still works, but answer quality is markedly worse: routing decisions become guesses, refusals climb, and the judge verdict trends warn or fail.

---

## The four parts

Each glossary entry is per-model. Open *Project Agent → Models* and pick a model to edit.

### 1. Model overview

Two to four sentences describing what the model is. Avoid cataloguing measures here — the engine already exposes those. Instead, describe the *grain*, the *time window*, and the *use case*. A good overview answers "what is this model for, and what is it not for".

> Orders fact at the order-line grain, going back 36 months. Use this for revenue, units, and basket analysis. Do not use this for inventory or for forward-looking forecasts.

### 2. Analytical capabilities

Free-form prose. List the kinds of questions this model can answer well: "trends by month, region, product category", "year-on-year comparisons", "top-N by any dimension". Be specific about scope: a sentence noting what the model *cannot* answer is as valuable as listing what it can.

### 3. Abbreviation conflict rules

The most directly load-bearing field. List each business term that is ambiguous in your context, and the rule for resolving it. The agent reads this verbatim. Two patterns work well:

- **Defaults.** "By default, *revenue* means net revenue (sales after discounts and returns)."
- **Discriminators.** "When the user says *gross*, prefer the gross-revenue measure; when they say *net*, prefer net revenue. If they say *revenue* without qualifier, ask a clarifying question."

If a term is unambiguous, do not add a rule for it — every line you add is read on every turn. Reserve this space for terms the LLM will otherwise get wrong.

### 4. Example questions

Five to ten worked examples. Each entry pairs a question with a short note on how it should decompose: which measure, which dimension, which time window. The agent uses these as in-context exemplars; the eval runner uses them as a regression suite.

> Q: How did Q3 stack up against last year?
> Decompose: revenue measure, by month dimension, time window = current quarter vs same quarter prior year.

The questions you list here are the canon for *Run eval*. Pick questions that exercise the model's edges — questions a casual user actually asks, plus a few that test the boundary cases the abbreviation rules govern.

---

## Auto-derived sections

Three sections are populated automatically and surface in the same panel: an aggregates summary, a calendar alias list, and a dimension alias list. These are read-only — they are derived from the published model. If the model changes, click *Refresh derived* on the model context page (or *Refresh all* on the project agent page) to re-pull them. The agent reads the derived sections so an analyst question naming a calendar alias or a dimension alias resolves to the right physical column without the modeller writing those mappings by hand.

---

## How the LLM uses the glossary

When a question lands, the prompt assembler concatenates: the project brief, then for each allow-listed model the four glossary parts plus the auto-derived sections, then the tool spec, then any conversation history, then the user message. The LLM emits a tool call. The first model it picks is influenced strongly by the example questions and the abbreviation rules; the measure and dimension it picks are influenced by the auto-derived alias lists.

In practice this means a vague question like "how were units last week" will route to the model whose example questions mention units, with `units` resolved against the alias rules. A long, accurate glossary noticeably reduces clarify and refuse rates.

---

## A worked example

The bundled `modelx` in the demo tenant ships with a minimal glossary. Here is a fuller version that demonstrates the four parts on a familiar shape.

**Overview.**
> Orders fact at the order-line grain, 36 months. Use for revenue, units, and basket analysis. Not suitable for inventory or returns analysis — those live in `model_returns`.

**Analytical capabilities.**
> Trends by month/quarter/year. Year-on-year comparisons (the engine handles same-period-prior-year via the calendar alias). Top-N by region, category, or merchant. Drill from totals into the contributing rows via the merchant dimension.

**Abbreviation conflict rules.**
> By default, *revenue* means net revenue (after discounts and returns).
> When the user says *gross*, prefer `revenue_gross`.
> *Units* means line-item quantity. Do not interpret as "active customers".
> *Last month* means the previous calendar month, not the trailing 30 days.

**Example questions.**
> Q: What were sales last month?
> Decompose: net revenue, time window previous calendar month.
>
> Q: Top 10 merchants by revenue this quarter.
> Decompose: net revenue by merchant, current quarter, sorted descending, limit 10.
>
> Q: How are units trending year-on-year?
> Decompose: units by month, current 12 months vs prior 12 months.

---

## Common pitfalls

- **Long overviews.** A six-paragraph overview crowds out the example questions. Two to four sentences is the right length.
- **Generic example questions.** "Show me the data" teaches nothing. Each example should name a measure, a dimension, and a time window.
- **Abbreviation rules for unambiguous terms.** They cost tokens and add no value.
- **Not refreshing derived sections after a model change.** The auto-derived calendar/dimension lists are a snapshot. Refresh after every meaningful model edit.
- **Forgetting cross-model rules.** If two models share a term that means different things, write the rule on *both* model glossaries. The agent reads each model's section in isolation.

---

## Related reading

- [Configure your project agent](configure-agent.md)
- [Write a judge rubric](write-a-judge-rubric.md)
- [Cross-model calculation recipes](cross-model-recipes.md)

---

← [Project-Level Personas](project-personas.md) | [Home](../index.md) | [Write a judge rubric →](write-a-judge-rubric.md)
