"""Semantic-to-physical resolution for Usage & Downstream Assets (Bug-8471/8483).

Pure functions only — no DB session, no request context — so the resolution
contract can be asserted against known answers instead of through a mocked
session. ``impact_scan.py`` owns the routes and the database reads; this module
owns the question "which PHYSICAL columns/tables does this query actually use".

Why this module exists (Bug-8471). The gateway query log stores two different
SQL texts and neither is authoritative on its own:

  ``raw_query``        the SEMANTIC SQL the BI client sent
                       ``SELECT SUM("Revenue") AS m0 FROM "modelx"``
  ``rewritten_query``  the PHYSICAL SQL actually executed
                       ``SELECT SUM("payment_transaction"."transaction_amount")
                         FROM "demo_data"."payment_transaction" ...``

A physical table name is essentially never present in ``raw_query``, so matching
``ModelTable.physical_name`` against it matched nothing and the whole feature
reported zero usage. But ``rewritten_query`` is no better as a sole authority:
whenever the query was accelerated it names the aggregate/pocket target
(``"trgt"."3732ce3df075"``) and contains no model table at all, so it
systematically loses exactly the hottest queries.

The real authority is neither text. It is the binder-produced stable column
reference persisted on the bind ``RouteLog`` (``column_usage_refs``), which
``GET /impact/column-usage`` already consumes. The texts are a fallback for logs
written before that contract existed.

``build_semantic_closure`` closes the remaining hole (Bug-8483): a calculated
measure or a UDA-backed field has no ``source_column_id``, so the binder cannot
emit a physical column for it. The semantic object identity is recorded instead,
and this module expands it to the physical columns it transitively reads.
"""
from __future__ import annotations

from typing import Iterable, Optional

import sqlglot
from sqlglot import exp

from shared.connector_qualify import CONNECTOR_TO_SQLGLOT

# Semantic object types that carry a physical-column closure. Matches the
# ``object_type`` values the query-router writes into ``semantic_object_refs``.
MEASURE = "measure"
DIMENSION = "dimension"


