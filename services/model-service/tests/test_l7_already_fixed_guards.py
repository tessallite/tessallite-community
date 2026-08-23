"""Lock-in guards for L7 issues verified ALREADY FIXED in current code.

Each of these was reported against an older tree and is no longer reproducible.
Rather than invent a change, this module pins the correct behaviour so the
defect cannot come back unnoticed. Every assertion here was mutation-proven:
reverting the named line makes the test red.

  * **Bug-9014** — owner-bound drill-through validation compared ORM entities
    to UUIDs, so a VALID same-model, same-table detail column was rejected with
    ``DRILL_DETAIL_COLUMN_OFF_TABLE``. Current code selects the scalar
    ``ModelColumn.id``. The original happy-path fake returned a UUID rather
    than the ORM shape production produces, which is exactly what masked it —
    so the guard asserts on the QUERY SHAPE, not on a fake.
  * **Bug-9071** — the model-revalidation endpoint read ``Measure.source_table_id``,
    an attribute the ORM does not define, so Re-check 500'd on any real model.
    Current code resolves the fact table through ``ModelColumn``.
  * **Bug-8726** — ``list_kpis`` guarded the deployed-snapshot resolver with
    ``and kpis``, so an EMPTY catalogue skipped validation and returned 200 for
    a malformed deployed snapshot while the named-set sibling returned 409.
  * **Bug-6685** — ``MeasureResponse`` never populated
    ``resolved_calendar_id`` / ``resolved_date_col_id``, so the API reported
    null for a variant whose stored row had them.
  * **Bug-6574** — ``_SEMI_ADDITIVE_INELIGIBLE_FAMILIES`` was duplicated in
    ``api/measures.py`` and ``shared/schemas/measure_formats.py`` (drift risk).
  * **Bug-8568** — ``_EVALUATION_ERROR`` conflated "a correctness guard
    refused" with "the router call failed", so a 10-second timeout on the
    builder preview told the modeller to debug a correct definition.

Tier: T2 (fixed-bug regression guards).
"""
from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# AST helpers — these guards must FAIL CLOSED
# ---------------------------------------------------------------------------
# L7-R4: three of these guards used to assert a SUBSTRING of the function source.
# That fails OPEN in two directions, both demonstrated: ``assert "if deployed_only
# and kpis" not in source`` passes for ``if kpis and deployed_only:`` and for
# ``if deployed_only and len(kpis) > 0:`` — the same defect written differently —
# and ``assert "resolved_calendar_id=" in source`` is satisfied by a COMMENT
# mentioning the field while the keyword argument is gone.
#
# Per the coverage-tool blind-spot rule, a guard that proves "property X holds"
# must fail CLOSED on a shape it does not recognise: if the construct it inspects
# cannot be located at all, that is a FAILURE, never a silent pass. Each helper
# below therefore returns the located nodes and every caller asserts on a
# non-empty result before asserting the property.


def _parse(fn) -> ast.AST:
    return ast.parse(textwrap.dedent(inspect.getsource(fn)))


def _if_nodes_testing_name(fn, name: str) -> list[ast.If]:
    """Every ``if`` in *fn* whose test MENTIONS *name*, however it is written.

    Deliberately over-collects: a conjunction, a call wrapping the name, and a
    bare reference all land here, so the caller can assert on the SHAPE of the
    test rather than on the absence of one spelling of it.
    """
    return [
        node for node in ast.walk(_parse(fn))
        if isinstance(node, ast.If)
        and any(
            isinstance(sub, ast.Name) and sub.id == name
            for sub in ast.walk(node.test)
        )
    ]


def _call_keywords(fn, callee: str) -> list[ast.keyword]:
    """Every keyword argument of every ``callee(...)`` call inside *fn*."""
    out: list[ast.keyword] = []
    for node in ast.walk(_parse(fn)):
        if not isinstance(node, ast.Call):
            continue
        if (getattr(node.func, "id", None) or getattr(node.func, "attr", None)) != callee:
            continue
        out.extend(node.keywords)
    return out


