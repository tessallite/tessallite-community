"""Exact direct-read rewrite for a Phase-5 derived-grain serve proof.

Spec: architecture_derived-grain-aggregate-routing.md §8.1 (canonical plan, one
transpile) + §8.2 (exact direct read). Phase 5 serves ONLY the EXACT verdict
(rollout stages 4 + 5):

  - Stage 5 — exact EXPRESSION-KEY identity: the query's inline GROUP BY
    expression fingerprint equals a materialised artifact grain key; project the
    stored PHYSICAL key column under the requested alias.
  - Stage 4 — strict BIJECTION attribute relabel: project the recorded detail
    PASSENGER column instead of the key. Because a bijection preserves the whole
    tuple partition, this is still an EXACT direct read — no DISTINCT, no GROUP BY,
    no hidden SUM, no mapping join (§8.2).

An EXACT read at exact grain reads each measure's stored component column
DIRECTLY (no re-aggregation wrap): the aggregate row already IS the answer for
that group. This reuses the vetted ``_phys_expr_for_node(..., is_exact_grain=True)``
so the derived path and the ordinary exact-grain aggregate path render measures
identically.

Construction rules (identical to the ordinary aggregate rewrite contract):
  - one SQLGlot AST built in canonical PostgreSQL, transpiled ONCE to the target;
  - every identifier quoted through ``shared/connector_qualify``;
  - no per-connector ``if`` branch; execution flows through the normal
    source_executor gateway (the router returns an aggregate RouteDecision).

Fail-CLOSED: any shape this rewrite cannot faithfully serve (an unmapped key, a
non-direct measure plan, an unresolved passenger/physical name, a raw HAVING, a
derived ORDER/WHERE not already representable) raises
``AggregateRewriteUnsupported`` so the router falls back to SOURCE — never a
silently-wrong aggregate read.
"""
from __future__ import annotations

from typing import Optional

import sqlglot

from shared.connector_qualify import safe_ident
from shared.db.models import AggregateDefinition
from src.ir.logical_query import BoundQuery
from src.rewrite.aggregate import (
    AggregateRewriteUnsupported,
    _build_col_lookup,
    _full_table_ref,
    _phys_expr_for_node,
    _render_pagination_suffix,
)
from src.rewrite.dialects import _dialect_to_connector, render_tree_for_dialect
from src.routing.derived_expression_proof import (
    DIRECT_ATTRIBUTE_RELABEL,
    DIRECT_EXPRESSION_KEY,
    DIRECT_PHYSICAL_KEY,
    EXACT,
    DerivedServeProof,
)
from src.routing.derived_measure_proof import (
    AVG_FROM_SUM_COUNT,
    DIRECT,
    MAX_OF_MAX,
    MIN_OF_MIN,
    SUM_OF_COUNT,
    SUM_OF_SUM,
)

# Measure roll-up plan -> the physical stat this exact-grain read must project.
# At EXACT grain every additive plan reads its stored component directly; the
# stat token here is what ``_phys_expr_for_node`` looks up in the col lookup.
_PLAN_TO_STAT: dict[str, str] = {
    SUM_OF_SUM: "sum",
    SUM_OF_COUNT: "count",
    MIN_OF_MIN: "min",
    MAX_OF_MAX: "max",
    AVG_FROM_SUM_COUNT: "avg",
    DIRECT: "direct",  # quantile / dispersion / distinct — resolved per-request
}


def _strip_alias(text: str) -> str:
    """Drop a trailing ``AS alias`` (case-insensitive) from a projection's raw text.

    The parser carries the alias separately in ``expr.alias``; this strips a
    possibly-lower-case ``as`` so the remaining core canonicalises identically
    whichever case the client used (Fable R1 #11 consistency).
    """
    t = text or ""
    low = t.lower()
    idx = low.find(" as ")
    return (t[:idx] if idx != -1 else t).strip()


def _pgq(name: str) -> str:
    """PostgreSQL-canonical identifier quote (safe_ident doubles embedded quotes).

    The whole statement is built canonical-postgres then transpiled once, so every
    identifier is quoted in postgres form here (Bug-921 discipline)."""
    return safe_ident(name)


