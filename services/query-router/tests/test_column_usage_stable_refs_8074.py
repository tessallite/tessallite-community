from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.db.models import RouteLog
from src.ir.logical_query import (
    BoundColumnRef,
    BoundDerivedExpression,
    BoundQuery,
    LogicalFilter,
)
from src.logging.query_logger import log_query

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_bind_trace_records_stable_column_ids_for_usage_analytics():
    """Bug-8074: the usage producer records binder-resolved IDs for selected,
    filtered, measured, and derived references instead of relying on SQL names."""
    region_column = uuid.uuid4()
    amount_column = uuid.uuid4()
    derived_column = uuid.uuid4()
    dimension = types.SimpleNamespace(
        id=uuid.uuid4(), name="region", source_column_id=region_column,
        display_column_id=None, is_hierarchy_level=False,
    )
    measure = types.SimpleNamespace(
        id=uuid.uuid4(), name="revenue", source_column_id=amount_column,
        semi_additive_account_column_id=None,
    )
    derived = BoundDerivedExpression(
        occurrence_ids=["select:0"],
        model_id="model-1",
        canonical_sql="UPPER(region_code)",
        expression_fingerprint="expr-1",
        inputs=[BoundColumnRef(
            model_id="model-1", table_id="table-1",
            column_id=str(derived_column), physical_column="region_code",
        )],
    )
    logical = types.SimpleNamespace(
        protocol="jdbc", raw_query="SELECT region, SUM(revenue) FROM sales",
        query_fingerprint="f" * 64, select_expressions=[], select_star=False,
        grain=["region"], requested_measures=["revenue"],
        requested_dimensions=["region"],
        filters=[LogicalFilter("region", "eq", "EMEA")],
        order_by=[], limit=None, offset=None,
    )
    bound = BoundQuery(
        logical_query=logical,
        model=types.SimpleNamespace(id=uuid.uuid4()),
        resolved_dimensions=[dimension],
        resolved_measures=[measure],
        resolved_filters=[LogicalFilter("region", "eq", "EMEA")],
        resolved_dimensions_by_name={"region": dimension},
        bound_derived_expressions=[derived],
    )
    decision = types.SimpleNamespace(
        route_type="source", aggregate_id=None, pocket_id=None,
        rewritten_query="SELECT region, SUM(amount) FROM sales",
        reason="source", security_rules_applied=[],
    )

    added: list[object] = []
    db = AsyncMock()
    db.add = MagicMock(side_effect=added.append)

    async def _flush():
        added[0].id = uuid.uuid4()

    db.flush = AsyncMock(side_effect=_flush)
    db.commit = AsyncMock()

    await log_query(
        db=db, bound_query=bound, decision=decision,
        execution_ms=10, rows_returned=1, bytes_processed=20,
    )

    bind_log = next(
        item for item in added
        if isinstance(item, RouteLog) and item.route_stage == "bind"
    )
    refs = {
        (ref["column_id"], ref["semantic_type"], ref["role"])
        for ref in bind_log.detail["column_usage_refs"]
    }
    assert (str(region_column), "dimension", "select") in refs
    assert (str(region_column), "dimension", "filter") in refs
    assert (str(amount_column), "measure", "measure") in refs
    assert (str(derived_column), "derived_expression", "derived") in refs


async def _bind_detail(bound, decision) -> dict:
    """Run log_query against a stub session and return the bind RouteLog detail."""
    added: list[object] = []
    db = AsyncMock()
    db.add = MagicMock(side_effect=added.append)

    async def _flush():
        added[0].id = uuid.uuid4()

    db.flush = AsyncMock(side_effect=_flush)
    db.commit = AsyncMock()

    await log_query(
        db=db, bound_query=bound, decision=decision,
        execution_ms=10, rows_returned=1, bytes_processed=20,
    )
    bind_log = next(
        item for item in added
        if isinstance(item, RouteLog) and item.route_stage == "bind"
    )
    return bind_log.detail