def _clean(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def build_semantic_closure(snapshot) -> dict[tuple[str, str], frozenset[str]]:
    """Physical ``ModelColumn`` ids each measure/dimension transitively reads.

    Bug-8483. A measure or dimension can reach a physical column through several
    hops that no single foreign key expresses:

      * a calculated measure references other measures by name
        (``MeasureRow.calc_reference_ids``), each of which may itself be
        calculated;
      * a variant measure derives from its base measure
        (``variant_of_measure_id``);
      * a UDA-backed measure or dimension reads the columns the attribute is
        built from (``UdaRow.column_ref_ids``);
      * a calculated dimension names columns only inside its expression
        (``DimensionRow.calc_expression_column_ids``).

    Without the closure, dropping a column that feeds a calculated measure looks
    safe in the usage panel — the exact harm the feature exists to prevent, one
    layer below the direct binding.

    Returns ``(object_type, object_id) -> frozenset(column_id)``. Cycle-safe: a
    calculated measure that (invalidly) references itself resolves to the union
    of everything reachable without recursing forever.
    """
    measures = {m.id: m for m in getattr(snapshot, "measures", ()) or ()}
    dimensions = {d.id: d for d in getattr(snapshot, "dimensions", ()) or ()}
    udas = {u.id: u for u in getattr(snapshot, "udas", ()) or ()}

    resolved: dict[tuple[str, str], frozenset[str]] = {}

    def uda_columns(uda_id) -> set[str]:
        uda = udas.get(_clean(uda_id) or "")
        if uda is None:
            return set()
        return {c for c in (uda.column_ref_ids or ()) if c}

    def walk(
        object_type: str,
        object_id: str,
        in_progress: set[tuple[str, str]],
    ) -> tuple[set[str], bool]:
        """Return ``(columns, saw_cycle)``.

        ``saw_cycle`` propagates upward so a frame whose result depended on a
        truncated back-edge is NOT memoized — that partial answer is only valid
        for the traversal that produced it. Every other frame IS memoized, which
        is what keeps the walk linear: memoizing only the outermost frame turned
        a branching calculated-measure DAG exponential (40 measures at depth 20
        measured at 4.2s of event-loop-blocking CPU inside the request).
        """
        key = (object_type, object_id)
        cached = resolved.get(key)
        if cached is not None:
            return set(cached), False
        if key in in_progress:
            # Cycle: contribute nothing on the back-edge. The outer frame still
            # accumulates every column reachable by the non-cyclic paths.
            return set(), True
        in_progress.add(key)
        columns: set[str] = set()
        saw_cycle = False

        def descend(child_type: str, child_id: str) -> None:
            nonlocal saw_cycle
            child_columns, child_cycle = walk(child_type, child_id, in_progress)
            columns.update(child_columns)
            saw_cycle = saw_cycle or child_cycle

        if object_type == MEASURE:
            row = measures.get(object_id)
            if row is not None:
                for attr in (
                    "source_column_id",
                    "semi_additive_account_column_id",
                    "resolved_date_col_id",
                    "date_dimension_column_id",
                ):
                    value = _clean(getattr(row, attr, None))
                    if value:
                        columns.add(value)
                columns |= uda_columns(getattr(row, "user_defined_attribute_id", None))
                for ref_id in getattr(row, "calc_reference_ids", ()) or ():
                    if ref_id:
                        descend(MEASURE, str(ref_id))
                variant_of = _clean(getattr(row, "variant_of_measure_id", None))
                if variant_of:
                    descend(MEASURE, variant_of)
        elif object_type == DIMENSION:
            row = dimensions.get(object_id)
            if row is not None:
                for attr in ("source_column_id", "display_column_id"):
                    value = _clean(getattr(row, attr, None))
                    if value:
                        columns.add(value)
                columns |= uda_columns(getattr(row, "user_defined_attribute_id", None))
                for column_id in getattr(row, "calc_expression_column_ids", ()) or ():
                    if column_id:
                        columns.add(str(column_id))
        in_progress.discard(key)
        if not saw_cycle:
            resolved[key] = frozenset(columns)
        return columns, saw_cycle

    closure: dict[tuple[str, str], frozenset[str]] = {}
    for object_type, rows in ((MEASURE, measures), (DIMENSION, dimensions)):
        for object_id in rows:
            columns, _saw_cycle = walk(object_type, object_id, set())
            closure[(object_type, object_id)] = frozenset(columns)
    return closure


def build_semantic_name_index(snapshot) -> dict[str, set[tuple[str, str]]]:
    """Lowercased semantic name -> the objects that answer to it.

    Used only for LEGACY query logs, whose ``raw_query`` names measures and
    dimensions by their SEMANTIC name (``SUM("Revenue")``). A name shared by a
    measure and a dimension yields both; the caller unions their closures rather
    than picking one.
    """
    index: dict[str, set[tuple[str, str]]] = {}
    for object_type, rows in (
        (MEASURE, getattr(snapshot, "measures", ()) or ()),
        (DIMENSION, getattr(snapshot, "dimensions", ()) or ()),
    ):
        for row in rows:
            for candidate in (getattr(row, "name", None), getattr(row, "display_name", None)):
                name = _clean(candidate)
                if not name:
                    continue
                index.setdefault(name.lower(), set()).add((object_type, row.id))
    return index


def source_dialects(connector_types: Iterable[Optional[str]]) -> tuple[Optional[str], ...]:
    """sqlglot read dialects to try for a model's ``rewritten_query`` texts.

    ``rewritten_query`` is written in the SOURCE connector's dialect, so a model
    backed by BigQuery logs ``FROM `large_demo_data`.`payment_transaction```.
    Parsing that with sqlglot's default dialect yields NOTHING — backticks are
    not identifier quotes there — which silently disabled the physical-SQL half
    of the usage scan on every BigQuery model (measured: 313/369 modell logs,
    44/239 inventory, 6/19 onboarding all resolved zero tables).

    The mapping is the shared ``CONNECTOR_TO_SQLGLOT`` table, never a local
    per-connector branch. ``None`` (sqlglot's default) is always included so an
    unknown or missing connector type degrades to today's behaviour instead of
    parsing nothing at all — this must fail OPEN toward more parsing, because
    the failure mode being fixed is a silent empty result.
    """
    dialects: list[Optional[str]] = [None]
    for connector in connector_types:
        mapped = CONNECTOR_TO_SQLGLOT.get((connector or "").lower())
        if mapped and mapped not in dialects:
            dialects.append(mapped)
    return tuple(dialects)


def extract_physical_tables(
    sql: str,
    dialects: Iterable[Optional[str]] = (None,),
) -> set[tuple[Optional[str], str]]:
    """``(schema_lower_or_None, table_lower)`` for every table named in ``sql``.

    Parsed with sqlglot rather than matched with a regex because the physical SQL
    the router emits quotes every identifier — the live rewritten queries read
    ``FROM "demo_data"."payment_transaction"`` on PostgreSQL and
    ``FROM `demo_data`.`payment_transaction`` on BigQuery, neither of which a
    regex for the bare dotted spelling can see.

    Results are UNIONED across ``dialects`` (see :func:`source_dialects`). A
    dialect that cannot read the text contributes nothing rather than vetoing
    the ones that can, and the caller filters every candidate against the
    model's real table names, so a wrong-dialect misparse cannot invent usage.

    CTE names are excluded: a CTE is a query-local alias, not a physical table
    (Bug-8474 shape 1).

    Best-effort: a statement no dialect can parse contributes nothing.
    """
    tables: set[tuple[Optional[str], str]] = set()
    if not sql:
        return tables
    for dialect in dialects or (None,):
        try:
            parsed = sqlglot.parse(
                sql, read=dialect, error_level=sqlglot.ErrorLevel.IGNORE,
            )
        except Exception:
            continue
        for statement in parsed:
            if statement is None:
                continue
            cte_names = _cte_names(statement)
            for table in statement.find_all(exp.Table):
                name = (table.name or "").lower()
                if not name or name in cte_names:
                    continue
                schema = (getattr(table, "db", "") or "").lower() or None
                tables.add((schema, name))
    return tables


def _cte_names(statement) -> set[str]:
    """Lowercased names of every CTE declared anywhere in ``statement``."""
    names: set[str] = set()
    try:
        ctes = list(statement.find_all(exp.CTE))
    except Exception:
        return names
    for cte in ctes:
        alias = cte.alias_or_name
        if alias:
            names.add(str(alias).lower())
    return names


def split_table(name: Optional[str]) -> tuple[Optional[str], str]:
    """``"demo_data.orders"`` -> ``("demo_data", "orders")``; bare -> ``(None, n)``."""
    text = (name or "").lower()
    if "." in text:
        schema, _, table = text.rpartition(".")
        return (schema or None), table
    return None, text


def tables_agree(model_table: str, query_schema: Optional[str], query_table: str) -> bool:
    """Does a table named in a query refer to this model table?

    ``ModelTable.physical_name`` is stored schema-qualified in real models
    (``demo_data.dim_service_type``) but a query may name the table bare, via an
    alias, or under a DIFFERENT schema. Rules:

      * bare names must always match;
      * when BOTH sides carry a schema, the schemas must match too — otherwise
        ``other_schema.orders`` would be attributed to ``demo_data.orders``
        (Bug-8474 shape 3);
      * when either side is unqualified the bare match stands, because the
        missing schema genuinely does not contradict anything (Bug-8463).
    """
    model_schema, model_bare = split_table(model_table)
    if model_bare != (query_table or "").lower():
        return False
    if model_schema and query_schema and model_schema != query_schema:
        return False
    return True


def extract_semantic_names(sql: str) -> set[str]:
    """Lowercased FIELD references in a legacy semantic query.

    Legacy logs predate the stable bind reference, so the only record of what a
    query touched is the semantic text itself. Only ``exp.Column`` nodes count —
    deliberately NOT a plain identifier sweep. A sweep also returns output
    aliases, the model name in the FROM, and bare keywords, so a model with a
    measure named ``value`` would be credited usage by
    ``SELECT SUM("Revenue") AS value FROM "modelx"``, which names no such
    measure. That is the same over-attribution class as Bug-8474, and this path
    is load-bearing for the whole legacy corpus, so it must not be re-introduced
    here.

    Best-effort: an unparseable statement (a DAX/MDX raw query, say) contributes
    nothing, exactly as the existing column-usage fallback treats it.
    """
    names: set[str] = set()
    try:
        parsed = sqlglot.parse(sql or "", error_level=sqlglot.ErrorLevel.IGNORE)
    except Exception:
        return names
    for statement in parsed:
        if statement is None:
            continue
        for column in statement.find_all(exp.Column):
            if column.name:
                names.add(column.name.lower())
    return names


# Protocols whose ``raw_query`` is SQL that sqlglot can be trusted to read.
# A DAX/MDX statement is NOT: sqlglot parses
# ``EVALUATE SUMMARIZECOLUMNS("Region", "Total", [Revenue])`` into a column
# named ``evaluate`` and loses every real field, so a model with a field named
# ``evaluate`` would be credited usage by a query that names no such field.
# Both LEGACY text paths — the semantic-name resolution and the physical-token
# scan — must fail CLOSED on a language they cannot read.
#
# An allowlist, not a denylist, because an unrecognised protocol contributes
# nothing rather than inventing usage. The cost is under-reporting a NEW
# SQL-speaking protocol until it is added here, so the set is pinned by a test
# against the protocol values that actually occur
# (``test_sql_protocol_allowlist_covers_every_sql_speaking_protocol``).
# Deliberately excluded: ``discover_members``, whose raw_query is a
# ``DISCOVER_MEMBERS(field)`` metadata-discovery call rather than SQL and
# rather than an analytic read of the column.
SQL_PROTOCOLS: frozenset[str] = frozenset({
    "jdbc", "plugin", "headless", "introspect", "mcp", "preview",
})


def is_sql_protocol(protocol: Optional[str]) -> bool:
    return (protocol or "").strip().lower() in SQL_PROTOCOLS


def resolve_semantic_objects(
    sql: str,
    name_index: dict[str, set[tuple[str, str]]],
) -> set[tuple[str, str]]:
    """Semantic objects a legacy ``raw_query`` names, via ``name_index``."""
    found: set[tuple[str, str]] = set()
    for name in extract_semantic_names(sql):
        for obj in name_index.get(name, ()):  # type: ignore[arg-type]
            found.add(obj)
    return found


_OTHER_TYPE = {MEASURE: DIMENSION, DIMENSION: MEASURE}


def expand_objects(
    objects: Iterable[tuple[str, str]],
    closure: dict[tuple[str, str], frozenset[str]],
) -> list[str]:
    """Physical column ids each semantic object reads, WITH multiplicity.

    One entry per (object, column) pair, not a flattened set: two calculated
    measures that both read ``net_amount`` are two distinct semantic references
    to that column, exactly as two directly bound measures on the same column
    produce two ``column_usage_refs`` entries. Collapsing to a set here would
    make closure-reached references count less than direct ones for the same
    query shape.

    The declared ``object_type`` is tried first, then the other type. The binder
    wraps a dimension referenced inside an aggregate as a synthetic measure (and
    a measure referenced outside one as a synthetic dimension), carrying the
    WRAPPED object's id under the wrapper's type. Object ids are UUIDs, so the
    cross-type retry cannot collide with a different object — it only recovers
    the wrapper case instead of silently reporting no usage.
    """
    columns: list[str] = []
    for object_type, object_id in objects:
        hit = closure.get((object_type, object_id))
        if hit is None:
            hit = closure.get((_OTHER_TYPE.get(object_type, object_type), object_id))
        if hit:
            columns.extend(sorted(hit))
    return columns


def columns_for_objects(
    objects: Iterable[tuple[str, str]],
    closure: dict[tuple[str, str], frozenset[str]],
) -> set[str]:
    """Distinct physical column ids the given semantic objects read.

    Use for TABLE resolution, where multiplicity is meaningless. Use
    :func:`expand_objects` when the caller is counting references.
    """
    return set(expand_objects(objects, closure))