def rewrite_for_derived_exact(
    *,
    bound_query: BoundQuery,
    aggregate: AggregateDefinition,
    proof: DerivedServeProof,
    target_dialect: str = "postgres",
) -> str:
    """Build the exact direct-read SQL for an EXACT derived serve proof (§8.2).

    v1 serves only the pure GROUP-BY exact case (no WHERE/HAVING/ORDER, no active
    RLS — the router gates all of these). Raises ``AggregateRewriteUnsupported`` on
    any shape that cannot be faithfully served, so the router falls back to SOURCE.
    """
    if proof.verdict != EXACT:
        # Defence-in-depth: Phase 5 serves EXACT only; the router already gates on
        # this, so reaching here with another verdict is a wiring bug — fail closed.
        raise AggregateRewriteUnsupported(
            f"derived exact rewrite requires EXACT verdict, got {proof.verdict}"
        )

    lq = bound_query.logical_query
    # Conservative v1 serving surface (§8.5). WHERE/HAVING/ORDER on a derived key
    # requires a per-column proof that the predicate/sort maps faithfully to the
    # aggregate's physical key or passenger. That per-clause proof is a Phase-6/§8.5
    # deliverable; until it lands, a derived-key query that carries ANY of them is
    # routed to SOURCE (never partially served — partial movement is forbidden).
    # This eliminates the wrong-number risk of mapping an unproven predicate onto a
    # relabel/expression key while still serving the pure GROUP-BY exact case.
    if lq.having_raw or getattr(lq, "having_columns", None):
        raise AggregateRewriteUnsupported("derived exact read cannot serve a HAVING clause in v1")
    if lq.filters or getattr(lq, "has_unresolvable_where", False):
        raise AggregateRewriteUnsupported("derived exact read cannot serve a WHERE clause in v1")
    if lq.order_by or getattr(lq, "has_unresolvable_order", False):
        raise AggregateRewriteUnsupported("derived exact read cannot serve an ORDER BY in v1")

    # SELECT-shape guard (§8.2 answer-identity). The exact read reconstructs the
    # projection from the proof's key + measure plans, NOT from ``lq.select_expressions``.
    # So the v1 serving surface is restricted to a SELECT list that is EXACTLY the
    # served keys + bare (unwrapped) measures, in that shape — otherwise the served
    # result would silently DROP an extra projection (e.g. a second scalar of the
    # grouped key), a wrapper (ROUND(SUM(x))), or a hidden aggregate the proof did
    # not account for. Any select item that is not a served group-key expression or
    # a bare AGG(col) measure routes to SOURCE (full SELECT-order-preserving rewrite
    # is deferred to the operational turn-on).
    _assert_select_shape_is_exactly_keys_and_bare_measures(bound_query, proof)

    connector = _dialect_to_connector(target_dialect)
    col_lookup = _build_col_lookup(aggregate)

    # §8.2 answer-identity by construction: build the served SELECT by WALKING the
    # query's ACTUAL ``select_expressions`` IN ORDER (the ordinary aggregate rewrite
    # pattern), projecting each item under ITS OWN alias in ITS OWN position. This
    # replaces the earlier keys-then-measures reconstruction that dropped SELECT
    # order and measure aliases (the Phase-5 review churn root cause). The SELECT-
    # shape guard above still fail-closes any item this walk cannot faithfully
    # reproduce; the walk makes the REPRESENTABLE case correct-by-construction.
    #
    # Synthetic IR with no parsed SELECT list (older unit tests) keeps the
    # proof-driven keys-then-measures order — nothing to walk.
    select_exprs = list(getattr(lq, "select_expressions", []) or [])
    if select_exprs:
        select_parts = _walk_select_expressions(
            select_exprs=select_exprs,
            lq=lq,
            bound_query=bound_query,
            aggregate=aggregate,
            proof=proof,
            col_lookup=col_lookup,
        )
    else:
        select_parts = _projection_from_proof(
            aggregate=aggregate, proof=proof, col_lookup=col_lookup,
        )

    if not select_parts:
        raise AggregateRewriteUnsupported("derived exact read: no projections")

    table_ref = _full_table_ref(aggregate, connector="postgresql")

    # No WHERE in v1 (rejected above) — the exact read is the full aggregate at its
    # exact grain, projected/relabelled per the proof. RLS-active queries never
    # reach here (the router gates them to the RLS-safe / source path).
    stmt = f"SELECT {', '.join(select_parts)} FROM {table_ref}"

    # --- Pagination (dialect-correct suffix, one transpile for the body) ------
    pagination = _render_pagination_suffix(lq.limit, lq.offset, target_dialect)

    # F-006-02: route through the single dialect render boundary so WEEK->ISOWEEK,
    # semi-additive fail-loud, and T-SQL bracket escaping fire on derived-exact SQL.
    # Previously used sqlglot.transpile(write=target_dialect) which bypassed
    # the boundary (Fable SHOULD-FIX D-adjacent finding).
    tree = sqlglot.parse_one(stmt, read="postgres")
    transpiled = render_tree_for_dialect(tree, target_dialect)
    return f"{transpiled}{pagination}"


