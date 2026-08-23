---
title: "Dimension Attribute Relationships"
audience: modeller
area: modelling
updated: 2026-07-16
---

## What this covers

A dimension attribute relationship tells Tessallite that two columns on the same dimension always describe the same thing — for example, a country code and the country name that goes with it. Once that link is declared and proven in the real data, Tessallite can answer a query grouped by one column using a summary that was built on the other, without going back to the raw source. This article explains what a relationship is, the difference between a one-to-one (bijection) link and a many-to-one link, why the link must be proven in real data before it is trusted, what null handling means, and where the common traps are. A worked example runs through the whole idea end to end.

---

## The idea in one sentence

If every value of column A lines up with exactly one value of column B, and every value of B lines up with exactly one value of A, then grouping by A and grouping by B produce the same set of groups — just with different labels. A declared, proven relationship lets Tessallite swap one label for the other and still give the exact same answer.

---

## What a relationship is

Every dimension in a model points at one **key column** — the column that identifies each member. A member often also carries **detail columns** that travel with the key: a display name, an ISO code, a short code, a phone prefix, and so on. A relationship is a statement a modeller makes about one such pair:

> "On this dimension, the key column and this detail column describe the same member."

That statement is not automatically true. A modeller might be wrong. So Tessallite treats the declaration as a claim to be checked, not a fact to be trusted. Nothing accelerates until the claim has been proven against the actual data.

A dimension can carry several relationships at once — one for the name, one for the ISO code, one for a phone key — because a key can have many details that each travel with it. Each is declared and proven on its own.

---

## Two kinds of relationship

There are two shapes a key-to-detail link can take, and they behave very differently.

### Bijection (one-to-one)

A **bijection** means the two columns are perfectly interchangeable: each key maps to exactly one detail, *and* each detail maps back to exactly one key. `country_code` and `country_name` are usually a bijection — `US` always means `United States`, and `United States` always means `US`. Nothing else shares either value.

Because the mapping is exact both ways, grouping by the code and grouping by the name split the data into the same groups. This is the powerful case: a query grouped by the name can be served from a summary built on the code with **identical** results, no re-computation, and no loss of any statistic.

### Many-to-one (N:1)

A **many-to-one** relationship means each key still maps to exactly one detail, but several keys can share the same detail. `city_id` to `country_name` is many-to-one — many cities sit inside one country. Grouping by country is *coarser* than grouping by city: it merges groups together.

This kind of link is a genuine roll-up, not a relabel. It can still help for measures that add up cleanly (totals and counts), but it cannot serve statistics that do not survive merging, such as distinct counts or medians. Tessallite never treats a many-to-one link as if it were one-to-one, even if today's data happens to have no duplicates.

---

## Why a bijection must be proven in real data

A wrong bijection produces wrong numbers, silently. If a modeller declares that two columns are interchangeable and they are not, Tessallite would relabel groups that are not actually the same and hand back an answer that looks right but is not. That is the worst possible failure for an analytics platform.

To make that impossible, Tessallite **proves** the relationship before it ever uses it, by checking the complete data — not a sample. A sample can only ever find a problem; it can never prove the absence of one. The check confirms, over every row governed by the model:

- each key value maps to exactly one detail value, and
- for a bijection, each detail value maps back to exactly one key value.

Only when both directions hold on the full population does the relationship become trusted. If the check fails, finds a counterexample, errors, or has not yet run for the current version of the model, the relationship stays untrusted.

An untrusted relationship changes nothing about correctness: the query simply runs the ordinary way, against the governed source path, and returns the right answer. This is called **failing closed** — when the system is not certain, it takes the safe road rather than the fast one. Failing closed is the default any time the relationship is ambiguous, stale, disabled, or the data has moved on since it was last proven.

---

## Null handling: REJECT_NULL

A missing value breaks the whole idea of "these two columns describe the same member." If a key or a detail is null, there is nothing to line up, and the mapping is undefined.

Tessallite handles this with a strict rule called **REJECT_NULL**: if either the key column or the detail column contains any null value on the governed data, the relationship is not proven and is not used. There is no "treat null as its own group" shortcut and no filling-in of blanks. For you as a modeller this means one practical thing: **declare relationships on clean columns.** If a detail column has gaps, clean the data at the source, or pick a column that is fully populated, before you expect any acceleration from it.

---

## Worked example: country code and country name

Suppose a model has a `country` dimension whose key column is `country_code` (`US`, `GB`, `FR`, …) and which also carries a `country_name` column (`United States`, `United Kingdom`, `France`, …).

An aggregate has already been built at the `country_code` grain to speed up "sales by country code" reports. Now analysts start asking for "sales by country **name**" instead — the friendlier label. Normally that query would miss the aggregate, because the aggregate was built on the code, not the name, and fall through to the source.

Here is what a declared, proven bijection changes:

