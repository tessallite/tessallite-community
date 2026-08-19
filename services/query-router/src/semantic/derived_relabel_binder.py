"""Stage-4 relabel binding: stable leaf ids + dimension->relationship resolver.

Spec: architecture_derived-grain-stage4-relabel-serving.md §3.1 (Gap 1 stable-UUID
derived-expression leaves), §3.2 (Gap 2 dimension-to-relationship resolver), §3.4
(Gap 4 SELECT reachability + unchanged-PHYSICAL identity).

This module is the pure resolution layer the binder calls when the deployed
snapshot is available. It resolves derived-expression leaves and bare detail
dimensions to STABLE ids against the pinned :class:`DeployedShape`, using the
parsed FROM scope — never a logical/display/source name. Everything fails CLOSED:
an unresolved, ambiguous, cross-model, out-of-scope, or non-BIJECTION shape yields
no serving identity, so the router routes to source.

Nothing here changes routed SQL on its own — the OFF-by-default routing flag gates
the whole derived-serving path. These records only become serving requests when a
production-built artifact carries matching stable ids.
"""
from __future__ import annotations

from typing import Any, Optional

import sqlglot
from sqlglot import exp

from shared.semantic.derived_expression import CanonicalLeaf
from src.ir.logical_query import (
    BoundAttributeRelabel,
    BoundColumnRef,
    LogicalQuery,
)
from src.semantic.snapshot_resolver import DeployedShape

BIJECTION = "BIJECTION"
REJECT_NULL = "REJECT_NULL"


def build_from_scope_alias_map(
    query: LogicalQuery, shape: DeployedShape,
) -> dict[str, str]:
    """Map parsed FROM-scope alias/token -> deployed table_id (spec §3.1/§3.2).

    Walks the query's own FROM/JOIN tree (parsed with the query's input dialect,
    never regex) and builds ``alias-or-token -> table_id`` for every relation whose
    underlying table token resolves through the deployed table-name index. An alias
    that is ambiguous within the parsed scope, or whose table token is not in the
    deployed model, is OMITTED (poisoned) so a qualified leaf under it fails closed.

    The map keys are lower-cased. A relation contributes:
      - its explicit alias, when present;
      - the table token's terminal identifier, when no alias (bare ``FROM orders``).
    """
    out: dict[str, str] = {}
    poisoned: set[str] = set()
    seen: dict[str, Optional[str]] = {}
    raw = getattr(query, "raw_query", None)
    if not raw:
        return out
    input_dialect = getattr(query, "input_dialect", "postgres") or "postgres"
    try:
        ast = sqlglot.parse_one(raw, read=input_dialect)
    except Exception:
        return out
    if ast is None:
        return out

    def _add(key: Optional[str], table_id: Optional[str]) -> None:
        # Bug-7873b: poison on ANY conflicting alias/token reuse in the parsed
        # scope, INCLUDING a reuse where one (or both) sides resolve to an UNKNOWN
        # deployed table (``table_id is None``). The prior logic returned early on
        # ``table_id is None`` without recording the alias, so ``FROM unknown u JOIN
        # orders u`` left ``u`` resolving cleanly to ``orders`` — a qualified leaf
        # ``u.col`` would then bind to orders even though ``u`` is ambiguous in the
        # query. Now: an alias seen twice with DIFFERENT bindings (any of which may
        # be None) is poisoned; only a token that maps to a single known table_id
        # survives into ``out``.
        if not key:
            return
        k = key.lower()
        if k in poisoned:
            return
        if k in seen:
            if seen[k] != table_id:
                poisoned.add(k)
                out.pop(k, None)
                return
        else:
            seen[k] = table_id
        if table_id:
            out[k] = table_id

    for tbl in ast.find_all(exp.Table):
        token = (tbl.name or "")
        if not token:
            continue
        table_id = shape.table_name_ids.get(token.lower())
        # A table token absent from the deployed model index is recorded with a
        # None binding so a later conflicting reuse of the same alias/token still
        # poisons (see _add). A qualified leaf under an unknown-only token still
        # fails closed (it never enters ``out``).
        alias = tbl.alias or None
        if alias:
            _add(alias, table_id)
        else:
            _add(token, table_id)
    return out


def resolve_leaf_column_ids(
    leaves: list[CanonicalLeaf],
    *,
    shape: DeployedShape,
    alias_map: dict[str, str],
) -> Optional[list[tuple[str, str]]]:
    """Resolve an ordered leaf tuple to ordered ``(table_id, column_id)`` (spec §3.1).

    ALL-OR-NOTHING: returns the ordered list ONLY when every leaf resolves to a
    stable deployed column id; otherwise returns ``None`` so the whole expression
    withholds serving lineage. A qualified leaf resolves through the FROM-scope
    alias map + deployed qualified column index; an unqualified leaf resolves only
    when exactly one deployed column carries that name (``physical_column_ids`` is
    already poisoned for ambiguous names). Order is preserved exactly.
    """
    resolved: list[tuple[str, str]] = []
    for leaf in leaves:
        if leaf.qualifier:
            table_id = alias_map.get(leaf.qualifier.lower())
            if not table_id:
                return None  # qualifier absent from FROM scope / ambiguous
            col_id = shape.qualified_column_ids.get((table_id, leaf.name))
            if not col_id:
                return None
            resolved.append((table_id, col_id))
        else:
            col_id = shape.physical_column_ids.get(leaf.name)
            if not col_id:
                return None  # unresolved or ambiguous unqualified name
            table_id = _table_id_for_column(shape, col_id)
            if not table_id:
                return None
            resolved.append((table_id, col_id))
    return resolved