def _key_plan_physical(aggregate: AggregateDefinition, kp) -> str:
    """PostgreSQL-quoted physical projection for one served key plan (§8.2).

    An exact expression/physical key projects its BUILT physical grain column; a
    bijection relabel projects the detail PASSENGER column (still exact — the
    bijection preserves the tuple partition, so no DISTINCT/GROUP BY/mapping join).
    Raises ``AggregateRewriteUnsupported`` on an unresolved physical name or a
    non-exact key plan (a coarsening is Phase 6) so the router falls back to source.
    """
    if kp.plan in (DIRECT_EXPRESSION_KEY, DIRECT_PHYSICAL_KEY):
        # Resolve by canonical artifact key id (never logical name, §3.4); an
        # EXPRESSION plan cross-checks its fingerprint too.
        phys = _resolve_key_physical(
            aggregate, kp.artifact_key_fingerprint,
            key_id=getattr(kp, "artifact_key_id", None),
        )
        if not phys:
            raise AggregateRewriteUnsupported(
                f"derived exact read: unresolved physical key for {kp.query_key}"
            )
        return _pgq(phys)
    if kp.plan == DIRECT_ATTRIBUTE_RELABEL:
        passenger = kp.passenger_column
        if not passenger:
            raise AggregateRewriteUnsupported(
                f"derived exact read: bijection relabel missing passenger column "
                f"for {kp.query_key}"
            )
        return _pgq(passenger)
    raise AggregateRewriteUnsupported(
        f"derived exact read: key plan {kp.plan} is not an exact direct read"
    )


def _fold_unquoted_identifier(name: str, input_dialect: Optional[str]) -> str:
    """Fold an unquoted identifier the way its parse dialect would (Bug-7873c).

    Different SQL dialects case-fold UNQUOTED identifiers differently, so the
    reproduced output label must match what the source engine would have emitted:
    PostgreSQL/BigQuery/Spark/Redshift fold to lower case; Snowflake folds to UPPER;
    an ANSI/DB2/Oracle-style engine also folds UPPER. The prior code hardcoded
    ``lower()``, which produced a lower-cased label for a Snowflake source query
    whose engine would have returned the column UPPER-cased — a mismatched label on
    the served side. We defer to sqlglot's per-dialect ``normalization_strategy``:
    ``UPPERCASE`` -> upper, everything else (LOWERCASE / CASE_SENSITIVE /
    CASE_INSENSITIVE) -> lower, matching the folding the parser applied.
    """
    upper = False
    try:
        from sqlglot.dialects.dialect import Dialect
        from sqlglot.dialects.dialect import NormalizationStrategy

        dialect = Dialect.get_or_raise(input_dialect or "postgres")
        upper = dialect.NORMALIZATION_STRATEGY == NormalizationStrategy.UPPERCASE
    except Exception:  # noqa: BLE001 — unknown/missing dialect -> conservative lower
        upper = False
    return name.upper() if upper else name.lower()


def _reproducible_relabel_label(
    expr, raw: str, alias: Optional[str], input_dialect: Optional[str] = None,
) -> str:
    """The output label for a bare detail relabel projection (spec §3.4).

    An explicit alias is honoured exactly. Otherwise the passenger is projected
    ``AS`` the terminal requested dimension spelling: an unquoted spelling follows
    the PARSED dialect's identifier case folding (Bug-7873c — lower for PG/BigQuery,
    upper for Snowflake), a quoted spelling is preserved exactly. The generated
    passenger name is NEVER exposed as the label. Raises
    ``AggregateRewriteUnsupported`` when the spelling is not reproducible.
    """
    if alias:
        return alias
    # ``raw`` is the projection text with any ``AS`` stripped. Take the terminal
    # identifier (strip a single table qualifier), preserving quotes.
    token = raw.strip()
    if not token:
        raise AggregateRewriteUnsupported(
            "derived exact read: relabel projection has no reproducible label"
        )
    # A simple quoted identifier ``"name"`` (exactly two quotes, none embedded) is
    # preserved exactly (drop the surrounding quotes; _pgq re-quotes canonically).
    # A qualified quoted terminal ``t."name"`` likewise. Any OTHER quote shape — a
    # doubled embedded quote (``"we""ird"``), an unbalanced quote, or a mixed
    # quoted/unquoted composite — is NOT safely reproducible, so fail closed to
    # source rather than emit a wrong label (Fable R1 #11).
    if '"' in token or "`" in token:
        terminal = token.rsplit(".", 1)[-1].strip()
        if (
            terminal.startswith('"') and terminal.endswith('"')
            and terminal.count('"') == 2
        ):
            return terminal[1:-1]
        raise AggregateRewriteUnsupported(
            f"derived exact read: relabel label {token!r} is not reproducibly quoted"
        )
    # Unquoted: the terminal identifier folds per the parsed dialect (Bug-7873c).
    return _fold_unquoted_identifier(token.rsplit(".", 1)[-1], input_dialect)


