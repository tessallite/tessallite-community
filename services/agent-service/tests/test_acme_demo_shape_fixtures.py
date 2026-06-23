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


@pytest.fixture(scope="module")
def modely_bundle() -> dict:
    data = json.loads(_SEED_PATH.read_text())
    for bundle in data["models"]:
        if bundle["model"]["slug"] == "modely":
            return bundle
    raise AssertionError("modely seed bundle not found")


def _variant_measures(bundle: dict) -> dict[str, dict]:
    return {
        measure["variant_kind"]: measure
        for measure in bundle["measures"]
        if measure.get("variant_kind")
    }


def test_modely_seed_contains_every_supported_time_variant(modely_bundle):
    variants = _variant_measures(modely_bundle)
    assert set(variants) == set(TIME_VARIANT_NAMES)


def test_modely_seed_variants_link_to_base_measure_and_calendar(modely_bundle):
    variants = _variant_measures(modely_bundle)
    base = next(m for m in modely_bundle["measures"] if m["name"] == "base_amount")
    calendar = next(h for h in modely_bundle["hierarchies"] if h["name"] == "Calendar")
    calendar_alias = next(t for t in modely_bundle["tables"] if t["alias"] == "business_date_calendar")
    calendar_table = next(c for c in modely_bundle["calendar_tables"] if c["table_name"] == "demo_data.calendar")

    for measure in variants.values():
        assert measure["variant_of_measure_id"] == base["id"]
        assert measure["source_column_id"] == base["source_column_id"]
        assert measure["hierarchy_id"] == calendar["id"]
        assert measure["calendar_model_table_id"] == calendar_alias["id"]
        assert measure["resolved_calendar_id"] == calendar_table["id"]
        assert measure["resolved_date_col_id"] == "2b2e54a1-2ae8-48c3-afcf-091c1a34f579"
        assert measure["date_dimension_column_id"] == "2b2e54a1-2ae8-48c3-afcf-091c1a34f579"


def test_modely_seed_hierarchy_has_time_calc_capabilities(modely_bundle):
    calendar = next(h for h in modely_bundle["hierarchies"] if h["name"] == "Calendar")
    assert calendar["calendar_type"] == "standard"
    assert calendar["fiscal_year_start_month"] == 1
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
