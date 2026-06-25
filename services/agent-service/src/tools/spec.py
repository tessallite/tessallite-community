"""Tool spec for the answer LLM (B1: query / clarify / refuse; B3: run_recipe).

The LLM is required to emit a single JSON object whose top-level key is
the tool name. We use JSON tool calls (rather than provider-native
tool_use) because we run multiple providers behind a thin completion
shim — the JSON contract is the same shape across all of them.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from src.recipes.eval import check_node_shape
from src.tools.expressions import (
    DimRef,
    ExpressionError,
    PredRef,
    ProjRef,
    is_structured_predicate,
    normalize_dimension,
    normalize_dimensions,
    normalize_filter,
    normalize_projection,
    predicate_has_aggregate,
)

logger = logging.getLogger(__name__)


_SPEC_PREAMBLE = """\
You MUST respond with exactly one JSON object and nothing else. No prose,
no markdown fences, no preamble. The object MUST have exactly one of these
top-level keys:

  "query":              ground the answer in semantic-layer data.
  "compound_query":     compare, combine, or derive values from multiple sub-queries.
  "run_recipe":         invoke a configured cross-model recipe by id.
  "evaluate_kpi":       evaluate a named KPI to get its current value, goal, status, and trend.
  "preview_named_set":  preview the members of a named set.
  "create_aggregate":   create a materialised aggregate table via the optimizer.
  "clarify":            ask the user one targeted clarifying question.
  "refuse":             decline policy-violating, unsupported, invalid, or out-of-scope requests.

Schemas:
"""

_QUERY_SCHEMA = """\
{
  "query": {
    "model_id":   "<uuid of an allow-listed model>",
    "measures":   ["<measure name>", ...],
    "dimensions": [
      "<dimension name>",
      {"name": "<date dimension>", "grain": "year|quarter|month|week|day"},
      {"expr": <expression node>, "alias": "<output column name>"}
    ],
    "projections": [
      {"expr": <expression node>, "alias": "<output column name>"}
    ],
    "where": [
      {"name": "<dimension_or_filterable_measure>",
       "op": "eq|in|between|like|is_null|is_not_null|gt|gte|lt|lte",
       "value": <scalar | list | [lo, hi]>},
      {"left": <expression node>, "op": "eq|neq|gt|gte|lt|lte|like|in|between|is_null|is_not_null",
       "right": <expression node | [node, ...] | [lo, hi] | omitted>},
      {"and": [<predicate>, ...]}, {"or": [<predicate>, ...]}, {"not": <predicate>}
    ],
    "having": [
      {"name": "<measure name>",
       "op": "eq|in|between|gt|gte|lt|lte",
       "value": <scalar | list | [lo, hi]>},
      {"left": <aggregate expression node>, "op": "gt|gte|lt|lte|eq|neq",
       "right": <expression node>}
    ],
    "sort": [
      {"name": "<measure_or_dimension>", "direction": "asc|desc"}
    ],
    "limit":      <int 1..1000, OPTIONAL. OMIT entirely unless the user explicitly asks for a specific number of rows (e.g. "top 10", "last 50"). A sensible default applies when omitted.>
  }
}

Use "where" for row-level filters before aggregation.
Use "having" for aggregate filters after grouping.
Use "sort" for ranking and ordering.
If no where, having, or sort is needed, output an empty array for that field."""

_QUERY_SCHEMA_WITH_CHART = """\
{
  "query": {
    "model_id":   "<uuid of an allow-listed model>",
    "measures":   ["<measure name>", ...],
    "dimensions": [
      "<dimension name>",
      {"name": "<date dimension>", "grain": "year|quarter|month|week|day"},
      {"expr": <expression node>, "alias": "<output column name>"}
    ],
    "projections": [
      {"expr": <expression node>, "alias": "<output column name>"}
    ],
    "where": [
      {"name": "<dimension_or_filterable_measure>",
       "op": "eq|in|between|like|is_null|is_not_null|gt|gte|lt|lte",
       "value": <scalar | list | [lo, hi]>},
      {"left": <expression node>, "op": "eq|neq|gt|gte|lt|lte|like|in|between|is_null|is_not_null",
       "right": <expression node | [node, ...] | [lo, hi] | omitted>},
      {"and": [<predicate>, ...]}, {"or": [<predicate>, ...]}, {"not": <predicate>}
    ],
    "having": [
      {"name": "<measure name>",
       "op": "eq|in|between|gt|gte|lt|lte",
       "value": <scalar | list | [lo, hi]>},
      {"left": <aggregate expression node>, "op": "gt|gte|lt|lte|eq|neq",
       "right": <expression node>}
    ],
    "sort": [
      {"name": "<measure_or_dimension>", "direction": "asc|desc"}
    ],
    "limit":      <int 1..1000, OPTIONAL. OMIT entirely unless the user explicitly asks for a specific number of rows (e.g. "top 10", "last 50"). A sensible default applies when omitted.>,
    "chart_type": "<optional — see chart guidance below>"
  }
}

Use "where" for row-level filters before aggregation.
Use "having" for aggregate filters after grouping.
Use "sort" for ranking and ordering.
If no where, having, or sort is needed, output an empty array for that field.

chart_type (ALWAYS include — pick the best chart for the data; use "none" only when the result is genuinely unsuitable for any visual):
  bar         — category comparisons (vertical bars)
  h_bar       — horizontal bars (best when >8 categories)
  line        — time series or trends
  pie         — proportions: 2–7 categories, all values positive, no time dimension.
                NEVER use pie for a single value — use kpi instead.
                NEVER use pie when the result has only one row.
  grouped_bar — multi-measure comparisons across categories
  stacked_bar — stacked multi-measure comparisons across categories
  kpi         — single aggregate value with no grouping dimension (e.g. total revenue)
  none        — data table only (use sparingly)

CHART SELECTION RULES (apply in order before choosing chart_type):
  1. Single value or single row → ALWAYS kpi. No other chart type is valid.
     This applies regardless of how the user phrases the request.
  2. No grouping dimension in the query → kpi.
  3. Time-based dimension present → prefer line; never pie.
  4. More than 7 categories → prefer h_bar over bar; never pie.
  5. Multiple measures, one dimension → grouped_bar or stacked_bar.
  6. Any zero or negative value in the result → bar or h_bar; never pie.
  7. pie is valid only when: 2–7 categories, all positive, no time dimension, more than one row."""

_OTHER_SCHEMAS = """
{
  "run_recipe": {
    "recipe_id":  "<uuid of a configured cross-model recipe>",
    "parameters": {"<parameter_name>": <string, number, date, or boolean>, ...}
  }
}