def _measure_component_expr(mp, col_lookup: dict) -> str:
    """PostgreSQL-canonical physical component read for one served measure plan.

    At EXACT grain the stored component column IS the answer (no re-aggregation
    wrap). Reuses the ordinary path's ``_phys_expr_for_node(..., is_exact_grain=
    True)`` so the derived and ordinary exact-grain measure reads are identical.
    Raises ``AggregateRewriteUnsupported`` when the plan is not servable or the
    stored component column is absent (never emits ``NULL`` as if it were a value).
    """
    stat = _PLAN_TO_STAT.get(mp.plan)
    if stat is None:
        raise AggregateRewriteUnsupported(
            f"derived exact read: measure plan {mp.plan} not servable"
        )
    m_name = mp.measure_name
    if stat == "direct":
        # Direct-only statistic (quantile/dispersion/distinct): read its stored
        # stat column at exact grain using the REQUESTED stat token (never a fuzzy
        # fallback that could substitute a different stat).
        m_func = (mp.requested_stat or "").strip().lower()
    else:
        m_func = stat
    phys_expr = _phys_expr_for_node(
        m_name if m_name != "__row_count__count" else "__row_count",
        m_func, col_lookup, is_exact_grain=True, connector="postgresql",
    )
    if phys_expr == "NULL":
        raise AggregateRewriteUnsupported(
            f"derived exact read: measure {m_name} stat {m_func} not stored"
        )
    return phys_expr