1. The modeller declares a **bijection** relationship on the `country` dimension: key `country_code`, detail `country_name`.
2. On the next model deploy, Tessallite checks the full data. Every code maps to one name, and every name maps back to one code, with no nulls on either side. The relationship becomes trusted.
3. An analyst runs "total sales by country name."
4. Tessallite recognises that `country_name` is a proven one-to-one partner of `country_code`, serves the query from the existing `country_code` aggregate, and simply relabels each group with its country name.
5. The result is byte-for-byte identical to running the query against the source — same rows, same totals, same distinct counts — but it comes back at aggregate speed.

If the data later changed so that two different codes mapped to the same name (breaking the reverse direction), the next proof would fail, the relationship would stop being trusted, and the same query would quietly go back to the source path and stay correct.

---

## What this is *not*: the display column is not a relationship

Every dimension can nominate a **display column** (`display_column_id`) — the caption a BI tool shows for each member instead of the raw key. It is tempting to assume that choosing a display column already tells Tessallite the two columns are interchangeable. **It does not.**

The display column is purely a presentation choice. It creates no relationship, is never checked against the data, and produces **no routing acceleration** whatsoever. A dimension can show `country_name` as its caption while a query grouped by `country_name` still falls through to the source, because no relationship was ever declared or proven.

If you want the acceleration, you must **declare the relationship explicitly** and let it be proven. Setting a display column and declaring a relationship are two separate actions with two separate purposes: one is about how members look, the other is about whether one column can safely stand in for another during routing.

This is also different from a **dimension alias**, which is about one physical table playing several business roles (merchant city versus payment city). An attribute relationship is about two columns *on the same dimension* describing the same member. See [Dimension Aliases](dimension-aliases.md) for that separate concept.

---

## When to declare a relationship

Declare a **bijection** when:

- Two columns on a dimension are genuinely interchangeable identifiers of the same member (code and name, internal ID and external ID), and both are fully populated.
- Analysts group by one column but your aggregates are built on the other, and you want those queries accelerated with no loss of any statistic.

Declare a **many-to-one** relationship when:

- One column is a coarser attribute of the member (a city's country, a product's category), and you want additive measures (sums, counts) at the coarser level served from a finer aggregate.

Do **not** declare a relationship when:

- The columns are not actually a stable mapping in the data — the proof will fail and nothing will be gained.
- Either column has nulls — REJECT_NULL will keep it untrusted until the data is cleaned.
- You only want a friendlier caption. That is what the display column is for.

---

## Pitfalls

- **Assuming the display column already accelerates.** It never does. Declare the relationship.
- **Declaring a bijection that is only many-to-one.** If two keys can ever share a detail, it is not a bijection. Tessallite will catch this at proof time and refuse to trust it, but you save a deploy cycle by classifying it correctly up front.
- **Declaring on a column with nulls.** REJECT_NULL keeps it untrusted. Clean the data first.
- **Expecting a many-to-one link to serve distinct counts or medians.** It cannot — those statistics do not survive a roll-up. Only a bijection preserves every statistic.
- **Expecting trust to survive a data change.** A relationship is proven against a specific deployed version and data state. When the model is redeployed or the underlying data moves, it is re-proven. If the mapping no longer holds, it fails closed and queries return to the source. This is correct behaviour, not a fault.

---

## How relationships are declared (multi-select add)

You declare relationships in the model builder, on the dimension they belong to. The flow is designed for bulk: you can select several columns at once, optionally check them for 1:1 validity before adding, and add them all in one action.

1. Open the model in the model builder and select the **Dimensions** panel.
2. Find the dimension whose key column you want to relate, and click its **edit** (pencil) icon to open the **Edit dimension** dialog.
3. Scroll to the **Attribute relationships** section inside that dialog. It lists any relationships already declared on the dimension, each with its detail column and current proof status.
4. Click **Add relationship**. A list of candidate columns appears. Candidates come from **both** the dimension table and the fact table joined to it (denormalised columns on the fact, such as `country_name` alongside the `country_id` FK, are valid candidates). Key columns, join (FK) columns, calculated columns, user-defined attributes, and columns already declared are excluded. Each candidate is labelled with the table it belongs to ([dim] or [fact]).
5. **Select** the columns you want to declare as bijection details by checking the boxes next to them. You can select as many as you need.
6. (Optional) Click **Validate selection** to run a fast advisory 1:1 check against the live source data. Each selected column receives a badge: **Looks 1:1** (all checks passed), **Not 1:1** (a violation was found), or **Check failed** (an error occurred). This check is **advisory only** -- it gives you fast feedback but does not grant serving. The authoritative proof runs on deploy. A column that shows "Not 1:1" in the advisory check will also fail the deploy proof and never accelerate queries.
7. Click **Add N selected**. Each selected column is declared as a bijection relationship. All appear in the declared-relationships table with a **Declared** status.

### What happens when you add a bijection detail

When you declare a bijection detail attribute, Tessallite does three things automatically:

