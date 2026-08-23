"""Shared pure build-manifest planner (spec §3.6, Gap 6).

One shared pure planner turns a pinned deployed shape + the exact
``ResolvedAggregateLayout`` used for SQL generation into an ordered
``MaterializedGrainKey`` list (and, when relationships are eligible, passenger +
attribute-edge drafts). It performs NO writes and NO SQL — the producer
(optimizer creator, scheduler full/incremental refresh) consumes the draft, and
``advance_artifact_manifest`` validates/hashes/persists it.

Grain-key construction (spec §3.6):
  - Physical dimension: ``key_id=dim:<Dimension.id>``, ``kind=PHYSICAL_COLUMN``,
    ``input_column_ids=[Dimension.source_column_id]``, physical_column = the final
    built alias (``ResolvedGrainCol.physical_col_name``).
  - Expression (UDA / derived grain): canonicalise the exact pre-transpile CTAS
    expression, ``key_id=expr:<ce.fingerprint>``, lineage resolved from the ordered
    qualifier-aware ``ce.leaves`` through the pinned catalogue in the shared
    canonical order (spec §3.1 producer/consumer contract). A qualified leaf has no
    build-side FROM scope in v1, so it withholds the whole lineage (fail closed,
    symmetric with the query side).

Any missing/mismatched identity makes a key NON-SERVABLE: the physical column must
exist and belong to the dimension; an expression's leaves must all resolve. A
non-servable key is still emitted (so ``grain_keys`` is never ``[]`` for a grained
artifact, spec §3.5) but with empty ``input_column_ids`` so the query-side lineage
gate fails closed — never a wrong serve.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from shared.semantic.artifact_manifest import (
    KIND_ARTIFACT_EXPRESSION,
    KIND_PHYSICAL_COLUMN,
    MaterializedGrainKey,
)
from shared.semantic.derived_expression import canonicalise_sql

logger = logging.getLogger(__name__)


def unambiguous_col_id_by_name(model_columns: Any) -> dict[str, str]:
    """lowercase ``ModelColumn.column_name`` -> stable id, POISONING ambiguous names.

    Bug-7874: this producer-side name->column-id poisoning map was triplicated in
    the three build producers (optimizer creator, scheduler full-refresh, scheduler
    incremental) with byte-identical semantics. Hoisted here (its natural home, next
    to the planner that consumes it) so all three call ONE implementation and can
    never drift.

    A name that resolves to more than one ``ModelColumn`` across the model's tables
    is OMITTED (poisoned) so an expression-key leaf binding to it fails closed rather
    than picking an arbitrary winner — mirroring the binder/snapshot poisoning
    (spec §3.1). The resulting map feeds ``build_grain_key_manifest`` so the
    producer's ordered expression-leaf ids match the query binder's byte-for-byte.
    """
    ids_by_name: dict[str, set[str]] = {}
    for c in model_columns:
        name = (getattr(c, "column_name", None) or "").lower()
        if name and getattr(c, "id", None) is not None:
            ids_by_name.setdefault(name, set()).add(str(c.id))
    return {n: next(iter(ids)) for n, ids in ids_by_name.items() if len(ids) == 1}


def column_meta_by_id(model_columns: Any) -> dict[str, dict]:
    """stable ``ModelColumn.id`` -> ``{data_type, is_nullable}`` (Bug-7874 companion).

    The producers built this identical dict alongside the poisoning map; hoisted so
    the type/nullability lineage the grain-key manifest carries is derived one way.
    """
    out: dict[str, dict] = {}
    for c in model_columns:
        if getattr(c, "id", None) is not None:
            out[str(c.id)] = {
                "data_type": getattr(c, "data_type", None),
                "is_nullable": getattr(c, "is_nullable", True),
            }
    return out


def build_grain_key_manifest(
    *,
    layout: Any,
    dimension_by_id: dict[str, Any],
    column_id_by_name: dict[str, str],
    column_meta_by_id: Optional[dict[str, dict]] = None,
) -> list[MaterializedGrainKey]:
    """Build the ordered ``MaterializedGrainKey`` list for a layout (spec §3.6).

    Args:
      layout: the ``ResolvedAggregateLayout`` used for SQL generation.
      dimension_by_id: stringified ``Dimension.id`` -> Dimension (deployed), used
        to resolve each physical grain's ``source_column_id``.
      column_id_by_name: lowercase physical column name -> stable ``ModelColumn.id``
        (POISONED for ambiguous names by the caller), used to resolve expression
        leaf lineage in the shared canonical order.
      column_meta_by_id: optional stable-id -> {data_type, is_nullable} for typing.

    Returns one key per ``layout.grain_cols`` entry, in output order.
    """
    column_meta_by_id = column_meta_by_id or {}
    keys: list[MaterializedGrainKey] = []
    for ordinal, grain in enumerate(layout.grain_cols):
        if grain.source_expression is not None:
            keys.append(_expression_key(
                ordinal, grain, column_id_by_name, column_meta_by_id,
            ))
        else:
            keys.append(_physical_key(
                ordinal, grain, dimension_by_id, column_meta_by_id,
            ))
    return keys


def _physical_key(
    ordinal: int, grain: Any, dimension_by_id: dict[str, Any],
    column_meta_by_id: dict[str, dict],
) -> MaterializedGrainKey:
    dim_id = str(getattr(grain, "dimension_id", "") or "")
    dim = dimension_by_id.get(dim_id)
    src_col_id = getattr(dim, "source_column_id", None) if dim is not None else None
    input_ids: list[str] = []
    output_type = None
    nullable = True
    if src_col_id is not None:
        src_col_id = str(src_col_id)
        input_ids = [src_col_id]
        meta = column_meta_by_id.get(src_col_id)
        if meta:
            output_type = meta.get("data_type")
            nullable = bool(meta.get("is_nullable", True))
    return MaterializedGrainKey(
        ordinal=ordinal,
        key_id=f"dim:{dim_id}" if dim_id else f"dim:unresolved-{ordinal}",
        kind=KIND_PHYSICAL_COLUMN,
        physical_column=grain.physical_col_name,
        logical_name=getattr(grain, "logical_name", None),
        source_dimension_id=dim_id or None,
        input_column_ids=input_ids,  # empty => non-servable (fail-closed)
        output_type=output_type,
        nullable=nullable,
    )


def _expression_key(
    ordinal: int, grain: Any, column_id_by_name: dict[str, str],
    column_meta_by_id: dict[str, dict],
) -> MaterializedGrainKey:
    """Build an expression grain key from the pre-transpile CTAS expression.

    The build-time fingerprint recanonicalises the exact expression the CTAS uses;
    lineage is resolved from the ordered qualifier-aware ``ce.leaves`` (the shared
    canonical order) so it matches the query binder's ordered leaf tuple
    byte-for-byte (spec §3.1/§3.6). A qualified leaf withholds the whole lineage.
    """
    expr_sql = grain.source_expression or ""
    ce = canonicalise_sql(expr_sql, input_dialect="postgres")
    if ce is None or ce.has_unknown_function:
        # Unknown canonicalisation prevents derived-serving eligibility; still emit
        # the key (grain manifest completeness) but with no lineage -> fail closed.
        return MaterializedGrainKey(
            ordinal=ordinal,
            key_id=f"expr:unresolved-{ordinal}",
            kind=KIND_ARTIFACT_EXPRESSION,
            physical_column=grain.physical_col_name,
            logical_name=getattr(grain, "logical_name", None),
            canonical_expression=expr_sql,
            input_column_ids=[],
        )
    # Resolve each leaf to a stable id from the SAME ordered, qualifier-aware leaf
    # tuple the query binder consumes (``ce.leaves``, spec §3.1) — NOT the bare
    # de-duped ``input_columns`` — so the producer and consumer lineage tuples are
    # byte-identical for identical expressions. A QUALIFIED leaf cannot be resolved
    # by the model-wide bare-name map (there is no build-side FROM scope for a
    # stored artifact expression in v1), so its presence poisons the WHOLE lineage
    # (empty -> fail closed on BOTH sides consistently, never a silent mismatch).
    # An unresolved/ambiguous unqualified leaf likewise poisons all-or-nothing.
    input_ids: list[str] = []
    ok = True
    for leaf in ce.leaves:
        if leaf.qualifier is not None:
            ok = False  # qualified artifact-expression leaf -> fail closed (v1)
            break
        cid = column_id_by_name.get(leaf.name)
        if not cid:
            ok = False
            break
        input_ids.append(cid)
    return MaterializedGrainKey(
        ordinal=ordinal,
        key_id=f"expr:{ce.fingerprint}",
        kind=KIND_ARTIFACT_EXPRESSION,
        physical_column=grain.physical_col_name,
        logical_name=getattr(grain, "logical_name", None),
        canonical_expression=ce.canonical_sql,
        expression_fingerprint=ce.fingerprint,
        input_column_ids=input_ids if ok else [],
    )
