"""Time variants become first-class measure rows.

Revision ID: 0033
Revises: 0032
Create Date: 2026-04-23

Pivot from synthetic, query-time variant measures to first-class
``Measure`` rows. See ``docs/architecture/architecture_measures.md`` and
``work/measures-variant-pivot-action-plan.md`` for the full design.

Changes:

  1. Add ``measures.variant_kind``, ``measures.variant_of_measure_id``,
     ``measures.variant_n``.
  2. For every measure with ``time_variants_enabled = TRUE``, insert one
     new measure row per admissible variant (snapshot fields copied from
     the base; admissibility computed against linked hierarchies and the
     model's calendar binding).
  3. Drop ``measures.time_variants_enabled``, ``measures.trailing_n``,
     ``measures.moving_avg_n``.

The variant catalog (names, families, required units, calendar
requirement, default Ns) is duplicated locally inside this migration so
the data-expansion step is stable against future renames or refactors
of ``shared/schemas/measure_formats``.
"""
from __future__ import annotations

import uuid
from typing import Optional

import sqlalchemy as sa
from alembic import op


revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# Local copy of the variant catalog. Frozen here so the migration is
# stable across future refactors of shared/schemas/measure_formats.
# ---------------------------------------------------------------------------

_TIME_VARIANT_NAMES: tuple[str, ...] = (
    "lag",
    "prior_year",
    "prior_quarter",
    "prior_month",
    "prior_week",
    "ytd",
    "qtd",
    "mtd",
    "wtd",
    "ytd_prior_year",
    "yoy_growth",
    "yoy_growth_pct",
    "trailing_n",
    "moving_avg_n",
)

_TIME_VARIANT_FAMILY: dict[str, str] = {
    "lag": "lag",
    "prior_year": "parallel_period",
    "prior_quarter": "parallel_period",
    "prior_month": "parallel_period",
    "prior_week": "parallel_period",
    "ytd": "period_to_date",
    "qtd": "period_to_date",
    "mtd": "period_to_date",
    "wtd": "period_to_date",
    "ytd_prior_year": "period_to_date",
    "yoy_growth": "parallel_period",
    "yoy_growth_pct": "parallel_period",
    "trailing_n": "moving_window",
    "moving_avg_n": "moving_window",
}

_TIME_VARIANT_REQUIRED_UNIT: dict[str, Optional[str]] = {
    "lag": None,
    "prior_year": "year",
    "prior_quarter": "quarter",
    "prior_month": "month",
    "prior_week": "week",
    "ytd": "year",
    "qtd": "quarter",
    "mtd": "month",
    "wtd": "week",
    "ytd_prior_year": "year",
    "yoy_growth": "year",
    "yoy_growth_pct": "year",
    "trailing_n": None,
    "moving_avg_n": None,
}

_TIME_VARIANTS_NEEDING_CALENDAR: frozenset[str] = frozenset({
    "prior_year", "prior_quarter", "prior_month", "prior_week",
    "ytd", "qtd", "mtd", "wtd",
    "ytd_prior_year", "yoy_growth", "yoy_growth_pct",
})

_TIME_VARIANT_DEFAULT_TRAILING_N = 12
_TIME_VARIANT_DEFAULT_MOVING_AVG_N = 30


def _column_exists(table: str, column: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = :table AND column_name = :column"
        ),
        {"table": table, "column": column},
    )
    return result.scalar() is not None


def _constraint_exists(table: str, constraint: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.table_constraints "
            "WHERE table_schema = current_schema() "
            "AND table_name = :table AND constraint_name = :constraint"
        ),
        {"table": table, "constraint": constraint},
    )
    return result.scalar() is not None


# ---------------------------------------------------------------------------
# Admissibility (mirrors shared/semantic/time_variants.py:expand_measure)
# ---------------------------------------------------------------------------


def _admissible_variants(
    *,
    level_units: set[str],
    level_calcs: set[str],
    calendar_bound: bool,
) -> list[str]:
    """Return the variant kinds admissible under these capabilities.

    Variants are dropped silently if their family is unsupported, their
    required unit is absent, or they need a calendar binding that does
    not exist on any of the model's data sources.
    """
    out: list[str] = []
    for variant in _TIME_VARIANT_NAMES:
        family = _TIME_VARIANT_FAMILY[variant]
        if family not in level_calcs:
            continue
        required_unit = _TIME_VARIANT_REQUIRED_UNIT[variant]
        if required_unit is not None and required_unit not in level_units:
            continue
        if variant in _TIME_VARIANTS_NEEDING_CALENDAR and not calendar_bound:
            continue
        out.append(variant)
    return out