- **Auto-adds a dimension.** The detail column is added to the dimension list as a new dimension entry, marked "detail of [owning dimension]". This makes the detail column available for analysts to group by. The auto-added dimension cannot be deleted directly from the dimension list while it is an active detail attribute -- you must remove the detail declaration first.
- **Symmetric pair sync.** If the owning dimension is part of a fact-dimension join, the declaration is automatically mirrored to the other side of the pair. This ensures both sides of the pair see the detail.
- **Provenance chip.** The auto-added dimension shows a "detail of [X]" chip in the Dimensions panel so you can tell at a glance which dimensions are detail attributes and which are independent.

### Advisory validate vs deploy proof

There are **two separate checks**, and they mean different things:

- **Advisory validate** (the "Validate selection" button): a fast, cheap check that runs on demand against the source table. It tells you whether the data *looks* 1:1 right now. It is useful for catching obvious problems before you commit, but it does not prove anything for serving purposes and is never stored.
- **Deploy proof** (the status chip: Declared / Proven / Broken / Stale / Error): the authoritative, complete-data check that runs on every model deploy. Only a deploy proof can promote a relationship to Proven and enable acceleration. A relationship that shows Proven on deploy is trusted for live routing until the model is redeployed or the data changes.

### Which columns, and where

A relationship is always declared **on the dimension**, between that dimension's **key column** and one **detail column**. The detail column can live on either the dimension table or the fact table:

- **Dimension-table detail:** the common case. The detail is on the same table as the key (e.g. `d_country.country_name`). The advisory check runs against the dimension table (cheap, one row per member).
- **Fact-table detail:** a denormalised column on the fact table (e.g. `ftable.country_name` alongside `ftable.country_id`). The advisory check runs against the fact table (bounded). Note: serving of fact-table details through the relabel path is not yet supported by the routing engine. The declaration is valid and persisted, but the detail will not accelerate queries until a future engine update. The UI indicates this with a "fact-table column (declaration only)" badge.

**You do not choose the key.** The key side is fixed to the dimension's own key column. The form only asks for the detail columns.

### Deleting a relationship: downstream safety and aggregate retirement

When you delete a relationship, Tessallite shows you what depends on it before anything is removed:

- **Linked dimensions:** auto-added dimensions created by this relationship (and its symmetric pair) are listed. They will be removed.
- **Affected aggregates:** aggregates whose grain includes any of the linked dimensions are listed. You are asked whether to **retire** these aggregates (which stops them from serving and drops their physical tables) or delete the relationship without retiring them.

This downstream-safety check ensures you never silently break a query, a report, or an aggregate by removing a relationship.

**Worked mapping.** Suppose a fact table `ftable` has a `country` column that joins to `country_code` in a `d_country` dimension, and `d_country` also carries `country_name` and `country_iso_code`. You want reports grouped by the name or the ISO code to be served from an aggregate built on the code. You declare it like this:

1. Open **`d_country`** in the Dimensions panel and edit it. Its key column is `country_code`.
2. Click **Add relationship**. Select **`country_name`** and **`country_iso_code`** from the candidate list.
3. (Optional) Click **Validate selection** to check both columns for 1:1 validity. Both should show "Looks 1:1".
4. Click **Add 2 selected**. Both relationships are declared. Two new dimension entries appear in the Dimensions panel, each marked "detail of d_country".
5. **Deploy.** Both are proven against the full data and show **Proven**.
6. From then on, a query grouped by `country_name` or `country_iso_code` is served from the `country_code` aggregate, relabelled, with byte-identical numbers.

A relationship needs a **physical key column**, so the section is only available on dimensions backed by a source column. A user-defined or calculated dimension (no physical key) shows a short note instead of the table.

> Verification runs on deploy, not on demand. After you declare or edit a relationship, deploy the model to see its proof status update.

A relationship lives in the model's definition, so it also travels with the model through export and import — a relationship declared in one environment arrives in the next, and is re-proven there on deploy.

Two further controls decide whether a proven relationship actually speeds anything up:

- A per-model build control decides whether Tessallite builds the enriched aggregates that carry the detail column alongside the key. See the derived-expression auto-build setting in [Model Configuration](../admin/model-configuration.md).
- A system-wide serving control decides whether proven relationships route live queries or are only observed. When either control is off, a declared and proven relationship changes nothing about how queries run — they keep returning correct answers from the ordinary source path.

---

## Related

- [Define Dimensions](define-dimensions.md)
- [Dimension Aliases](dimension-aliases.md)
- [Dimensions and Measures](../concepts/dimensions-and-measures.md)
- [Query Routing](../concepts/query-routing.md)
- [Configure Aggregates](configure-aggregates.md)
- [Model Configuration](../admin/model-configuration.md)

---

← [Dimension Aliases](dimension-aliases.md) | [Home](../index.md) | [Business Glossary →](business-glossary.md)