{
  "evaluate_kpi": {
    "model_id": "<uuid of the model containing the KPI>",
    "kpi_id":   "<uuid of the KPI to evaluate>"
  }
}

{
  "preview_named_set": {
    "model_id":      "<uuid of the model containing the named set>",
    "named_set_id":  "<uuid of the named set to preview>"
  }
}

{
  "create_aggregate": {
    "model_id":   "<uuid of an allow-listed model>",
    "measures":   ["<measure name>", ...],
    "dimensions": ["<dimension name>", ...],
    "description": "<short human-readable reason for creating this aggregate>"
  }
}
Use create_aggregate when the user explicitly asks to create, build, or add
an aggregate table for specific measures and dimensions. This calls the
optimizer API to materialise the aggregate. Requires modeller or admin role.

{
  "clarify": {
    "question": "<one short question to the user>"
  }
}

{
  "refuse": {
    "reason":   "policy_denied_topic|out_of_scope|unsupported_metric|no_valid_model|invalid_request|other",
    "message":  "<one sentence, under 20 words, shown to the user>"
  }
}"""

_COMPOUND_QUERY_SCHEMA = """
{
  "compound_query": {
    "steps": [
      {
        "name": "<short lowercase identifier, e.g. germany, jan, total>",
        "model_id": "<uuid of an allow-listed model>",
        "measures": ["<measure name>", ...],
        "dimensions": [
          "<dimension name>",
          {"name": "<date dimension>", "grain": "year|quarter|month|week|day"},
          {"expr": <expression node>, "alias": "<output column name>"}
        ],
        "where": [<same filter format as query>],
        "having": [<same having format as query>],
        "sort": [<same sort format as query>],
        "limit": <int 1..1000, OPTIONAL. OMIT entirely unless the user explicitly asks for a specific number of rows (e.g. "top 10", "last 50"). A sensible default applies when omitted.>
      }
    ],
    "expression": <EXPRESSION TREE combining step results (see EXPRESSION TREE rules). A node is one of: {"const": <number>}, {"ref": {"step": "<step name>", "measure": "<measure name>"}}, or {"op": "<add|sub|mul|div|round|...>", "args": [<node>, ...]}>,
    "result_label": "<human-readable label for the computed value>"
  }
}"""

_COMPOUND_QUERY_SCHEMA_WITH_CHART = """
{
  "compound_query": {
    "steps": [
      {
        "name": "<short lowercase identifier, e.g. germany, jan, total>",
        "model_id": "<uuid of an allow-listed model>",
        "measures": ["<measure name>", ...],
        "dimensions": [
          "<dimension name>",
          {"name": "<date dimension>", "grain": "year|quarter|month|week|day"},
          {"expr": <expression node>, "alias": "<output column name>"}
        ],
        "where": [<same filter format as query>],
        "having": [<same having format as query>],
        "sort": [<same sort format as query>],
        "limit": <int 1..1000, OPTIONAL. OMIT entirely unless the user explicitly asks for a specific number of rows (e.g. "top 10", "last 50"). A sensible default applies when omitted.>
      }
    ],
    "expression": <EXPRESSION TREE combining step results (see EXPRESSION TREE rules). A node is one of: {"const": <number>}, {"ref": {"step": "<step name>", "measure": "<measure name>"}}, or {"op": "<add|sub|mul|div|round|...>", "args": [<node>, ...]}>,
    "result_label": "<human-readable label for the computed value>",
    "chart_type": "<REQUIRED when user asks for a graph/chart/plot. Values: kpi (scalar), bar, h_bar, line, pie, grouped_bar, stacked_bar. Use line for trends over time, bar for comparisons, pie for proportions (2–7 categories, all positive, never a single value).>"
  }
}"""

_COMPOUND_QUERY_RULES = """
COMPOUND QUERY RULES:
- Use "compound_query" ONLY when the answer requires combining results
  from two or more sub-queries (e.g. percentage, ratio, difference,
  period-over-period growth rate, comparison). A simple breakdown by
  country or time is a single query — not a compound query. Use
  compound_query for growth rate only when computing a ratio between
  two separate sub-queries (e.g. this month vs last month); a single
  time-dimension query is sufficient for a plain trend.
- MINIMISE the number of steps. Use dimensions within steps instead of
  creating separate steps per dimension value. For example, to compare
  Germany vs worldwide across months, use 2 steps (germany + worldwide)
  each with a month dimension — NOT one step per month.
- Steps can return multiple rows via dimensions. When steps share the
  same dimensions, the expression is applied row-by-row after aligning
  rows on matching dimension values (like a join). If one step returns
  a single row and another returns multiple rows, the single value
  expands across all rows of the multi-row step.
- Each step carries its own model_id. Prefer the primary model for all
  steps. Only use different models across steps when semantically required.
- Each step name must be a short, descriptive lowercase identifier
  (e.g. "germany", "worldwide", "jan", "feb"). Max 30 characters,
  alphanumeric + underscore only. Each step name must be unique within
  the compound query.
- The expression is an EXPRESSION TREE (structured JSON), NOT a formula
  string. It must produce ONE value per row. Each node is exactly one of:
    {"const": <number>}                              a literal number
    {"ref": {"step": "<name>", "measure": "<name>"}}  a step's measure value
    {"op": "<name>", "args": [<node>, ...]}           an operation
  A step's measure is referenced ONLY via a "ref" node — never by writing
  "step.measure" as text. Because step names are carried as plain data, a
  step may be named anything (even "global", "class", "for") with no effect.
- Allowed "op" values and argument counts:
    add sub mul div floordiv mod pow            (exactly 2 args)
    eq ne lt le gt ge                           (exactly 2 args, returns bool)
    and or                                      (2+ args)   not  neg            (1 arg)
    round                                       (1 or 2 args)
    min max sum                                 (1+ args)   abs  len            (1 arg)
    if                                          (exactly 3 args: cond, then, else)
- Example: round(germany.transaction_amount / worldwide.transaction_amount * 100, 2)
  becomes
    {"op": "round", "args": [
      {"op": "mul", "args": [
        {"op": "div", "args": [
          {"ref": {"step": "germany", "measure": "transaction_amount"}},
          {"ref": {"step": "worldwide", "measure": "transaction_amount"}}]},
        {"const": 100}]},
      {"const": 2}]}