def _decision():
    return types.SimpleNamespace(
        route_type="source", aggregate_id=None, pocket_id=None,
        rewritten_query="SELECT 1", reason="source", security_rules_applied=[],
    )


def _logical(measures, dimensions, filters=()):
    return types.SimpleNamespace(
        protocol="jdbc", raw_query="SELECT 1", query_fingerprint="f" * 64,
        select_expressions=[], select_star=False, grain=list(dimensions),
        requested_measures=list(measures), requested_dimensions=list(dimensions),
        filters=list(filters), order_by=[], limit=None, offset=None,
    )


@pytest.mark.asyncio
async def test_calculated_measure_is_recorded_as_a_semantic_object_ref():
    """Bug-8483: a calculated measure has no source_column_id, so the physical
    layer records nothing for it. Without its IDENTITY on the bind trace, the
    consumer cannot know it was used at all and the physical columns feeding it
    report zero usage - a modeller can then drop one and break the measure."""
    profit_id = uuid.uuid4()
    profit = types.SimpleNamespace(
        id=profit_id, name="Profit", source_column_id=None,
        semi_additive_account_column_id=None, measure_type="calculated",
        expression='measure("Revenue") - measure("Cost")',
    )
    bound = BoundQuery(
        logical_query=_logical(["Profit"], []),
        model=types.SimpleNamespace(id=uuid.uuid4()),
        resolved_dimensions=[], resolved_measures=[profit], resolved_filters=[],
    )

    detail = await _bind_detail(bound, _decision())

    assert detail["column_usage_refs"] == []
    assert detail["semantic_object_refs"] == [{
        "object_type": "measure",
        "object_id": str(profit_id),
        "object_name": "Profit",
        "role": "measure",
    }]


@pytest.mark.asyncio
async def test_uda_backed_dimension_is_recorded_as_a_semantic_object_ref():
    """Bug-8483, dimension half: a UDA-backed dimension's physical inputs live
    on the attribute, so binding produces no column for it either."""
    dim_id = uuid.uuid4()
    dimension = types.SimpleNamespace(
        id=dim_id, name="UdaDim", source_column_id=None, display_column_id=None,
        user_defined_attribute_id=uuid.uuid4(), is_hierarchy_level=False,
    )
    bound = BoundQuery(
        logical_query=_logical([], ["UdaDim"]),
        model=types.SimpleNamespace(id=uuid.uuid4()),
        resolved_dimensions=[dimension], resolved_measures=[], resolved_filters=[],
    )

    detail = await _bind_detail(bound, _decision())

    assert detail["column_usage_refs"] == []
    assert detail["semantic_object_refs"] == [{
        "object_type": "dimension",
        "object_id": str(dim_id),
        "object_name": "UdaDim",
        "role": "select",
    }]


@pytest.mark.asyncio
async def test_a_directly_bound_field_is_not_double_recorded():
    """A measure or dimension that DID bind to a physical column must appear in
    the physical layer only. Listing it in both would make the consumer count
    the same reference twice once it expands the closure."""
    region_column = uuid.uuid4()
    amount_column = uuid.uuid4()
    dimension = types.SimpleNamespace(
        id=uuid.uuid4(), name="region", source_column_id=region_column,
        display_column_id=None, is_hierarchy_level=False,
    )
    measure = types.SimpleNamespace(
        id=uuid.uuid4(), name="revenue", source_column_id=amount_column,
        semi_additive_account_column_id=None,
    )
    bound = BoundQuery(
        logical_query=_logical(["revenue"], ["region"]),
        model=types.SimpleNamespace(id=uuid.uuid4()),
        resolved_dimensions=[dimension], resolved_measures=[measure],
        resolved_filters=[],
    )

    detail = await _bind_detail(bound, _decision())

    assert len(detail["column_usage_refs"]) == 2
    assert detail["semantic_object_refs"] == []