def _table_id_for_column(shape: DeployedShape, column_id: str) -> Optional[str]:
    col = shape.columns_by_id.get(column_id)
    if not col:
        return None
    tid = col.get("model_table_id")
    return str(tid) if tid else None


def bind_expression_leaves(
    leaves: list[CanonicalLeaf],
    *,
    model_id: str,
    shape: DeployedShape,
    alias_map: dict[str, str],
) -> Optional[list[BoundColumnRef]]:
    """Bind ordered leaves to BoundColumnRef with stable table_id + column_id.

    Returns ``None`` (withhold all lineage) when any leaf is unresolved/ambiguous/
    out-of-scope (spec §3.1 all-or-nothing). The physical name and logical name are
    diagnostics only; the stable ids are the serving evidence.
    """
    resolved = resolve_leaf_column_ids(leaves, shape=shape, alias_map=alias_map)
    if resolved is None:
        return None
    out: list[BoundColumnRef] = []
    for leaf, (table_id, column_id) in zip(leaves, resolved):
        col = shape.columns_by_id.get(column_id) or {}
        phys = col.get("column_name") or leaf.name
        out.append(BoundColumnRef(
            model_id=str(model_id),
            table_id=str(table_id),
            column_id=str(column_id),
            physical_column=str(phys),
            logical_name=leaf.name,
        ))
    return out


def resolve_dimension_relabel(
    *,
    query_dimension: Any,
    shape: DeployedShape,
    group_ordinal: int,
    select_ordinals: list[int],
    requested_name: str,
    output_alias: Optional[str],
) -> Optional[BoundAttributeRelabel]:
    """Resolve a bare detail dimension to one BIJECTION relationship (spec §3.2).

    Returns a fully-populated :class:`BoundAttributeRelabel` ONLY when the query
    detail dimension resolves to EXACTLY ONE enabled, deployed BIJECTION
    relationship satisfying every §3.2 condition; otherwise ``None`` (route to
    source). Never consults ``display_column_id``; several eligible declarations
    are ambiguous even if current data agrees and route to source.
    """
    dq_source_col = getattr(query_dimension, "source_column_id", None)
    if dq_source_col is None:
        return None
    detail_col_id = str(dq_source_col)

    # Exactly one enabled deployed relationship whose detail_column_id == Dq's
    # source column. Several matches are ambiguous -> None.
    matches = [
        r for r in shape.attribute_relationships
        if bool(r.get("enabled"))
        and r.get("detail_column_id") is not None
        and str(r.get("detail_column_id")) == detail_col_id
    ]
    if len(matches) != 1:
        return None
    rel = matches[0]

    cardinality = str(rel.get("cardinality") or "")
    if cardinality != BIJECTION:
        return None
    if str(rel.get("null_policy") or REJECT_NULL) != REJECT_NULL:
        return None

    key_col_id = rel.get("key_column_id")
    rel_id = rel.get("id")
    owning_dim_id = rel.get("dimension_id")
    declaration_hash = rel.get("declaration_hash")
    if not (key_col_id and rel_id and owning_dim_id and declaration_hash):
        return None
    key_col_id = str(key_col_id)
    rel_id = str(rel_id)
    owning_dim_id = str(owning_dim_id)

    # The owning dimension must resolve in the deployed catalogue and its
    # source_column_id must equal the relationship key column (§3.2 cond. 3-4).
    owning_dim = shape.dimensions_by_id.get(owning_dim_id)
    if owning_dim is None:
        return None
    owning_src = getattr(owning_dim, "source_column_id", None)
    if owning_src is None or str(owning_src) != key_col_id:
        return None

    # Key and detail columns must exist in the deployed catalogue and belong to
    # the SAME relation (same-relation v1 contract, §3.2 cond. 5).
    key_col = shape.columns_by_id.get(key_col_id)
    detail_col = shape.columns_by_id.get(detail_col_id)
    if key_col is None or detail_col is None:
        return None
    if str(key_col.get("model_table_id") or "") != str(detail_col.get("model_table_id") or ""):
        return None

    return BoundAttributeRelabel(
        group_ordinal=group_ordinal,
        select_ordinals=list(select_ordinals),
        query_dimension_id=str(getattr(query_dimension, "id", "")),
        owning_dimension_id=owning_dim_id,
        relationship_id=rel_id,
        attribute_key=f"attr:{rel_id}",
        key_column_id=key_col_id,
        detail_column_id=detail_col_id,
        cardinality=cardinality,
        declaration_hash=str(declaration_hash),
        requested_name=requested_name,
        output_alias=output_alias,
    )
