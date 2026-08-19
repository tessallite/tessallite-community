"""Loader contract tests (Bug-7787, spec §6.2, §5.5, §12.3).

Drives ``ModelDependencyLoader`` with a fake AsyncSession that returns
purpose-built ORM instances, and asserts the loader:

- resolves calc-measure ``measure("name")`` refs to measure IDs,
- resolves KPI expression measure/dimension refs to IDs,
- resolves named-list ``builder_definition`` names to IDs,
- resolves calc-dimension column refs to column IDs,
- resolves aggregate grain names to dimension IDs,
- POPULATES ``unresolved_definitions`` on a parse failure (fail closed),
- builds the same-project cross-model reverse index (§12.3),
- never crosses into a different project.

These are producer-derived contract assertions: the loader is the sole producer
of the ``ModelDependencySnapshot`` the engine consumes, so a resolution
regression here silently corrupts every downstream impact result.
"""
from __future__ import annotations

import uuid

import pytest
from .result_fakes import FakeScalarResult

from shared.db.models import (
    AggregateColumn,
    AggregateDefinition,
    DataSource,
    DataTarget,
    Dimension,
    KPI,
    Measure,
    Model,
    ModelColumn,
    ModelTable,
    NamedSet,
    PocketDefinition,
    PocketPredicate,
)
from src.dependencies.loader import ModelDependencyLoader

PROJECT = uuid.uuid4()
MODEL = uuid.uuid4()
OTHER_MODEL = uuid.uuid4()

TB = uuid.uuid4()
COL_GROSS = uuid.uuid4()
COL_KEY = uuid.uuid4()
SRC = uuid.uuid4()
TGT = uuid.uuid4()
MSR_GROSS = uuid.uuid4()
MSR_NET = uuid.uuid4()        # calculated, measure("Gross Sales")
MSR_BROKEN = uuid.uuid4()     # calculated, unparseable expression
DIM_CUST = uuid.uuid4()
DIM_CALC = uuid.uuid4()       # calc dimension over gross_amount
KPI_MARGIN = uuid.uuid4()     # expression measure("Net Sales") dimension("Customer")
NL_TOP = uuid.uuid4()         # builder_definition entity=Customer measure=Gross Sales
AGG = uuid.uuid4()
AGG_COL = uuid.uuid4()
POCKET = uuid.uuid4()         # defining_sql over the orders table, gross_amount
OTHER_MSR = uuid.uuid4()      # in OTHER_MODEL, references MSR_GROSS
TGT2 = TGT


def _model() -> Model:
    m = Model(id=MODEL, project_id=PROJECT, slug="modelx", display_name="Model X", seed="s")
    m.dependency_revision = 7
    m.target_id = TGT
    return m


def _rows() -> dict[type, list]:
    src = DataSource(id=SRC, model_id=MODEL, project_connection_id=uuid.uuid4(),
                     source_type="jdbc", display_name="Warehouse")
    tgt = DataTarget(id=TGT, model_id=MODEL, project_connection_id=uuid.uuid4(),
                     target_type="jdbc", display_name="Target")
    tbl = ModelTable(id=TB, model_id=MODEL, source_id=SRC, table_type="fact",
                     physical_name="orders", alias="orders", display_name="Orders")
    col_gross = ModelColumn(id=COL_GROSS, model_table_id=TB, column_name="gross_amount",
                            display_name="Gross amount", data_type="numeric")
    col_key = ModelColumn(id=COL_KEY, model_table_id=TB, column_name="customer_id",
                          display_name="Customer id", data_type="int")
    msr_gross = Measure(id=MSR_GROSS, model_id=MODEL, name="Gross Sales",
                        display_name="Gross Sales", source_column_id=COL_GROSS,
                        measure_type="standard")
    msr_net = Measure(id=MSR_NET, model_id=MODEL, name="Net Sales", display_name="Net Sales",
                      measure_type="calculated", expression='measure("Gross Sales") * 0.9')
    msr_broken = Measure(id=MSR_BROKEN, model_id=MODEL, name="Broken", display_name="Broken",
                         measure_type="calculated", expression="this is ((not valid")
    dim_cust = Dimension(id=DIM_CUST, model_id=MODEL, name="Customer", display_name="Customer",
                         source_column_id=COL_KEY)
    dim_calc = Dimension(id=DIM_CALC, model_id=MODEL, name="Order Bucket",
                         display_name="Order Bucket",
                         calc_expression="CASE WHEN gross_amount > 0 THEN 'a' ELSE 'b' END",
                         calc_expression_tables=[str(TB)])
    kpi = KPI(id=KPI_MARGIN, model_id=MODEL, name="Margin", display_name="Margin",
              expression='safe_div(measure("Net Sales"), measure("Gross Sales"))')
    nl = NamedSet(id=NL_TOP, model_id=MODEL, name="Top Customers", display_name="Top Customers",
                  expression="{}", builder_definition={"entity": "Customer", "measure": "Gross Sales"})
    agg = AggregateDefinition(id=AGG, model_id=MODEL, target_id=TGT,
                              physical_table_name="agg_cust", status="active",
                              grain=["Customer"])
    agg_col = AggregateColumn(id=AGG_COL, aggregate_definition_id=AGG, measure_id=MSR_GROSS,
                              physical_col_name="gross_agg", stat_type="sum")
    pocket = PocketDefinition(id=POCKET, model_id=MODEL, target_id=TGT,
                              physical_table_name="pocket_orders",
                              defining_sql="SELECT gross_amount FROM orders WHERE gross_amount > 0",
                              query_fingerprint="fp", predicate_set_hash="ph", status="stale")
    other = Measure(id=OTHER_MSR, model_id=OTHER_MODEL, name="Other Gross",
                    display_name="Other Gross", measure_type="standard",
                    cross_model_source_model_id=MODEL,
                    cross_model_source_measure_id=MSR_GROSS)
    return {
        DataSource: [src], DataTarget: [tgt], ModelTable: [tbl],
        ModelColumn: [col_gross, col_key], Measure: [msr_gross, msr_net, msr_broken],
        Dimension: [dim_cust, dim_calc], KPI: [kpi], NamedSet: [nl],
        AggregateDefinition: [agg], AggregateColumn: [agg_col],
        PocketDefinition: [pocket], PocketPredicate: [],
        _OtherMeasure: [other],
    }