@pytest.mark.asyncio
async def test_a_dimension_bound_only_through_its_display_column_is_not_covered():
    """Bug-8697 (AKA source Bug-8737): a dimension whose ``source_column_id`` is
    empty but whose ``display_column_id`` points at a physical column is NOT
    covered. The caption column is what is shown INSTEAD of the dimension's
    value, not the value itself, so it is auxiliary — exactly as a measure's
    account/date columns are auxiliary under Bug-8483.

    Reachable state: both FKs are ``ON DELETE SET NULL`` (``models.py``
    ``Dimension.source_column_id`` / ``.display_column_id``), so dropping the
    key column from the source nulls the key and leaves the caption behind.

    The dimension must emit a ``semantic_object_ref`` so the consumer's
    dependency closure (``impact_usage.py``) runs and credits every column the
    dimension really reads. Marking it covered suppressed that expansion and
    reported ZERO usage for columns the dimension cannot run without."""
    display_column = uuid.uuid4()
    dimension = types.SimpleNamespace(
        id=uuid.uuid4(), name="region", source_column_id=None,
        display_column_id=display_column, is_hierarchy_level=False,
    )
    bound = BoundQuery(
        logical_query=_logical([], ["region"]),
        model=types.SimpleNamespace(id=uuid.uuid4()),
        resolved_dimensions=[dimension], resolved_measures=[], resolved_filters=[],
    )

    detail = await _bind_detail(bound, _decision())

    # No physical ref: the dimension's own value never reached a column.
    assert detail["column_usage_refs"] == []
    # The semantic object IS recorded, so the consumer closure expands it. The
    # closure walks ``display_column_id`` too, so the caption is still credited
    # exactly once and the two lists keep partitioning the references.
    assert detail["semantic_object_refs"] == [{
        "object_type": "dimension",
        "object_id": str(dimension.id),
        "object_name": "region",
        "role": "select",
    }]


@pytest.mark.asyncio
async def test_a_key_bound_dimension_still_credits_its_display_column():
    """Bug-8697 non-regression: when the dimension IS covered by its own key
    column, the caption column stays an ordinary additional physical reference
    under the ``*_display`` role and no semantic object is emitted."""
    key_column = uuid.uuid4()
    display_column = uuid.uuid4()
    dimension = types.SimpleNamespace(
        id=uuid.uuid4(), name="region", source_column_id=key_column,
        display_column_id=display_column, is_hierarchy_level=False,
    )
    bound = BoundQuery(
        logical_query=_logical([], ["region"]),
        model=types.SimpleNamespace(id=uuid.uuid4()),
        resolved_dimensions=[dimension], resolved_measures=[], resolved_filters=[],
    )

    detail = await _bind_detail(bound, _decision())

    assert {(r["column_id"], r["role"]) for r in detail["column_usage_refs"]} == {
        (str(key_column), "select"),
        (str(display_column), "select_display"),
    }
    assert detail["semantic_object_refs"] == []


@pytest.mark.asyncio
async def test_measure_wrapped_as_a_dimension_keeps_its_measure_identity():
    """The binder wraps a measure referenced outside an aggregate as a synthetic
    dimension carrying the MEASURE's id. Recording it as a dimension would send
    the consumer looking up a dimension that does not exist."""
    measure_id = uuid.uuid4()
    wrapped = types.SimpleNamespace(
        id=measure_id, name="Profit", source_column_id=None,
        display_column_id=None, is_measure_as_dimension=True,
        is_hierarchy_level=False,
    )
    bound = BoundQuery(
        logical_query=_logical([], ["Profit"]),
        model=types.SimpleNamespace(id=uuid.uuid4()),
        resolved_dimensions=[wrapped], resolved_measures=[], resolved_filters=[],
    )

    detail = await _bind_detail(bound, _decision())

    assert detail["semantic_object_refs"] == [{
        "object_type": "measure",
        "object_id": str(measure_id),
        "object_name": "Profit",
        "role": "select",
    }]