def _walk_select_expressions(
    *,
    select_exprs: list,
    lq,
    bound_query: BoundQuery,
    aggregate: AggregateDefinition,
    proof: DerivedServeProof,
    col_lookup: dict,
) -> list[str]:
    """Build the served SELECT list by walking the query's items IN ORDER (§8.2).

    Each parsed SELECT item is projected under ITS OWN alias, in ITS OWN position,
    so the served result matches the source path's column order and labels
    (measure aliases preserved). Item -> projection mapping:
      - passthrough (the projected derived GROUP-BY key): the served physical key /
        passenger for the key plan whose fingerprint equals the item's canonical
        fingerprint;
      - analytical ``AGG(col)`` / literal ``COUNT(*)``: the physical component for
        the measure plan of that measure;
    Any item that does not map to a served key or measure plan raises
    ``AggregateRewriteUnsupported`` (fail-closed -> source). Every explicit alias is
    honoured; an item with no explicit alias whose source-path label is an engine
    default that cannot be reproduced also fails closed.
    """
    from shared.semantic.derived_expression import canonicalise_sql

    # fingerprint -> served key plan (only exact/relabel plans reach here; the
    # SELECT-shape guard already rejected any coarsening key plan).
    key_plan_by_fp: dict[str, object] = {}
    # attribute_key -> served relabel plan (bare-detail relabel projections resolve
    # by attribute identity, never by fingerprint or name, §3.4).
    key_plan_by_ak: dict[str, object] = {}
    for kp in proof.query_key_plans:
        if kp.artifact_key_fingerprint:
            key_plan_by_fp[kp.artifact_key_fingerprint] = kp
        ak = getattr(kp, "attribute_key", None)
        if ak:
            key_plan_by_ak[ak] = kp
    # SELECT ordinal -> bound group-key projection (incl. duplicates), so a bare
    # detail projection at a given ordinal resolves to its ATTRIBUTE/PHYSICAL key.
    # Defense-in-depth (Fable R2 #1): a real ordinal claimed by >1 projection would
    # collapse last-writer-wins and serve the wrong key -> raise (the binder already
    # poisons this, so reaching here is a wiring bug). The -1 identity sentinel is
    # skipped (not a projection).
    proj_by_ordinal: dict[int, object] = {}
    for pj in (getattr(bound_query, "bound_group_key_projections", []) or []):
        if pj.select_ordinal < 0:
            continue
        if pj.select_ordinal in proj_by_ordinal:
            raise AggregateRewriteUnsupported(
                f"derived exact read: SELECT ordinal {pj.select_ordinal} is claimed "
                "by more than one group key (multiply-matched item)"
            )
        proj_by_ordinal[pj.select_ordinal] = pj
    # lowercased measure name -> served measure plan. The lookup is
    # case-insensitive because the query spelling (``SUM(revenue)``) and the model's
    # canonical measure name (``Revenue``) can differ in case (binder parity,
    # Bug-7780). BUT measure names are unique only case-SENSITIVELY (the DB
    # constraint is ``(model_id, name)``), so two measures whose names differ ONLY
    # in case (``Margin`` / ``margin``) can legally coexist and collapse to one
    # lowercased key. Last-writer-wins there would serve one measure's stored
    # component under the OTHER measure's alias (wrong number) — the exact
    # same-name/last-writer-wins hazard the leaf-id maps poison. POISON an ambiguous
    # lowercased measure name (>1 distinct plan) so any query touching it fails
    # closed to source, never a mislabelled component. A unique name resolves.
    _plans_by_lname: dict[str, list] = {}
    for mp in proof.measure_plans:
        _plans_by_lname.setdefault(str(mp.measure_name).lower(), []).append(mp)
    measure_plan_by_name: dict[str, object] = {}
    _ambiguous_measure_lnames: set[str] = set()
    for _ln, _mps in _plans_by_lname.items():
        # Distinct by (measure_name, plan, requested_stat): two plan objects for the
        # genuinely same measure are not a collision; two DIFFERENT measures folding
        # to one lowercased name are.
        _distinct = {(m.measure_name, m.plan, getattr(m, "requested_stat", None)) for m in _mps}
        if len(_distinct) > 1:
            _ambiguous_measure_lnames.add(_ln)
        else:
            measure_plan_by_name[_ln] = _mps[0]

    parts: list[str] = []
    for ordinal, expr in enumerate(select_exprs):
        cls = getattr(expr, "classification", None)
        alias = getattr(expr, "alias", None)

        if cls == "passthrough":
            raw = _strip_alias(getattr(expr, "raw_text", None) or "")
            # ATTRIBUTE / unchanged-PHYSICAL bare-detail projection (§3.4): the
            # binder bound this SELECT ordinal to a group key. Resolve the served
            # plan by attribute identity (relabel) or by the physical key id, then
            # project the passenger / built key column. Bare details have no
            # function fingerprint, so this must run BEFORE the expression-key path.
            pj = proj_by_ordinal.get(ordinal)
            if pj is not None and pj.kind in ("ATTRIBUTE", "PHYSICAL"):
                kp = None
                if pj.kind == "ATTRIBUTE" and pj.attribute_key:
                    # An ATTRIBUTE projection resolves ONLY a relabel plan (by
                    # attribute key), never a PHYSICAL plan — the passenger is the
                    # served column.
                    _cand = key_plan_by_ak.get(pj.attribute_key)
                    if _cand is not None and getattr(_cand, "plan", None) == DIRECT_ATTRIBUTE_RELABEL:
                        kp = _cand
                elif pj.kind == "PHYSICAL" and pj.key_id:
                    # A PHYSICAL projection resolves ONLY the unchanged-physical plan
                    # (Fable R3 #1). A BIJECTION relabel plan shares the SAME
                    # ``artifact_key_id = dim:<owning>`` as the owning dimension's
                    # PHYSICAL plan; matching on id alone would serve the passenger
                    # under the KEY column's label (a wrong-numbers serve). Require
                    # ``plan == DIRECT_PHYSICAL_KEY`` so the built KEY column — never
                    # the passenger — is projected for a physical group key.
                    for _kp in proof.query_key_plans:
                        if (
                            getattr(_kp, "plan", None) == DIRECT_PHYSICAL_KEY
                            and getattr(_kp, "artifact_key_id", None) == pj.key_id
                        ):
                            kp = _kp
                            break
                if kp is None:
                    raise AggregateRewriteUnsupported(
                        f"derived exact read: bound group key {raw!r} not in the proof plan"
                    )
                label = _reproducible_relabel_label(
                    expr, raw, alias, getattr(lq, "input_dialect", "postgres"),
                )
                parts.append(f"{_key_plan_physical(aggregate, kp)} AS {_pgq(label)}")
                continue
            # The projected derived (function) group key. Match it to a served key
            # plan by canonical fingerprint (the SAME canonicaliser + dialect the
            # binder used), then project the served physical key under the alias.
            ce = None
            try:
                ce = canonicalise_sql(raw, input_dialect=getattr(lq, "input_dialect", "postgres"))
            except Exception:  # noqa: BLE001
                ce = None
            kp = key_plan_by_fp.get(ce.fingerprint) if ce is not None else None
            if kp is None:
                raise AggregateRewriteUnsupported(
                    f"derived exact read: projection {raw!r} is not a served group key"
                )
            if not alias:
                # A served key with no explicit alias: the source path would label
                # it with an engine default (e.g. ``date_trunc``) this read cannot
                # reproduce -> route to source.
                raise AggregateRewriteUnsupported(
                    "derived exact read: served group key has no explicit alias to preserve"
                )
            parts.append(f"{_key_plan_physical(aggregate, kp)} AS {_pgq(alias)}")

        elif cls == "analytical":
            m_name = (getattr(expr, "inner_column", None) or "")
            if m_name.lower() in _ambiguous_measure_lnames:
                # Two measures differing only in case fold to this name -> we cannot
                # tell which stored component the query means -> fail closed.
                raise AggregateRewriteUnsupported(
                    f"derived exact read: measure name {m_name!r} is case-ambiguous "
                    "across measures — cannot resolve which component to serve"
                )
            mp = measure_plan_by_name.get(m_name.lower())
            if mp is None:
                raise AggregateRewriteUnsupported(
                    f"derived exact read: measure {m_name!r} not in the proof plan"
                )
            if not alias:
                raise AggregateRewriteUnsupported(
                    f"derived exact read: measure {m_name} has no explicit alias to preserve"
                )
            parts.append(f"{_measure_component_expr(mp, col_lookup)} AS {_pgq(alias)}")

        elif cls == "literal":
            # Only COUNT(*)/COUNT(1) -> the synthetic row-count measure plan.
            # (Absent from the plan map either because the proof has no row-count
            # measure OR because ``__row_count`` was poisoned as case-ambiguous —
            # both fail closed to source, which is correct.)
            mp = measure_plan_by_name.get("__row_count")
            if mp is None:
                raise AggregateRewriteUnsupported(
                    "derived exact read: COUNT(*) has no unambiguous row-count plan"
                )
            if not alias:
                raise AggregateRewriteUnsupported(
                    "derived exact read: COUNT(*) has no explicit alias to preserve"
                )
            parts.append(f"{_measure_component_expr(mp, col_lookup)} AS {_pgq(alias)}")

        else:
            raise AggregateRewriteUnsupported(
                f"derived exact read: unsupported select item classification {cls!r}"
            )
    return parts


