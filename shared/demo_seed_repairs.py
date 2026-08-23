"""Deterministic repairs applied to the checked-in demo export bundle.

The Community and Acme seed callers consume the same ``acme-demo`` export, but
the Community image does not ship the repository-level ``scripts`` package.
Keeping these bundle-only repairs in ``shared`` makes the supported callers use
one implementation while preserving the bundle itself as immutable input.
"""
from __future__ import annotations


def validate_bundle_calendar_tables(bundle: dict) -> list[str]:
    """Return conflicting calendar registrations before any DB writes."""
    errors: list[str] = []
    for model_snap in bundle.get("models", []):
        slug = model_snap.get("model", {}).get("slug", "<unknown>")
        seen: dict[tuple[str, str], tuple] = {}
        for row in model_snap.get("calendar_tables", []) or []:
            source_id = str(row.get("data_source_id") or "")
            raw_table = row.get("table_name") or ""
            parts = str(raw_table).split(".")
            table_key = ".".join(parts[-2:]) if len(parts) >= 3 else str(raw_table)
            if not source_id or not table_key:
                continue
            key = (source_id, table_key)
            signature = (
                row.get("calendar_type"),
                row.get("date_column"),
                row.get("year_column"),
                row.get("half_column"),
                row.get("quarter_column"),
                row.get("month_column"),
                row.get("week_column"),
                row.get("day_column"),
            )
            previous = seen.get(key)
            if previous is not None and previous != signature:
                errors.append(
                    f"model '{slug}': conflicting calendar mappings for "
                    f"source table {table_key!r} (data_source_id={source_id})"
                )
            seen[key] = signature
    return errors


def repair_demo_structural_flags(bundle: dict) -> None:
    """Clear only stale fact-anchor reachability diagnostics in the demo bundle.

    The checked-in export predates the validator's fact-anchor reachability
    repair.  ``modell`` and ``onboarding`` now carry the required direct joins,
    so those exact old invalid reasons are stale.  A missing required join is a
    fatal bundle error; no broad invalid-flag reset is permitted.
    """
    expected = {
        "modell": (
            "large_demo_data.calendar",
            "large_demo_data.dim_channel_code",
            "large_demo_data.dim_yes_no_flag",
        ),
        "onboarding": ("cmb_digital_banking.dim_date",),
    }
    for model_snap in bundle.get("models", []):
        model = model_snap.get("model") or {}
        slug = model.get("slug")
        source_names = expected.get(slug)
        if source_names is None:
            continue
        tables = {row.get("id"): row for row in model_snap.get("tables", [])}
        fact_ids = {
            table_id
            for table_id, row in tables.items()
            if row.get("table_type") == "fact"
        }
        for source_name in source_names:
            source_ids = {
                table_id
                for table_id, row in tables.items()
                if row.get("physical_name") == source_name
            }
            reachable_join = any(
                {join.get("left_table_id"), join.get("right_table_id")}
                == {source_id, fact_id}
                for join in model_snap.get("joins", [])
                for source_id in source_ids
                for fact_id in fact_ids
            )
            if not reachable_join:
                raise ValueError(
                    f"canonical demo bundle {slug!r} is missing the required "
                    f"fact-anchor join for {source_name!r}"
                )
            stale_reason = (
                f"Source table {source_name} is no longer reachable from the fact table"
            )
            for family in ("dimensions", "measures"):
                for row in model_snap.get(family, []) or []:
                    if row.get("invalid_reason") == stale_reason:
                        row["is_invalid"] = False
                        row["invalid_reason"] = None


def strip_unmaterialisable_variant_columns(bundle: dict) -> int:
    """Remove only variant aggregate columns with no physical time grain.

    The source route remains available for these measures.  Ordinary measure
    columns at the same grain are retained so the demo still gets useful
    acceleration without asking the refresh service to manufacture a time
    context that is not present in the selected aggregate.
    """
    removed = 0
    for model_snap in bundle.get("models", []):
        dimensions = model_snap.get("dimensions", []) or []
        dimension_by_ref = {
            str(row.get(key)): row
            for row in dimensions
            for key in ("id", "name")
            if row.get(key) is not None
        }
        variant_ids = {
            str(row.get("id"))
            for row in (model_snap.get("measures", []) or [])
            if row.get("id") is not None and row.get("variant_kind")
        }
        if not variant_ids:
            continue

        for aggregate in model_snap.get("aggregates", []) or []:
            columns = aggregate.get("columns", []) or []
            if not columns:
                continue
            has_source_backed_time = any(
                (
                    (dimension := dimension_by_ref.get(str(grain))) is not None
                    and dimension.get("is_time_dim") is True
                    and dimension.get("source_column_id") is not None
                )
                for grain in (aggregate.get("grain", []) or [])
            )
            if has_source_backed_time:
                continue
            kept = [
                column
                for column in columns
                if str(column.get("measure_id")) not in variant_ids
            ]
            removed += len(columns) - len(kept)
            if len(kept) != len(columns):
                aggregate["columns"] = kept
    return removed


__all__ = [
    "repair_demo_structural_flags",
    "strip_unmaterialisable_variant_columns",
    "validate_bundle_calendar_tables",
]