class _OtherMeasure:  # marker key for the cross-model reverse-index query
    pass


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return FakeScalarResult(self._rows)

    def all(self):
        return list(self._rows)


class FakeSession:
    """Minimal AsyncSession stand-in that resolves ``select(Entity)`` queries by
    the primary entity, plus ``db.get(Entity, pk)``. Deterministic and
    dependency-free so the loader's resolution logic is tested in isolation."""

    def __init__(self, rows: dict[type, list]):
        self._rows = rows
        self.info = {"tenant_id": "acme"}
        self._cross_model_seen = False

    async def get(self, entity, pk):
        for row in self._rows.get(entity, []):
            if getattr(row, "id", None) == pk or getattr(row, "model_id", None) == pk:
                return row
        return None

    async def execute(self, stmt):
        entity = _primary_entity(stmt)
        col_names = _column_names(stmt)
        # Cross-model reverse index: select(Measure) FILTERED (in the WHERE clause,
        # not merely projecting the column) by cross_model_source_model_id.
        if entity is Measure and "cross_model_source_model_id" in _where_text(stmt):
            return _Result(self._rows.get(_OtherMeasure, []))
        if entity is Model and col_names == {"id"}:
            # other_model_ids probe for the cross-model index.
            return _Result([OTHER_MODEL])
        rows = self._rows.get(entity, [])
        return _Result(rows)


def _where_text(stmt) -> str:
    try:
        wc = stmt.whereclause
        return str(wc) if wc is not None else ""
    except Exception:  # noqa: BLE001
        return ""


def _primary_entity(stmt):
    try:
        desc = stmt.column_descriptions
        if desc:
            ent = desc[0].get("entity")
            if ent is not None:
                return ent
            typ = desc[0].get("type")
            return typ
    except Exception:  # noqa: BLE001
        pass
    return None


def _column_names(stmt) -> set:
    try:
        return {d.get("name") for d in stmt.column_descriptions}
    except Exception:  # noqa: BLE001
        return set()


@pytest.mark.asyncio
async def test_loader_resolves_all_references_to_ids():
    db = FakeSession({Model: [_model()], **_rows()})
    loader = ModelDependencyLoader(db)
    snap = await loader.load(PROJECT, MODEL)

    assert snap.dependency_revision == 7
    assert snap.model_default_target_id == str(TGT)

    # calc-measure Net Sales resolves measure("Gross Sales") -> MSR_GROSS.
    net = next(m for m in snap.measures if m.id == str(MSR_NET))
    assert net.calc_reference_ids == (str(MSR_GROSS),)

    # KPI Margin resolves measure("Net Sales") and measure("Gross Sales").
    margin = next(k for k in snap.kpis if k.id == str(KPI_MARGIN))
    assert set(margin.measure_ids) == {str(MSR_NET), str(MSR_GROSS)}

    # Named list resolves entity Customer -> dim, measure Gross Sales -> measure.
    nl = next(n for n in snap.named_lists if n.id == str(NL_TOP))
    assert nl.dimension_ids == (str(DIM_CUST),)
    assert nl.measure_ids == (str(MSR_GROSS),)

    # Calc dimension resolves the gross_amount column ref -> COL_GROSS.
    calc = next(d for d in snap.dimensions if d.id == str(DIM_CALC))
    assert str(COL_GROSS) in calc.calc_expression_column_ids
    assert str(TB) in calc.calc_expression_tables

    # Aggregate grain "Customer" -> DIM_CUST.
    agg = next(a for a in snap.aggregates if a.id == str(AGG))
    assert agg.grain_dimension_ids == (str(DIM_CUST),)

    # Same-project cross-model reverse index (§12.3): OTHER_MODEL's measure ->
    # MSR_GROSS in THIS model.
    assert (str(OTHER_MODEL), str(OTHER_MSR), "Other Gross", str(MSR_GROSS)) in \
        snap.cross_model_measures