def _projection_from_proof(
    *, aggregate: AggregateDefinition, proof: DerivedServeProof, col_lookup: dict,
) -> list[str]:
    """Proof-driven keys-then-measures projection for SYNTHETIC IR (no parsed
    SELECT list). Keys are projected under ``DerivedKeyPlan.query_key`` and
    measures under their measure name — the pre-SELECT-walk behaviour retained for
    unit tests that construct a BoundQuery without ``select_expressions``.
    """
    parts: list[str] = []
    for kp in proof.query_key_plans:
        parts.append(f"{_key_plan_physical(aggregate, kp)} AS {_pgq(kp.query_key)}")
    for mp in proof.measure_plans:
        parts.append(f"{_measure_component_expr(mp, col_lookup)} AS {_pgq(mp.measure_name)}")
    return parts


def _assert_select_shape_is_exactly_keys_and_bare_measures(
    bound_query: BoundQuery, proof: DerivedServeProof,
) -> None:
    """Refuse any SELECT list the proof-driven projection would not reproduce faithfully.

    The exact read projects ONLY the proof's key plans (as ``phys AS alias``) and its
    measure plans (as ``component AS measure_name``). So the served SELECT must be
    EXACTLY: the served derived group-key expression(s) + bare, unwrapped ``AGG(col)``
    measures — nothing more. Any of these route to SOURCE (fail-closed):
      - an extra projection (e.g. a second scalar of the grouped key) -> would be
        silently DROPPED (Fable F2a);
      - a wrapped measure like ``ROUND(SUM(x), 2)`` -> the wrapper would be dropped
        and the raw component served (F2c);
      - a passthrough hiding an aggregate the proof did not map (F3);
      - a bare column projection not covered by a key plan.
    A full SELECT-order/alias/wrapper-preserving rewrite (reusing the ordinary path's
    raw-text AST transform) is deferred to the operational turn-on.
    """
    lq = bound_query.logical_query
    select_exprs = list(getattr(lq, "select_expressions", []) or [])
    if not select_exprs:
        return  # nothing parsed (e.g. synthetic IR) — the proof plans drive projection

    # Canonical fingerprints of the served group-key expressions (from the bound
    # derived expressions the proof consumed).
    served_key_fps = {
        bde.expression_fingerprint
        for bde in (getattr(bound_query, "bound_derived_expressions", []) or [])
        if bde.expression_fingerprint
    }
    # Measure names the proof will project.
    proof_measure_names = {mp.measure_name for mp in proof.measure_plans}
    # SELECT ordinal -> bound group-key projection (ATTRIBUTE / unchanged-PHYSICAL
    # bare-detail keys, §3.4). A passthrough at such an ordinal is a served group
    # key even though it carries no expression fingerprint.
    proj_by_ordinal: dict[int, object] = {}
    for pj in (getattr(bound_query, "bound_group_key_projections", []) or []):
        if pj.select_ordinal < 0:
            continue
        if pj.select_ordinal in proj_by_ordinal:
            raise AggregateRewriteUnsupported(
                f"derived exact read: SELECT ordinal {pj.select_ordinal} is claimed "
                "by more than one group key (multiply-matched item)"
            )
        proj_by_ordinal[pj.select_ordinal] = pj
    # Served ids/keys, PARTITIONED BY PLAN KIND (Fable R3 #1): an ATTRIBUTE
    # projection must resolve a RELABEL plan and a PHYSICAL projection a
    # DIRECT_PHYSICAL_KEY plan — a bijection relabel and the owning dimension's
    # physical key share the same ``artifact_key_id = dim:<owning>``, so an
    # unpartitioned membership test would accept the wrong plan.
    served_ak = {
        getattr(kp, "attribute_key", None)
        for kp in proof.query_key_plans
        if getattr(kp, "plan", None) == DIRECT_ATTRIBUTE_RELABEL
    }
    served_physical_key_ids = {
        getattr(kp, "artifact_key_id", None)
        for kp in proof.query_key_plans
        if getattr(kp, "plan", None) == DIRECT_PHYSICAL_KEY
    }

    from shared.semantic.derived_expression import canonicalise_sql

    for ordinal, expr in enumerate(select_exprs):
        cls = getattr(expr, "classification", None)
        if cls == "analytical":
            # A bare measure AGG(col): the parser's raw_text must be exactly the
            # aggregate call (optionally aliased), with no surrounding wrapper. A
            # wrapper (ROUND/CAST/arithmetic) means raw_text != AGG(inner_column).
            inner = (getattr(expr, "inner_column", None) or "")
            func = (getattr(expr, "agg_function", None) or "")
            raw = (getattr(expr, "raw_text", None) or "")
            # Strip a trailing ``AS alias`` (case-insensitive) for the wrapper check.
            core = "".join(_strip_alias(raw).lower().split())
            # A bare inner may be table-qualified (``SUM(orders.amount)``) while
            # ``inner_column`` is unqualified (``amount``). Drop a single leading
            # ``<ident>.`` qualifier from the core argument so a legitimate qualified
            # bare aggregate is not over-restricted to source. (This only widens the
            # ACCEPT set for a bare AGG; a wrapper still fails because its outer
            # function token is not the aggregate.)
            inner_l = inner.lower()
            inner_unqual = inner_l.rsplit(".", 1)[-1]
            accepted_cores = {
                "".join(f"{func}({inner_l})".split()),
                "".join(f"{func}({inner_unqual})".split()),
            }
            if func == "count_distinct":
                accepted_cores.add("".join(f"count(distinct {inner_l})".split()))
                accepted_cores.add("".join(f"count(distinct {inner_unqual})".split()))
            # Also accept a qualified form of the CORE by stripping a leading
            # ``func(<ident>.`` qualifier from the core's argument, then re-matching.
            core_arg = core[len(func) + 1:-1] if core.startswith(f"{func}(") and core.endswith(")") else None
            core_normalised = core
            if core_arg is not None and "." in core_arg and "(" not in core_arg:
                core_normalised = f"{func}({core_arg.rsplit('.', 1)[-1]})"
            if core not in accepted_cores and core_normalised not in accepted_cores:
                # Not a bare AGG(col) — a wrapper or composition is present.
                raise AggregateRewriteUnsupported(
                    f"derived exact read: measure item {raw!r} is not a bare aggregate"
                )
            proof_names_lower = {n.lower() for n in proof_measure_names}
            # Item-LOCAL membership: this analytical item's own measure must be in
            # the proof plan. (The old ``and "__row_count" not in ...`` clause was
            # item-independent, so any query containing COUNT(*) disabled this raise
            # for EVERY analytical item — a broken guard.)
            if inner.lower() not in proof_names_lower:
                raise AggregateRewriteUnsupported(
                    f"derived exact read: measure {inner!r} not in the proof plan"
                )
        elif cls == "literal":
            # Only COUNT(*) / COUNT(1) is allowed (maps to __row_count).
            if (getattr(expr, "agg_function", None) or "").lower() != "count":
                raise AggregateRewriteUnsupported(
                    "derived exact read: non-count literal projection not servable"
                )
        elif cls == "passthrough":
            raw = _strip_alias(getattr(expr, "raw_text", None) or "")
            # ATTRIBUTE / unchanged-PHYSICAL bare-detail projection (§3.4): the
            # binder bound this ordinal to a served group key. The reproducible
            # label rule supplies the output name, so an explicit alias is not
            # required here (unlike a function key, whose default label the exact
            # read cannot reproduce).
            pj = proj_by_ordinal.get(ordinal)
            if pj is not None and pj.kind in ("ATTRIBUTE", "PHYSICAL"):
                if pj.kind == "ATTRIBUTE" and pj.attribute_key in served_ak:
                    continue
                if pj.kind == "PHYSICAL" and pj.key_id in served_physical_key_ids:
                    continue
                raise AggregateRewriteUnsupported(
                    f"derived exact read: bound group key {raw!r} not in the proof plan"
                )
            # Otherwise it must be a served (function) group-key expression by
            # canonical fingerprint, and it must carry an explicit alias (§8.2 label
            # parity — an engine-default label cannot be reproduced).
            ce = None
            try:
                ce = canonicalise_sql(raw, input_dialect=lq.input_dialect)
            except Exception:  # noqa: BLE001
                ce = None
            if ce is None or ce.fingerprint not in served_key_fps:
                raise AggregateRewriteUnsupported(
                    f"derived exact read: projection {raw!r} is not a served group key"
                )
            if not getattr(expr, "alias", None):
                raise AggregateRewriteUnsupported(
                    "derived exact read: served group key has no explicit alias to preserve"
                )
        else:
            raise AggregateRewriteUnsupported(
                f"derived exact read: unsupported select item classification {cls!r}"
            )

    # ARITY check (§3.4): every parsed group-key (passthrough) SELECT item must bind
    # to its OWN output projection, INCLUDING DUPLICATES. Counting bound output
    # projections — not unique proof plans — lets duplicate relabel-key projections
    # with different aliases pass while catching a merge/drop: if a bare-detail
    # group-key ordinal is NOT in the bound projection map AND does not canonicalise
    # to a served function-expression key, or if the proof projects fewer distinct
    # columns than the SELECT requires, an item would be silently dropped.
    #
    # A function (expression) group-key SELECT item is NOT in the bound projection
    # map (those are relabel/physical bare details); it is validated by fingerprint
    # in the passthrough branch above. So the arity we assert is: every passthrough
    # ordinal is either a bound group-key projection OR a served function key —
    # already enforced item-by-item above. The remaining drop case is a query with
    # MORE group-key SELECT items than the binder bound projections for AND that are
    # not function keys; that item raised above. This positive count guard confirms
    # the walk emits one output column per SELECT item (no silent collapse).
    function_key_passthroughs = 0
    for expr in select_exprs:
        if getattr(expr, "classification", None) != "passthrough":
            continue
        _raw = _strip_alias(getattr(expr, "raw_text", None) or "")
        _ce = None
        try:
            _ce = canonicalise_sql(_raw, input_dialect=lq.input_dialect)
        except Exception:  # noqa: BLE001
            _ce = None
        if _ce is not None and _ce.fingerprint in served_key_fps:
            function_key_passthroughs += 1
    measure_items = sum(
        1 for e in select_exprs
        if getattr(e, "classification", None) in ("analytical", "literal")
    )
    # Bound bare-detail group-key SELECT items = passthroughs that resolved through
    # the bound projection map (ATTRIBUTE / unchanged-PHYSICAL), incl. duplicates.
    bound_detail_passthroughs = sum(
        1 for ordinal, e in enumerate(select_exprs)
        if getattr(e, "classification", None) == "passthrough"
        and proj_by_ordinal.get(ordinal) is not None
        and proj_by_ordinal[ordinal].kind in ("ATTRIBUTE", "PHYSICAL")
    )
    projected = function_key_passthroughs + bound_detail_passthroughs + measure_items
    if len(select_exprs) != projected:
        raise AggregateRewriteUnsupported(
            f"derived exact read: SELECT has {len(select_exprs)} items but binds "
            f"{projected} output projections (a duplicate/merged item would be dropped)"
        )


