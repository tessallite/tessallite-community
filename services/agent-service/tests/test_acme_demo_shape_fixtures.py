from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared.schemas.measure_formats import (
    TIME_VARIANT_ALIASES,
    TIME_VARIANT_NAMES,
    canonical_variant_kind,
)


_SEED_PATH = Path(__file__).resolve().parents[3] / "seeds" / "acme-demo" / "project.json"
_EXECUTABLE_SMOKE_VARIANTS = {
    "ytd",
    "prior_month",
    "prior_year",
    "trailing_n",
    "moving_avg_n",
    "yoy_growth_pct",
    "pct_change",
}
_COMPOUNDED_HIERARCHY_PATTERNS = (
    "auto_generated_for_hierarchy_auto_generated_for_hierarchy",
    "Auto-generated for hierarchy 'Auto-generated for hierarchy",
)


@pytest.fixture(scope="module")
def project_bundle() -> dict:
    return json.loads(_SEED_PATH.read_text())


@pytest.fixture(scope="module")
def modely_bundle(project_bundle: dict) -> dict:
    for bundle in project_bundle["models"]:
        if bundle["model"]["slug"] == "modely":
            return bundle
    raise AssertionError("modely seed bundle not found")


def _variant_measures(bundle: dict) -> dict[str, dict]:
    """Canonical variant per kind: the ``base_amount_<kind>`` measure.

    The seed also carries per-calendar ytd_prior_year fixtures
    (``..._retail`` / ``..._fiscal`` / ``..._iso_week`` / ``..._thai_buddhist``,
    Bug-6682) that share a variant_kind but deliberately anchor on their own
    calendar aliases — they must not shadow the canonical set here.
    """
    return {
        measure["variant_kind"]: measure
        for measure in bundle["measures"]
        if measure.get("variant_kind")
        and measure["name"] == f"base_amount_{measure['variant_kind']}"
    }