@pytest.mark.asyncio
async def test_select_and_filter_on_one_unexpanded_field_are_two_references():
    """Roles stay separate for unexpanded objects exactly as they do for bound
    columns, so the consumer's counting contract is the same on both paths."""
    dim_id = uuid.uuid4()
    dimension = types.SimpleNamespace(
        id=dim_id, name="UdaDim", source_column_id=None, display_column_id=None,
        user_defined_attribute_id=uuid.uuid4(), is_hierarchy_level=False,
    )
    query_filter = LogicalFilter("UdaDim", "eq", "EMEA")
    bound = BoundQuery(
        logical_query=_logical([], ["UdaDim"], [query_filter]),
        model=types.SimpleNamespace(id=uuid.uuid4()),
        resolved_dimensions=[dimension], resolved_measures=[],
        resolved_filters=[query_filter],
        resolved_dimensions_by_name={"UdaDim": dimension},
    )

    detail = await _bind_detail(bound, _decision())

    roles = sorted(ref["role"] for ref in detail["semantic_object_refs"])
    assert roles == ["filter", "select"]
    assert {ref["object_id"] for ref in detail["semantic_object_refs"]} == {str(dim_id)}


@pytest.mark.asyncio
async def test_time_variant_measure_records_its_calendar_date_columns():
    """Bug-8696. A time-variant measure reads a calendar date column for its
    period boundaries. Nothing recorded it, so that column showed ZERO usage and
    a modeller could drop it and break every YoY/MTD variant and the KPIs built
    on them - the Bug-8483 harm one shape narrower."""
    amount_column = uuid.uuid4()
    date_column = uuid.uuid4()
    date_dim_column = uuid.uuid4()
    measure = types.SimpleNamespace(
        id=uuid.uuid4(), name="Revenue YoY", source_column_id=amount_column,
        semi_additive_account_column_id=None,
        resolved_date_col_id=date_column,
        date_dimension_column_id=date_dim_column,
    )
    bound = BoundQuery(
        logical_query=_logical(["Revenue YoY"], []),
        model=types.SimpleNamespace(id=uuid.uuid4()),
        resolved_dimensions=[], resolved_measures=[measure], resolved_filters=[],
    )

    detail = await _bind_detail(bound, _decision())

    by_role = {}
    for ref in detail["column_usage_refs"]:
        by_role.setdefault(ref["role"], []).append(ref["column_id"])
    assert by_role["measure"] == [str(amount_column)]
    # Both calendar pointers land under ONE role - they name the same
    # dependency - and both columns are recorded because they differ here.
    assert sorted(by_role["measure_date"]) == sorted(
        [str(date_column), str(date_dim_column)]
    )
    assert "measure_date_dimension" not in by_role
    # It bound its own value column, so it is not an unexpanded object.
    assert detail["semantic_object_refs"] == []


@pytest.mark.asyncio
async def test_a_calculated_measure_with_a_calendar_is_still_reported_unexpanded():
    """A calendar date column is an auxiliary dependency, not the measure's own
    value binding. If it marked the measure covered, a CALCULATED measure that
    happens to carry a calendar would emit no semantic_object_ref and the
    consumer would lose the closure over its real inputs (Bug-8483)."""
    date_column = uuid.uuid4()
    measure_id = uuid.uuid4()
    measure = types.SimpleNamespace(
        id=measure_id, name="Profit YoY", source_column_id=None,
        semi_additive_account_column_id=None,
        resolved_date_col_id=date_column,
        date_dimension_column_id=None,
        measure_type="calculated",
    )
    bound = BoundQuery(
        logical_query=_logical(["Profit YoY"], []),
        model=types.SimpleNamespace(id=uuid.uuid4()),
        resolved_dimensions=[], resolved_measures=[measure], resolved_filters=[],
    )

    detail = await _bind_detail(bound, _decision())

    # It bound no value column, so it belongs to the unexpanded list and must
    # contribute NOTHING to the physical list - including its date column, which
    # its dependency closure already carries. Emitting it in both places counted
    # the same reference twice.
    assert detail["column_usage_refs"] == []
    assert detail["semantic_object_refs"] == [{
        "object_type": "measure",
        "object_id": str(measure_id),
        "object_name": "Profit YoY",
        "role": "measure",
    }]