def _attribute_chain(node: ast.AST) -> str:
    """Render ``KPI.is_deployed.is_`` from its Attribute/Name nodes."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _where_call_chains(fn) -> list[str]:
    """The attribute chain of the first argument of every ``.where(...)`` call."""
    chains: list[str] = []
    for node in ast.walk(_parse(fn)):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "attr", None) != "where" or not node.args:
            continue
        arg = node.args[0]
        # ``KPI.is_deployed.is_(True)`` is a Call whose func is the chain.
        target = arg.func if isinstance(arg, ast.Call) else arg
        chains.append(_attribute_chain(target))
    return chains


class TestBug9014DrillThroughMembershipIsScalar:
    def test_validation_selects_the_column_id_not_the_entity(self):
        """``found`` must be a set of UUIDs.

        Selecting ``ModelColumn`` entities makes ``cid not in found`` true for
        every requested UUID, so every valid column is rejected.
        """
        from src.api import measures as measures_mod

        fn = measures_mod._validate_detail_columns
        tree = ast.parse(inspect.getsource(fn).lstrip())
        selects = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (getattr(node.func, "id", None) or getattr(node.func, "attr", None))
            in ("select", "scoped_select")
        ]
        assert selects, "the membership query disappeared"
        membership = selects[0]
        assert membership.args, "select() called with no argument"
        arg = membership.args[0]
        assert isinstance(arg, ast.Attribute) and arg.attr == "id", (
            "the membership query selects ORM entities again — the consumer "
            "compares requested UUIDs against them, so every valid "
            "same-table column is rejected with DRILL_DETAIL_COLUMN_OFF_TABLE "
            "(Bug-9014)"
        )

    def test_projectability_check_also_selects_a_scalar(self):
        from src.api import measures as measures_mod

        source = inspect.getsource(measures_mod._validate_detail_columns)
        assert "select(Dimension.source_column_id)" in source, (
            "the projectability check must compare UUIDs to UUIDs too"
        )


class TestBug9071RevalidateNeverReadsSourceTableId:
    def test_measure_orm_has_no_source_table_id(self):
        from shared.db.models import Measure

        assert not hasattr(Measure, "source_table_id"), (
            "if Measure ever gains this column the guard below must be "
            "re-derived rather than silently passing"
        )

    def test_revalidation_resolves_the_fact_table_through_model_column(self):
        from src.api import alerts as alerts_mod

        tree = ast.parse(inspect.getsource(alerts_mod))
        reads = [
            node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == "source_table_id"
        ]
        assert reads == [], (
            "the revalidation endpoint reads .source_table_id again (line "
            f"{reads}) — an attribute the Measure ORM does not define, so "
            "Re-check 500s on any real model (Bug-9071)"
        )
        assert any(
            isinstance(node, ast.Attribute) and node.attr == "source_column_id"
            for node in ast.walk(tree)
        ), (
            "the fact table must be resolved through the measure's source "
            "COLUMN, the only link the ORM actually has"
        )


class TestBug8726EmptyKpiCatalogueStillValidatesTheSnapshot:
    def test_resolver_is_not_short_circuited_on_an_empty_catalogue(self):
        """Every ``deployed_only`` gate must be a BARE ``deployed_only`` test.

        L7-R4: the previous form (``"if deployed_only and kpis" not in source``)
        caught exactly one spelling. ``if kpis and deployed_only:`` and
        ``if deployed_only and len(kpis) > 0:`` are the same defect and both
        passed it. Asserting the SHAPE of the test — a bare ``Name`` — admits
        only the correct construct, and finding no gate at all is a failure
        rather than a pass.
        """
        from src.api import kpis as kpis_mod

        gates = _if_nodes_testing_name(kpis_mod.list_kpis, "deployed_only")
        assert gates, (
            "no `if deployed_only` gate found in list_kpis at all — the guard "
            "cannot verify anything, so it fails closed (Bug-8726)"
        )
        for gate in gates:
            assert isinstance(gate.test, ast.Name), (
                "a `deployed_only` gate at line "
                f"{gate.lineno} is no longer a bare `if deployed_only:` — it "
                f"is a `{type(gate.test).__name__}`. Any extra condition "
                "re-introduces the short-circuit: a model with a missing or "
                "malformed deployed snapshot returns 200 with an empty list "
                "instead of failing closed like the named-set sibling "
                "(Bug-8726)"
            )

        resolver = _parse(kpis_mod.list_kpis)
        called = {
            (getattr(n.func, "id", None) or getattr(n.func, "attr", None))
            for n in ast.walk(resolver) if isinstance(n, ast.Call)
        }
        assert "resolve_served_kpis" in called, (
            "the deployed-snapshot resolver is no longer called"
        )
        handled = {
            _attribute_chain(h.type).split(".")[-1]
            for n in ast.walk(resolver) if isinstance(n, ast.Try)
            for h in n.handlers if h.type is not None
        }
        assert "KpiSnapshotInvalidError" in handled, (
            "an invalid deployed snapshot must be CAUGHT and surfaced as 409, "
            "not left to propagate as a 500 or dropped"
        )


class TestBug6685MeasureResponseReportsResolvedCalendar:
    def test_builder_populates_both_resolved_fields(self):
        """The fields must be KEYWORD ARGUMENTS of the ``MeasureResponse(...)``
        call, bound to something other than a literal ``None``.

        L7-R4: ``assert "resolved_calendar_id=" in source`` is satisfied by a
        COMMENT naming the field, so the guard passed with the argument deleted.
        It also passed for ``resolved_calendar_id=None``, which is the exact
        defect Bug-6685 reported (the API reporting null for a variant whose
        stored row has the value).
        """
        from src.api import measures as measures_mod

        keywords = _call_keywords(measures_mod._build_response, "MeasureResponse")
        assert keywords, (
            "no MeasureResponse(...) call found in _build_response — the guard "
            "cannot verify anything, so it fails closed (Bug-6685)"
        )
        bound = {kw.arg: kw.value for kw in keywords if kw.arg}
        for field in ("resolved_calendar_id", "resolved_date_col_id"):
            assert field in bound, (
                f"MeasureResponse is built without {field}, so the API reports "
                "it as null and a client inspecting a variant's resolved "
                "calendar is misled (Bug-6685)"
            )
            value = bound[field]
            assert not (
                isinstance(value, ast.Constant) and value.value is None
            ), (
                f"{field} is hard-bound to None, which is the Bug-6685 defect "
                "with the keyword present"
            )

    def test_response_model_declares_the_fields(self):
        from shared.schemas.pydantic_models import MeasureResponse

        assert "resolved_calendar_id" in MeasureResponse.model_fields
        assert "resolved_date_col_id" in MeasureResponse.model_fields


class TestBug6574SemiAdditiveFamiliesHaveOneOwner:
    def test_measures_imports_the_shared_constant(self):
        from shared.schemas.measure_formats import (
            SEMI_ADDITIVE_INELIGIBLE_FAMILIES as shared_set,
        )
        from src.api import measures as measures_mod

        assert measures_mod.SEMI_ADDITIVE_INELIGIBLE_FAMILIES is shared_set, (
            "api/measures.py holds its own copy of the ineligible-family set "
            "again; the two definitions drift and the API and the validator "
            "then disagree about which variants are semi-additive (Bug-6574)"
        )

    def test_no_local_redefinition_in_measures(self):
        from src.api import measures as measures_mod

        tree = ast.parse(inspect.getsource(measures_mod))
        assignments = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name)
                and t.id.endswith("SEMI_ADDITIVE_INELIGIBLE_FAMILIES")
                for t in node.targets
            )
        ]
        assert assignments == [], (
            f"a local redefinition reappeared at line "
            f"{[a.lineno for a in assignments]}"
        )


class TestBug8568RefusalAndExecutionFailureAreDistinct:
    def test_the_two_sentinels_are_separate_objects(self):
        from src.api import kpis as kpis_mod

        assert kpis_mod._GUARD_REFUSED is not kpis_mod._EVALUATION_ERROR, (
            "a correctness refusal and a router failure share one sentinel "
            "again, so a transient timeout tells the modeller to debug a "
            "correct definition (Bug-8568)"
        )
        assert kpis_mod._is_evaluation_failure(kpis_mod._GUARD_REFUSED)
        assert kpis_mod._is_evaluation_failure(kpis_mod._EVALUATION_ERROR)

    def test_adhoc_dispositions_them_differently(self):
        from src.api import kpis as kpis_mod

        source = inspect.getsource(kpis_mod.evaluate_adhoc)
        assert "if value is _GUARD_REFUSED:" in source, (
            "a definition refusal must still be a 400 about the definition"
        )
        assert "if value is _EVALUATION_ERROR:" in source, (
            "an execution failure must be reported as an execution failure"
        )
        assert "status_code=503" in source, (
            "a failed RUN must not be answered by a DIFFERENT (unfiltered) "
            "query at 200, nor blamed on the modeller's expression"
        )

    def test_no_unreachable_failure_branch_remains_after_the_python_fallback(self):
        """The dead ``result.value is None and _is_evaluation_failure(value)``
        branch inside the ``_COMPILER_UNSUPPORTED`` block could never fire —
        ``value`` IS ``_COMPILER_UNSUPPORTED`` there."""
        from src.api import kpis as kpis_mod

        tree = ast.parse(inspect.getsource(kpis_mod.evaluate_adhoc).lstrip())
        dead = [
            node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (getattr(node.func, "id", None) or getattr(node.func, "attr", None))
            == "_is_evaluation_failure"
            and node.args
            and getattr(node.args[0], "id", None) == "value"
        ]
        assert dead == [], (
            "an unreachable failure branch is back inside the "
            f"_COMPILER_UNSUPPORTED block (line {dead})"
        )


class TestBug9216ModellerPublishControlExists:
    """The publish control a modeller needs, and the two-flag contract.

    Bug-9216 reported "no modeller publish control" alongside empty BI
    catalogues. The endpoints exist; what was unguarded is that they stay
    modeller-gated and keep publication LAYERED on model deploy rather than
    replacing it. (The gateway half — the BI read asking for deployed_only —
    is guarded in the gateway suite; the seed half in
    ``tests/unit/test_seed_bundle_kpi_governance.py``.)
    """

    def _route(self, suffix):
        from src.api import kpis as kpis_mod

        matches = [
            r for r in kpis_mod.router.routes
            if getattr(r, "path", "").endswith(suffix)
        ]
        assert matches, f"no route ending {suffix}"
        return matches[0]

    def test_deploy_and_undeploy_routes_exist(self):
        for suffix in ("/{kpi_id}/deploy", "/{kpi_id}/undeploy"):
            route = self._route(suffix)
            assert "POST" in route.methods
            assert route.dependencies, (
                f"{suffix} lost its role dependency — publication would become "
                "available to any viewer (Bug-9216)"
            )

    def test_publication_is_layered_on_model_deploy_not_a_substitute(self):
        """Publishing a KPI on an UNDEPLOYED model must refuse.

        The snapshot authority withholds it anyway, so allowing the flag would
        leave the modeller believing the KPI is live when it is not.
        """
        source = inspect.getsource(
            __import__("src.api.kpis", fromlist=["deploy_kpi"]).deploy_kpi
        )
        assert "deployed_version_id" in source, (
            "deploy_kpi no longer checks that the MODEL is deployed"
        )
        assert "status_code=409" in source

    def test_list_kpis_filters_on_the_publication_flag(self):
        """The publication filter must be a real ``.where(KPI.is_deployed...)``.

        L7-R4: ``assert "KPI.is_deployed.is_(True)" in source`` is satisfied by
        a comment or a docstring naming the expression, so the guard passed with
        the filter deleted.
        """
        from src.api import kpis as kpis_mod

        chains = _where_call_chains(kpis_mod.list_kpis)
        assert chains, (
            "no .where(...) call found in list_kpis — the guard cannot verify "
            "anything, so it fails closed (Bug-9216)"
        )
        assert any(chain.startswith("KPI.is_deployed") for chain in chains), (
            "deployed_only no longer filters on the publication flag, so an "
            "unpublished KPI reaches BI clients (Bug-9216). Filters found: "
            f"{chains}"
        )

    def test_the_publication_filter_is_gated_on_deployed_only(self):
        """...and it must sit INSIDE the ``if deployed_only:`` branch.

        An ungated publication filter would hide drafts from the MODELLER too,
        which is the mirror-image defect: the builder must keep showing them
        (F-017-05).
        """
        from src.api import kpis as kpis_mod

        gates = _if_nodes_testing_name(kpis_mod.list_kpis, "deployed_only")
        assert gates, "no `if deployed_only` gate found in list_kpis"
        gated = [
            chain
            for gate in gates
            for node in ast.walk(ast.Module(body=gate.body, type_ignores=[]))
            if isinstance(node, ast.Call)
            and getattr(node.func, "attr", None) == "where"
            and node.args
            for chain in [
                _attribute_chain(
                    node.args[0].func if isinstance(node.args[0], ast.Call)
                    else node.args[0]
                )
            ]
        ]
        assert any(chain.startswith("KPI.is_deployed") for chain in gated), (
            "the is_deployed filter is not inside an `if deployed_only:` "
            f"branch (found gated filters: {gated}) — either BI clients see "
            "unpublished KPIs, or modellers stop seeing their own drafts"
        )