def _variant_n_for(variant: str, base_trailing_n: Optional[int],
                   base_moving_avg_n: Optional[int]) -> Optional[int]:
    if variant == "trailing_n":
        return base_trailing_n if base_trailing_n is not None else \
            _TIME_VARIANT_DEFAULT_TRAILING_N
    if variant == "moving_avg_n":
        return base_moving_avg_n if base_moving_avg_n is not None else \
            _TIME_VARIANT_DEFAULT_MOVING_AVG_N
    return None


def _expand_existing_variants() -> None:
    """For every measure with ``time_variants_enabled = TRUE``, insert one
    variant measure row per admissible kind. Uses raw SQL throughout so
    the migration is independent of the ORM (which already reflects the
    new schema by the time this runs)."""
    conn = op.get_bind()

    # Pull every variant-enabled base measure with its model id and the
    # parametric Ns. Skip rows that already have variant_kind set
    # (defensive — should not happen at this stage but cheap to check).
    bases = conn.execute(sa.text(
        "SELECT id, model_id, name, display_name, description, format, "
        "       data_type, default_agg, is_additive, "
        "       trailing_n, moving_avg_n, display_folder, "
        "       source_column_id, user_defined_attribute_id "
        "FROM measures "
        "WHERE time_variants_enabled = TRUE "
        "  AND variant_kind IS NULL"
    )).mappings().all()

    if not bases:
        return

    base_ids_by_model: dict[uuid.UUID, list[uuid.UUID]] = {}
    base_rows_by_id: dict[uuid.UUID, dict] = {}
    for row in bases:
        base_ids_by_model.setdefault(row["model_id"], []).append(row["id"])
        base_rows_by_id[row["id"]] = dict(row)

    # Build a map measure_id -> set(level_units), set(level_calcs)
    # by joining hierarchy_measure_links → hierarchy_levels.
    units_by_measure: dict[uuid.UUID, set[str]] = {}
    calcs_by_measure: dict[uuid.UUID, set[str]] = {}

    base_id_list = list(base_rows_by_id.keys())
    if base_id_list:
        rows = conn.execute(sa.text(
            "SELECT hml.measure_id AS measure_id, "
            "       hl.time_unit AS time_unit, "
            "       hl.allowed_time_calcs AS allowed_time_calcs "
            "FROM hierarchy_measure_links hml "
            "JOIN hierarchy_levels hl ON hl.hierarchy_id = hml.hierarchy_id "
            "WHERE hml.measure_id = ANY(:ids)"
        ), {"ids": base_id_list}).mappings().all()
        for r in rows:
            mid = r["measure_id"]
            if r["time_unit"]:
                units_by_measure.setdefault(mid, set()).add(r["time_unit"])
            calcs = r["allowed_time_calcs"] or []
            calcs_by_measure.setdefault(mid, set()).update(calcs)

    # calendar_bound per model.
    calendar_bound_by_model: dict[uuid.UUID, bool] = {}
    for model_id in base_ids_by_model.keys():
        bound = conn.execute(sa.text(
            "SELECT 1 FROM data_sources "
            "WHERE model_id = :model_id AND calendar_table_id IS NOT NULL "
            "LIMIT 1"
        ), {"model_id": model_id}).scalar() is not None
        calendar_bound_by_model[model_id] = bound

    # Insert variants. Use a separate INSERT per row for clarity; the
    # volume per migration is small (number of variant-enabled measures
    # times up to 14).
    insert_sql = sa.text(
        "INSERT INTO measures ("
        "  id, model_id, name, display_name, description, display_folder, "
        "  source_column_id, user_defined_attribute_id, "
        "  measure_type, expression, data_type, default_agg, format, "
        "  is_additive, is_invalid, "
        "  variant_kind, variant_of_measure_id, variant_n, "
        "  created_at, updated_at"
        ") VALUES ("
        "  :id, :model_id, :name, :display_name, :description, :display_folder, "
        "  :source_column_id, :user_defined_attribute_id, "
        "  :measure_type, NULL, :data_type, :default_agg, :format, "
        "  :is_additive, FALSE, "
        "  :variant_kind, :variant_of_measure_id, :variant_n, "
        "  NOW(), NOW()"
        ")"
    )

    for base_id, base in base_rows_by_id.items():
        units = units_by_measure.get(base_id, set())
        calcs = calcs_by_measure.get(base_id, set())
        calendar_bound = calendar_bound_by_model.get(base["model_id"], False)
        admissible = _admissible_variants(
            level_units=units,
            level_calcs=calcs,
            calendar_bound=calendar_bound,
        )
        if not admissible:
            continue
        for variant in admissible:
            display_base = base["display_name"] or base["name"]
            conn.execute(insert_sql, {
                "id": uuid.uuid4(),
                "model_id": base["model_id"],
                "name": f"{base['name']}_{variant}",
                "display_name": f"{display_base} ({variant})",
                "description": base["description"],
                "display_folder": base["display_folder"],
                # Snapshot the base's physical reference so the rewriter
                # can resolve the underlying column without a second
                # lookup. variant_of_measure_id remains for cascade-delete
                # and UI traversal.
                "source_column_id": base["source_column_id"],
                "user_defined_attribute_id": base["user_defined_attribute_id"],
                "measure_type": "standard",
                "data_type": base["data_type"],
                "default_agg": base["default_agg"],
                "format": base["format"],
                "is_additive": base["is_additive"],
                "variant_kind": variant,
                "variant_of_measure_id": base_id,
                "variant_n": _variant_n_for(
                    variant, base["trailing_n"], base["moving_avg_n"]
                ),
            })