def _iter_strings(value: object, path: str = "$"):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _iter_strings(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _iter_strings(child, f"{path}[{index}]")


def _compounded_hierarchy_hits(value: object) -> list[str]:
    return [
        f"{path}: {text}"
        for path, text in _iter_strings(value)
        if any(pattern in text for pattern in _COMPOUNDED_HIERARCHY_PATTERNS)
    ]


def _assert_no_compounded_hierarchy_names(value: object) -> None:
    hits = _compounded_hierarchy_hits(value)
    if not hits:
        return
    sample = "\n".join(hits[:20])
    remaining = len(hits) - 20
    suffix = f"\n... and {remaining} more" if remaining > 0 else ""
    raise AssertionError(f"Found compounded generated hierarchy names:\n{sample}{suffix}")


def _exported_deployed_snapshot(bundle: dict) -> dict:
    deployed_id = bundle.get("exported_deployed_version_id")
    if not deployed_id:
        raise AssertionError("modely seed bundle has no exported deployed version")
    for version in bundle.get("model_versions", []):
        if version.get("id") == deployed_id:
            return version.get("snapshot_json") or {}
    raise AssertionError(f"exported deployed version {deployed_id} not found")


def _iter_predictive_version_refs(value: object, path: str = "$"):
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key == "predictive_built_for_version_id":
                yield child_path, child
            else:
                yield from _iter_predictive_version_refs(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _iter_predictive_version_refs(child, f"{path}[{index}]")


def test_modely_seed_contains_every_supported_time_variant(modely_bundle):
    variants = _variant_measures(modely_bundle)
    assert set(variants) == set(TIME_VARIANT_NAMES)


def test_modely_seed_bundle_has_no_compounded_calendar_aliases(modely_bundle):
    _assert_no_compounded_hierarchy_names(modely_bundle)


def test_modely_seed_active_and_deployed_shapes_have_no_compounded_calendar_aliases(modely_bundle):
    active_shape = {
        "tables": modely_bundle.get("tables", []),
        "hierarchies": modely_bundle.get("hierarchies", []),
        "columns": modely_bundle.get("columns", []),
        "dimensions": modely_bundle.get("dimensions", []),
        "user_defined_attributes": modely_bundle.get("user_defined_attributes", []),
    }
    _assert_no_compounded_hierarchy_names(active_shape)
    _assert_no_compounded_hierarchy_names(_exported_deployed_snapshot(modely_bundle))


def test_seed_predictive_version_refs_are_retained_or_null(project_bundle):
    dangling_refs = []
    for bundle in project_bundle["models"]:
        retained_version_ids = {
            version["id"]
            for version in bundle.get("model_versions", [])
            if version.get("id")
        }
        slug = bundle["model"]["slug"]
        dangling_refs.extend(
            f"{slug} {path}: {value}"
            for path, value in _iter_predictive_version_refs(bundle)
            if value is not None and value not in retained_version_ids
        )
    assert dangling_refs == []


def test_modely_seed_variants_link_to_base_measure_and_calendar(modely_bundle):
    variants = _variant_measures(modely_bundle)
    base = next(m for m in modely_bundle["measures"] if m["name"] == "base_amount")
    calendar = next(h for h in modely_bundle["hierarchies"] if h["name"] == "business_date Calendar")
    calendar_alias = next(t for t in modely_bundle["tables"] if t["alias"] == "business_date_calendar")
    calendar_table = next(c for c in modely_bundle["calendar_tables"] if c["table_name"] == "demo_data.calendar")

    # The variants' date anchor is the FACT table's business_date column.
    # Derive it from the bundle instead of pinning a seed UUID — the id
    # changes on every bundle regeneration (broke on the 2026-07-08 regen).
    col_by_id = {c["id"]: c for c in modely_bundle["columns"]}
    base_col = col_by_id[base["source_column_id"]]
    fact_business_date = next(
        c for c in modely_bundle["columns"]
        if c["model_table_id"] == base_col["model_table_id"]
        and c["column_name"] == "business_date"
    )

    for measure in variants.values():
        assert measure["variant_of_measure_id"] == base["id"]
        assert measure["source_column_id"] == base["source_column_id"]
        assert measure["hierarchy_id"] == calendar["id"]
        assert measure["calendar_model_table_id"] == calendar_alias["id"]
        assert measure["resolved_calendar_id"] == calendar_table["id"]
        assert measure["resolved_date_col_id"] == fact_business_date["id"]
        assert measure["date_dimension_column_id"] == fact_business_date["id"]


def test_modely_seed_hierarchy_has_time_calc_capabilities(modely_bundle):
    calendar = next(h for h in modely_bundle["hierarchies"] if h["name"] == "business_date Calendar")
    # None normalizes to "standard" / model-default fiscal start throughout
    # the semantic layer (see shared calendar_dialects) — the auto-created
    # replacement hierarchy from the Bug-6683 repair leaves both unset.
    assert calendar["calendar_type"] in (None, "standard")
    assert calendar["fiscal_year_start_month"] in (None, 1)
    units = {level["time_unit"] for level in calendar["levels"]}
    assert {"year", "month", "day"}.issubset(units)
    for level in calendar["levels"]:
        assert {
            "lag",
            "moving_window",
            "parallel_period",
            "period_to_date",
        }.issubset(set(level["allowed_time_calcs"]))


def test_modely_seed_executable_smoke_variant_subset(modely_bundle):
    variants = _variant_measures(modely_bundle)
    for variant in _EXECUTABLE_SMOKE_VARIANTS:
        assert variant in variants
    assert variants["trailing_n"]["variant_n"] == 12
    assert variants["moving_avg_n"]["variant_n"] == 3
    assert variants["yoy_growth_pct"]["is_additive"] is False


@pytest.mark.parametrize("alias, canonical", sorted(TIME_VARIANT_ALIASES.items()))
def test_time_variant_aliases_have_seed_or_metadata_coverage(modely_bundle, alias, canonical):
    variants = _variant_measures(modely_bundle)
    assert alias in variants
    assert canonical in variants
    assert canonical_variant_kind(alias) == canonical