@pytest.mark.asyncio
async def test_loader_populates_unresolved_on_parse_failure():
    db = FakeSession({Model: [_model()], **_rows()})
    loader = ModelDependencyLoader(db)
    snap = await loader.load(PROJECT, MODEL)

    # The Broken calc measure's expression cannot parse -> fail closed via
    # unresolved_definitions (NOT silently dropped).
    owners = {(t, oid) for (t, oid, _f, _r) in snap.unresolved_definitions}
    assert ("measure", str(MSR_BROKEN)) in owners
    reasons = {r for (_t, oid, _f, r) in snap.unresolved_definitions if oid == str(MSR_BROKEN)}
    assert any("parse_failure" in r for r in reasons)


@pytest.mark.asyncio
async def test_loader_resolves_pocket_sql_to_model_objects():
    """Pocket defining_sql (SQLGlot parse-only, no source access, §12.7) resolves
    the orders alias + gross_amount column to the pocket's referenced table and
    the measure backed by that column."""
    db = FakeSession({Model: [_model()], **_rows()})
    loader = ModelDependencyLoader(db)
    snap = await loader.load(PROJECT, MODEL)

    pocket = next(p for p in snap.pockets if p.id == str(POCKET))
    assert str(TB) in pocket.referenced_table_ids
    # gross_amount backs the Gross Sales measure -> pocket references it.
    assert str(MSR_GROSS) in pocket.referenced_measure_ids


@pytest.mark.asyncio
async def test_loader_resolves_pocket_sql_by_physical_table_name():
    """A pocket defining_sql that names the PHYSICAL table (not the model alias)
    still resolves to the model table + backed measure (§12.7); matching only the
    alias would silently drop the pocket's edges."""
    rows = _rows()
    # Give the orders table a distinct physical name vs its alias.
    for t in rows[ModelTable]:
        t.physical_name = "public.fact_orders"
        t.alias = "orders"
    # Pocket SQL references the physical (schema-qualified) name.
    rows[PocketDefinition][0].defining_sql = \
        "SELECT gross_amount FROM public.fact_orders WHERE gross_amount > 0"
    db = FakeSession({Model: [_model()], **rows})
    loader = ModelDependencyLoader(db)
    snap = await loader.load(PROJECT, MODEL)
    pocket = next(p for p in snap.pockets if p.id == str(POCKET))
    assert str(TB) in pocket.referenced_table_ids
    assert str(MSR_GROSS) in pocket.referenced_measure_ids
    # No spurious unresolved-table diagnostic for a resolvable physical name.
    assert not any(
        oid == str(POCKET) and r == "pocket_sql_unresolved_table"
        for (_t, oid, _f, r) in snap.unresolved_definitions
    )


@pytest.mark.asyncio
async def test_loader_pocket_from_model_slug_is_not_unresolved():
    """CANONICAL pocket shape: ``SELECT * FROM <model_slug> WHERE ...`` (the only
    FROM the pocket grammar allows). The slug is NOT a model table, so a naive
    resolver fail-closes every valid pocket with a phantom hard unresolved tuple
    and flips a persona-delete preview from acknowledgement_required to
    blocked_unresolved. The loader must treat the slug FROM as the whole model."""
    rows = _rows()
    # _model().slug is 'modelx'. Canonical pocket over the slug.
    rows[PocketDefinition][0].defining_sql = "SELECT * FROM modelx WHERE Customer = 'ACME'"
    rows[PocketPredicate] = []
    db = FakeSession({Model: [_model()], **rows})
    loader = ModelDependencyLoader(db)
    snap = await loader.load(PROJECT, MODEL)
    # No phantom unresolved-table tuple for the slug FROM.
    assert not any(
        oid == str(POCKET) and r == "pocket_sql_unresolved_table"
        for (_t, oid, _f, r) in snap.unresolved_definitions
    )
    pocket = next(p for p in snap.pockets if p.id == str(POCKET))
    # The slug FROM references the whole model -> all tables reachable to the pocket.
    assert str(TB) in pocket.referenced_table_ids
    # The WHERE dimension name still resolves.
    assert str(DIM_CUST) in pocket.referenced_dimension_ids