@pytest.mark.asyncio
async def test_an_unexpanded_object_contributes_no_physical_column_ref():
    """architecture_query-routing.md: column_usage_refs and semantic_object_refs
    PARTITION the query's semantic references - an object appears in exactly one
    of them - which is what lets the consumer credit both without
    double-counting. A calculated measure that carries a calendar violated this:
    it emitted its date column directly AND appeared as an unexpanded object
    whose closure contains that same date column.

    Asserted on the PARTITION itself, not on one shape, so a future object type
    that reaches the same seam is caught here rather than in the numbers."""
    date_column = uuid.uuid4()
    measure_id = uuid.uuid4()
    measure = types.SimpleNamespace(
        id=measure_id, name="Profit YoY", source_column_id=None,
        semi_additive_account_column_id=None,
        resolved_date_col_id=date_column, date_dimension_column_id=None,
        measure_type="calculated",
    )
    bound = BoundQuery(
        logical_query=_logical(["Profit YoY"], []),
        model=types.SimpleNamespace(id=uuid.uuid4()),
        resolved_dimensions=[], resolved_measures=[measure], resolved_filters=[],
    )

    detail = await _bind_detail(bound, _decision())

    unexpanded = {obj["object_id"] for obj in detail["semantic_object_refs"]}
    physical = {ref["semantic_id"] for ref in detail["column_usage_refs"]}
    assert unexpanded, "the calculated measure must still be reported unexpanded"
    assert not (unexpanded & physical), (
        "an object listed in semantic_object_refs also contributed a physical "
        f"column ref, so the consumer will count it twice: {unexpanded & physical}"
    )


@pytest.mark.asyncio
async def test_the_two_producer_lists_partition_a_mixed_query():
    """The partition must hold for a realistic query that mixes shapes: a bound
    dimension, a bound time-variant measure, a UDA-backed dimension and a
    calculated measure in one statement."""
    region_col, amount_col, date_col = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    bound_dim = types.SimpleNamespace(
        id=uuid.uuid4(), name="region", source_column_id=region_col,
        display_column_id=None, is_hierarchy_level=False,
    )
    uda_dim = types.SimpleNamespace(
        id=uuid.uuid4(), name="UdaDim", source_column_id=None,
        display_column_id=None, user_defined_attribute_id=uuid.uuid4(),
        is_hierarchy_level=False,
    )
    bound_measure = types.SimpleNamespace(
        id=uuid.uuid4(), name="Revenue YoY", source_column_id=amount_col,
        semi_additive_account_column_id=None, resolved_date_col_id=date_col,
        date_dimension_column_id=None,
    )
    calc_measure = types.SimpleNamespace(
        id=uuid.uuid4(), name="Profit", source_column_id=None,
        semi_additive_account_column_id=None, measure_type="calculated",
    )
    bound = BoundQuery(
        logical_query=_logical(["Revenue YoY", "Profit"], ["region", "UdaDim"]),
        model=types.SimpleNamespace(id=uuid.uuid4()),
        resolved_dimensions=[bound_dim, uda_dim],
        resolved_measures=[bound_measure, calc_measure],
        resolved_filters=[],
    )

    detail = await _bind_detail(bound, _decision())

    unexpanded = {obj["object_id"] for obj in detail["semantic_object_refs"]}
    physical = {ref["semantic_id"] for ref in detail["column_usage_refs"]}
    assert unexpanded == {str(uda_dim.id), str(calc_measure.id)}
    assert physical == {str(bound_dim.id), str(bound_measure.id)}
    assert not (unexpanded & physical)
    # The bound time-variant measure keeps its date column on the direct path.
    assert {ref["role"] for ref in detail["column_usage_refs"]} == {
        "select", "measure", "measure_date",
    }


