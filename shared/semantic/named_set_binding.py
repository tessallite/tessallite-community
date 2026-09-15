"""Reference extraction and probe-query construction for named sets and lists.

SQL generation rule 4 (persona layering): a derivation is visible to a persona
if, and only if, it BINDS over that persona's model query. For a named set the
derivation is its MDX expression (or, for a Tessallite Named List, its builder
definition); the probe query built here is the model query that expression sits
on top of:

    SELECT <referenced dimensions>, <aggregated referenced measures>
    FROM   <model relation>
    GROUP BY <referenced dimensions>

The caller submits that probe to the query-router's own persona-scoped path
(``POST /api/v1/explain`` with the caller's ``persona_id``); a 2xx means the set
binds for that persona and may be advertised and executed, anything else means
it does not. This module deliberately holds NO policy: it only says which model
objects a set references and what query would exercise them, so model-service,
agent-service and any future consumer reach the same verdict from one primitive
instead of three private token scans (audit rows A18, A39, A46; Bug-9877).

Reference extraction fails CLOSED by construction: every candidate it returns
must be resolvable to a real model dimension or measure by the caller, and a
candidate that resolves to neither hides the set. That is the opposite of the
bracket-token scan this replaces, which left an unresolvable token VISIBLE and
never looked at the ranking measure inside ``TopCount(...)`` at all.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from shared.connector_qualify import safe_ident

__all__ = [
    "NamedSetReferences",
    "extract_named_set_references",
    "build_binding_probe_sql",
    "mdx_reference_chains",
]


# Chain heads that are MDX grammar, not model objects. ``Measures`` is handled
# separately (it introduces a measure name); the rest can never name a
# dimension and must not be turned into a candidate.
_GRAMMAR_HEADS = {"model", "members", "all", "measures"}


@dataclass(frozen=True)
class NamedSetReferences:
    """What a named set's definition references, before persona resolution.

    ``dimension_names`` / ``measure_names`` are candidate names exactly as
    authored (case preserved). ``raw_sql`` is set only for a ``sql_query``
    Named List, whose free-hand SQL is itself the probe. ``undecidable`` is
    non-empty when the definition could not be decomposed at all — the caller
    must hide the set for any restricted persona rather than guess.
    """

    dimension_names: frozenset[str] = frozenset()
    measure_names: frozenset[str] = frozenset()
    raw_sql: str | None = None
    undecidable: tuple[str, ...] = field(default=())


def mdx_reference_chains(expression: str | None) -> list[list[str]]:
    """Split an MDX expression into its bracketed identifier CHAINS.

    ``TopCount([Customer].[Customer].[Name].Members, 5, [Measures].[Revenue])``
    yields ``[["Customer", "Customer", "Name"], ["Measures", "Revenue"]]``.

    A chain is a run of ``[...]`` segments joined by ``.``. Member keys
    (``&[...]``) are dropped — they carry arbitrary source values, never model
    object names. ``]]`` inside a segment is the MDX escape for a literal ``]``
    (see ``shared.named_list_compiler._escape_mdx_name``) and does not close the
    segment. String literals and comments are skipped so a caption or a
    commented-out fragment can never contribute a candidate.
    """
    if not expression:
        return []

    text = expression
    n = len(text)
    chains: list[list[str]] = []
    current: list[str] = []
    i = 0
    # True when the previous meaningful character was a '.' following a closing
    # bracket, i.e. the next '[' continues the same chain.
    continues = False
    quote = ""

    def _flush() -> None:
        nonlocal current
        if current:
            chains.append(current)
            current = []

    while i < n:
        ch = text[i]

        if quote:
            if ch == quote:
                quote = ""
            i += 1
            continue

        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue

        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue

        if text.startswith("//", i) or text.startswith("--", i):
            nl = text.find("\n", i + 2)
            i = n if nl < 0 else nl + 1
            continue

        if ch == "[":
            is_member_key = False
            j = i - 1
            while j >= 0 and text[j].isspace():
                j -= 1
            if j >= 0 and text[j] == "&":
                is_member_key = True

            i += 1
            buf: list[str] = []
            while i < n:
                if text[i] == "]":
                    if i + 1 < n and text[i + 1] == "]":
                        buf.append("]")
                        i += 2
                        continue
                    i += 1
                    break
                buf.append(text[i])
                i += 1

            segment = "".join(buf).strip()
            if not continues:
                _flush()
            if not is_member_key and segment:
                current.append(segment)

            # Does a '.' follow (possibly after whitespace)? Then the chain
            # continues into the next segment or a bare function/property.
            k = i
            while k < n and text[k].isspace():
                k += 1
            continues = k < n and text[k] == "."
            if continues:
                i = k + 1
                while i < n and text[i].isspace():
                    i += 1
                if i < n and text[i] not in ("[", "&"):
                    # A bare identifier after the dot (``.Members``,
                    # ``.Children``) is MDX grammar, not a model object.
                    while i < n and (text[i].isalnum() or text[i] == "_"):
                        i += 1
                    k = i
                    while k < n and text[k].isspace():
                        k += 1
                    if k < n and text[k] == ".":
                        i = k + 1
                        continues = True
                    else:
                        continues = False
            continue

        if not ch.isspace():
            # Any other syntax ends the current chain.
            if not continues:
                _flush()
            continues = False
        i += 1

    _flush()
    return chains


def _add_name(target: set[str], value: object) -> None:
    if isinstance(value, str) and value.strip():
        target.add(value.strip())


def extract_named_set_references(
    *,
    expression: str | None,
    builder_definition: dict | None,
    persisted_dimensions: str | None = None,
) -> NamedSetReferences:
    """Return the model objects a named set / named list definition references.

    The builder definition is the AUTHORITATIVE source when present — it names
    the entity and the ranking/filter measure directly, so no MDX parsing is
    needed. Only a hand-authored MDX expression falls back to
    :func:`mdx_reference_chains`.

    ``persisted_dimensions`` is the named set's ``dimensions`` column (the
    comma/semicolon-separated names the builder recorded, Bug-6329). Those
    names are FOLDED INTO the probe rather than checked separately, so the one
    bind verdict still covers them.
    """
    dims: set[str] = set()
    measures: set[str] = set()
    undecidable: list[str] = []
    raw_sql: str | None = None

    if persisted_dimensions:
        for part in str(persisted_dimensions).replace(";", ",").split(","):
            _add_name(dims, part)

    bd = builder_definition if isinstance(builder_definition, dict) else None
    btype = str(bd.get("type", "")) if bd else ""

    if btype in ("fixedMembers", "fixed"):
        _add_name(dims, bd.get("dimension"))
    elif btype in ("topN", "dynamic_top_n"):
        _add_name(dims, bd.get("entity"))
        _add_name(measures, bd.get("measure"))
    elif btype in ("filter", "filtered"):
        _add_name(dims, bd.get("entity"))
        for cond in bd.get("conditions") or []:
            if isinstance(cond, dict):
                _add_name(measures, cond.get("field"))
    elif btype == "sql_query":
        query = str(bd.get("query") or "").strip()
        if query:
            raw_sql = query
        else:
            undecidable.append("sql_query definition has no query")
    else:
        # No builder definition (or an unknown type): decompose the MDX.
        chains = mdx_reference_chains(expression)
        if not chains and not dims:
            undecidable.append("expression references no resolvable model object")
        for chain in chains:
            head = chain[0]
            if head.lower() == "measures":
                if len(chain) >= 2:
                    measures.add(chain[1])
                else:
                    undecidable.append("[Measures] with no measure name")
                continue
            if head.lower() in _GRAMMAR_HEADS:
                continue
            dims.add(head)

    return NamedSetReferences(
        dimension_names=frozenset(dims),
        measure_names=frozenset(measures),
        raw_sql=raw_sql,
        undecidable=tuple(undecidable),
    )


def build_binding_probe_sql(
    *,
    model_relation: str,
    dimension_columns: list[str],
    measure_aggregates: list[str],
) -> str:
    """Build the model query a named set's definition layers on top of.

    ``measure_aggregates`` are complete aggregate expressions (e.g.
    ``SUM("total_sales")``) as produced by the caller from each measure's own
    default aggregation, so the probe asks for exactly the aggregation the set
    would compute. Identifiers are quoted through ``shared.connector_qualify``
    (SQL generation rule 2); the router transpiles to the source dialect (rule
    1) and executes nothing — the probe is submitted to ``/explain``.
    """
    relation = safe_ident(model_relation)
    dim_idents = [safe_ident(d) for d in sorted(dimension_columns)]

    if measure_aggregates:
        select_parts = list(dim_idents) + [
            f"{agg} AS _bind_probe_{i}"
            for i, agg in enumerate(sorted(measure_aggregates))
        ]
        sql = f"SELECT {', '.join(select_parts)} FROM {relation}"
        if dim_idents:
            sql += f" GROUP BY {', '.join(dim_idents)}"
        return sql

    if dim_idents:
        return f"SELECT DISTINCT {', '.join(dim_idents)} FROM {relation}"

    raise ValueError(
        "a binding probe needs at least one dimension or measure reference"
    )
