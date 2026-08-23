"""Bug-9394 (backend half) — measure rename must be previewable before it runs.

The KPI DSL binds measures by NAME, so renaming a measure silently orphans
every KPI expression that referenced the old name unless a cascade rewrites it.
``propagate_measure_renames`` already performs that cascade. What was missing is
the modeller's ability to SEE which KPIs a rename would change before committing
to it — the confirmation dialog the fix calls for has no data to render.

This module pins the backend contract the frontend half (lane L13) consumes:

    GET .../measures/{measure_id}/rename-impact?new_name=<candidate>
      -> { measure_id, current_name, new_name, safe,
           rewrites: [{consumer_type, consumer_id, consumer_name, field}],
           blockers: [...] }

``safe=false`` means the PATCH would return 409 with the same references, so
the dialog can warn instead of offering a rename that cannot succeed.

The load-bearing property is that the preview and the rename share ONE
enumeration: a preview computed by a second, parallel walk drifts from the
writer the first time a consumer type is added on one side only.

Test escape: the cascade had unit coverage; nothing asserted that a modeller
could see its effect first, and nothing tied the preview to the writer's own
walk. Guard: this module. Tier: T2.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from src.measure_rename import (
    MeasureRenamePlan,
    UnsafeMeasureRename,
    plan_measure_renames,
    propagate_measure_renames,
)

pytestmark = pytest.mark.unit


class _ScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _ScalarResult(self._rows)


class _Db:
    """Serves each ORM entity's rows by the entity named in the query."""

    def __init__(self, rows_by_entity: dict[str, list]):
        self._rows = rows_by_entity
        self.deleted: list = []

    async def execute(self, query):
        text = str(query)
        for entity, rows in self._rows.items():
            if entity.lower() in text.lower():
                return _Result(rows)
        return _Result([])

    async def delete(self, obj):
        self.deleted.append(obj)


def _kpi(name: str, expression: str, **extra):
    base = dict(
        id=uuid.uuid4(),
        name=name,
        display_name=name,
        expression=expression,
        target_expression=None,
        status_expression=None,
        trend_expression=None,
        business_definition=None,
    )
    base.update(extra)
    return SimpleNamespace(**base)


def _db_with_kpis(kpis):
    return _Db({"kpis": kpis})


class TestPlanIsReadOnly:
    @pytest.mark.asyncio
    async def test_planning_does_not_mutate_the_kpi(self):
        kpi = _kpi("Revenue (YTD)", 'period_to_date(measure("Revenue"), "year")')
        db = _db_with_kpis([kpi])

        _updates, _cov, unsafe, plan = await plan_measure_renames(
            db, uuid.uuid4(), {uuid.uuid4(): ("Revenue", "Net Revenue")},
            for_update=False,
        )

        assert unsafe == []
        assert kpi.expression == 'period_to_date(measure("Revenue"), "year")', (
            "the PREVIEW must not rewrite anything"
        )
        assert db.deleted == []
        assert isinstance(plan, MeasureRenamePlan)

    @pytest.mark.asyncio
    async def test_plan_names_the_affected_kpi_for_the_dialog(self):
        kpi = _kpi("Revenue (YTD)", 'period_to_date(measure("Revenue"), "year")')
        db = _db_with_kpis([kpi])

        *_ignored, plan = await plan_measure_renames(
            db, uuid.uuid4(), {uuid.uuid4(): ("Revenue", "Net Revenue")},
            for_update=False,
        )

        assert plan.safe is True
        kpi_items = [r for r in plan.rewrites if r.consumer_type == "kpi"]
        assert kpi_items, "the affected KPI must be listed"
        assert kpi_items[0].consumer_id == str(kpi.id)
        assert kpi_items[0].consumer_name == "Revenue (YTD)"
        assert kpi_items[0].field == "expression"

    @pytest.mark.asyncio
    async def test_an_unrelated_kpi_is_not_listed(self):
        kpi = _kpi("Orders", 'measure("Orders")')
        db = _db_with_kpis([kpi])

        *_ignored, plan = await plan_measure_renames(
            db, uuid.uuid4(), {uuid.uuid4(): ("Revenue", "Net Revenue")},
            for_update=False,
        )
        assert plan.rewrites == []
        assert plan.safe is True


