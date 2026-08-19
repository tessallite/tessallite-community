"""Lane B — agent grounding & prompt-construction guards.

Covers:
  R3  (Bug-7932) — per-attribute semantics lines rendered into AVAILABLE MODELS.
  RG  (Bug-7931) — attention-budget glossary policy (full glossary in the stable
                   prefix vs two-tier term index + score>0 retrieval in the
                   per-turn suffix; no zero-score padding; slug not UUID).
  Bug-7935       — persona filter must not mutate shared profile objects.
  Bug-6556       — selectable-models consumes the public assembler facade.

Test escape / guard notes are recorded per test class.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.prompt import assembler as A
from src.prompt.assembler import (
    _GlossaryGrounding,
    _ModelProfile,
    _PersonaScope,
    _apply_persona_filter,
    _dimension_semantics_line,
    _estimate_tokens,
    _format_glossary_layer,
    _format_grounding_matches,
    _format_model_layer,
    _measure_semantics_line,
    _resolve_glossary_grounding,
    _sorted_model_profiles,
    _truncate_description,
)
from src.planning.measure_metadata import MeasureRoleMetadata
from src.retrieval.glossary import GlossaryCard, GlossaryTermIndexEntry


def _profile(
    *,
    measure_names=("revenue",),
    dimension_names=("region",),
    measure_metadata=None,
    measure_descriptions=None,
    dimension_descriptions=None,
    dimensions=None,
    tagged_fields=None,
) -> _ModelProfile:
    return _ModelProfile(
        id=uuid.uuid4(),
        slug="test",
        display_name="Test Model",
        overview=None,
        analytical_capabilities=None,
        abbreviation_conflict_rules=None,
        example_questions=[],
        measure_names=list(measure_names),
        dimension_names=list(dimension_names),
        filterable_where_names=sorted(set(dimension_names) | set(measure_names)),
        sortable_names=sorted(set(dimension_names) | set(measure_names)),
        aggregates_summary=[],
        calendar_aliases=[],
        dimension_aliases=[],
        tagged_fields=tagged_fields or {},
        dimension_value_hints={},
        measure_metadata=measure_metadata or {},
        dimensions=dimensions or {},
        measure_descriptions=measure_descriptions or {},
        dimension_descriptions=dimension_descriptions or {},
    )


# ---------------------------------------------------------------------------
# R3 (Bug-7932) — per-attribute semantics rendering
# ---------------------------------------------------------------------------
# Test escape: measure_metadata (variant_kind, is_additive, default_agg) was
# loaded but _format_model_layer never rendered it, so the SHAPE CONTRACT
# instruction to honour variant/additivity referenced data the planner could
# not see. Guard: assert the semantics catalogue renders agg/type/additivity/
# variant/description for each surviving attribute. Tier: T1 (contract).


class TestAttributeSemanticsRendering:
    def test_measure_semantics_line_renders_agg_type_additivity(self):
        md = MeasureRoleMetadata(
            name="transaction_amount",
            default_agg="sum",
            data_type="decimal",
            is_additive=True,
        )
        line = _measure_semantics_line("transaction_amount", md, "gross settled amount")
        assert "transaction_amount" in line
        assert "sum" in line
        assert "decimal" in line
        assert "additive" in line
        assert "gross settled amount" in line

    def test_measure_semantics_line_marks_avg_non_additive(self):
        md = MeasureRoleMetadata(name="avg_ticket", default_agg="avg", data_type="decimal")
        line = _measure_semantics_line("avg_ticket", md, None)
        assert "avg" in line
        assert "non-additive" in line

    def test_measure_semantics_line_explicit_non_additive_wins(self):
        md = MeasureRoleMetadata(
            name="m", default_agg="sum", data_type="decimal", is_additive=False
        )
        line = _measure_semantics_line("m", md, None)
        assert "non-additive" in line

    def test_measure_semantics_line_renders_variant(self):
        md = MeasureRoleMetadata(
            name="revenue_py",
            default_agg="sum",
            variant_kind="prior_year",
            variant_of_measure="revenue",
            is_additive=False,
        )
        line = _measure_semantics_line("revenue_py", md, None)
        assert "variant:" in line
        assert "revenue" in line

    def test_variant_lineage_from_mapping_renders_name_not_uuid(self):
        # Review R1 finding 1 — the production path (from_mapping with the
        # assembler-shaped dict) must render the resolved base measure NAME,
        # never the UUID it also carries.
        base_id = str(uuid.uuid4())
        md = MeasureRoleMetadata.from_mapping(
            "revenue_py",
            {
                "default_agg": "sum",
                "data_type": "decimal",
                "variant_kind": "prior_year",
                "variant_of_measure": "revenue",
                "variant_of_measure_id": base_id,
            },
        )
        line = _measure_semantics_line("revenue_py", md, None)
        assert "of revenue" in line
        assert base_id not in line

    def test_variant_lineage_unresolved_uuid_never_rendered(self):
        # If the base-measure name did not resolve (invalid/deleted base),
        # from_mapping falls back to the raw UUID — the render must omit the
        # lineage suffix rather than print a UUID into the prompt.
        base_id = str(uuid.uuid4())
        md = MeasureRoleMetadata.from_mapping(
            "revenue_py",
            {
                "default_agg": "sum",
                "variant_kind": "prior_year",
                "variant_of_measure": None,
                "variant_of_measure_id": base_id,
            },
        )
        line = _measure_semantics_line("revenue_py", md, None)
        assert base_id not in line
        assert "variant:" in line  # the variant kind fact itself stays

    def test_time_variant_annotated_non_additive_without_explicit_flag(self):
        # Review R1 finding 3 — a time-variant measure with is_additive unset
        # must render non-additive (mirrors MeasureRoleMetadata.additivity_notes:
        # variants are treated non-additive unless proven additive), so the
        # planner never sees a bare "sum" it could wrongly stack across periods.
        md = MeasureRoleMetadata.from_mapping(
            "revenue_py",
            {"default_agg": "sum", "data_type": "decimal",
             "variant_kind": "prior_year", "variant_of_measure": "revenue"},
        )
        assert md.is_additive is None
        line = _measure_semantics_line("revenue_py", md, None)
        assert "non-additive" in line

    def test_calculated_measure_annotated_non_additive_without_explicit_flag(self):
        md = MeasureRoleMetadata.from_mapping(
            "margin_pct",
            {"measure_type": "calculated", "default_agg": "sum",
             "expression": "a / b"},
        )
        line = _measure_semantics_line("margin_pct", md, None)
        assert "non-additive" in line

    def test_variant_with_true_flag_still_renders_non_additive_across_periods(self):
        # Review R3 finding 1 — PRODUCTION SHAPE: is_additive is ORM-defaulted
        # True and the variant create path persists it verbatim, so a True
        # flag on a variant is indistinguishable from an untouched default.
        # Spec 3.3 renders variants "non-additive across periods"; stacking a
        # PY/trailing sum across periods double-counts even though the base
        # agg is sum. The flag must NOT override the variant's nature.
        md = MeasureRoleMetadata.from_mapping(
            "revenue_py",
            {"default_agg": "sum", "data_type": "decimal",
             "variant_kind": "prior_year", "variant_of_measure": "revenue",
             "is_additive": True},
        )
        assert md.is_additive is True  # the production state
        line = _measure_semantics_line("revenue_py", md, None)
        assert "non-additive across periods" in line
        assert ", additive" not in line

    def test_calculated_with_true_flag_renders_non_additive(self):
        # Same production shape for calculated expressions (ratios do not
        # re-aggregate); the defaulted True flag must not render "additive".
        md = MeasureRoleMetadata.from_mapping(
            "margin_pct",
            {"measure_type": "calculated", "default_agg": "sum",
             "expression": "a / b", "is_additive": True},
        )
        line = _measure_semantics_line("margin_pct", md, None)
        assert "non-additive" in line
        assert ", additive" not in line

    def test_avg_with_defaulted_true_flag_renders_non_additive(self):
        # Review R2 finding 1 — PRODUCTION SHAPE: Measure.is_additive is a
        # non-null ORM column defaulted True and no create path coerces it, so
        # an untouched avg measure arrives as (avg, is_additive=True). The
        # render must let mathematical impossibility win: an average is never
        # additive across partitions, and labelling it "additive" instructs
        # the planner into the exact stack/re-aggregate error R3 closes.
        md = MeasureRoleMetadata.from_mapping(
            "avg_base_amount",
            {"default_agg": "avg", "data_type": "decimal", "is_additive": True},
        )
        assert md.is_additive is True  # the production state
        line = _measure_semantics_line("avg_base_amount", md, None)
        assert "(avg, decimal, non-additive)" in line
        assert ", additive" not in line

    def test_max_with_defaulted_true_flag_renders_non_additive(self):
        md = MeasureRoleMetadata.from_mapping(
            "max_transaction",
            {"default_agg": "max", "data_type": "decimal", "is_additive": True},
        )
        line = _measure_semantics_line("max_transaction", md, None)
        assert "(max, decimal, non-additive)" in line

    def test_sum_with_true_flag_still_renders_additive(self):
        md = MeasureRoleMetadata.from_mapping(
            "revenue",
            {"default_agg": "sum", "data_type": "decimal", "is_additive": True},
        )
        line = _measure_semantics_line("revenue", md, None)
        assert "(sum, decimal, additive)" in line

    def test_sum_with_explicit_false_flag_renders_non_additive(self):
        # A modeller may deliberately mark a sum measure non-additive (e.g.
        # mixed currencies); the explicit False must be honoured.
        md = MeasureRoleMetadata.from_mapping(
            "mixed_ccy_amount",
            {"default_agg": "sum", "data_type": "decimal", "is_additive": False},
        )
        line = _measure_semantics_line("mixed_ccy_amount", md, None)
        assert "non-additive" in line

    def test_additivity_classification_covers_producer_domain(self):
        # Review R1 finding 7 — contract test against the shared producer
        # domain: every valid default_agg must have a deliberate additivity
        # classification (additive or non-additive), so a newly added agg can
        # never silently escape annotation.
        from shared.schemas.domains.dimensions_measures import VALID_DEFAULT_AGGS
        from src.prompt.assembler import _ADDITIVE_AGGS, _NON_ADDITIVE_AGGS

        assert _ADDITIVE_AGGS | _NON_ADDITIVE_AGGS == frozenset(VALID_DEFAULT_AGGS)
        assert _ADDITIVE_AGGS & _NON_ADDITIVE_AGGS == frozenset()
        # Pin the deliberate choice: only sum and count re-aggregate safely.
        assert _ADDITIVE_AGGS == frozenset({"sum", "count"})

    def test_measure_semantics_line_no_metadata_still_names(self):
        line = _measure_semantics_line("bare", None, None)
        assert line.strip().startswith("- bare")

    def test_dimension_semantics_line_renders_date_grain(self):
        meta = {"is_time_dim": True, "time_grain": "day"}
        line = _dimension_semantics_line("business_date", meta, "transaction date")
        assert "business_date" in line
        assert "day grain" in line
        assert "transaction date" in line

    def test_dimension_semantics_line_non_time_just_name_and_desc(self):
        line = _dimension_semantics_line("region", {"is_time_dim": False}, "sales region")
        assert "region" in line
        assert "sales region" in line
        # No temporal fact for a non-time dim.
        assert "grain" not in line

    def test_truncate_description_caps_at_120(self):
        long = "x" * 300
        out = _truncate_description(long)
        assert out is not None
        assert len(out) <= 120
        assert out.endswith("…")

    def test_truncate_description_boundary_exact(self):
        # Review R1 finding 10 — pin the boundary: exactly 120 chars passes
        # through untouched; 121 chars truncates to exactly 120 incl. ellipsis.
        exact = "x" * 120
        assert _truncate_description(exact) == exact
        over = "x" * 121
        out = _truncate_description(over)
        assert out is not None
        assert len(out) == 120
        assert out.endswith("…")

    def test_truncate_description_collapses_whitespace(self):
        out = _truncate_description("a\n  b   c")
        assert out == "a b c"

    def test_truncate_description_none_and_empty(self):
        assert _truncate_description(None) is None
        assert _truncate_description("   ") is None

    def test_model_layer_renders_semantics_catalogue(self):
        p = _profile(
            measure_names=["transaction_amount", "avg_ticket"],
            dimension_names=["business_date", "region"],
            measure_metadata={
                "transaction_amount": MeasureRoleMetadata(
                    name="transaction_amount", default_agg="sum",
                    data_type="decimal", is_additive=True,
                ),
                "avg_ticket": MeasureRoleMetadata(
                    name="avg_ticket", default_agg="avg", data_type="decimal",
                ),
            },
            measure_descriptions={"transaction_amount": "gross settled amount"},
            dimensions={
                "business_date": {"is_time_dim": True, "time_grain": "day"},
                "region": {"is_time_dim": False},
            },
            dimension_descriptions={"region": "sales region"},
        )
        text = _format_model_layer([p])
        assert "Measure semantics" in text
        assert "Dimension semantics" in text
        assert "gross settled amount" in text
        assert "avg, decimal, non-additive" in text
        assert "sales region" in text
        assert "day grain" in text

    def test_semantics_suppresses_restricted_field_descriptions(self):
        # A tagged (restricted) field must not get its description disclosed in
        # the semantics catalogue — the bare list already withholds detail.
        p = _profile(
            measure_names=["amount"],
            dimension_names=["account_id", "region"],
            dimensions={"account_id": {}, "region": {}},
            dimension_descriptions={
                "account_id": "customer bank account number",
                "region": "sales region",
            },
            tagged_fields={"pii": ["account_id"]},
        )
        text = _format_model_layer([p])
        assert "customer bank account number" not in text
        assert "sales region" in text

    def test_semantics_only_for_surviving_persona_names(self):
        # After persona narrowing, only surviving names appear in the catalogue.
        p = _profile(
            measure_names=["revenue"],  # persona-narrowed: "secret" removed
            dimension_names=["region"],
            measure_metadata={
                "revenue": MeasureRoleMetadata(name="revenue", default_agg="sum"),
                "secret": MeasureRoleMetadata(name="secret", default_agg="sum"),
            },
            measure_descriptions={"secret": "should not appear"},
        )
        text = _format_model_layer([p])
        assert "should not appear" not in text
        assert "secret" not in text.split("Cross-model")[0]


# ---------------------------------------------------------------------------
# Bug-7935 — persona filter must not mutate shared profile objects
# ---------------------------------------------------------------------------
# Test escape: _apply_persona_filter narrowed measure_names/dimensions in place
# on the shared _ModelProfile, so a second concurrent request could see the
# first request's persona scope. Guard: the same source object passed through
# two different scopes yields independent results and is itself unchanged.
# Tier: T1 (safety-adjacent: field-scope leak across requests).


class TestPersonaFilterNoMutation:
    def test_source_profile_unchanged_after_filter(self):
        p = _profile(
            measure_names=["revenue", "cost", "margin"],
            dimension_names=["region", "product"],
            dimensions={"region": {"is_time_dim": False}, "product": {}},
        )
        original_measures = list(p.measure_names)
        original_dims = list(p.dimension_names)
        original_dim_meta = dict(p.dimensions)

        scope = {
            p.id: _PersonaScope(
                model_id=p.id,
                measure_names={"revenue"},
                dimension_names={"region"},
            )
        }
        out = _apply_persona_filter([p], scope)

        # Source object untouched.
        assert p.measure_names == original_measures
        assert p.dimension_names == original_dims
        assert p.dimensions == original_dim_meta
        # Result is narrowed.
        assert out[0].measure_names == ["revenue"]
        assert out[0].dimension_names == ["region"]
        assert set(out[0].dimensions) == {"region"}
        # And it is a different object.
        assert out[0] is not p

    def test_two_scopes_do_not_leak_across_shared_profile(self):
        p = _profile(
            measure_names=["a", "b", "c"],
            dimension_names=["x", "y"],
            dimensions={"x": {}, "y": {}},
        )
        scope_a = {
            p.id: _PersonaScope(model_id=p.id, measure_names={"a"}, dimension_names={"x"})
        }
        scope_b = {
            p.id: _PersonaScope(model_id=p.id, measure_names={"b"}, dimension_names={"y"})
        }
        out_a = _apply_persona_filter([p], scope_a)
        out_b = _apply_persona_filter([p], scope_b)
        assert out_a[0].measure_names == ["a"]
        assert out_b[0].measure_names == ["b"]
        assert out_a[0].dimension_names == ["x"]
        assert out_b[0].dimension_names == ["y"]

    def test_filter_preserves_context_available_and_descriptions(self):
        # Lane F's context_available and Lane B's R3 description maps must
        # survive the copy verbatim.
        p = _profile(
            measure_names=["revenue"],
            dimension_names=["region"],
            measure_descriptions={"revenue": "gross"},
        )
        p.context_available = False
        scope = {
            p.id: _PersonaScope(
                model_id=p.id, measure_names={"revenue"}, dimension_names={"region"}
            )
        }
        out = _apply_persona_filter([p], scope)
        assert out[0].context_available is False
        assert out[0].measure_descriptions == {"revenue": "gross"}

    def test_empty_scope_widens_to_full_lists_without_mutation(self):
        # An empty include set means full model access; the copy keeps all names.
        p = _profile(measure_names=["a", "b"], dimension_names=["x"])
        scope = {
            p.id: _PersonaScope(model_id=p.id, measure_names=set(), dimension_names=set())
        }
        out = _apply_persona_filter([p], scope)
        assert out[0].measure_names == ["a", "b"]
        assert out[0].dimension_names == ["x"]
        assert p.measure_names == ["a", "b"]

    def test_out_of_scope_model_dropped(self):
        p = _profile()
        out = _apply_persona_filter([p], {})  # no scope entry for p.id
        assert out == []

    def test_persona_excluded_cross_model_measure_name_does_not_leak(self):
        # Integration fix — persona-scope invariant for the cross-model
        # disclosure line. Cross-model reference measures live ONLY in
        # measure_metadata (never in the executable measure_names), so before
        # this fix _apply_persona_filter left measure_metadata unfiltered and the
        # "Cross-model reference measures are not directly queryable: ..." line
        # in _format_model_layer leaked a persona-excluded cross-model measure
        # NAME. Guard: after persona narrowing, a cross-model measure outside the
        # persona measure scope is absent from measure_metadata AND from the
        # rendered model layer; an in-scope cross-model measure still shows.
        # Test escape: _apply_persona_filter narrowed measure_names but preserved
        # measure_metadata verbatim. Guard: this test. Tier: T1 (persona scope /
        # names-only disclosure invariant).
        p = _profile(
            measure_names=["revenue"],  # executable measures (cross-model excl.)
            dimension_names=["region"],
            measure_metadata={
                "revenue": MeasureRoleMetadata(name="revenue", default_agg="sum"),
                "cm_allowed": MeasureRoleMetadata(
                    name="cm_allowed", source_kind="cross_model"
                ),
                "cm_secret": MeasureRoleMetadata(
                    name="cm_secret", source_kind="cross_model"
                ),
            },
        )
        scope = {
            p.id: _PersonaScope(
                model_id=p.id,
                # Persona may reference revenue + cm_allowed, NOT cm_secret.
                measure_names={"revenue", "cm_allowed"},
                dimension_names={"region"},
            )
        }
        out = _apply_persona_filter([p], scope)
        assert "cm_secret" not in out[0].measure_metadata
        assert "cm_allowed" in out[0].measure_metadata
        # Source profile untouched (copy-on-write).
        assert "cm_secret" in p.measure_metadata

        text = _format_model_layer(out)
        assert "cm_secret" not in text
        assert "cm_allowed" in text
        assert "Cross-model reference measures are not directly queryable" in text

    def test_empty_persona_scope_keeps_all_cross_model_metadata(self):
        # An empty measure include set means full model access — measure_metadata
        # (including cross-model names) must be preserved intact.
        p = _profile(
            measure_names=["revenue"],
            dimension_names=["region"],
            measure_metadata={
                "revenue": MeasureRoleMetadata(name="revenue", default_agg="sum"),
                "cm_x": MeasureRoleMetadata(name="cm_x", source_kind="cross_model"),
            },
        )
        scope = {
            p.id: _PersonaScope(
                model_id=p.id, measure_names=set(), dimension_names=set()
            )
        }
        out = _apply_persona_filter([p], scope)
        assert "cm_x" in out[0].measure_metadata


# ---------------------------------------------------------------------------
# Cache-prefix byte-stability — deterministic AVAILABLE MODELS order
# ---------------------------------------------------------------------------
# Test escape: _load_model_profiles emitted profiles in DB row order, which is
# nondeterministic for an unordered SELECT, so the AVAILABLE MODELS section (and
# the cacheable system prefix) could reorder between calls. Guard: the profile
# sort produces one stable order regardless of input order. Tier: T1 (cache
# byte-stability contract).


class TestModelProfileDeterministicOrder:
    def _named(self, display_name, slug, uid):
        from dataclasses import replace
        return replace(
            _profile(), display_name=display_name, slug=slug,
            id=uuid.UUID(uid),
        )

    def test_sort_is_stable_regardless_of_input_order(self):
        a = self._named("Alpha", "alpha", "00000000-0000-0000-0000-000000000001")
        b = self._named("Beta", "beta", "00000000-0000-0000-0000-000000000002")
        c = self._named("gamma", "gamma", "00000000-0000-0000-0000-000000000003")

        order_one = _sorted_model_profiles([c, a, b])
        order_two = _sorted_model_profiles([b, c, a])
        assert [p.display_name for p in order_one] == ["Alpha", "Beta", "gamma"]
        assert [p.display_name for p in order_one] == [
            p.display_name for p in order_two
        ]

    def test_same_name_breaks_tie_on_slug_then_id(self):
        # Two models share a display_name — order must fall back to slug, then id.
        p1 = self._named("Dup", "z-slug", "00000000-0000-0000-0000-000000000009")
        p2 = self._named("Dup", "a-slug", "00000000-0000-0000-0000-000000000001")
        p3 = self._named("Dup", "a-slug", "00000000-0000-0000-0000-000000000000")
        out = _sorted_model_profiles([p1, p2, p3])
        # a-slug before z-slug; within a-slug, lower id first.
        assert [str(p.id) for p in out] == [
            "00000000-0000-0000-0000-000000000000",
            "00000000-0000-0000-0000-000000000001",
            "00000000-0000-0000-0000-000000000009",
        ]


# ---------------------------------------------------------------------------
# RG (Bug-7931) — glossary card rendering: slug not UUID, no zero-score pad
# ---------------------------------------------------------------------------
# Test escape: cards were prefixed with the volatile UUID (breaking a legible,
# stable cache prefix) and retrieval padded to top-K with zero-score cards.
# Guard: card ref uses the slug; retrieval never emits zero-score cards (see
# TestGlossaryRetrievalThreshold). Tier: T1.


class TestGlossaryCardRendering:
    def test_card_rendered_with_slug_not_uuid(self):
        mid = uuid.uuid4()
        card = GlossaryCard(
            model_id=mid, term="ARR", definition="Annual recurring revenue",
            synonyms=[], model_slug="finance",
        )
        out = _format_glossary_layer([card], [])
        assert "[finance]" in out
        assert str(mid) not in out

    def test_card_falls_back_to_uuid_without_slug(self):
        mid = uuid.uuid4()
        card = GlossaryCard(
            model_id=mid, term="ARR", definition="def", synonyms=[], model_slug=None,
        )
        out = _format_glossary_layer([card], [])
        assert f"[{mid}]" in out

    def test_full_glossary_heading_when_flagged(self):
        card = GlossaryCard(
            model_id=uuid.uuid4(), term="ARR", definition="def",
            synonyms=[], model_slug="s",
        )
        out = _format_glossary_layer([card], [], full_glossary=True)
        assert "every defined business term" in out.lower()

    def test_term_index_renders_terms_without_definitions(self):
        idx = [
            GlossaryTermIndexEntry(
                model_id=uuid.uuid4(), model_slug="fin", term="ARR",
                synonyms=["annual recurring revenue"],
            )
        ]
        out = _format_glossary_layer([], [], term_index=idx)
        assert "TERM INDEX" in out
        assert "ARR" in out
        assert "annual recurring revenue" in out  # synonym present
        # A definition-only word from a card must not appear (index has no defs).
        assert "GLOSSARY CARDS" not in out

    def test_grounding_matches_suffix_renders_cards(self):
        card = GlossaryCard(
            model_id=uuid.uuid4(), term="ARR", definition="Annual recurring revenue",
            synonyms=[], model_slug="fin",
        )
        out = _format_grounding_matches([card])
        # Dedup: the "## GROUNDING MATCHES" heading is added by BOTH consumers
        # (planner user suffix + judge evidence pack), so the block must NOT
        # repeat the "GROUNDING MATCHES" label itself — it keeps only the
        # descriptive guidance ("candidate semantic hints").
        assert "GROUNDING MATCHES" not in out
        assert "candidate semantic hints" in out
        assert "ARR" in out
        assert "[fin]" in out

    def test_grounding_matches_empty_returns_empty_string(self):
        assert _format_grounding_matches([]) == ""

    def test_retrieval_only_mode_discloses_per_question_delivery(self):
        # Review R2 finding 4 — mode 3 must not claim "(no glossary ... )" in
        # the prefix while definitions actually arrive per-question.
        out = _format_glossary_layer([], [], retrieval_only=True)
        assert "(no glossary or alias map content)" not in out
        assert "supplied per-question" in out
        assert "GROUNDING MATCHES" in out

    def test_empty_glossary_without_retrieval_keeps_no_content_line(self):
        out = _format_glossary_layer([], [], retrieval_only=False)
        assert "(no glossary or alias map content)" in out


# ---------------------------------------------------------------------------
# RG — retrieval threshold (score>0, no padding)
# ---------------------------------------------------------------------------


class TestGlossaryRetrievalThreshold:
    @pytest.mark.asyncio
    async def test_zero_score_cards_not_padded(self):
        from src.retrieval.glossary import retrieve_glossary_cards

        mid = uuid.uuid4()
        # One relevant, one irrelevant entry.
        rel = types.SimpleNamespace(
            id=uuid.uuid4(), model_id=mid, term="Revenue",
            definition="total revenue", status="approved",
            visibility="show", confidence="high", sample_values=None,
        )
        irrel = types.SimpleNamespace(
            id=uuid.uuid4(), model_id=mid, term="Latency",
            definition="response time milliseconds", status="approved",
            visibility="show", confidence="high", sample_values=None,
        )
        db = AsyncMock()
        entries_result = MagicMock()
        entries_result.scalars.return_value.all.return_value = [rel, irrel]
        syn_result = MagicMock()
        syn_result.scalars.return_value.all.return_value = []
        slug_result = MagicMock()
        slug_result.all.return_value = [(mid, "sales")]
        db.execute = AsyncMock(side_effect=[entries_result, syn_result, slug_result])

        cards = await retrieve_glossary_cards(db, [mid], "revenue")
        # Only the score>0 card comes back; the zero-overlap "Latency" is dropped.
        assert [c.term for c in cards] == ["Revenue"]


class TestGlossaryDeterministicOrder:
    @pytest.mark.asyncio
    async def test_duplicate_terms_ordered_by_entry_id_regardless_of_scan_order(self):
        # Review R1 finding 4 — terms are not unique per model (case variants /
        # duplicates) and the DB scan order is nondeterministic; the entry-id
        # tiebreaker must make the full-glossary order byte-stable across turns
        # or the cacheable prefix silently breaks.
        import src.retrieval.glossary as G
        from src.retrieval.glossary import list_glossary_cards

        mid = uuid.uuid4()
        id_a = uuid.UUID("00000000-0000-0000-0000-000000000001")
        id_b = uuid.UUID("00000000-0000-0000-0000-000000000002")
        e_a = types.SimpleNamespace(
            id=id_a, model_id=mid, term="Revenue", definition="first",
            sample_values=None,
        )
        e_b = types.SimpleNamespace(
            id=id_b, model_id=mid, term="revenue", definition="second",
            sample_values=None,
        )
        slugs = {mid: "s"}

        with patch.object(
            G, "_load_admissible_entries",
            AsyncMock(return_value=([e_b, e_a], {}, slugs)),
        ):
            order_one = await list_glossary_cards(MagicMock(), [mid])
        with patch.object(
            G, "_load_admissible_entries",
            AsyncMock(return_value=([e_a, e_b], {}, slugs)),
        ):
            order_two = await list_glossary_cards(MagicMock(), [mid])

        assert [c.definition for c in order_one] == ["first", "second"]
        assert [c.definition for c in order_one] == [
            c.definition for c in order_two
        ]


# ---------------------------------------------------------------------------
# RG — attention-budget policy selection
# ---------------------------------------------------------------------------
# Test escape: per-question cards sat mid-system-prompt, defeating the stable
# cacheable prefix; policy chooses full-glossary-in-prefix vs two-tier
# index+suffix by an attention budget. Guard: exercise all three modes with a
# controlled budget. Tier: T1.


def _card(term: str, definition: str, slug: str = "s") -> GlossaryCard:
    return GlossaryCard(
        model_id=uuid.uuid4(), term=term, definition=definition,
        synonyms=[], model_slug=slug,
    )


class TestGlossaryBudgetPolicy:
    @pytest.mark.asyncio
    async def test_full_glossary_mode_when_it_fits(self):
        cards = [_card("ARR", "annual recurring revenue")]
        with patch.object(A, "list_glossary_cards", AsyncMock(return_value=cards)):
            g = await _resolve_glossary_grounding(
                MagicMock(), [uuid.uuid4()], "q", "", budget_tokens=8000
            )
        assert g.full_glossary is True
        assert g.prefix_cards == cards
        assert g.term_index == []
        assert g.suffix_cards == []

    @pytest.mark.asyncio
    async def test_two_tier_mode_when_over_budget(self):
        # 50 cards with fat definitions: full render far exceeds the budget,
        # the compact index (terms only) fits. The term index and the score>0
        # retrieval are DERIVED (single glossary load, review R1 finding 5) —
        # no separate build_term_index / retrieve calls to patch.
        big = [_card(f"term{i}", "x " * 40) for i in range(50)]
        big[7] = _card("revenue", "total settled revenue")
        with patch.object(A, "list_glossary_cards", AsyncMock(return_value=big)):
            # Budget below the full-glossary size (~600+ tokens) but above the
            # compact term index (~150 tokens) -> two-tier mode.
            g = await _resolve_glossary_grounding(
                MagicMock(), [uuid.uuid4()], "show me revenue", "",
                budget_tokens=300,
            )
        assert g.full_glossary is False
        assert g.prefix_cards == []
        # Index derived from ALL cards, in card order.
        assert [t.term for t in g.term_index] == [c.term for c in big]
        # Retrieval derived from the same load: only the score>0 match.
        assert [c.term for c in g.suffix_cards] == ["revenue"]

    @pytest.mark.asyncio
    async def test_retrieval_only_when_index_over_budget(self):
        big = [
            _card(f"a_very_long_term_name_number_{i}", "x " * 40)
            for i in range(50)
        ]
        big[3] = _card("revenue", "total settled revenue")
        with patch.object(A, "list_glossary_cards", AsyncMock(return_value=big)):
            g = await _resolve_glossary_grounding(
                MagicMock(), [uuid.uuid4()], "show me revenue", "",
                budget_tokens=1,
            )
        assert g.full_glossary is False
        assert g.prefix_cards == []
        assert g.term_index == []
        assert g.retrieval_only is True
        assert [c.term for c in g.suffix_cards] == ["revenue"]

    @pytest.mark.asyncio
    async def test_empty_glossary_is_empty_grounding(self):
        with patch.object(A, "list_glossary_cards", AsyncMock(return_value=[])):
            g = await _resolve_glossary_grounding(
                MagicMock(), [uuid.uuid4()], "q", "", budget_tokens=8000
            )
        assert g == _GlossaryGrounding([], [], [], full_glossary=False)

    @pytest.mark.asyncio
    async def test_budget_boundary_exact_fit_is_full_mode(self):
        # Review R1 finding 10 — pin the <= boundary: an estimate EXACTLY equal
        # to the budget stays in full-glossary mode; one token less flips to a
        # non-full mode.
        from src.prompt.assembler import _render_card

        cards = [_card("ARR", "annual recurring revenue")]
        exact = _estimate_tokens("\n".join(_render_card(c) for c in cards))
        assert exact > 0
        with patch.object(A, "list_glossary_cards", AsyncMock(return_value=cards)):
            at_budget = await _resolve_glossary_grounding(
                MagicMock(), [uuid.uuid4()], "q", "", budget_tokens=exact
            )
            under_budget = await _resolve_glossary_grounding(
                MagicMock(), [uuid.uuid4()], "q", "", budget_tokens=exact - 1
            )
        assert at_budget.full_glossary is True
        assert under_budget.full_glossary is False

    def test_estimate_tokens_positive(self):
        assert _estimate_tokens("") == 0
        assert _estimate_tokens("abcd") == 1
        assert _estimate_tokens("a" * 40) == 10


# ---------------------------------------------------------------------------
# RG — cache-friendliness: retrieved cards live in the user prompt, not system
# ---------------------------------------------------------------------------


class TestCacheFriendlyPlacement:
    def _cfg(self):
        return types.SimpleNamespace(
            id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            primary_model_id=None,
            pinned_model_id=None,
            session_history_depth=20,
            agent_role="data analyst",
            safety_policy="",
            default_locale="en-GB",
            project_brief="",
            disclosure_text="",
            brand_guidelines="",
            content_rules="",
            chart_type_selector="auto",
            chart_renderer="echarts",
        )

    def _allow_db(self, ids):
        allow_result = MagicMock()
        allow_result.scalars.return_value.all.return_value = list(ids)
        empty = MagicMock()
        empty.scalars.return_value.all.return_value = []
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[allow_result] + [empty] * 20)
        return db

    async def _profiles(self, db, project_id, allow_list_ids):
        return [
            _profile(measure_names=["revenue"], dimension_names=["region"])
            for _ in allow_list_ids
        ]

    @pytest.mark.asyncio
    async def test_two_tier_retrieved_cards_in_user_not_system(self):
        from src.prompt.assembler import assemble_prompt

        mid = uuid.uuid4()
        db = self._allow_db([mid])
        retrieved = [_card("SecretMatch", "relevant definition", slug="s")]
        idx = [GlossaryTermIndexEntry(model_id=mid, model_slug="s", term="Idxterm", synonyms=[])]
        grounding = _GlossaryGrounding([], idx, retrieved, full_glossary=False)

        with patch.object(A, "_load_model_profiles", side_effect=self._profiles), \
             patch.object(A, "_resolve_glossary_grounding", AsyncMock(return_value=grounding)), \
             patch.object(A, "retrieve_alias_maps", AsyncMock(return_value=[])), \
             patch.object(A, "_conversation_history", AsyncMock(return_value=[])):
            bundle = await assemble_prompt(
                db, self._cfg(), conversation_id=uuid.uuid4(),
                user_message="show me the secret match",
            )

        # Retrieved card is a per-question artifact — it must be in the user
        # prompt (GROUNDING MATCHES), never in the cacheable system prefix.
        assert "SecretMatch" in bundle.user
        assert "GROUNDING MATCHES" in bundle.user
        assert "SecretMatch" not in bundle.system
        # The always-on term index stays in the system prefix.
        assert "Idxterm" in bundle.system

    @pytest.mark.asyncio
    async def test_full_glossary_in_system_prefix(self):
        from src.prompt.assembler import assemble_prompt

        mid = uuid.uuid4()
        db = self._allow_db([mid])
        cards = [_card("ARR", "annual recurring revenue", slug="s")]
        grounding = _GlossaryGrounding(cards, [], [], full_glossary=True)

        with patch.object(A, "_load_model_profiles", side_effect=self._profiles), \
             patch.object(A, "_resolve_glossary_grounding", AsyncMock(return_value=grounding)), \
             patch.object(A, "retrieve_alias_maps", AsyncMock(return_value=[])), \
             patch.object(A, "_conversation_history", AsyncMock(return_value=[])):
            bundle = await assemble_prompt(
                db, self._cfg(), conversation_id=uuid.uuid4(),
                user_message="anything",
            )

        # Full glossary belongs in the stable system prefix; no per-turn suffix.
        assert "ARR" in bundle.system
        assert "GROUNDING MATCHES" not in bundle.user

    # ── R1 (F1) — date anchor moved out of the cacheable system prefix ──────

    @pytest.mark.asyncio
    async def test_date_anchor_in_user_not_system(self):
        # R1: CURRENT_DATE must NOT sit in the cacheable system prefix (it would
        # break byte-stability across day boundaries) — it belongs in the
        # per-turn user DATE ANCHOR block.
        from src.prompt.assembler import assemble_prompt

        mid = uuid.uuid4()
        db = self._allow_db([mid])
        grounding = _GlossaryGrounding([], [], [], full_glossary=True)

        with patch.object(A, "_load_model_profiles", side_effect=self._profiles), \
             patch.object(A, "_resolve_glossary_grounding", AsyncMock(return_value=grounding)), \
             patch.object(A, "retrieve_alias_maps", AsyncMock(return_value=[])), \
             patch.object(A, "_conversation_history", AsyncMock(return_value=[])):
            bundle = await assemble_prompt(
                db, self._cfg(), conversation_id=uuid.uuid4(),
                user_message="anything",
            )

        assert "CURRENT_DATE" not in bundle.system
        assert "## DATE ANCHOR" in bundle.user
        assert "CURRENT_DATE" in bundle.user
        # Integration fix — the same anchor text is surfaced on the bundle for
        # the judge (bundle.date_anchor), so the judge can anchor date-range
        # verification on the SAME current date the planner used. It is NOT part
        # of the cacheable system prefix.
        assert bundle.date_anchor
        assert "CURRENT_DATE" in bundle.date_anchor
        assert bundle.date_anchor in bundle.user
        assert bundle.date_anchor not in bundle.system

    # ── R2 (F2) — section list is the single source of ``system`` ──────────

    @pytest.mark.asyncio
    async def test_system_sections_join_reproduces_system(self):
        # The judge distils from bundle.system_sections; joining them must
        # reproduce bundle.system byte-for-byte so the planner and judge can
        # never diverge.
        from src.prompt.assembler import assemble_prompt

        mid = uuid.uuid4()
        db = self._allow_db([mid])
        grounding = _GlossaryGrounding([], [], [], full_glossary=True)

        with patch.object(A, "_load_model_profiles", side_effect=self._profiles), \
             patch.object(A, "_resolve_glossary_grounding", AsyncMock(return_value=grounding)), \
             patch.object(A, "retrieve_alias_maps", AsyncMock(return_value=[])), \
             patch.object(A, "_conversation_history", AsyncMock(return_value=[])):
            bundle = await assemble_prompt(
                db, self._cfg(), conversation_id=uuid.uuid4(),
                user_message="anything",
            )

        assert bundle.system_sections, "sections must be populated"
        parts = []
        for idx, (heading, body) in enumerate(bundle.system_sections):
            if idx > 0:
                parts.append("")
            parts += [heading, body]
        assert "\n".join(parts) == bundle.system
        # The evidential headings the judge keeps are all present.
        headings = {h for h, _ in bundle.system_sections}
        assert "## AVAILABLE MODELS" in headings
        assert "## GROUNDING" in headings
        assert "## OUTPUT FORMAT" in headings  # present for planner, stripped by judge

    @pytest.mark.asyncio
    async def test_bundle_exposes_grounding_matches_for_judge(self):
        # Intake fix: the retrieved cards Lane B put in bundle.user must ALSO be
        # exposed on bundle.grounding_matches so the judge dispatch can thread
        # them into the evidence pack.
        from src.prompt.assembler import assemble_prompt

        mid = uuid.uuid4()
        db = self._allow_db([mid])
        retrieved = [_card("SecretMatch", "relevant definition", slug="s")]
        idx = [GlossaryTermIndexEntry(model_id=mid, model_slug="s", term="Idxterm", synonyms=[])]
        grounding = _GlossaryGrounding([], idx, retrieved, full_glossary=False)

        with patch.object(A, "_load_model_profiles", side_effect=self._profiles), \
             patch.object(A, "_resolve_glossary_grounding", AsyncMock(return_value=grounding)), \
             patch.object(A, "retrieve_alias_maps", AsyncMock(return_value=[])), \
             patch.object(A, "_conversation_history", AsyncMock(return_value=[])):
            bundle = await assemble_prompt(
                db, self._cfg(), conversation_id=uuid.uuid4(),
                user_message="show me the secret match",
            )

        assert "SecretMatch" in bundle.grounding_matches
        # and it is NOT in the system prefix (still a per-turn artifact).
        assert "SecretMatch" not in bundle.system

    @pytest.mark.asyncio
    async def test_judge_strip_keep_sets_cover_assembler_headings_exactly(self):
        # R2 producer-derived parity guard (review R1 finding 1): the judge's
        # strip/keep classification must cover the assembler's section headings
        # EXACTLY. If a heading is renamed or a section added, this test goes
        # red so a human classifies it as evidential (keep) or boilerplate
        # (strip) — the judge itself fails safe (keeps unknowns), but drift
        # must never go unnoticed.
        from src.judge.judge import (
            _JUDGE_EVIDENTIAL_HEADINGS,
            _JUDGE_NON_EVIDENTIAL_HEADINGS,
        )
        from src.prompt.assembler import assemble_prompt

        mid = uuid.uuid4()
        db = self._allow_db([mid])
        grounding = _GlossaryGrounding([], [], [], full_glossary=True)

        with patch.object(A, "_load_model_profiles", side_effect=self._profiles), \
             patch.object(A, "_resolve_glossary_grounding", AsyncMock(return_value=grounding)), \
             patch.object(A, "retrieve_alias_maps", AsyncMock(return_value=[])), \
             patch.object(A, "_conversation_history", AsyncMock(return_value=[])):
            bundle = await assemble_prompt(
                db, self._cfg(), conversation_id=uuid.uuid4(),
                user_message="anything",
            )

        produced = {h for h, _ in bundle.system_sections}
        classified = _JUDGE_EVIDENTIAL_HEADINGS | _JUDGE_NON_EVIDENTIAL_HEADINGS
        assert produced == classified, (
            f"Assembler headings and judge strip/keep classification diverged. "
            f"unclassified={produced - classified} stale={classified - produced}"
        )


# ---------------------------------------------------------------------------
# Bug-7953 — dict-valued example-question decomposition must not crash
# ---------------------------------------------------------------------------
# Test escape: example_questions is untyped JSONB; the eval module and the
# agent_config API accept dict-valued decompositions, but the renderer called
# .strip() on the raw value, raising AttributeError on EVERY chat turn for the
# model. Guard: _coerce_prompt_text normalises str/dict/list/None/other before
# rendering. Tier: T2 (fixed-bug regression guard).


class TestExampleQuestionDecompositionNormalisation:
    def test_dict_decomposition_renders_compact_json_no_crash(self):
        p = _profile()
        p.example_questions = [
            {
                "q": "Total revenue last month?",
                "decomposition": {
                    "model": "sales",
                    "measures": ["revenue"],
                    "dimensions": [],
                },
            }
        ]
        text = _format_model_layer([p])
        assert "Total revenue last month?" in text
        # Dict renders as deterministic compact JSON, not a Python repr.
        assert '"measures": ["revenue"]' in text
        assert "'measures'" not in text

    def test_string_decomposition_still_stripped(self):
        p = _profile()
        p.example_questions = [
            {"q": "Q?", "decomposition": "  measure: revenue  "}
        ]
        text = _format_model_layer([p])
        assert "decomposition: measure: revenue" in text

    def test_none_and_missing_decomposition_render_question_only(self):
        p = _profile()
        p.example_questions = [
            {"q": "First?", "decomposition": None},
            {"q": "Second?"},
        ]
        text = _format_model_layer([p])
        assert "First?" in text
        assert "Second?" in text
        assert "decomposition:" not in text

    def test_non_string_question_does_not_crash(self):
        # Same untyped-JSONB class on the q field.
        p = _profile()
        p.example_questions = [{"q": 42, "decomposition": "d"}]
        text = _format_model_layer([p])
        assert "Q: 42" in text

    def test_dict_json_is_deterministic_sorted_keys(self):
        from src.prompt.assembler import _coerce_prompt_text

        a = _coerce_prompt_text({"b": 1, "a": 2})
        b = _coerce_prompt_text({"a": 2, "b": 1})
        assert a == b
        assert a.index('"a"') < a.index('"b"')


# ---------------------------------------------------------------------------
# Bug-6556 — selectable-models consumes the public assembler facade
# ---------------------------------------------------------------------------
# Test escape: the endpoint reached into private assembler internals
# (_load_model_profiles/_load_persona_scopes/_apply_persona_filter), fragile to
# refactors. Guard: a public facade (load_selectable_models / SelectableModelInfo)
# exists, and it applies the same embed + persona narrowing as assemble_prompt.
# Tier: T2 (regression guard on the public interface).


class TestSelectableModelsFacade:
    def test_public_facade_symbols_exist(self):
        from src.prompt.assembler import SelectableModelInfo, load_selectable_models

        assert callable(load_selectable_models)
        fields = SelectableModelInfo.__dataclass_fields__
        assert "id" in fields
        assert "display_name" in fields

    def test_endpoint_uses_public_facade_not_private_internals(self):
        import inspect
        from src.api import agent_config

        src = inspect.getsource(agent_config.list_selectable_models)
        assert "load_selectable_models" in src
        # The endpoint body must not call the private helpers directly.
        assert "_load_model_profiles(" not in src
        assert "_load_persona_scopes(" not in src
        assert "_apply_persona_filter(" not in src

    @pytest.mark.asyncio
    async def test_facade_drops_models_outside_persona_scope(self):
        # Review R1 finding 10 — the meaningful narrowing assertion: a model
        # WITHOUT a persona scope entry must be dropped by the facade, while an
        # in-scope model survives. (Passing only the in-scope model through
        # would not prove the filter ran at all.)
        from src.prompt.assembler import load_selectable_models

        in_scope = uuid.uuid4()
        out_of_scope = uuid.uuid4()
        persona_id = uuid.uuid4()
        prof_in = A._ModelProfile(**{**_profile().__dict__, "id": in_scope})
        prof_out = A._ModelProfile(**{**_profile().__dict__, "id": out_of_scope})

        scope = {
            in_scope: _PersonaScope(
                model_id=in_scope,
                measure_names={"revenue"},
                dimension_names={"region"},
            )
            # No entry for out_of_scope — the persona does not grant it.
        }
        with patch.object(
            A, "_load_model_profiles", AsyncMock(return_value=[prof_in, prof_out])
        ), patch.object(A, "_load_persona_scopes", AsyncMock(return_value=scope)):
            models = await load_selectable_models(
                MagicMock(), uuid.uuid4(), [in_scope, out_of_scope],
                persona_id=persona_id,
            )
        assert [m.id for m in models] == [in_scope]


class TestSemiAdditiveCatalogueLockstep:
    """Bug-8257 (deep-review R6 finding 3).

    ``_measure_semantics_line`` is a comment-documented mirror of
    ``shared...dimensions_measures.derive_is_additive`` ("keep the two in
    lockstep"). R5 added the semi-additive rule to the producer and not to the
    mirror. That is reachable: this assembler reads Measure ORM rows DIRECTLY,
    not through the rehydrator, so any row persisted BEFORE the producer
    coercion landed still carries ``is_additive=True`` beside a
    ``last_non_empty`` behaviour -- the exact shape this wave exists to fix --
    and the catalogue announced it to the LLM as additive. The agent then tells
    the user to sum daily balances: 310 where the closing balance is 90.

    Test escape: nothing pinned the two chains against each other.
    Guard: this class. Tier: T1 producer/consumer contract.
    """

    def test_a_semi_additive_measure_is_announced_non_additive(self) -> None:
        md = MeasureRoleMetadata(
            name="account_balance",
            default_agg="sum",
            measure_type="standard",
            is_additive=True,               # the stale persisted flag
            semi_additive_behavior="last_non_empty",
        )
        line = _measure_semantics_line("account_balance", md, None)
        assert "non-additive" in line, line
        assert "(sum, numeric, additive" not in line, line

    def test_a_plain_sum_measure_is_still_announced_additive(self) -> None:
        """Control: the new branch must fire only on a declared behaviour."""
        md = MeasureRoleMetadata(
            name="revenue", default_agg="sum", measure_type="standard",
            is_additive=True, semi_additive_behavior=None,
        )
        assert "additive" in _measure_semantics_line("revenue", md, None)
        assert "non-additive" not in _measure_semantics_line("revenue", md, None)

    def test_from_mapping_carries_the_behaviour(self) -> None:
        """The ORM->metadata hop must not drop the field on the floor."""
        md = MeasureRoleMetadata.from_mapping(
            "account_balance",
            {"default_agg": "sum", "is_additive": True,
             "semi_additive_behavior": "last_non_empty"},
        )
        assert md.semi_additive_behavior == "last_non_empty"
        assert "non-additive" in _measure_semantics_line("account_balance", md, None)

    def test_the_mirror_implements_every_rule_the_producer_does(self) -> None:
        """Order parity, checked by behaviour rather than by reading. For each
        shape the shared producer calls non-additive, the catalogue must say so
        too -- otherwise the two drift again the next time a rule is added."""
        import inspect

        from shared.schemas.domains.dimensions_measures import derive_is_additive

        # Deep-review R7 finding 6: the shape list below is hand-written, so a
        # FIFTH rule added to the producer would not be discovered and the
        # "these two cannot drift" claim would quietly stop being true. Pin the
        # list against the producer's own signature: adding a parameter forces
        # whoever added it to extend the shapes here.
        params = set(inspect.signature(derive_is_additive).parameters) - {"declared"}
        assert params == {
            "default_agg", "measure_type", "variant_kind",
            "semi_additive_behavior",
        }, (
            "derive_is_additive grew a new input; add a shape for it below and "
            f"mirror the rule in _measure_semantics_line. Params now: {params}"
        )

        shapes = [
            {"default_agg": "avg"},
            {"default_agg": "sum", "semi_additive_behavior": "last_non_empty"},
            {"default_agg": "sum", "variant_kind": "ytd"},
            {"default_agg": "sum", "measure_type": "calculated"},
        ]
        for shape in shapes:
            derived = derive_is_additive(
                default_agg=shape.get("default_agg"),
                measure_type=shape.get("measure_type") or "standard",
                variant_kind=shape.get("variant_kind"),
                semi_additive_behavior=shape.get("semi_additive_behavior"),
                declared=True,
            )
            assert derived is False, shape
            md = MeasureRoleMetadata(
                name="m",
                default_agg=shape.get("default_agg") or "sum",
                measure_type=shape.get("measure_type") or "standard",
                variant_kind=shape.get("variant_kind"),
                semi_additive_behavior=shape.get("semi_additive_behavior"),
                is_additive=True,
            )
            assert "non-additive" in _measure_semantics_line("m", md, None), shape