def _collapse_existing_variants_back() -> None:
    """Inverse of ``_expand_existing_variants`` for downgrade.

    Best-effort: any base measure with at least one variant row pointing
    at it gets ``time_variants_enabled = TRUE``; the highest variant_n
    among ``trailing_n`` / ``moving_avg_n`` rows is copied back.
    """
    conn = op.get_bind()

    rows = conn.execute(sa.text(
        "SELECT variant_of_measure_id AS base_id, variant_kind, variant_n "
        "FROM measures WHERE variant_kind IS NOT NULL"
    )).mappings().all()
    if not rows:
        return

    by_base: dict[uuid.UUID, dict[str, Optional[int]]] = {}
    for r in rows:
        bid = r["base_id"]
        slot = by_base.setdefault(bid, {"trailing_n": None, "moving_avg_n": None})
        if r["variant_kind"] == "trailing_n":
            slot["trailing_n"] = r["variant_n"]
        elif r["variant_kind"] == "moving_avg_n":
            slot["moving_avg_n"] = r["variant_n"]

    for base_id, slot in by_base.items():
        conn.execute(sa.text(
            "UPDATE measures SET time_variants_enabled = TRUE, "
            "  trailing_n = :trailing_n, moving_avg_n = :moving_avg_n "
            "WHERE id = :id"
        ), {"id": base_id, **slot})

    conn.execute(sa.text("DELETE FROM measures WHERE variant_kind IS NOT NULL"))


# ---------------------------------------------------------------------------
# Schema operations
# ---------------------------------------------------------------------------


def upgrade() -> None:
    if not _column_exists("measures", "variant_kind"):
        op.add_column(
            "measures",
            sa.Column("variant_kind", sa.String(length=32), nullable=True),
        )
    if not _column_exists("measures", "variant_of_measure_id"):
        op.add_column(
            "measures",
            sa.Column(
                "variant_of_measure_id",
                sa.dialects.postgresql.UUID(as_uuid=True),
                sa.ForeignKey("measures.id", ondelete="CASCADE"),
                nullable=True,
            ),
        )
    if not _column_exists("measures", "variant_n"):
        op.add_column(
            "measures",
            sa.Column("variant_n", sa.Integer(), nullable=True),
        )
    if not _constraint_exists("measures", "measures_variant_consistency"):
        op.create_check_constraint(
            "measures_variant_consistency",
            "measures",
            "(variant_kind IS NULL AND variant_of_measure_id IS NULL) "
            "OR (variant_kind IS NOT NULL AND variant_of_measure_id IS NOT NULL)",
        )

    # Data expansion runs against the new schema (new columns exist; old
    # columns still present so the SELECT can read time_variants_enabled).
    _expand_existing_variants()

    if _column_exists("measures", "time_variants_enabled"):
        op.drop_column("measures", "time_variants_enabled")
    if _column_exists("measures", "trailing_n"):
        op.drop_column("measures", "trailing_n")
    if _column_exists("measures", "moving_avg_n"):
        op.drop_column("measures", "moving_avg_n")


def downgrade() -> None:
    if not _column_exists("measures", "time_variants_enabled"):
        op.add_column(
            "measures",
            sa.Column(
                "time_variants_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )
    if not _column_exists("measures", "trailing_n"):
        op.add_column(
            "measures",
            sa.Column("trailing_n", sa.Integer(), nullable=True),
        )
    if not _column_exists("measures", "moving_avg_n"):
        op.add_column(
            "measures",
            sa.Column("moving_avg_n", sa.Integer(), nullable=True),
        )

    _collapse_existing_variants_back()

    if _constraint_exists("measures", "measures_variant_consistency"):
        op.drop_constraint("measures_variant_consistency", "measures", type_="check")
    if _column_exists("measures", "variant_n"):
        op.drop_column("measures", "variant_n")
    if _column_exists("measures", "variant_of_measure_id"):
        op.drop_column("measures", "variant_of_measure_id")
    if _column_exists("measures", "variant_kind"):
        op.drop_column("measures", "variant_kind")