- To rank by a derived expression, do not sort within individual
  steps. Return all rows — the ranking is visible from the computed
  result.
- Division by zero returns null. round(null, n) returns null.
  The narration handles null results.
- result_label should be a short description of what the computed
  value represents.
- Do NOT use compound_query when a single query can answer the question.
  A breakdown by country with totals is a single query, not a compound query.

COMPOUND QUERY TRIGGERS — use compound_query when the question asks for:
- "percentage of" or "% of" or "share of" → numerator / denominator * 100
- "ratio of X to Y" → X / Y
- "rate" (e.g. refund rate, chargeback rate) → part_amount / total_amount * 100
- "compared to" or "versus" when the user wants a computed difference or ratio
- "relative to" or "proportion" → part / whole
If a ratio, percentage, or rate can be computed from two sub-queries using
available measures, use compound_query. Do NOT refuse with "unsupported_metric"
when the individual measures exist. Do NOT dump raw numerator and denominator
values without computing the ratio — that forces the user to do the maths.

Example — percentage of total (scalar):
  Question: "What percentage of transactions are from Germany in January?"
  {
    "compound_query": {
      "steps": [
        {"name": "germany", "model_id": "...",
         "measures": ["transaction_amount"],
         "dimensions": [], "where": [
           {"name": "country_code", "op": "eq", "value": "DE"},
           {"name": "business_date", "op": "between",
            "value": ["2026-01-01", "2026-01-31"]}
         ], "having": [], "sort": []},
        {"name": "worldwide", "model_id": "...",
         "measures": ["transaction_amount"],
         "dimensions": [], "where": [
           {"name": "business_date", "op": "between",
            "value": ["2026-01-01", "2026-01-31"]}
         ], "having": [], "sort": []}
      ],
      "expression": {"op": "round", "args": [{"op": "mul", "args": [{"op": "div", "args": [{"ref": {"step": "germany", "measure": "transaction_amount"}}, {"ref": {"step": "worldwide", "measure": "transaction_amount"}}]}, {"const": 100}]}, {"const": 2}]},
      "result_label": "Germany share (%)"
    }
  }

Example — percentage trend across months (row-aligned):
  Question: "How does Germany's share of transactions change month by month?"
  {
    "compound_query": {
      "steps": [
        {"name": "germany", "model_id": "...",
         "measures": ["transaction_amount"],
         "dimensions": ["month_no"],
         "where": [{"name": "country_code", "op": "eq", "value": "DE"}],
         "having": [], "sort": [{"name": "month_no", "direction": "asc"}]},
        {"name": "worldwide", "model_id": "...",
         "measures": ["transaction_amount"],
         "dimensions": ["month_no"],
         "where": [], "having": [],
         "sort": [{"name": "month_no", "direction": "asc"}]}
      ],
      "expression": {"op": "round", "args": [{"op": "mul", "args": [{"op": "div", "args": [{"ref": {"step": "germany", "measure": "transaction_amount"}}, {"ref": {"step": "worldwide", "measure": "transaction_amount"}}]}, {"const": 100}]}, {"const": 2}]},
      "result_label": "Germany share (%)",
      "chart_type": "line"
    }
  }

Example — ratio by dimension (aligned rows):
  Question: "What is the fee amount as a percentage of transaction amount by payment method?"
  {
    "compound_query": {
      "steps": [
        {"name": "fees", "model_id": "...",
         "measures": ["fee_amount"],
         "dimensions": ["payment_method_name"],
         "where": [], "having": [], "sort": []},
        {"name": "transactions", "model_id": "...",
         "measures": ["transaction_amount"],
         "dimensions": ["payment_method_name"],
         "where": [], "having": [], "sort": []}
      ],
      "expression": {"op": "round", "args": [{"op": "mul", "args": [{"op": "div", "args": [{"ref": {"step": "fees", "measure": "fee_amount"}}, {"ref": {"step": "transactions", "measure": "transaction_amount"}}]}, {"const": 100}]}, {"const": 2}]},
      "result_label": "Fee percentage (%)"
    }
  }
"""

_SPEC_RULES = """
Rules:
- Safety refusal ALWAYS overrides query preference. If the request
  violates the safety policy, refuse — do not attempt to query.
- Do not invent measure or dimension names. Only use names listed in
  the AVAILABLE MODELS section. If a name is not listed, do not use it.
- Prefer querying over refusing when there is a plausible, safe
  interpretation.
- Use "clarify" when two or more equally-plausible interpretations
  exist. Do not use "refuse" for ambiguity — use "clarify" instead.
- When using "clarify", ask exactly ONE short question. Do not pack
  multiple questions into a single sentence.
- When clarifying a follow-up that references previous result values,
  ask the user to name the specific values
  (e.g. "Which merchants did you mean — could you name them?").
- When a term is both ambiguous AND unsupported, prefer "clarify" if a
  plausible valid interpretation exists. Use "refuse" only when no
  valid interpretation is possible.
- Do not output prose, markdown, or more than one JSON object.
- Use the correct "refuse.reason" value:
    policy_denied_topic  — request violates the safety policy.
    out_of_scope         — question is outside the available data.
    unsupported_metric   — metric cannot be computed from the semantic layer.
    no_valid_model       — no allow-listed model can answer the question.
    invalid_request      — the request is structurally contradictory or invalid.
    other                — use only when no other reason applies.
- Distinction between aggregate max and record lookup:
    Use a pre-aggregated max measure (e.g. max_payment_amount) when the
    user asks for the highest/lowest value as a number.
    Use sort + limit 1 when the user asks for the specific record
    (transaction, entry, row) with that extreme value.

Unsupported term rules:
- If a term cannot be mapped to any allowed measure, dimension, valid
  formula, or valid filter: use "clarify" if a close valid alternative
  exists, otherwise "refuse" with reason "unsupported_metric" or
  "out_of_scope".
- Do not invent fields. Do not use physical column names from glossary
  text. Do not guess based only on business knowledge.