class TestPlanReportsBlockers:
    @pytest.mark.asyncio
    async def test_a_status_expression_reference_blocks_the_rename(self):
        """``status_expression`` / ``trend_expression`` are not rewritable, so
        the rename 409s. The dialog must be able to say so up front."""
        kpi = _kpi(
            "Revenue (YTD)",
            'measure("Orders")',
            status_expression='measure("Revenue") > 100',
        )
        db = _db_with_kpis([kpi])

        _updates, _cov, unsafe, plan = await plan_measure_renames(
            db, uuid.uuid4(), {uuid.uuid4(): ("Revenue", "Net Revenue")},
            for_update=False,
        )

        assert unsafe, "the unsafe reference must be detected"
        assert plan.safe is False
        assert any(
            b.consumer_type == "kpi" and b.field == "status_expression"
            for b in plan.blockers
        ), plan.blockers


class TestBug9396PrivateSavedQueryImpact:
    """Personal artifact details stay private during model-wide rename."""

    @pytest.mark.asyncio
    async def test_safe_private_query_is_rewritten_but_hidden_from_other_user_preview(self):
        private_query = SimpleNamespace(
            id=uuid.uuid4(),
            name="My compensation analysis",
            query_text='SELECT "Revenue" FROM sales',
            query_type="sql",
            created_by="analyst@example.com",
            is_shared=False,
        )
        db = _Db({"saved_queries": [private_query]})

        updates, _coverage, unsafe, plan = await plan_measure_renames(
            db,
            uuid.uuid4(),
            {uuid.uuid4(): ("Revenue", "Net Revenue")},
            for_update=False,
        )

        assert unsafe == []
        assert updates, "the private query must still be kept compatible"
        assert plan.for_viewer("modeler@example.com").rewrites == []
        owner_view = plan.for_viewer("analyst@example.com")
        assert owner_view.rewrites[0].consumer_id == str(private_query.id)
        assert owner_view.rewrites[0].consumer_name == private_query.name

    @pytest.mark.asyncio
    async def test_private_blocker_is_redacted_for_other_user_and_exact_for_owner(self):
        private_query = SimpleNamespace(
            id=uuid.uuid4(),
            name="Private DAX",
            query_text="EVALUATE Revenue",
            query_type="dax",
            created_by="analyst@example.com",
            is_shared=False,
        )
        db = _Db({"saved_queries": [private_query]})

        _updates, _coverage, unsafe, plan = await plan_measure_renames(
            db,
            uuid.uuid4(),
            {uuid.uuid4(): ("Revenue", "Net Revenue")},
            for_update=False,
        )

        other_view = plan.for_viewer("modeler@example.com")
        assert other_view.safe is False
        assert other_view.blockers[0].consumer_id == ""
        assert other_view.blockers[0].consumer_name is None
        assert str(private_query.id) not in str(other_view.blockers)
        assert private_query.name not in str(other_view.blockers)

        owner_view = plan.for_viewer("analyst@example.com")
        assert owner_view.blockers[0].consumer_id == str(private_query.id)

        error = UnsafeMeasureRename(unsafe)
        assert str(private_query.id) not in error.detail_for("modeler@example.com")
        assert private_query.name not in error.detail_for("modeler@example.com")
        assert str(private_query.id) in error.detail_for("analyst@example.com")

    @pytest.mark.asyncio
    async def test_private_scratchpad_rewrites_and_blockers_are_owner_scoped(self):
        owner = "analyst@example.com"
        safe = SimpleNamespace(
            id=uuid.uuid4(),
            name="My safe calculation",
            expression='measure("Revenue")',
            created_by=owner,
        )
        blocker = SimpleNamespace(
            id=uuid.uuid4(),
            name="My opaque calculation",
            expression="Revenue",
            created_by=owner,
        )
        db = _Db({"scratchpad_measures": [safe, blocker]})

        _updates, _coverage, unsafe, plan = await plan_measure_renames(
            db,
            uuid.uuid4(),
            {uuid.uuid4(): ("Revenue", "Net Revenue")},
            for_update=False,
        )

        other_view = plan.for_viewer("modeler@example.com")
        assert other_view.rewrites == []
        assert other_view.safe is False
        assert other_view.blockers[0].consumer_id == ""
        assert str(safe.id) not in str(other_view)
        assert str(blocker.id) not in str(other_view)

        owner_view = plan.for_viewer(owner)
        assert owner_view.rewrites[0].consumer_id == str(safe.id)
        assert owner_view.blockers[0].consumer_id == str(blocker.id)
        assert str(blocker.id) not in UnsafeMeasureRename(unsafe).detail_for(
            "modeler@example.com"
        )

    def test_api_preview_and_both_rename_writers_apply_visibility(self):
        """The privacy primitive must be wired to every public rename caller."""
        import inspect

        from src.api import dimensions, measures

        preview = inspect.getsource(measures.measure_rename_impact)
        single_writer = inspect.getsource(measures.update_measure)
        bulk_writer = inspect.getsource(dimensions.bulk_rename_attributes)

        assert ".for_viewer(" in preview
        assert ".detail_for(" in single_writer
        assert ".detail_for(" in bulk_writer