def _resolve_key_physical(
    aggregate: AggregateDefinition, fingerprint: Optional[str],
    *, key_id: Optional[str] = None,
) -> Optional[str]:
    """Resolve the BUILT physical column name for a grain key (Bug-7806, §3.4).

    Preferred resolution is by canonical ``key_id`` (``dim:<uuid>`` /
    ``expr:<fingerprint>``) — a PHYSICAL key is NEVER resolved by logical name, and
    an expression key resolves by id with a fingerprint cross-check. Falls back to
    the fingerprint lookup for a pure EXPRESSION plan that carries no id (older
    manifests). The name comes from the immutable ``grain_keys`` manifest, never a
    logical-name guess. Returns None (caller fails closed) when unresolved.
    """
    grain_keys = aggregate.grain_keys or []
    if key_id:
        for gk in grain_keys:
            if not isinstance(gk, dict) or gk.get("key_id") != key_id:
                continue
            # Expression id cross-check: the stored fingerprint must agree when a
            # fingerprint was supplied (EXPRESSION plan).
            if fingerprint and gk.get("expression_fingerprint") not in (None, fingerprint):
                return None
            phys = gk.get("physical_column")
            return str(phys) if phys else None
        return None
    if not fingerprint:
        return None
    for gk in grain_keys:
        if isinstance(gk, dict) and gk.get("expression_fingerprint") == fingerprint:
            phys = gk.get("physical_column")
            return str(phys) if phys else None
    return None