@pytest.mark.asyncio
async def test_coincident_calendar_pointers_are_one_reference_not_two():
    """Found by LIVE probe, not by a unit test.

    ``resolved_date_col_id`` and ``date_dimension_column_id`` are two schema
    fields naming the SAME dependency, and in a real model they routinely
    resolve to the SAME physical column - modely's ``base_amount_ytd`` has both
    pointing at ``business_date``. Recording them under two separate roles made
    that one dependency two references, so the Column Usage panel showed
    hit_count=2 for a single query, while a model whose two fields happen to
    differ showed 1 each. Same reference, different number, depending only on
    how the model is wired."""
    amount_column = uuid.uuid4()
    shared_date_column = uuid.uuid4()
    measure = types.SimpleNamespace(
        id=uuid.uuid4(), name="base_amount_ytd", source_column_id=amount_column,
        semi_additive_account_column_id=None,
        resolved_date_col_id=shared_date_column,
        date_dimension_column_id=shared_date_column,
    )
    bound = BoundQuery(
        logical_query=_logical(["base_amount_ytd"], []),
        model=types.SimpleNamespace(id=uuid.uuid4()),
        resolved_dimensions=[], resolved_measures=[measure], resolved_filters=[],
    )

    detail = await _bind_detail(bound, _decision())

    date_refs = [
        ref for ref in detail["column_usage_refs"]
        if ref["column_id"] == str(shared_date_column)
    ]
    assert len(date_refs) == 1, (
        "the same calendar column was recorded twice for one query: "
        f"{date_refs}"
    )
    assert date_refs[0]["role"] == "measure_date"


# ---------------------------------------------------------------------------
# Round-3 deep-review promotions.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_auxiliary_column_does_not_mark_a_measure_covered():
    """An account column is an AUXILIARY dependency, not the measure's value
    binding - the same argument round 2 applied to the calendar columns. A
    UDA-backed (or calculated) semi-additive measure binds no value column, so
    it MUST still appear in semantic_object_refs; otherwise its UDA/calc inputs
    are never expanded and report zero usage (Bug-8483, unfixed for this shape).
    Its closure already carries semi_additive_account_column_id, so it must
    contribute NO direct ref either."""
    measure_id = uuid.uuid4()
    measure = types.SimpleNamespace(
        id=measure_id, name="Balance", source_column_id=None,
        semi_additive_account_column_id=uuid.uuid4(),
        user_defined_attribute_id=uuid.uuid4(),
        semi_additive_behavior="by_account",
    )
    bound = BoundQuery(
        logical_query=_logical(["Balance"], []),
        model=types.SimpleNamespace(id=uuid.uuid4()),
        resolved_dimensions=[], resolved_measures=[measure], resolved_filters=[],
    )

    detail = await _bind_detail(bound, _decision())

    assert detail["semantic_object_refs"] == [{
        "object_type": "measure", "object_id": str(measure_id),
        "object_name": "Balance", "role": "measure",
    }]
    assert detail["column_usage_refs"] == []


@pytest.mark.asyncio
async def test_a_bound_semi_additive_measure_still_records_its_account_column():
    """The other half: a measure that DID bind its value column keeps recording
    the account column directly, so the semi-additive dependency is not lost."""
    value_col, acct_col = uuid.uuid4(), uuid.uuid4()
    measure = types.SimpleNamespace(
        id=uuid.uuid4(), name="Balance", source_column_id=value_col,
        semi_additive_account_column_id=acct_col,
        semi_additive_behavior="by_account",
    )
    bound = BoundQuery(
        logical_query=_logical(["Balance"], []),
        model=types.SimpleNamespace(id=uuid.uuid4()),
        resolved_dimensions=[], resolved_measures=[measure], resolved_filters=[],
    )

    detail = await _bind_detail(bound, _decision())

    by_role = {ref["role"]: ref["column_id"] for ref in detail["column_usage_refs"]}
    assert by_role["measure"] == str(value_col)
    assert by_role["measure_account"] == str(acct_col)
    assert detail["semantic_object_refs"] == []