WHERE rules:
- Use "where" for filters applied before aggregation.
- Allowed where fields:
  1. Dimensions from the selected model.
  2. Filterable numeric measures from the selected model, only when the
     user is filtering individual records or values (e.g. "payments
     over 1000", "transactions below 50").
- Allowed where operators:
  eq (one exact value), in (list of exact values),
  between (date or numeric range), gt, gte, lt, lte (numeric comparison),
  like (partial text match — value must include % wildcards,
    e.g. {"name": "merchant_name", "op": "like", "value": "%amazon%"}),
  is_null, is_not_null (missing/present).
- For a single value, "eq" and "in" with one element are both valid.
- An empty "in" list is ignored — no filter is applied.
  Use "is_null" to match null values.
- For "is_null" and "is_not_null", the "value" field is not used —
  omit it or set "value": null.
- Date filter format: {"name": "business_date", "op": "between",
  "value": ["YYYY-MM-DD", "YYYY-MM-DD"]}.

HAVING rules:
- Use "having" only for filters on aggregated results after grouping.
- Example: "merchants with more than 100 payments" means
  having transaction_count gt 100.
- Having fields must be valid measures from the selected model.
- If a measure appears in "having", include that measure in "measures"
  unless it is already selected.
- Allowed having operators: eq, in, between, gt, gte, lt, lte.

SORT rules:
- Use "sort" for top, bottom, highest, lowest, largest, smallest,
  best, worst, rank, and order-by requests.
- Use direction "desc" for top/highest/largest/biggest/best/most.
- Use direction "asc" for bottom/lowest/smallest/least/worst.
- Sort fields must be valid measures or dimensions from the selected model.
- If sorting by a measure, include that measure in "measures" unless
  it is already in "measures".
- If sorting by a dimension, include that dimension in "dimensions"
  unless it is already in "dimensions".
- Do not sort by a field that is neither selected nor needed to answer
  the user question.
- When the user specifies multiple sort fields, list them in the order
  given (first entry is primary, subsequent entries are secondary).

NULL DIMENSION VALUES:
- Dimension groupings may include NULL values from the source data.
  Do not add a where filter to exclude nulls unless the user explicitly
  asks to exclude them.

Limit rules:
- Include "limit" ONLY when the user explicitly asks for a specific number of rows (e.g. "top 10", "last 50"). OMIT it otherwise — a sensible default is applied, and omitting it lets the engine return a full time-series trend instead of clipping it.
- Use lower limits for "top N" / "largest" / "worst" requests.
- Maximum limit: 1000.
- If the user asks for "all", "everything", or "all records" without a
  specific number, use the default limit of 100.
- Always include "where", "having", and "sort" as arrays, even when empty.

FINAL VALIDATION GATE:
Before outputting JSON:
1. Exactly one top-level key: query, compound_query, clarify, refuse,
   run_recipe, evaluate_kpi, preview_named_set, or create_aggregate.
2. If using "query":
   a. model_id exists in AVAILABLE MODELS.
   b. Every measure exists in the selected model's measure list.
   c. Every dimension exists in the selected model's dimension list.
   d. Every where field is listed under "Filterable where fields" for
      the selected model.
   e. Every having field exists as a measure in the selected model.
   f. Every sort field exists as a measure or dimension in the selected model.
   g. If a sort field is a measure, it must appear in "measures".
      If a sort field is a dimension, it must appear in "dimensions".
   h. Every where operator is allowed for where.
   i. Every having operator is allowed for having.
   j. Every sort direction is either "asc" or "desc".
   k. No physical table names, physical column names, or glossary-only
      terms are used as field names.
   l. No alias source phrases are used — only canonical target names.
   If using "compound_query":
   a. At least 2 steps are present.
   b. Every step name is unique and matches [a-z][a-z0-9_]{0,29}.
   c. Each step follows the same field validity rules as a regular
      query (2a–2l above).
   d. The expression is an expression tree whose every "ref" node names a
      defined step and a measure declared on that step.
   e. result_label is a non-empty string.
   If using "run_recipe":
   a. recipe_id exists in CROSS-MODEL RECIPES.
   b. All required parameters are provided.
   If using "evaluate_kpi":
   a. model_id exists in AVAILABLE MODELS.
   b. kpi_id exists in the selected model's KPI list.
   c. Use this only when the user asks to evaluate a named KPI's goal,
      target, status, trend, health, or performance. For analytical
      presentation requests like "show revenue as a KPI", use "query"
      with chart_type "kpi" instead.
   If using "preview_named_set":
   a. model_id exists in AVAILABLE MODELS.
   b. named_set_id exists in the selected model's named set list.
   If using "create_aggregate":
   a. model_id exists in AVAILABLE MODELS.
   b. Every measure exists in the selected model's measure list.
   c. Every dimension exists in the selected model's dimension list.
   If using "clarify":
   a. question is a single, short, non-compound question.
   If using "refuse":
   a. reason is one of the defined enum values.
   b. message is one sentence, under 20 words.
3. If validation fails:
   - Optional fields are: chart_type, extra sort entries not needed to
     answer the core question, having clauses no longer relevant.
   - Remove invalid optional fields only if the query still answers
     the user's core question.
   - Do not remove a user-requested measure, breakdown dimension, date
     filter, threshold, having condition, or ranking sort if it
     represents the core intent.
   - If the invalid part is core to the user's request, use "clarify".
   - If the concept is clearly unsupported, use "refuse".
4. For follow-ups:
   - Preserve previous valid where clauses unless explicitly changed.
   - Preserve previous valid having clauses only when still relevant.
   - Preserve previous sort only when still relevant to the follow-up.
5. Output valid JSON with no trailing commas.
"""

_EXPRESSION_DIMENSION_RULES = """
EXPRESSION DIMENSIONS (Bug-5349 — prefer a plain "<name>" string whenever a
bare dimension answers the question; only use the structured forms below when a
function is genuinely required):
- Grain shorthand — to bucket a raw date/timestamp dimension by a calendar
  grain when AVAILABLE MODELS has no pre-built month/quarter/year dimension for
  it, use {"name": "<date dimension>", "grain": "month"}. Valid grains: year,
  quarter, month, week, day. This produces a DATE_TRUNC grouping, so monthly /
  quarterly / yearly / weekly trends work from any raw date column. Do NOT fall
  back to the daily dimension for a "monthly/quarterly/yearly" request when the
  grain shorthand can answer it.
- Expression object — {"expr": <node>, "alias": "<output column name>"}, where
  <node> is exactly one of:
    {"field": "<dimension name>"}
    {"literal": <string | number | bool | null>}
    {"fn": "<function>", "args": [<node>, ...]}
- Allowed functions in a dimension: date_trunc, extract, date_part, lower,
  upper, trim, concat, substring, round, abs, ceil, floor, coalesce, nullif.
  No other function name is accepted. Aggregate functions (sum, avg, min, max,
  count) are NOT valid as a grouping dimension.
- Every "field" must be a real dimension from AVAILABLE MODELS for the selected
  model. Expressions do NOT bypass field validation or persona scope.
- compound_query steps accept the same structured dimensions as query steps.
  Row alignment uses deterministic semantic alignment keys derived from the
  normalized dimension refs, not display aliases alone.

EXPRESSION WHERE FILTERS (Bug-5349 — prefer the plain {"name","op","value"}
filter whenever a bare column answers the question; use the structured forms
below only when a function, a column-to-column comparison, or OR/NOT is
genuinely required):
- Function on a column: compare a registered scalar function of a column.
    {"left": {"fn": "extract", "args": [{"literal": "month"}, {"field": "<date>"}]},
     "op": "eq", "right": {"literal": 6}}                  -> EXTRACT(MONTH FROM d) = 6
    {"left": {"fn": "lower", "args": [{"field": "<city>"}]},
     "op": "eq", "right": {"literal": "cairo"}}            -> LOWER(city) = 'cairo'
- Column-to-column (same-row comparison):
    {"left": {"field": "<settlement_date>"}, "op": "gt",
     "right": {"field": "<transaction_date>"}}
- Boolean composition: {"and": [<pred>, ...]}, {"or": [<pred>, ...]},
  {"not": <pred>}. Use these only when the plain AND-of-filters list cannot
  express the request (an OR, a negation, or a grouped predicate).
- Allowed predicate ops: eq, neq, gt, gte, lt, lte, like, in, between, is_null,
  is_not_null. For "in" the right side is a list of nodes; for "between" it is a
  two-element list [lo, hi]; for is_null / is_not_null omit the right side.

EXPRESSION PROJECTIONS (Bug-5349 — derived/computed SELECT columns):
- Use "projections" for a computed output column that is neither a bare measure
  nor a grouping dimension: arithmetic, ROUND, CONCAT, SUBSTRING, a date part,
  or a CASE bucket. Each entry is {"expr": <node>, "alias": "<column name>"}.
- Inline arithmetic node: {"arith": "add|sub|mul|div", "left": <node>,
  "right": <node>}.
- CASE node (searched form): {"case": [{"when": <predicate>, "then": <node>},
  ...], "else": <node | omitted>}.  Example bucket:
    {"expr": {"case": [{"when": {"left": {"field": "<amount>"}, "op": "gt",
      "right": {"literal": 1000}}, "then": {"literal": "high"}}],
      "else": {"literal": "low"}}, "alias": "amount_band"}
- Aggregate functions (sum, avg, min, max, count, count_distinct) ARE allowed in
  a projection and in HAVING (e.g. ROUND(SUM(amount), 2)); they are NOT allowed
  in a grouping dimension.

EXPRESSION HAVING (Bug-5349 — computed aggregate thresholds):
- For a ratio of aggregates or any computed aggregate threshold, use the
  structured HAVING form. The left side MUST contain an aggregate.
    {"left": {"arith": "div", "left": {"fn": "sum", "args": [{"field": "<fees>"}]},
      "right": {"fn": "sum", "args": [{"field": "<amount>"}]}},
     "op": "gt", "right": {"literal": 0.5}}        -> HAVING SUM(fees)/SUM(amount) > 0.5
- A structured HAVING predicate that references no aggregate is rejected; use
  "where" for row-level filters instead.

Every "field" in any expression (filter, projection, having) must be a real
measure or dimension from AVAILABLE MODELS for the selected model. Expressions
in EVERY clause are subject to the same field validation and persona scope as
bare names — a function cannot hide a hidden column.
"""

TOOL_SPEC_TEXT = _SPEC_PREAMBLE + "\n" + _QUERY_SCHEMA + "\n" + _COMPOUND_QUERY_SCHEMA + "\n" + _OTHER_SCHEMAS + "\n" + _SPEC_RULES + "\n" + _EXPRESSION_DIMENSION_RULES + "\n" + _COMPOUND_QUERY_RULES


@dataclass
class QueryToolCall:
    model_id: str
    measures: list[str]
    dimensions: list[str]
    where: list[dict[str, Any]]
    having: list[dict[str, Any]]
    sort: list[dict[str, Any]]
    limit: int = 100
    # True when the LLM emitted an explicit `limit` (the user asked for a
    # specific row count); False when it was defaulted. The trend-floor logic
    # (exec/query.py) must never override a user-requested limit (H2-001).
    limit_explicit: bool = False
    chart_type: Optional[str] = None
    # Bug-5349 / decision D1 — typed companion to `dimensions`. `dimensions`
    # carries the alias of each entry (bare name == alias for legacy strings);
    # `dimension_refs` carries the normalized expression AST that drives SQL
    # composition, persona scope, and trend detection. Defaults to bare refs
    # derived from `dimensions` so direct constructors (and existing tests)
    # keep working without supplying refs.
    dimension_refs: Optional[list[DimRef]] = None
    # Bug-5349 Phase 3 — computed SELECT projection columns
    # ({"expr": <node>, "alias": ..}). Bare measures/dimensions stay in their
    # own lists (byte-for-byte legacy SQL); these add derived columns.
    projection_refs: list[ProjRef] = None  # type: ignore[assignment]
    # Bug-5349 Phase 2/3 — typed companions to `where`/`having` carrying the
    # STRUCTURED predicate entries only (function-on-column, column-to-column,
    # OR/NOT, ratio-of-aggregates). Legacy flat {name/op/value} entries stay in
    # `where`/`having` and render through the legacy path unchanged.
    where_refs: list[PredRef] = None  # type: ignore[assignment]
    having_refs: list[PredRef] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.dimension_refs is None:
            self.dimension_refs = normalize_dimensions(list(self.dimensions))
        if self.projection_refs is None:
            self.projection_refs = []
        if self.where_refs is None:
            self.where_refs = []
        if self.having_refs is None:
            self.having_refs = []


@dataclass
class RunRecipeToolCall:
    recipe_id: str
    parameters: dict[str, Any]


@dataclass
class ClarifyToolCall:
    question: str


@dataclass
class RefuseToolCall:
    reason: str
    message: str


@dataclass
class CompoundStep:
    name: str
    model_id: str
    measures: list[str]
    dimensions: list[str]
    where: list[dict[str, Any]]
    having: list[dict[str, Any]]
    sort: list[dict[str, Any]]
    limit: int = 100
    limit_explicit: bool = False  # see QueryToolCall.limit_explicit (H2-001)
    # Bug-5349 / Phase 2B — shape parity with QueryToolCall. Compound steps
    # preserve structured dimension refs. The branch executor derives semantic
    # alignment aliases from these refs before row-aligned expression evaluation.
    dimension_refs: Optional[list[DimRef]] = None
    # Bug-5349 Phase 3 / D5 — shape parity for computed projection columns and
    # structured predicates inside a compound step.
    projection_refs: list[ProjRef] = None  # type: ignore[assignment]
    where_refs: list[PredRef] = None  # type: ignore[assignment]
    having_refs: list[PredRef] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.dimension_refs is None:
            self.dimension_refs = normalize_dimensions(list(self.dimensions))
        if self.projection_refs is None:
            self.projection_refs = []
        if self.where_refs is None:
            self.where_refs = []
        if self.having_refs is None:
            self.having_refs = []


@dataclass
class CompoundQueryToolCall:
    steps: list[CompoundStep]
    expression: dict[str, Any]  # semantic ExprNode tree (Bug-5346), never a string
    result_label: str
    chart_type: Optional[str] = None


@dataclass
class EvaluateKpiToolCall:
    model_id: str
    kpi_id: str


@dataclass
class PreviewNamedSetToolCall:
    model_id: str
    named_set_id: str


@dataclass
class CreateAggregateToolCall:
    model_id: str
    measures: list[str]
    dimensions: list[str]
    description: str


ToolCall = QueryToolCall | RunRecipeToolCall | CompoundQueryToolCall | EvaluateKpiToolCall | PreviewNamedSetToolCall | CreateAggregateToolCall | ClarifyToolCall | RefuseToolCall

def make_tool_spec(chart_type_selector: str = "none") -> str:
    """Return the tool spec text, using chart-aware schemas when selector=llm or auto."""
    if chart_type_selector in ("llm", "auto"):
        query = _QUERY_SCHEMA_WITH_CHART
        compound = _COMPOUND_QUERY_SCHEMA_WITH_CHART
    else:
        query = _QUERY_SCHEMA
        compound = _COMPOUND_QUERY_SCHEMA
    return (
        _SPEC_PREAMBLE + "\n" + query + "\n"
        + compound + "\n"
        + _OTHER_SCHEMAS + "\n"
        + _SPEC_RULES + "\n"
        + _EXPRESSION_DIMENSION_RULES + "\n"
        + _COMPOUND_QUERY_RULES
    )


class ToolCallParseError(ValueError):
    """The LLM produced something we cannot interpret as a tool call."""


def parse_tool_call(raw: str) -> ToolCall:
    """Pull the first JSON object out of `raw` and validate it."""
    raw = raw.strip()
    # Strip optional ```json fences.
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        # Try to find a JSON object inside larger text.
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            raise ToolCallParseError(
                f"LLM did not return JSON: {raw[:200]!r}"
            ) from exc
        try:
            obj = json.loads(match.group(0))
        except json.JSONDecodeError as inner_exc:
            raise ToolCallParseError(
                f"LLM returned malformed JSON object: {raw[:200]!r}"
            ) from inner_exc

    if not isinstance(obj, dict) or len(obj) != 1:
        raise ToolCallParseError(
            "Tool call must be a JSON object with exactly one top-level key."
        )

    (name, body), = obj.items()
    if not isinstance(body, dict):
        raise ToolCallParseError(f"Tool call body for {name!r} must be an object.")

    if name == "query":
        return _parse_query(body)
    if name == "run_recipe":
        return _parse_run_recipe(body)
    if name == "clarify":
        return _parse_clarify(body)
    if name == "refuse":
        return _parse_refuse(body)
    if name == "compound_query":
        return _parse_compound_query(body)
    if name == "evaluate_kpi":
        return _parse_evaluate_kpi(body)
    if name == "preview_named_set":
        return _parse_preview_named_set(body)
    if name == "create_aggregate":
        return _parse_create_aggregate(body)
    raise ToolCallParseError(f"Unknown tool name: {name!r}")


_VALID_FILTER_OPS = frozenset({
    "eq", "neq", "gt", "gte", "lt", "lte",
    "in", "between", "like", "is_null", "is_not_null",
})


def _parse_filter_list(
    body: dict[str, Any], key: str, *, ctx: str = "query"
) -> tuple[list[dict[str, Any]], list[PredRef]]:
    """Split a where/having list into (legacy flat entries, structured refs).

    Legacy ``{name, op, value}`` entries render through the unchanged flat path
    (byte-for-byte back-compat). Structured entries — boolean composition
    (``and``/``or``/``not``) or comparisons (``{left, op, right}``) — are
    normalized into the typed predicate AST (Bug-5349 Phase 2/3). A malformed
    structured predicate RAISES (fail-closed, R3) — it is never silently
    dropped, which would loosen a filter or leak a HAVING gate."""
    raw = body.get(key) or []
    if not isinstance(raw, list):
        raise ToolCallParseError(f"{ctx}.{key} must be a list.")
    clause = "where" if key == "where" else "having"
    flat: list[dict[str, Any]] = []
    refs: list[PredRef] = []
    for f in raw:
        if not isinstance(f, dict):
            raise ToolCallParseError(f"Each {key} entry must be an object.")
        if is_structured_predicate(f):
            try:
                ref = normalize_filter(f, clause=clause)
            except ExpressionError as exc:
                raise ToolCallParseError(f"{ctx}.{key} invalid: {exc}")
            if key == "having" and not predicate_has_aggregate(ref.node):
                raise ToolCallParseError(
                    f"{ctx}.having predicate must reference an aggregate "
                    f"(e.g. SUM/AVG/COUNT); use 'where' for row-level filters."
                )
            refs.append(ref)
            continue
        # legacy flat filter
        if "name" not in f or "op" not in f:
            raise ToolCallParseError(f"{key} entry requires 'name' and 'op'.")
        op = f["op"]
        if op not in _VALID_FILTER_OPS:
            raise ToolCallParseError(
                f"Invalid {key} operator {op!r} for field {f['name']!r}. "
                f"Valid operators: {', '.join(sorted(_VALID_FILTER_OPS))}"
            )
        if op in ("is_null", "is_not_null"):
            f.setdefault("value", None)
        flat.append(f)
    return flat, refs


def _parse_projections(raw: Any, *, ctx: str, taken: set[str]) -> list[ProjRef]:
    """Normalize the optional ``projections`` list (computed SELECT columns)."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ToolCallParseError(f"{ctx}.projections must be a list.")
    refs: list[ProjRef] = []
    for p in raw:
        try:
            refs.append(normalize_projection(p, taken))
        except ExpressionError as exc:
            raise ToolCallParseError(f"{ctx}.projections invalid: {exc}")
    return refs


def _parse_limit(raw: Any, field: str) -> tuple[int, bool]:
    """Return (limit, explicit). ``raw is None`` means the LLM omitted the limit
    (it was defaulted) — provenance the trend-floor logic relies on (H2-001).
    An explicit value is validated to 1..1000."""
    if raw is None:
        return 100, False
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1 or raw > 1000:
        raise ToolCallParseError(
            f"{field} must be an integer between 1 and 1000. Got: {raw!r}"
        )
    return raw, True


def _parse_sort_list(body: dict[str, Any]) -> list[dict[str, Any]]:
    raw = body.get("sort") or []
    if not isinstance(raw, list):
        raise ToolCallParseError("query.sort must be a list.")
    result: list[dict[str, Any]] = []
    for s in raw:
        if not isinstance(s, dict):
            raise ToolCallParseError("Each sort entry must be an object.")
        if "name" not in s:
            raise ToolCallParseError("sort entry requires 'name'.")
        direction = s.get("direction", "desc")
        if direction not in ("asc", "desc"):
            raise ToolCallParseError(
                f"Invalid sort direction {direction!r} for field {s['name']!r}. "
                f"Valid directions: asc, desc"
            )
        result.append({"name": s["name"], "direction": direction})
    return result


def _parse_dimensions(
    raw: Any, *, ctx: str, allow_expr: bool, taken: set[str] | None = None
) -> tuple[list[str], list[DimRef]]:
    """Normalize a dimensions list into (aliases, typed refs). Accepts bare
    name strings always; grain shorthand and expression objects only when
    ``allow_expr`` (the single ``query`` path, decision D4/D5). Returns aliases
    for the legacy ``dimensions`` field and typed refs for SQL composition.
    ``taken`` (when given) shares the alias-collision namespace with computed
    projections so a derived column cannot reuse a dimension alias OR a selected
    measure's output alias (R4); the caller seeds the measure names into
    ``taken`` before parsing projections."""
    if not isinstance(raw, list):
        raise ToolCallParseError(f"{ctx}.dimensions must be a list.")
    if taken is None:
        taken = set()
    refs: list[DimRef] = []
    for d in raw:
        if not allow_expr and not isinstance(d, str):
            raise ToolCallParseError(
                f"{ctx}.dimensions accepts bare dimension names only; "
                f"expression objects are not supported here."
            )
        try:
            refs.append(normalize_dimension(d, taken))
        except ExpressionError as exc:
            raise ToolCallParseError(f"{ctx}.dimensions invalid: {exc}")
    return [r.alias for r in refs], refs


def _parse_query(body: dict[str, Any]) -> QueryToolCall:
    model_id = body.get("model_id")
    if not isinstance(model_id, str) or not model_id:
        raise ToolCallParseError("query.model_id missing or not a string.")
    measures = body.get("measures") or []
    if not isinstance(measures, list) or not all(isinstance(m, str) for m in measures):
        raise ToolCallParseError("query.measures must be list[str].")
    # DR-B5349-P1-02 — when a follow-up replays a previous plan, the persisted
    # `dimension_exprs` (the original bare/grain/expression entries) is
    # authoritative over `dimensions` (which carries display aliases). Consuming
    # it here reconstructs the expression refs so the grain is preserved
    # round-trip (e.g. DATE_TRUNC survives) instead of degrading to a bare alias.
    raw_dims = body.get("dimension_exprs")
    if not isinstance(raw_dims, list):
        raw_dims = body.get("dimensions") or []
    taken: set[str] = set()
    dim_aliases, dim_refs = _parse_dimensions(
        raw_dims, ctx="query", allow_expr=True, taken=taken
    )
    # Bug-5349 Phase 3 / R4 — computed projection columns share the dimension AND
    # measure alias namespace so a derived column can never collide with a
    # grouping alias OR a selected measure's output column (a duplicate output
    # alias would silently drop one column in the name-keyed result rows).
    taken |= {str(m) for m in measures}
    projection_refs = _parse_projections(
        body.get("projections"), ctx="query", taken=taken
    )
    if not measures and not dim_aliases and not projection_refs:
        raise ToolCallParseError("query needs at least one measure, dimension, or projection.")
    # Backward compat: accept old "filters" as "where" if "where" is absent.
    if "where" not in body and "filters" in body:
        body["where"] = body.pop("filters")
    where, where_refs = _parse_filter_list(body, "where")
    having, having_refs = _parse_filter_list(body, "having")
    sort = _parse_sort_list(body)
    limit, limit_explicit = _parse_limit(body.get("limit"), "query.limit")
    chart_type = body.get("chart_type")
    if chart_type is not None and not isinstance(chart_type, str):
        chart_type = None
    return QueryToolCall(
        model_id=model_id,
        measures=[str(m) for m in measures],
        dimensions=dim_aliases,
        where=where,
        having=having,
        sort=sort,
        limit=limit,
        limit_explicit=limit_explicit,
        chart_type=chart_type,
        dimension_refs=dim_refs,
        projection_refs=projection_refs,
        where_refs=where_refs,
        having_refs=having_refs,
    )


def _parse_run_recipe(body: dict[str, Any]) -> RunRecipeToolCall:
    rid = body.get("recipe_id")
    if not isinstance(rid, str) or not rid:
        raise ToolCallParseError("run_recipe.recipe_id missing or not a string.")
    params = body.get("parameters") or {}
    if not isinstance(params, dict):
        raise ToolCallParseError("run_recipe.parameters must be an object.")
    return RunRecipeToolCall(recipe_id=rid, parameters=params)


def _parse_clarify(body: dict[str, Any]) -> ClarifyToolCall:
    q = body.get("question")
    if not isinstance(q, str) or not q.strip():
        raise ToolCallParseError("clarify.question must be a non-empty string.")
    return ClarifyToolCall(question=q.strip())


def _parse_refuse(body: dict[str, Any]) -> RefuseToolCall:
    reason = body.get("reason") or "other"
    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        raise ToolCallParseError("refuse.message must be a non-empty string.")
    return RefuseToolCall(reason=str(reason), message=message.strip())


_STEP_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,29}$")