class TestPreviewAndWriterShareOneEnumeration:
    """The property that stops the preview drifting from the rename."""

    @pytest.mark.asyncio
    async def test_the_writer_applies_exactly_what_the_plan_listed(self):
        expression = 'period_to_date(measure("Revenue"), "year")'
        preview_kpi = _kpi("Revenue (YTD)", expression)
        *_ignored, plan = await plan_measure_renames(
            _db_with_kpis([preview_kpi]), uuid.uuid4(),
            {uuid.uuid4(): ("Revenue", "Net Revenue")},
            for_update=False,
        )

        apply_kpi = _kpi("Revenue (YTD)", expression)
        apply_kpi.id = preview_kpi.id
        await propagate_measure_renames(
            _db_with_kpis([apply_kpi]), uuid.uuid4(),
            {uuid.uuid4(): ("Revenue", "Net Revenue")},
        )

        assert apply_kpi.expression == 'period_to_date(measure("Net Revenue"), "year")'
        assert [(r.consumer_id, r.field) for r in plan.rewrites] == [
            (str(apply_kpi.id), "expression"),
        ]

    def test_the_writer_delegates_to_the_planner(self):
        """Structural: a second, parallel walk is exactly the drift this fix
        removes, so the writer must not grow its own enumeration back."""
        import ast
        import inspect

        from src import measure_rename as mod

        tree = ast.parse(inspect.getsource(mod))
        writer = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "propagate_measure_renames"
        )
        calls = {
            getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            for node in ast.walk(writer)
            if isinstance(node, ast.Call)
        }
        assert "plan_measure_renames" in calls, (
            "propagate_measure_renames must consume the shared plan, not walk "
            "the consumers itself (Bug-9394)"
        )


class TestEndpointIsRegistered:
    def test_rename_impact_route_exists_and_is_modeller_gated(self):
        from src.api import measures as measures_mod

        routes = [
            r for r in measures_mod.router.routes
            if getattr(r, "path", "").endswith("/{measure_id}/rename-impact")
        ]
        assert routes, "the rename-impact preview endpoint is not registered"
        route = routes[0]
        assert "GET" in route.methods
        # Read-only preview of a modelling action — same role as the rename.
        assert route.dependencies, "the endpoint must carry a role dependency"


class TestBusinessDefinitionRewriteIsKeyScoped:
    """In-area wrong-numbers defect found while wiring the preview.

    ``_rewrite_json`` replaced ANY string that merely EQUALLED the old measure
    name, anywhere in ``business_definition``. That blob also holds dimension
    MEMBER values under ``filters[i].value`` / ``.values``, and measure names
    collide with member values in ordinary models ("Retail", "Online",
    "Direct"). Renaming a measure ``Retail`` therefore rewrote the KPI's
    ``channel = "Retail"`` filter to ``"Retail Sales"``, silently changing which
    rows the KPI computes over — a wrong number with no error anywhere.

    Test escape: every existing case renamed a measure whose name matched no
    filter value. Guard: this class. Tier: T2.
    """

    @pytest.mark.asyncio
    async def test_a_filter_value_equal_to_the_measure_name_is_not_rewritten(self):
        kpi = _kpi(
            "Retail revenue",
            'measure("Retail")',
            business_definition={
                "filters": [
                    {"dimension": "channel", "operator": "equals", "value": "Retail"},
                    {"dimension": "region", "operator": "in", "values": ["Retail"]},
                ],
                "_compiled": {
                    "expression": 'sum(measure("Retail"))',
                    "summary_tokens": {
                        "measure_name": "Retail",
                        "dimension_name": "Retail",
                    },
                },
            },
        )
        db = _db_with_kpis([kpi])

        await propagate_measure_renames(
            db, uuid.uuid4(), {uuid.uuid4(): ("Retail", "Retail Sales")},
        )

        bd = kpi.business_definition
        assert bd["filters"][0]["value"] == "Retail", (
            "the KPI's dimension filter was rewritten by a MEASURE rename — "
            "the KPI now computes over a different slice"
        )
        assert bd["filters"][1]["values"] == ["Retail"]
        # The dimension name is a DIMENSION, not the renamed measure.
        assert bd["_compiled"]["summary_tokens"]["dimension_name"] == "Retail"
        # The genuine measure-name carriers ARE rewritten.
        assert bd["_compiled"]["summary_tokens"]["measure_name"] == "Retail Sales"
        assert bd["_compiled"]["expression"] == 'sum(measure("Retail Sales"))'
        assert kpi.expression == 'measure("Retail Sales")'