@pytest.mark.asyncio
async def test_the_partition_holds_when_one_object_is_bound_in_two_roles():
    """binder.py's bare-reference wrapper copies only source_column_id and
    user_defined_attribute_id, never the account column. A measure that binds
    ONLY an account column was therefore 'covered' in its measure role and
    'unexpanded' in its select role, so the SAME object landed in BOTH producer
    lists and the consumer credited that column twice."""
    acct_column, uda_id, measure_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    measure = types.SimpleNamespace(
        id=measure_id, name="Balance", source_column_id=None,
        semi_additive_account_column_id=acct_column,
        user_defined_attribute_id=uda_id, semi_additive_behavior="by_account",
    )
    wrapper = types.SimpleNamespace(
        id=measure_id, name="Balance", source_column_id=None,
        display_column_id=None, user_defined_attribute_id=uda_id,
        is_measure_as_dimension=True, is_hierarchy_level=False,
    )
    bound = BoundQuery(
        logical_query=_logical(["Balance"], ["Balance"]),
        model=types.SimpleNamespace(id=uuid.uuid4()),
        resolved_dimensions=[wrapper], resolved_measures=[measure],
        resolved_filters=[],
    )

    detail = await _bind_detail(bound, _decision())

    unexpanded = {o["object_id"] for o in detail["semantic_object_refs"]}
    physical = {r["semantic_id"] for r in detail["column_usage_refs"]}
    assert not (unexpanded & physical), (
        "architecture_query-routing.md: the two lists must partition the query's "
        f"semantic references; overlap={unexpanded & physical}"
    )


@pytest.mark.asyncio
async def test_one_where_predicate_is_one_reference_not_two():
    """A resolvable predicate alongside an unresolvable one puts the SAME
    dimension in resolved_filters AND where_referenced_dimensions (Bug-5488), so
    ``WHERE region='EMEA' AND UPPER(payment_method)='CARD'`` recorded region's
    column twice for one predicate. The number then depended on whether an
    unrelated conjunct happened to be representable."""
    region_col = uuid.uuid4()
    region = types.SimpleNamespace(
        id=uuid.uuid4(), name="region", source_column_id=region_col,
        display_column_id=None, is_hierarchy_level=False,
    )
    payment = types.SimpleNamespace(
        id=uuid.uuid4(), name="payment_method", source_column_id=uuid.uuid4(),
        display_column_id=None, is_hierarchy_level=False,
    )
    f = LogicalFilter("region", "eq", "EMEA")
    bound = BoundQuery(
        logical_query=_logical([], [], [f]),
        model=types.SimpleNamespace(id=uuid.uuid4()),
        resolved_dimensions=[], resolved_measures=[], resolved_filters=[f],
        resolved_dimensions_by_name={"region": region, "payment_method": payment},
        where_referenced_dimensions={"region", "payment_method"},
    )

    detail = await _bind_detail(bound, _decision())

    region_refs = [
        r for r in detail["column_usage_refs"] if r["column_id"] == str(region_col)
    ]
    assert len(region_refs) == 1, f"one WHERE predicate recorded twice: {region_refs}"
    # The genuinely unfiltered WHERE dimension still gets its own reference.
    assert any(r["role"] == "where" for r in detail["column_usage_refs"])


@pytest.mark.asyncio
async def test_a_measure_wrapped_as_a_dimension_is_typed_as_a_measure_everywhere():
    """The persisted trace must not say ``semantic_type: dimension`` next to a
    MEASURE id. A future consumer of semantic_type would look up a dimension
    that does not exist."""
    measure_id = uuid.uuid4()
    column_id = uuid.uuid4()
    wrapper = types.SimpleNamespace(
        id=measure_id, name="Profit", source_column_id=column_id,
        display_column_id=None, is_measure_as_dimension=True,
        is_hierarchy_level=False,
    )
    bound = BoundQuery(
        logical_query=_logical([], ["Profit"]),
        model=types.SimpleNamespace(id=uuid.uuid4()),
        resolved_dimensions=[wrapper], resolved_measures=[], resolved_filters=[],
    )

    detail = await _bind_detail(bound, _decision())

    assert [r["semantic_type"] for r in detail["column_usage_refs"]] == ["measure"]