def _parse_compound_query(body: dict[str, Any]) -> CompoundQueryToolCall:
    steps_raw = body.get("steps")
    if not isinstance(steps_raw, list) or len(steps_raw) < 2:
        raise ToolCallParseError(
            "compound_query.steps must be a list with at least 2 steps."
        )

    seen_names: set[str] = set()
    steps: list[CompoundStep] = []
    for i, s in enumerate(steps_raw):
        if not isinstance(s, dict):
            raise ToolCallParseError(f"compound_query.steps[{i}] must be an object.")
        name = s.get("name")
        if not isinstance(name, str) or not _STEP_NAME_RE.match(name):
            raise ToolCallParseError(
                f"compound_query.steps[{i}].name must be lowercase alphanumeric "
                f"+ underscore, 1-30 chars. Got: {name!r}"
            )
        if name in seen_names:
            raise ToolCallParseError(
                f"Duplicate step name: {name!r}. Each step must have a unique name."
            )
        seen_names.add(name)

        model_id = s.get("model_id")
        if not isinstance(model_id, str) or not model_id:
            raise ToolCallParseError(
                f"compound_query.steps[{i}].model_id missing or not a string."
            )
        measures = s.get("measures") or []
        if not isinstance(measures, list) or not all(isinstance(m, str) for m in measures):
            raise ToolCallParseError(f"compound_query.steps[{i}].measures must be list[str].")
        # DR-B5349-P2B — as with direct queries, persisted `dimension_exprs`
        # wins over flattened display aliases when replaying a previous plan.
        raw_dims = s.get("dimension_exprs")
        if not isinstance(raw_dims, list):
            raw_dims = s.get("dimensions") or []
        step_ctx = f"compound_query.steps[{i}]"
        taken: set[str] = set()
        dim_aliases, dim_refs = _parse_dimensions(
            raw_dims, ctx=step_ctx, allow_expr=True, taken=taken
        )
        # D5 — expression projection columns and structured predicates inside a
        # compound step (shape parity with the single-query path). Seed the alias
        # namespace with measure names (R4 — no duplicate output column).
        taken |= {str(m) for m in measures}
        projection_refs = _parse_projections(
            s.get("projections"), ctx=step_ctx, taken=taken
        )
        if not measures and not dim_aliases and not projection_refs:
            raise ToolCallParseError(
                f"{step_ctx} needs at least one measure, dimension, or projection."
            )

        if "where" not in s and "filters" in s:
            s["where"] = s.pop("filters")
        where, where_refs = _parse_filter_list(s, "where", ctx=step_ctx)
        having, having_refs = _parse_filter_list(s, "having", ctx=step_ctx)
        sort = _parse_sort_list(s)
        limit, limit_explicit = _parse_limit(
            s.get("limit"), f"{step_ctx}.limit"
        )

        steps.append(CompoundStep(
            name=name,
            model_id=str(model_id),
            measures=[str(m) for m in measures],
            dimensions=dim_aliases,
            where=where,
            having=having,
            sort=sort,
            limit=limit,
            limit_explicit=limit_explicit,
            dimension_refs=dim_refs,
            projection_refs=projection_refs,
            where_refs=where_refs,
            having_refs=having_refs,
        ))

    expression = body.get("expression")
    if not isinstance(expression, dict):
        raise ToolCallParseError(
            "compound_query.expression must be an expression tree object "
            "({\"op\"|\"ref\"|\"const\": ...}), not a string."
        )
    try:
        check_node_shape(expression)
    except ValueError as exc:
        raise ToolCallParseError(f"compound_query.expression invalid: {exc}")

    result_label = body.get("result_label")
    if not isinstance(result_label, str) or not result_label.strip():
        raise ToolCallParseError("compound_query.result_label must be a non-empty string.")

    chart_type = body.get("chart_type")
    if chart_type is not None and not isinstance(chart_type, str):
        chart_type = None

    return CompoundQueryToolCall(
        steps=steps,
        expression=expression,
        result_label=result_label.strip(),
        chart_type=chart_type,
    )