@pytest.mark.asyncio
async def test_loader_pocket_unknown_table_fails_closed():
    """A pocket SQL that parses but references a table not in the model fails closed
    (§12.7 opaque definition) so a delete of a possibly-referenced object blocks."""
    rows = _rows()
    rows[PocketDefinition][0].defining_sql = "SELECT x FROM some_external_table"
    db = FakeSession({Model: [_model()], **rows})
    loader = ModelDependencyLoader(db)
    snap = await loader.load(PROJECT, MODEL)
    assert any(
        oid == str(POCKET) and r == "pocket_sql_unresolved_table"
        for (_t, oid, _f, r) in snap.unresolved_definitions
    )


@pytest.mark.asyncio
async def test_loader_fails_closed_on_ambiguous_name(monkeypatch):
    """Spec §12.4: a duplicate case-normalized name never resolves to an arbitrary
    match — it fails closed with an 'ambiguous_' unresolved definition. Simulated
    by seeding a second measure with the same name as Gross Sales, referenced by a
    calc measure."""
    rows = _rows()
    dupe = Measure(id=uuid.uuid4(), model_id=MODEL, name="Gross Sales",
                   display_name="Gross Sales (dupe)", measure_type="standard",
                   source_column_id=COL_KEY)
    rows[Measure] = rows[Measure] + [dupe]
    db = FakeSession({Model: [_model()], **rows})
    loader = ModelDependencyLoader(db)
    snap = await loader.load(PROJECT, MODEL)

    # Net Sales references measure("Gross Sales"); that name is now ambiguous, so
    # the calc reference must NOT resolve and must be recorded as ambiguous.
    net = next(m for m in snap.measures if m.id == str(MSR_NET))
    assert net.calc_reference_ids == ()
    reasons = [r for (_t, oid, _f, r) in snap.unresolved_definitions if oid == str(MSR_NET)]
    assert any(r.startswith("ambiguous_measure_name") for r in reasons)


@pytest.mark.asyncio
async def test_loader_row_security_resolves_last_path_segment():
    """Bug-7787 R5/Fable BLOCKER: row-security dimension_path must resolve by the
    LAST dotted segment (matching predicate_compiler._path_to_column and the
    create-time validator), not the first — else the rule binds to the wrong
    dimension and deleting the actually-bound one would not block (§12.6)."""
    from shared.db.models import RowSecurityRule

    rows = _rows()
    # Two dimensions: Customer (existing) and Region (add). The rule's path is
    # "Customer.Region" -> the BOUND attribute is Region (last segment).
    dim_region = Dimension(id=uuid.uuid4(), model_id=MODEL, name="Region",
                           display_name="Region", source_column_id=COL_KEY)
    rows[Dimension] = rows[Dimension] + [dim_region]
    rule = RowSecurityRule(id=uuid.uuid4(), model_id=MODEL, name="RLS",
                           dimension_path="Customer.Region", rule_type="role_predicate")
    rows[RowSecurityRule] = [rule]
    db = FakeSession({Model: [_model()], **rows})
    loader = ModelDependencyLoader(db)
    snap = await loader.load(PROJECT, MODEL)
    rsr = snap.row_security_rules[0]
    assert rsr.dimension_id == str(dim_region.id)  # Region, NOT Customer


@pytest.mark.asyncio
async def test_loader_pocket_resolves_semantic_dimension_name_in_where():
    """A pocket WHERE predicate names a DIMENSION (semantic name), not the backing
    physical column. The loader must resolve it to the dimension so deleting the
    dimension blocks (§5.3 pocket=hard, §12.7)."""
    rows = _rows()
    # Dimension 'Customer' backed by column customer_id (name != column name).
    rows[PocketDefinition][0].defining_sql = \
        "SELECT gross_amount FROM orders WHERE Customer = 'ACME'"
    rows[PocketPredicate] = []
    db = FakeSession({Model: [_model()], **rows})
    loader = ModelDependencyLoader(db)
    snap = await loader.load(PROJECT, MODEL)
    pocket = next(p for p in snap.pockets if p.id == str(POCKET))
    assert str(DIM_CUST) in pocket.referenced_dimension_ids


@pytest.mark.asyncio
async def test_loader_missing_model_raises():
    db = FakeSession({Model: []})
    loader = ModelDependencyLoader(db)
    with pytest.raises(KeyError):
        await loader.load(PROJECT, MODEL)
