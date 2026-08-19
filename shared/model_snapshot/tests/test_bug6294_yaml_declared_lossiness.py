"""Bug-6294 (AKA Fable:F-020-05) — YAML lossiness must not be silent, and the
format spec must not contradict the code.

Re-verified against current code 2026-08-03: CONFIRMED still live. A user could
export a model to YAML, edit it, re-import it, and silently lose configuration
with no error and no warning, while the specification document told them the
format was "roundtrippable".

Three properties are pinned here:

1. **Fields that change the NUMBERS or the ACCESS now round-trip.** `additive`
   (an `is_additive` reset to its NOT NULL True default makes a non-summable
   measure summable — Bug-8257's exact vector), `hidden` (a hidden field
   becoming visible is an exposure), `calc_mode` (`expression_as_written` vs
   `per_row_then_aggregate` compute different values, and the field is REQUIRED
   by the create API), and the time-dimension flag (`type: date` alone could not
   distinguish a time dimension from a plain dimension on a date column).

2. **What the format still cannot carry is DECLARED** in the exported file,
   listing only what that model actually has.

3. **A persona `filters` shape the row-security gate cannot read is REFUSED**
   rather than imported as a persona whose filters silently never apply. The
   spec documented (and its example showed) a list; the column is a mapping.

Test escape: the roundtrip suite asserted only that the fields it DID carry came
back; nothing asserted anything about the fields it dropped, so every one of
these was invisible. Guard: this file. Tier: T1 producer/consumer contract.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from shared.model_snapshot.yaml_deserialiser import YamlImportError, parse_model_yaml
from shared.model_snapshot.yaml_serialiser import snapshot_to_yaml

_SPEC = (
    Path(__file__).resolve().parents[3].parent
    / "docs" / "architecture" / "architecture_yaml-model-format.md"
)


def _snapshot(**over):
    snap = {
        "model": {"slug": "sales", "display_name": "Sales", "refresh": "manual"},
        "tables": [
            {"id": "t1", "alias": "orders", "physical_name": "public.orders",
             "table_type": "fact"},
        ],
        "columns": [
            {"id": "c_amount", "model_table_id": "t1", "column_name": "amount",
             "data_type": "numeric"},
            {"id": "c_secret", "model_table_id": "t1", "column_name": "cost",
             "data_type": "numeric", "is_hidden": True},
            {"id": "c_date", "model_table_id": "t1", "column_name": "order_date",
             "data_type": "date"},
        ],
        "measures": [
            {"id": "m1", "name": "total_amount", "source_column_id": "c_amount",
             "measure_type": "standard", "default_agg": "sum",
             "is_additive": True},
            {"id": "m2", "name": "avg_amount", "source_column_id": "c_amount",
             "measure_type": "standard", "default_agg": "avg",
             "is_additive": False},
            {"id": "m3", "name": "internal_cost", "source_column_id": "c_secret",
             "measure_type": "standard", "default_agg": "sum",
             "is_additive": True},
            {"id": "m4", "name": "margin_pct", "measure_type": "calculated",
             "expression": 'safe_div(measure("total_amount"), 1)',
             "calc_agg_mode": "per_row_then_aggregate",
             "default_agg": "sum", "is_additive": False},
        ],
        "dimensions": [
            {"id": "d1", "name": "signup_date", "source_column_id": "c_date",
             "is_time_dim": False},
        ],
        "joins": [],
        "hierarchies": [],
        "personas": [],
    }
    snap.update(over)
    return snap


def _export(**over) -> dict:
    return yaml.safe_load(snapshot_to_yaml(_snapshot(**over)))


def _measure(doc, name) -> dict:
    return next(m for m in doc["measures"] if m["name"] == name)


class TestNumbersAndAccessRoundTrip:
    def test_non_additive_measure_exports_and_imports_non_additive(self) -> None:
        """The wrong-numbers vector: without this the flag hit the NOT NULL True
        column default and a column of averages became summable."""
        doc = _export()
        assert _measure(doc, "avg_amount")["additive"] is False

        snap = parse_model_yaml(yaml.safe_dump(doc))
        row = next(m for m in snap["measures"] if m["name"] == "avg_amount")
        assert row["is_additive"] is False

    def test_additive_measure_stays_clean_in_the_file(self) -> None:
        """Only the non-default is written, so a simple model reads simply."""
        assert "additive" not in _measure(_export(), "total_amount")

    def test_an_absent_additive_flag_is_derived_not_assumed(self) -> None:
        """A hand-written or pre-fix file must not assert additive for a measure
        whose shape proves otherwise; the importer leaves it to the rehydrator's
        shared derivation instead of writing a default here."""
        doc = _export()
        m = _measure(doc, "avg_amount")
        del m["additive"]
        snap = parse_model_yaml(yaml.safe_dump(doc))
        row = next(m for m in snap["measures"] if m["name"] == "avg_amount")
        assert "is_additive" not in row

    def test_hidden_measure_round_trips_via_its_backing_column(self) -> None:
        """``hidden`` is a ModelColumn property the API cascades onto the
        measure — writing it onto the Measure row would fail the insert."""
        doc = _export()
        assert _measure(doc, "internal_cost")["hidden"] is True
        assert "hidden" not in _measure(doc, "total_amount")

        snap = parse_model_yaml(yaml.safe_dump(doc))
        cost_col = next(
            c for c in snap["columns"] if c["column_name"] == "cost"
        )
        assert cost_col["is_hidden"] is True
        assert not any(
            "is_hidden" in m for m in snap["measures"]
        ), "is_hidden must not be written onto the Measure row"

    def test_calc_mode_round_trips(self) -> None:
        """expression_as_written and per_row_then_aggregate are different
        numbers, and the field is required by the create API."""
        doc = _export()
        assert _measure(doc, "margin_pct")["calc_mode"] == "per_row_then_aggregate"

        snap = parse_model_yaml(yaml.safe_dump(doc))
        row = next(m for m in snap["measures"] if m["name"] == "margin_pct")
        assert row["calc_agg_mode"] == "per_row_then_aggregate"

    def test_non_time_dimension_on_a_date_column_stays_non_time(self) -> None:
        """``type: date`` was doing double duty as the column type AND the
        is_time_dim flag, so this dimension came back as a TIME dimension —
        a real semantic change (time dimensions drive calendar/variant
        resolution)."""
        doc = _export()
        dim = doc["dimensions"][0]
        assert dim["type"] == "date"
        assert dim["time"] is False

        snap = parse_model_yaml(yaml.safe_dump(doc))
        assert snap["dimensions"][0]["is_time_dim"] is False

    def test_a_real_time_dimension_still_imports_as_one(self) -> None:
        snap_in = _snapshot()
        snap_in["dimensions"][0]["is_time_dim"] = True
        doc = yaml.safe_load(snapshot_to_yaml(snap_in))
        assert doc["dimensions"][0]["type"] == "date"
        assert "time" not in doc["dimensions"][0]

        snap = parse_model_yaml(yaml.safe_dump(doc))
        assert snap["dimensions"][0]["is_time_dim"] is True


class TestDeclaredLossiness:
    def test_a_plain_model_declares_nothing(self) -> None:
        assert "not_exported" not in _export()

    @pytest.mark.parametrize(
        "section,needle",
        [
            ("row_security_rules", "row-level security"),
            ("kpis", "KPI definitions"),
            ("aggregates", "aggregate table definitions"),
            ("pockets", "pocket table definitions"),
            ("calendar_tables", "custom calendar tables"),
            ("data_tags", "data classification tags"),
            ("named_sets", "named sets"),
            # F-020-02: Named Queries are in the JSON snapshot but the YAML
            # format drops them; the loss MUST be declared in not_exported.
            ("named_queries", "named queries"),
        ],
    )
    def test_each_unrepresentable_section_is_declared_when_present(
        self, section, needle
    ) -> None:
        doc = _export(**{section: [{"id": "x"}]})
        blob = "\n".join(doc["not_exported"]).lower()
        assert needle.lower() in blob

    def test_the_declaration_is_data_driven_not_a_fixed_banner(self) -> None:
        """An empty section says nothing — otherwise every export would carry an
        alarming list of things it does not actually have."""
        assert "not_exported" not in _export(kpis=[], aggregates=[])

    def test_row_security_loss_is_stated_in_consequence_terms(self) -> None:
        """The reader needs to know what happens, not just which table is
        missing."""
        doc = _export(row_security_rules=[{"id": "r1"}])
        assert any("NO row filtering" in line for line in doc["not_exported"])

    def test_dropped_invalid_definitions_are_named(self) -> None:
        """An invalid measure is omitted ENTIRELY, the strongest loss of all,
        and it also breaks any variant whose base it was."""
        snap = _snapshot()
        snap["measures"].append({
            "id": "m9", "name": "broken_measure", "measure_type": "standard",
            "default_agg": "sum", "is_invalid": True,
        })
        doc = yaml.safe_load(snapshot_to_yaml(snap))
        assert not any(m["name"] == "broken_measure" for m in doc["measures"])
        assert any("broken_measure" in line for line in doc["not_exported"])

    def test_measure_level_bindings_are_declared(self) -> None:
        snap = _snapshot()
        snap["measures"][0]["semi_additive_account_column_id"] = "c_amount"
        doc = yaml.safe_load(snapshot_to_yaml(snap))
        assert any("account column" in line for line in doc["not_exported"])


class TestPersonaFilterShape:
    def _doc_with_filters(self, filters, audience_roles=None):
        persona = {"name": "regional", "filters": filters}
        if audience_roles is not None:
            persona["audience_roles"] = audience_roles
        return {
            "model": {"name": "sales", "display_name": "Sales"},
            "tables": [{"name": "orders", "source_table": "public.orders"}],
            "personas": [persona],
        }

    def test_mapping_filters_import(self) -> None:
        snap = parse_model_yaml(
            yaml.safe_dump(self._doc_with_filters(
                {"country": "{{user.region}}"},
                audience_roles=["analyst"],
            ))
        )
        assert snap["personas"][0]["default_filters"] == {
            "country": "{{user.region}}"
        }
        assert snap["personas"][0]["audience_roles"] == ["analyst"]

    def test_list_filters_are_refused_not_silently_stored(self) -> None:
        """The spec's own example used a list. Stored verbatim into the NOT NULL
        JSONB dict the persona gate reads by key, the persona's row filters would
        simply never apply — a security-relevant silent no-op."""
        doc = self._doc_with_filters(
            [{"dimension": "country", "operator": "equals", "value": "DE"}]
        )
        with pytest.raises(YamlImportError) as exc:
            parse_model_yaml(yaml.safe_dump(doc))
        assert "must be a mapping" in str(exc.value)

    def test_absent_filters_still_import(self) -> None:
        snap = parse_model_yaml(
            yaml.safe_dump(self._doc_with_filters(None))
        )
        assert snap["personas"][0]["default_filters"] == {}


class TestSpecMatchesTheCode:
    """The format spec is what an operator reads before hand-editing a file.
    A spec that contradicts the code is actively misleading about what survives
    a roundtrip, which is half of what Bug-6294 reported."""

    def _spec(self) -> str:
        return _SPEC.read_text(encoding="utf-8")

    def test_the_false_roundtrip_claim_is_gone(self) -> None:
        text = self._spec()
        assert "**Roundtrippable.**" not in text
        assert "Never describe this format as roundtrippable" in text

    def test_the_spec_has_a_what_is_not_exported_section(self) -> None:
        assert "## What is NOT exported" in self._spec()

    @pytest.mark.parametrize(
        "field", ["calc_mode", "additive", "hidden", "semi_additive",
                  "variant_of", "variant_n", "time", "primary_key"]
    )
    def test_every_field_the_code_emits_is_documented(self, field) -> None:
        """The spec used to document ``hidden`` the code never wrote, and omit
        ``semi_additive`` / ``variant`` the code did write."""
        assert f"| {field} |" in self._spec(), field

    def test_the_spec_no_longer_documents_a_field_neither_side_implements(
        self,
    ) -> None:
        """``dimension:`` on a hierarchy was documented but never emitted and
        never read."""
        text = self._spec()
        assert (
            "| dimension | conditional | string | Which dimension this "
            "hierarchy is for"
        ) not in text

    def test_the_aggregation_enum_matches_the_canonical_producer_domain(
        self,
    ) -> None:
        """The spec offered ``average``; the code emits and the API accepts
        ``avg``. A hand-written ``average`` used to be a create-time 422."""
        from shared.schemas.domains.dimensions_measures import VALID_DEFAULT_AGGS

        text = self._spec()
        assert "CANONICAL tokens only" in text
        for token in ("sum", "avg", "count", "count_distinct", "min", "max"):
            assert token in VALID_DEFAULT_AGGS
            assert f"`{token}`" in text

    def test_the_persona_filters_shape_is_documented_as_a_mapping(self) -> None:
        text = self._spec()
        assert "| filters | no | mapping |" in text
        assert "operator: equals" not in text


def test_every_unexported_snapshot_section_is_declared() -> None:
    """Bug-6294's honesty contract holds only if the hand-maintained
    ``_NOT_EXPORTED`` list stays complete — itself a coverage tool that can go
    blind. ``user_defined_attributes`` was missing on day one (deep-review
    finding 4). Promoted from the review that found it."""
    import ast
    import pathlib

    from shared.model_snapshot import yaml_serialiser as ser

    root = pathlib.Path(ser.__file__).resolve().parent
    src = (root / "serialiser.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    # ``snap["key"] = ...``
    produced = {
        node.slice.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Subscript)
        and getattr(node.value, "id", None) == "snap"
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    }
    # ``snap.get("key")`` — deep-review R3 finding 7: a section added only in
    # the .get form would have escaped this completeness guard silently. No
    # such gap exists today; this keeps it that way.
    produced |= {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and getattr(node.func.value, "id", None) == "snap"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }
    exported = {"model", "tables", "joins", "measures", "dimensions",
                "hierarchies", "personas", "columns"}
    # Runtime/physical state, deliberately out of a model-DEFINITION format.
    runtime_state = {
        "data_sources", "data_targets", "model_versions", "model_alias_map",
        "source_statistics", "source_join_statistics",
        "aggregate_lifecycle_events", "exported_deployed_version_id",
        "uda_column_refs",
    }
    # Deliberately NOT in runtime_state: row_security_rules is model definition,
    # its loss is the single most consequential thing this format drops, and
    # listing it in both sets would have let it be removed from the disclosure
    # without the guard noticing (deep-review R3 finding 7).
    declared = {key for key, _ in ser._NOT_EXPORTED}
    undeclared = produced - exported - runtime_state - declared
    assert undeclared == set(), (
        "these snapshot sections are neither exported nor declared in the "
        f"not_exported block: {sorted(undeclared)}"
    )


def test_a_model_with_user_defined_attributes_declares_the_loss() -> None:
    snap = {
        "model": {"name": "m", "slug": "m"},
        "tables": [{"id": "t1", "alias": "f", "physical_name": "f"}],
        "columns": [{"id": "c1", "model_table_id": "t1",
                     "column_name": "a", "data_type": "numeric"}],
        "measures": [{"id": "m1", "name": "A", "source_column_id": "c1",
                      "default_agg": "sum", "measure_type": "standard"}],
        "dimensions": [],
        "user_defined_attributes": [{"id": "u1", "name": "Band"}],
    }
    out = snapshot_to_yaml(snap, project_name="p", connection_name="c")
    assert "not_exported" in out
    assert "user-defined attribute" in out.lower(), out
