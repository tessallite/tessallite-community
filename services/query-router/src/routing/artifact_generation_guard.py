"""Artifact generation guard — the machinery shared by the pocket (Bug-8392 /
Bug-8455) and aggregate (Bug-8457) admission-to-scan guards.

Why this module exists
----------------------
A materialised artifact's physical table is REUSED in place across refreshes
(``DROP`` + CTAS, ``CREATE OR REPLACE``, or a staging swap into the same name).
A ``RouteDecision`` therefore names a TABLE, not a GENERATION of that table.
Everything the router proved at admission was proved against the row it read at
that instant; the scan happens later, against whatever is in that table then.

Bug-8392 closed that window for pockets with a three-part construction:

  1. **Admission binding** — the decision carries the generation stamp of the
     artifact the matcher actually admitted.
  2. **Execution-time re-proof** — immediately before the scan, the admission
     gates are re-run against a FRESH read of the row, and the live generation
     is required to equal the ADMITTED one.
  3. **Before/after stamp** — the stamp is re-read after the scan; any movement
     discards the rows.

All three are required. (1) alone cannot see a refresh that lands after the
decision. (2) alone re-proves whatever is live rather than what was admitted, so
a definition edit plus a complete refresh landing between the matcher's read and
the pre-check is accepted and serves a row population the matcher's proof never
covered. (3) alone would happily accept a generation that replaced the admitted
one before the pre-read, because the stamp is stable across the scan while the
proof was made against a different build.

Why DETECTION is sufficient (and what makes it sound)
-----------------------------------------------------
The remedy for "this scan may have crossed a refresh" is to DISCARD the rows and
re-route to source with the same compiled predicate — machinery that already
exists for a missing cache table. Nothing wrong is ever returned, so a
conservative detector is equivalent to exclusion; it only costs a fallback when
it fires. Over-detection is deliberate: a refresh that completes while the scan
is running (PostgreSQL makes its ``DROP`` wait behind the reader's
``ACCESS SHARE`` lock, so the rows themselves are still the admitted generation)
also trips the guard. That costs one source fallback and never returns a wrong
row.

Soundness rests on producer discipline that BOTH artifact kinds have:

  I1. every physical mutation is preceded by a COMMITTED transition away from
      the servable status. For pockets that is
      ``shared/pocket/refresh.refresh_pocket_definition`` committing
      ``status="invalidating"`` before any target DDL; for aggregates it is the
      Bug-7903 uniform pending-guard committing ``status="pending"`` (with the
      prior status durably snapshotted) before any physical change.
  I2. returning to the servable status always carries a NEW
      ``active_refresh_run_id``.
  I3. a refresh that fails after starting materialisation leaves a
      non-servable status.

So if the live row reads ``(servable, R)`` immediately before the scan and
``(servable, R)`` again immediately after it, no refresh mutated that table in
between; and if that same ``R`` is the run the matcher admitted, the rows came
from the generation the admission proof was actually made against.

Not covered here: a table dropped and never rebuilt (the existing
missing-relation fallback owns that) and changes to the SOURCE data an artifact
was built from (that is staleness, not a generation race).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class ArtifactGenerationChangedError(RuntimeError):
    """The artifact's physical generation is not (or may no longer be) the one
    the route decision was proved against.

    Callers must DISCARD any rows already read and re-route the query to source.
    This is never returned to the user as a failure when a fallback exists.
    """


@dataclass(frozen=True)
class ArtifactGeneration:
    """The identity of a materialised artifact's physical generation.

    ``active_refresh_run_id`` moves on every completed refresh and the location
    fields move when a refresh re-binds them (e.g. the BigQuery dataset rebind),
    so equality of this tuple across a scan means no refresh landed in between —
    and equality against the ADMITTED tuple means this is the same build the
    matcher proved. Values are normalised to strings so a UUID and its string
    form compare equal.

    ``target_id`` is part of the identity (Bug-8473): the schema/table names
    identify a table only WITHIN a database, and ``target_id`` decides which
    DataTarget — and therefore which connection and which database — those names
    resolve through. An artifact re-pointed at another target is a different
    generation even though every other field is unchanged.

    The artifact KIND is deliberately NOT a field: the stamp is a pure identity
    tuple that must compare equal across the admission read (an ORM object) and
    the execution re-read (a column SELECT). Kind is passed to the assertion
    helpers instead, where it is only ever used for the message.
    """

    status: Optional[str]
    active_refresh_run_id: Optional[str]
    physical_table_name: Optional[str]
    target_schema: Optional[str]
    target_id: Optional[str]


def as_str(value: Any) -> Optional[str]:
    return None if value is None else str(value)


def generation_from(row: Any) -> ArtifactGeneration:
    """Build a generation stamp from anything carrying the five fields — a
    column-SELECT row OR the ORM object the matcher admitted."""
    return ArtifactGeneration(
        status=as_str(getattr(row, "status", None)),
        active_refresh_run_id=as_str(getattr(row, "active_refresh_run_id", None)),
        physical_table_name=as_str(getattr(row, "physical_table_name", None)),
        target_schema=as_str(getattr(row, "target_schema", None)),
        target_id=as_str(getattr(row, "target_id", None)),
    )


async def fetch_columns(
    db: AsyncSession, model_cls: Any, artifact_id: Any, columns
) -> Any:
    """Read the named columns of one artifact straight from the database.

    A column SELECT (not ``session.get``) is deliberate: ``get`` would return the
    instance already in the session's identity map — the very row the router read
    at admission — so a concurrent refresh would be invisible. This issues a real
    statement, and under READ COMMITTED (the default for this service's sessions)
    each statement sees the latest committed state.
    """
    result = await db.execute(
        select(*columns).where(model_cls.id == artifact_id)
    )
    return result.first()


def location_parts(schema: Optional[str], table: Optional[str]) -> list[str]:
    """Identifier parts of an artifact's physical location.

    Mirrors ``rewrite/pocket.rewrite_for_pocket``: a dotted
    ``physical_table_name`` with no ``target_schema`` is already
    ``<schema>.<table>``, otherwise the two are separate parts. Returning PARTS
    (not a dotted string) is what makes the comparison work against emitted SQL,
    where each part is quoted separately.
    """
    schema = (schema or "").strip()
    table = (table or "").strip()
    if "." in table and not schema:
        return [p for p in table.split(".", 1) if p]
    return [p for p in (schema, table) if p]


# Text neutralised before the fallback textual location match: standard
# single-quoted literals (with '' escaping), block comments and line comments.
# Only relevant when the AST pass could not parse the SQL.
_SQL_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")
_SQL_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_SQL_LINE_COMMENT = re.compile(r"--[^\n]*")
_SQL_IDENT_QUOTES = re.compile(r'["`\[\]]')


def _ast_location_match(
    sql: str, dialect: Optional[str], parts: list[str]
) -> Optional[bool]:
    """Exact AST answer to "does this SQL scan the live artifact location?".

    Returns ``True``/``False`` when the SQL parses, ``None`` when it does not (the
    caller then falls back to the textual match rather than refusing a route it
    simply could not read).

    This is intentionally a relation-aware proof, not membership among every
    ``exp.Table`` node in the statement. A matching table inside ``IN (SELECT
    ...)`` is not the relation the routed scan reads. We therefore inspect only
    direct relation lineage from the outer query's FROM/JOIN list. A CTE or
    derived source is accepted only when that lineage resolves unambiguously to
    one physical table relation. Predicate subqueries are deliberately outside
    this relation lineage, so a matching table inside ``IN (SELECT ...)`` never
    proves the outer scan. Set operations, cyclic CTEs and repeated matching
    relations fail closed.

    One leading catalog/project qualifier is tolerated for a schema-qualified
    location: the rewriters emit at most ``db.table`` today, but a future BigQuery
    change that adds the project prefix (Bug-8452 territory) must not silently
    false-refuse every BigQuery route. A location with no schema requires an
    unqualified reference: a bare name resolves through the connection's
    ``search_path``, so accepting ``public.pkt`` for a live location of just
    ``pkt`` would be a guess.
    """
    try:
        import sqlglot
        from sqlglot import exp
    except Exception:  # pragma: no cover - sqlglot is a hard dependency
        return None
    tree = None
    for read in ((dialect or "postgres"), None):
        try:
            tree = sqlglot.parse_one(sql, read=read) if read else sqlglot.parse_one(sql)
            break
        except Exception:
            continue
    if tree is None:
        return None
    # Resolve only FROM/JOIN lineage. ``find_all(exp.Table)`` is deliberately
    # wrong here: it includes predicate subqueries, which do not supply rows to
    # the routed scan. CTE/derived forms are legitimate rewrite output, so walk
    # their direct relation graph rather than rejecting them wholesale.
    ctes: dict[str, Any] = {}
    with_clause = tree.args.get("with_")
    if with_clause is not None:
        for cte in with_clause.expressions:
            name = (cte.alias_or_name or "").lower()
            if not name or name in ctes:
                return False
            ctes[name] = cte.this

    def _sources(select_node: Any) -> list[Any] | None:
        if not isinstance(select_node, exp.Select):
            return None
        from_clause = select_node.args.get("from_")
        if from_clause is None or from_clause.this is None:
            return None
        sources = [from_clause.this]
        sources.extend(join.this for join in select_node.args.get("joins") or ())
        return sources

    def _resolve_relation(relation: Any, seen_ctes: frozenset[str]) -> list[Any] | None:
        if isinstance(relation, exp.Table):
            # An unqualified table named after a CTE denotes that CTE, not a
            # physical relation. Resolve it transitively while detecting cycles.
            cte_name = (relation.name or "").lower()
            if not relation.text("catalog") and not relation.text("db") and cte_name in ctes:
                if cte_name in seen_ctes:
                    return None
                return _resolve_select(ctes[cte_name], seen_ctes | {cte_name})
            return [relation]
        if isinstance(relation, exp.Subquery):
            return _resolve_select(relation.this, seen_ctes)
        return None

    def _resolve_select(select_node: Any, seen_ctes: frozenset[str]) -> list[Any] | None:
        sources = _sources(select_node)
        if sources is None:
            return None
        resolved: list[Any] = []
        for source in sources:
            source_relations = _resolve_relation(source, seen_ctes)
            if source_relations is None:
                return None
            resolved.extend(source_relations)
        return resolved

    relations = _resolve_select(tree, frozenset())
    if not relations:
        return False

    want = [p.lower() for p in parts]
    allowed_extra = 1 if len(want) >= 2 else 0
    matching_relations = 0
    for node in relations:
        got = [
            p.lower()
            for p in (node.text("catalog"), node.text("db"), node.name)
            if p
        ]
        if len(got) < len(want) or len(got) - len(want) > allowed_extra:
            continue
        if got[-len(want):] == want:
            matching_relations += 1
    # A second matching relation is ambiguous: it might be a self-join or a
    # table alias whose semantics the guard cannot safely prove.
    return matching_relations == 1


def targets_live_location(
    rewritten_query: str, row: Any, dialect: Optional[str] = None
) -> bool:
    """True when the SQL about to run names the artifact's LIVE physical location.

    The generation stamp catches a refresh that moves the table AFTER the
    pre-check, but a refresh that re-bound the location BEFORE it (the BigQuery
    dataset rebind in ``_refresh_same_db_bigquery`` is the live example) would
    otherwise leave us re-proving against the NEW location while the already-
    rewritten SQL still scans the OLD one.

    Primary answer is the AST (``_ast_location_match``) — exact, and immune to
    the false-accepts a text search suffers. The textual pass below is only a
    fallback for SQL sqlglot cannot parse; refusing outright there would kill
    cache acceleration for a shape that is otherwise perfectly valid.

    The fallback is structural, not membership: testing each identifier part
    independently false-passes on a prefix collision (live ``analytics.pkt_sales``
    against SQL scanning ``"analytics_eu"."pkt_sales"``, or live ``pkt_sales``
    against ``pkt_sales_v2``), so it strips identifier quoting — the rewrite emits
    double quotes for PG/Redshift and backticks for BigQuery — neutralises string
    literals and comments (which would otherwise let the location name inside a
    user-supplied filter value satisfy the check), and then requires the FULLY
    QUALIFIED dotted form as a whole identifier. Exotic literal forms the
    neutraliser does not model (PostgreSQL dollar quoting) could still satisfy it,
    which is one more reason the AST pass is primary and this check is a
    supplementary consistency guard layered on the generation stamp — never the
    primary proof on its own.

    Both passes are case-insensitive: these identifiers are the stored location
    values the rewriter emitted verbatim, so folding case cannot hide a rebind
    (only a location differing ONLY in case, which no rebind produces) while
    avoiding a false refusal on dialect-rendered casing.
    """
    sql = rewritten_query or ""
    parts = location_parts(
        getattr(row, "target_schema", None), getattr(row, "physical_table_name", None)
    )
    if not parts:
        return False

    ast_answer = _ast_location_match(sql, dialect, parts)
    if ast_answer is not None:
        return ast_answer

    text = _SQL_STRING_LITERAL.sub("''", sql)
    text = _SQL_BLOCK_COMMENT.sub(" ", text)
    text = _SQL_LINE_COMMENT.sub(" ", text)
    unquoted = _SQL_IDENT_QUOTES.sub("", text).lower()
    qualified = ".".join(p.lower() for p in parts)
    return bool(
        re.search(r"(?<![\w.])" + re.escape(qualified) + r"(?![\w.])", unquoted)
    )


def assert_admitted_generation(
    live: ArtifactGeneration,
    admitted: Optional[ArtifactGeneration],
    *,
    kind: str,
    artifact_id: Any,
    error_cls: type[ArtifactGenerationChangedError],
) -> None:
    """Bind the execution-time proof to the generation the MATCHER admitted.

    Bug-8455 (pocket) / Bug-8457 (aggregate). Without this the pre-check
    re-proves whatever is live, which is a strictly weaker statement: the
    matcher's own admission gates — above all the pocket matcher's containment
    proof (``query ⊆ pocket``) and the aggregate matcher's grain/measure
    coverage proof — are NOT re-run at execution time, so a definition edit plus
    a COMPLETE refresh landing between the matcher's read and the pre-check
    would be accepted and the query served from a row population the admission
    proof never covered (silently missing or wrong rows). Comparing the full
    stamp makes that undetectable-by-construction case detectable.

    ``admitted is None`` is tolerated so a duck-typed or legacy decision (the
    internal callers that build a ``RouteDecision`` by hand, and any route
    created before this field existed) keeps the Bug-8392 behaviour rather than
    losing acceleration outright. Every production creation site in
    ``routing/router`` populates it, and a contract test pins that.
    """
    if admitted is None:
        return
    if live == admitted:
        return
    logger.warning(
        "%s %s changed generation between admission and execution "
        "(admitted=%s live=%s); re-routing to source",
        kind, artifact_id, admitted, live,
    )
    raise error_cls(
        f"{kind.capitalize()} {artifact_id} was refreshed between the "
        f"route decision and its execution; the route was proved against a "
        f"different generation and the query is re-routed to source"
    )


def assert_generation_unchanged(
    before: ArtifactGeneration,
    after: Optional[ArtifactGeneration],
    *,
    kind: str,
    artifact_id: Any,
    error_cls: type[ArtifactGenerationChangedError],
) -> None:
    """Fail closed when the artifact's generation moved across the scan."""
    if after == before:
        return
    logger.warning(
        "%s %s changed generation during execution (before=%s after=%s); "
        "discarding the result and re-routing to source",
        kind, artifact_id, before, after,
    )
    raise error_cls(
        f"{kind.capitalize()} {artifact_id} was refreshed while its "
        f"query was executing; the result was discarded and the query "
        f"re-routed to source"
    )