class TestSummaryTokenCarrierKeysHaveOneOwner:
    """Bug-9483 — the key-scoped rewrite must know every key the PRODUCER writes.

    The key-scoping above fixed a wrong number, but it introduced a
    producer/consumer split: ``kpi_business_builder._build_summary`` writes FIVE
    measure-name keys and ``measure_rename`` knew THREE. The two it missed,
    ``measure_a_name`` / ``measure_b_name``, belong to the ``compare_measures``
    formula family — shipped and reachable from the wizard — so renaming a
    measure left that KPI's summary naming a measure that no longer exists.

    Test escape: the key-scoping tests all used ``single_measure`` /
    ``ratio``, the three families whose keys were in the set. Guard: this class,
    which derives the expectation FROM the producer rather than restating it.
    Tier: T1 (producer/consumer contract).
    """

    def test_the_consumer_uses_the_producers_own_constant(self):
        from src.kpi_business_builder import MEASURE_NAME_SUMMARY_TOKEN_KEYS
        from src import measure_rename as mod

        assert mod._MEASURE_NAME_JSON_KEYS is MEASURE_NAME_SUMMARY_TOKEN_KEYS, (
            "measure_rename holds its own copy of the carrier-key set again; "
            "the two drift and a rename stops rewriting whichever key the "
            "builder added last (Bug-9483)"
        )

    def test_every_measure_name_token_the_builder_emits_is_a_carrier(self):
        """Derived from the PRODUCER: build a summary for every formula family
        in ``FORMULA_TYPES`` and assert each token holding a measure NAME is
        declared as a carrier.

        L7R-02 — this guard's own ENUMERATION was the blind spot. It hand-listed
        ten families against ``FORMULA_TYPES``' eleven; the list it held was not
        a subset either, because ``distinct_count`` is not a formula type at all
        (``_build_summary`` returns an empty summary for it, so that entry
        exercised nothing) and the real ``count_distinct`` and ``count_records``
        were never built. Its claim to "survive a new formula family" was
        therefore false in both directions: a new family is invisible to a hand
        list, and a renamed one degrades to a silent no-op rather than a
        failure.

        The family list is now DERIVED from ``FORMULA_TYPES`` and the fixture
        map fails CLOSED: a family with no fixture is an error here, not a
        family quietly skipped.
        """
        from src.kpi_business_builder import (
            FORMULA_TYPES, MEASURE_NAME_SUMMARY_TOKEN_KEYS, _build_summary,
        )

        m_a, m_b = str(uuid.uuid4()), str(uuid.uuid4())
        dim = str(uuid.uuid4())
        measure_names = {m_a: "Retail", m_b: "Wholesale"}
        dimension_names = {dim: "Channel"}

        # One representative formula per family. Every id-shaped field a family
        # reads is supplied and resolvable through the name maps above, so a
        # measure-name token can never fall through to a raw id and be skipped
        # by the value filter below.
        fixtures: dict[str, dict] = {
            "single_measure": {"measure_id": m_a, "aggregation": "sum"},
            "ratio": {"numerator_measure_id": m_a,
                      "denominator_measure_id": m_b},
            "count_records": {},
            "count_distinct": {"dimension_id": dim},
            "moving_average": {"measure_id": m_a, "window_size": 3},
            "compare_periods": {"measure_id": m_a},
            "compare_measures": {"measure_a_id": m_a, "measure_b_id": m_b,
                                 "mode": "absolute"},
            "target_comparison": {"measure_id": m_a,
                                  "denominator_measure_id": m_b},
            "exception_sla": {"sla_type": "compliance_pct"},
            "share_rank": {"measure_id": m_a},
            "composite_score": {},
        }

        # Fail CLOSED on a shape this guard does not recognise, in BOTH
        # directions: a new formula family with no fixture, and a fixture for a
        # family that no longer exists (which is how ``distinct_count`` sat here
        # exercising nothing).
        assert set(fixtures) == set(FORMULA_TYPES), (
            "this guard's family enumeration has drifted from the producer's "
            f"own FORMULA_TYPES: unfixtured {set(FORMULA_TYPES) - set(fixtures)}, "
            f"not-a-formula-type {set(fixtures) - set(FORMULA_TYPES)}. Until "
            "every family is built, a new one could write a sixth measure-name "
            "token and this guard would pass (L7R-02)"
        )

        seen_carriers: set[str] = set()
        for family in sorted(FORMULA_TYPES):
            formula = {"type": family, **fixtures[family]}
            _summary, tokens = _build_summary(
                {"formula": formula}, measure_names, dimension_names,
            )
            for key, value in tokens.items():
                if not isinstance(value, str) or value not in ("Retail", "Wholesale"):
                    continue
                assert key in MEASURE_NAME_SUMMARY_TOKEN_KEYS, (
                    f"formula '{family}' writes the measure name under "
                    f"'{key}', which is not declared a carrier key — a rename "
                    "will leave this summary naming a measure that no longer "
                    "exists (Bug-9483)"
                )
                seen_carriers.add(key)

        assert seen_carriers == set(MEASURE_NAME_SUMMARY_TOKEN_KEYS), (
            "the declared carrier set and the keys the builder actually emits "
            f"have diverged: declared-only {set(MEASURE_NAME_SUMMARY_TOKEN_KEYS) - seen_carriers}, "
            f"emitted-only {seen_carriers - set(MEASURE_NAME_SUMMARY_TOKEN_KEYS)}"
        )

    def test_a_dimension_name_is_never_a_carrier(self):
        """The exclusions are load-bearing: ``dimension_name`` holds a DIMENSION
        and ``filter_dimensions`` holds rendered LABELS. Rewriting either turns
        a measure rename into a change of which rows the KPI reads."""
        from src.kpi_business_builder import MEASURE_NAME_SUMMARY_TOKEN_KEYS

        assert "dimension_name" not in MEASURE_NAME_SUMMARY_TOKEN_KEYS
        assert "filter_dimensions" not in MEASURE_NAME_SUMMARY_TOKEN_KEYS

    @pytest.mark.asyncio
    async def test_compare_measures_summary_tokens_are_rewritten(self):
        """The behavioural half: the family that was actually broken."""
        kpi = _kpi(
            "Retail vs Wholesale",
            'measure("Retail") - measure("Wholesale")',
            business_definition={
                "formula": {"type": "compare_measures"},
                "_compiled": {
                    "summary": "Retail - Wholesale",
                    "summary_tokens": {
                        "formula_type": "compare_measures",
                        "measure_a_name": "Retail",
                        "measure_b_name": "Wholesale",
                        "mode": "absolute",
                    },
                },
            },
        )
        db = _db_with_kpis([kpi])

        await propagate_measure_renames(
            db, uuid.uuid4(), {uuid.uuid4(): ("Retail", "Retail Sales")},
        )

        tokens = kpi.business_definition["_compiled"]["summary_tokens"]
        assert tokens["measure_a_name"] == "Retail Sales", (
            "a compare_measures KPI still names a measure that no longer "
            "exists after the rename (Bug-9483)"
        )
        assert tokens["measure_b_name"] == "Wholesale"