def _parse_evaluate_kpi(body: dict[str, Any]) -> EvaluateKpiToolCall:
    model_id = body.get("model_id")
    if not isinstance(model_id, str) or not model_id:
        raise ToolCallParseError("evaluate_kpi.model_id missing or not a string.")
    kpi_id = body.get("kpi_id")
    if not isinstance(kpi_id, str) or not kpi_id:
        raise ToolCallParseError("evaluate_kpi.kpi_id missing or not a string.")
    return EvaluateKpiToolCall(model_id=model_id, kpi_id=kpi_id)


def _parse_preview_named_set(body: dict[str, Any]) -> PreviewNamedSetToolCall:
    model_id = body.get("model_id")
    if not isinstance(model_id, str) or not model_id:
        raise ToolCallParseError("preview_named_set.model_id missing or not a string.")
    named_set_id = body.get("named_set_id")
    if not isinstance(named_set_id, str) or not named_set_id:
        raise ToolCallParseError("preview_named_set.named_set_id missing or not a string.")
    return PreviewNamedSetToolCall(model_id=model_id, named_set_id=named_set_id)


def _parse_create_aggregate(body: dict[str, Any]) -> CreateAggregateToolCall:
    model_id = body.get("model_id")
    if not isinstance(model_id, str) or not model_id:
        raise ToolCallParseError("create_aggregate.model_id missing or not a string.")
    measures = body.get("measures") or []
    dimensions = body.get("dimensions") or []
    if not isinstance(measures, list) or not all(isinstance(m, str) for m in measures):
        raise ToolCallParseError("create_aggregate.measures must be list[str].")
    if not isinstance(dimensions, list) or not all(isinstance(d, str) for d in dimensions):
        raise ToolCallParseError("create_aggregate.dimensions must be list[str].")
    if not measures:
        raise ToolCallParseError("create_aggregate needs at least one measure.")
    if not dimensions:
        raise ToolCallParseError("create_aggregate needs at least one dimension.")
    description = body.get("description", "")
    return CreateAggregateToolCall(
        model_id=model_id,
        measures=[str(m) for m in measures],
        dimensions=[str(d) for d in dimensions],
        description=str(description),
    )


def fallback_refuse(message: str) -> RefuseToolCall:
    return RefuseToolCall(reason="internal_error", message=message)


def is_query(call: Optional[ToolCall]) -> bool:
    return isinstance(call, QueryToolCall)