class TestConsumerTypeVocabularyIsOneClosedSet:
    """L7-R3 — one consumer, one token, and it is the documented one.

    The rewrites path recorded the model alias map as ``alias_map`` while the
    blockers path recorded the SAME consumer as ``model_alias_map``, and the
    response schema documented only the first. Lane L13 builds the confirmation
    dialog against that schema, so an undocumented token would reach it as an
    unlabelled row.
    """

    def _emitted_tokens(self) -> set[str]:
        """Every string literal handed to ``_record`` / ``UnsafeRenameReference``
        / ``consumer_type=``, read from the SOURCE.

        Derived from the producer rather than restated, so a new consumer added
        with a hand-written literal is caught here instead of in the UI.
        """
        import ast
        import inspect

        from src import measure_rename as mod

        tree = ast.parse(inspect.getsource(mod))
        tokens: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            candidates: list[ast.expr] = []
            if name in ("_record", "UnsafeRenameReference", "RenameImpactItem"):
                candidates.extend(node.args[:1])
                candidates.extend(
                    kw.value for kw in node.keywords if kw.arg == "consumer_type"
                )
            for candidate in candidates:
                if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str):
                    tokens.add(candidate.value)
        return tokens

    def test_no_call_site_hand_writes_a_consumer_type_literal(self):
        assert self._emitted_tokens() == set(), (
            "a consumer_type is written as a bare string literal again: "
            f"{sorted(self._emitted_tokens())}. Draw it from the "
            "CONSUMER_TYPE_* constants so the two paths cannot drift"
        )

    def test_every_constant_is_in_the_closed_set(self):
        from src import measure_rename as mod

        declared = {
            value for name, value in vars(mod).items()
            if name.startswith("CONSUMER_TYPE_") and isinstance(value, str)
        }
        assert declared, "the CONSUMER_TYPE_* constants disappeared"
        assert declared == set(mod.RENAME_CONSUMER_TYPES), (
            "a CONSUMER_TYPE_* constant is not listed in RENAME_CONSUMER_TYPES "
            f"(or vice versa): {declared ^ set(mod.RENAME_CONSUMER_TYPES)}"
        )

    def test_the_schema_documents_exactly_the_emitted_vocabulary(self):
        """Producer-derived: every token the module can emit must appear in the
        published ``consumer_type`` description, and the description must not
        promise a token the producer cannot emit."""
        from shared.schemas.pydantic_models import MeasureRenameImpactItem
        from src import measure_rename as mod

        described = MeasureRenameImpactItem.model_fields["consumer_type"].description
        assert described, "consumer_type lost its description"
        assert "One of:" in described, (
            "the description no longer opens with an explicit 'One of: a | b' "
            "enumeration, so the published vocabulary cannot be checked"
        )
        enumeration = described.split("One of:", 1)[1].split(".", 1)[0]
        documented = {token.strip() for token in enumeration.split("|")}

        assert documented == set(mod.RENAME_CONSUMER_TYPES), (
            "the documented consumer_type vocabulary and the one the producer "
            "can emit have diverged — the rename dialog renders an unlabelled "
            f"row for anything missing. Difference: "
            f"{documented ^ set(mod.RENAME_CONSUMER_TYPES)} (L7-R3)"
        )
        assert "alias_map" not in mod.RENAME_CONSUMER_TYPES, (
            "the rewrites path drifted back to 'alias_map'; the ORM entity is "
            "ModelAliasMap and the blockers path already says model_alias_map"
        )

    @pytest.mark.asyncio
    async def test_the_alias_map_rewrite_uses_the_documented_token(self):
        alias_row = SimpleNamespace(
            model_id=uuid.uuid4(),
            alias_map={"turnover": "Revenue"},
        )
        db = _Db({"model_alias_maps": [alias_row]})

        *_ignored, plan = await plan_measure_renames(
            db, uuid.uuid4(), {uuid.uuid4(): ("Revenue", "Net Revenue")},
            for_update=False,
        )

        alias_items = [
            item for item in plan.rewrites
            if item.field == "alias_map"
        ]
        assert alias_items, f"the alias map rewrite was not planned: {plan.rewrites}"
        assert alias_items[0].consumer_type == "model_alias_map", (
            "the rewrites path emits a consumer_type the schema does not "
            f"document: {alias_items[0].consumer_type} (L7-R3)"
        )

    @pytest.mark.asyncio
    async def test_the_alias_map_blocker_uses_the_same_token(self):
        """The blockers path already used it; this pins that the two AGREE."""
        alias_row = SimpleNamespace(
            model_id=uuid.uuid4(),
            alias_map={"turnover": ["Revenue"]},  # not a string -> unrewritable
        )
        db = _Db({"model_alias_maps": [alias_row]})

        *_ignored, plan = await plan_measure_renames(
            db, uuid.uuid4(), {uuid.uuid4(): ("Revenue", "Net Revenue")},
            for_update=False,
        )

        assert plan.safe is False
        assert [b.consumer_type for b in plan.blockers] == ["model_alias_map"]
