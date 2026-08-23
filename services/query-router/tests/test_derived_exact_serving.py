"""T0/T1 tests for Phase-5 derived-grain LIVE EXACT serving.

Spec: architecture_derived-grain-aggregate-routing.md §16 Phase 5 (rollout stages
4 + 5), §8.2 (exact direct read), §14.2 (known-value + adversarial cases).

Phase 5 serves ONLY the EXACT verdict:
  - stage 5: exact expression-key identity (project the built physical key);
  - stage 4: strict bijection relabel (project the detail passenger).
A ROLLUP / SOURCE_ONLY verdict, a near-miss, or the flag OFF -> route to SOURCE,
NEVER a derived read. These tests assert:
  - the exact rewrite emits a direct read (no GROUP BY, no outer aggregate wrap,
    projects the passenger for a relabel and the physical key for an expression
    identity, reads the measure component column directly);
  - every adversarial near-miss (filtered-key quantile, week boundary, EXTRACT
    year-collapse, stale/unverified mapping, N:1 coarsening) yields NO exact serve;
  - the serving adapter builds real CandidateManifest lineage from a manifest.
"""
from __future__ import annotations

import uuid

import pytest

from src.rewrite.derived_exact import (
    AggregateRewriteUnsupported,
    rewrite_for_derived_exact,
)
from src.routing.derived_expression_proof import (
    DIRECT_ATTRIBUTE_RELABEL,
    DIRECT_EXPRESSION_KEY,
    EXACT,
    ROLLUP,
    SOURCE_ONLY,
    CandidateManifest,
    DerivedKeyPlan,
    DerivedServeProof,
    MeasureRequest,
    QueryKeyRequest,
    build_serve_proof,
)
from src.routing.derived_measure_proof import DIRECT, SUM_OF_SUM, MeasureRollupPlan
from src.routing.derived_serving import (
    build_candidate_manifest,
    build_query_key_requests,
)
from src.routing.derived_trust_predicate import TrustInputs

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fakes: minimal stand-ins for the ORM objects the pure builders consume.
# ---------------------------------------------------------------------------


class _FakeMeasure:
    def __init__(self, name, physical, stat="sum"):
        self.name = name
        self.default_agg = stat


class _FakeAggColumn:
    def __init__(self, measure_name, stat, physical):
        class _M:
            pass
        self.measure = _M()
        self.measure.name = measure_name
        self.stat_type = stat
        self.physical_col_name = physical


def _normalise_manifest(grain_keys, attribute_edges, passenger_columns):
    """Bring legacy test fixtures onto the canonical-id contract (spec §2.1/§2.4).

    Auto-derives ``key_id=expr:<fp>`` for an expression grain key that carries only
    a fingerprint, and translates a legacy edge's ``key_grain_fingerprint`` +
    ``relationship_id`` into the new ``attribute_key`` + ``key_grain_key_id`` pair
    (and mirrors ``attribute_key`` onto the matching passenger). This keeps each
    test's behavioural intent while exercising the production vocabulary the
    producer now writes. Fixtures that already specify the new fields are untouched.
    """
    gks = []
    fp_to_id: dict[str, str] = {}
    for gk in (grain_keys or []):
        gk = dict(gk)
        fp = gk.get("expression_fingerprint")
        if not gk.get("key_id"):
            if fp:
                gk["key_id"] = f"expr:{fp}"
                gk.setdefault("kind", "ARTIFACT_EXPRESSION")
        if fp and gk.get("key_id"):
            fp_to_id[fp] = gk["key_id"]
        gks.append(gk)
    edges = []
    ak_by_rel: dict[str, str] = {}
    for e in (attribute_edges or []):
        e = dict(e)
        rel = e.get("relationship_id")
        if rel and not e.get("attribute_key"):
            e["attribute_key"] = f"attr:{rel}"
        if e.get("attribute_key") and rel:
            ak_by_rel[str(rel)] = e["attribute_key"]
        if not e.get("key_grain_key_id"):
            legacy_fp = e.get("key_grain_fingerprint")
            if legacy_fp and legacy_fp in fp_to_id:
                e["key_grain_key_id"] = fp_to_id[legacy_fp]
        edges.append(e)
    passengers = []
    for p in (passenger_columns or []):
        p = dict(p)
        rel = p.get("relationship_id")
        if rel and not p.get("attribute_key") and str(rel) in ak_by_rel:
            p["attribute_key"] = ak_by_rel[str(rel)]
        passengers.append(p)
    # Ensure every edge has a mirrored passenger (identity alignment, §2.4) when a
    # detail_passenger_column is present but no passenger row was supplied.
    have_ak = {p.get("attribute_key") for p in passengers}
    for e in edges:
        ak = e.get("attribute_key")
        if ak and ak not in have_ak and e.get("detail_passenger_column"):
            passengers.append({
                "attribute_key": ak,
                "relationship_id": e.get("relationship_id"),
                "passenger_column": e.get("detail_passenger_column"),
            })
            have_ak.add(ak)
    return gks, edges, passengers


class _FakeAgg:
    def __init__(self, *, grain_keys, attribute_edges=None, passenger_columns=None,
                 columns=None, physical_table="agg_x", schema="acme_aggregates",
                 status="active", is_stale=False, active_run="run-1"):
        self.id = uuid.uuid4()
        grain_keys, attribute_edges, passenger_columns = _normalise_manifest(
            grain_keys, attribute_edges, passenger_columns
        )
        self.grain_keys = grain_keys
        self.attribute_edges = attribute_edges or []
        self.passenger_columns = passenger_columns or []
        self.columns = columns or []
        self.physical_table_name = physical_table
        self.target_schema = schema
        self.status = status
        self.is_stale = is_stale
        self.active_refresh_run_id = active_run
        self.grain = []
        self.grain_physical_cols = []


class _SelExpr:
    def __init__(self, classification, agg_function=None, inner_column=None,
                 alias=None, composable=False):
        self.classification = classification
        self.agg_function = agg_function
        self.inner_column = inner_column
        self.alias = alias
        self.composable = composable


class _LQ:
    def __init__(self, limit=None, offset=None, grain=None, select_expressions=None):
        self.limit = limit
        self.offset = offset
        self.filters = []
        self.order_by = []
        self.having_raw = None
        self.having_columns = []
        self.has_unresolvable_where = False
        self.has_unresolvable_order = False
        self.grain = grain or []
        self.expression_occurrences = []
        self.select_expressions = select_expressions or []
        self.has_complex_sql = False
        self.has_window_functions = False
        self.has_window_aggregate = False
        self.cte_aliases = []


class _BQ:
    def __init__(self, lq, resolved_measures=None):
        self.logical_query = lq
        self.resolved_measures = resolved_measures or []
        self.bound_derived_expressions = []
        self.has_passthrough_expressions = False


async def _aslist(items):
    """Async stand-in for load_active_aggregates."""
    return items


async def _fail_load(*a, **k):
    raise AssertionError("load_active_aggregates must not run")


def _bq_with_explicit_sum():
    """A query SELECT SUM(latency) where latency.default_agg is 'p50'."""
    lq = _LQ(select_expressions=[
        _SelExpr("analytical", agg_function="sum", inner_column="latency"),
    ])
    m = _FakeMeasure("latency", "latency__p50", stat="p50")
    return _BQ(lq, resolved_measures=[m])


# ---------------------------------------------------------------------------
# Stage 5 — exact EXPRESSION-KEY identity: project the built physical key.
# ---------------------------------------------------------------------------


def _agg_expr_key():
    return _FakeAgg(
        grain_keys=[{
            "expression_fingerprint": "fp-month",
            "physical_column": "order_month",
            "input_column_ids": ["col-ts"],
        }],
        columns=[_FakeAggColumn("revenue", "sum", "revenue__sum")],
    )


def test_exact_expression_key_direct_read_projects_physical_key():
    agg = _agg_expr_key()
    proof = DerivedServeProof(
        verdict=EXACT, artifact_id=str(agg.id),
        query_key_plans=[DerivedKeyPlan(
            query_key="order_month", plan=DIRECT_EXPRESSION_KEY,
            artifact_key_fingerprint="fp-month",
        )],
        measure_plans=[MeasureRollupPlan(
            measure_name="revenue", requested_stat="sum", plan=SUM_OF_SUM,
        )],
    )
    sql = rewrite_for_derived_exact(
        bound_query=_BQ(_LQ()), aggregate=agg, proof=proof, target_dialect="postgres",
    )
    upper = sql.upper()
    # §8.2 exact direct read: project the physical key + read the stored sum
    # component directly; NO GROUP BY, NO outer SUM() wrap.
    assert "ORDER_MONTH" in upper
    assert "REVENUE__SUM" in upper
    assert "GROUP BY" not in upper
    assert "SUM(" not in upper  # direct component read, not a re-aggregation
    assert 'AS "order_month"'.upper() in upper or "AS ORDER_MONTH" in upper


# ---------------------------------------------------------------------------
# Stage 4 — verified BIJECTION relabel: project the detail passenger, not key.
# ---------------------------------------------------------------------------


def test_bijection_relabel_projects_passenger_not_key():
    agg = _FakeAgg(
        grain_keys=[{"expression_fingerprint": "fp-cid", "physical_column": "country_id"}],
        columns=[_FakeAggColumn("revenue", "sum", "revenue__sum")],
    )
    proof = DerivedServeProof(
        verdict=EXACT, artifact_id=str(agg.id),
        query_key_plans=[DerivedKeyPlan(
            query_key="country_name", plan=DIRECT_ATTRIBUTE_RELABEL,
            passenger_column="passenger_country_name", artifact_key_fingerprint="fp-cid",
        )],
        measure_plans=[MeasureRollupPlan(
            measure_name="revenue", requested_stat="sum", plan=SUM_OF_SUM,
        )],
    )
    sql = rewrite_for_derived_exact(
        bound_query=_BQ(_LQ()), aggregate=agg, proof=proof, target_dialect="postgres",
    )
    upper = sql.upper()
    # §8.2: project the passenger under the requested alias; still a direct read.
    assert "PASSENGER_COUNTRY_NAME" in upper
    assert "COUNTRY_ID" not in upper  # the id column is NOT projected
    assert "GROUP BY" not in upper
    assert "DISTINCT" not in upper


def test_direct_quantile_at_exact_reads_stored_stat_no_wrap():
    # A verified bijection relabel preserves the tuple, so a DIRECT quantile reads
    # the stored p50 column directly (§7.7) — no outer aggregate around it.
    agg = _FakeAgg(
        grain_keys=[{"expression_fingerprint": "fp-cid", "physical_column": "country_id"}],
        columns=[_FakeAggColumn("latency", "p50", "latency__p50")],
    )
    proof = DerivedServeProof(
        verdict=EXACT, artifact_id=str(agg.id),
        query_key_plans=[DerivedKeyPlan(
            query_key="country_name", plan=DIRECT_ATTRIBUTE_RELABEL,
            passenger_column="passenger_country_name", artifact_key_fingerprint="fp-cid",
        )],
        measure_plans=[MeasureRollupPlan(
            measure_name="latency", requested_stat="p50", plan=DIRECT,
        )],
    )
    sql = rewrite_for_derived_exact(
        bound_query=_BQ(_LQ()), aggregate=agg, proof=proof, target_dialect="postgres",
    )
    upper = sql.upper()
    assert "LATENCY__P50" in upper
    assert "GROUP BY" not in upper


# ---------------------------------------------------------------------------
# Fail-closed: the rewrite refuses everything that is not a pure exact read.
# ---------------------------------------------------------------------------


def test_rewrite_refuses_non_exact_verdict():
    agg = _agg_expr_key()
    proof = DerivedServeProof(verdict=ROLLUP, artifact_id=str(agg.id))
    with pytest.raises(AggregateRewriteUnsupported):
        rewrite_for_derived_exact(
            bound_query=_BQ(_LQ()), aggregate=agg, proof=proof, target_dialect="postgres",
        )


def test_rewrite_refuses_where_having_order_in_v1():
    agg = _agg_expr_key()
    proof = DerivedServeProof(
        verdict=EXACT, artifact_id=str(agg.id),
        query_key_plans=[DerivedKeyPlan(
            query_key="order_month", plan=DIRECT_EXPRESSION_KEY,
            artifact_key_fingerprint="fp-month",
        )],
        measure_plans=[MeasureRollupPlan(
            measure_name="revenue", requested_stat="sum", plan=SUM_OF_SUM,
        )],
    )
    lq_where = _LQ()
    lq_where.filters = [object()]
    with pytest.raises(AggregateRewriteUnsupported):
        rewrite_for_derived_exact(bound_query=_BQ(lq_where), aggregate=agg, proof=proof)
    lq_order = _LQ()
    lq_order.order_by = [("order_month", "asc")]
    with pytest.raises(AggregateRewriteUnsupported):
        rewrite_for_derived_exact(bound_query=_BQ(lq_order), aggregate=agg, proof=proof)
    lq_having = _LQ()
    lq_having.having_raw = "SUM(revenue) > 10"
    with pytest.raises(AggregateRewriteUnsupported):
        rewrite_for_derived_exact(bound_query=_BQ(lq_having), aggregate=agg, proof=proof)


def test_rewrite_refuses_unresolved_physical_key():
    # A grain-key fingerprint that resolves to no physical column -> fail closed.
    agg = _FakeAgg(grain_keys=[{"expression_fingerprint": "fp-month"}],  # no physical_column
                   columns=[_FakeAggColumn("revenue", "sum", "revenue__sum")])
    proof = DerivedServeProof(
        verdict=EXACT, artifact_id=str(agg.id),
        query_key_plans=[DerivedKeyPlan(
            query_key="order_month", plan=DIRECT_EXPRESSION_KEY,
            artifact_key_fingerprint="fp-month",
        )],
        measure_plans=[MeasureRollupPlan("revenue", "sum", SUM_OF_SUM)],
    )
    with pytest.raises(AggregateRewriteUnsupported):
        rewrite_for_derived_exact(bound_query=_BQ(_LQ()), aggregate=agg, proof=proof)


def test_rewrite_refuses_missing_measure_component():
    # The stored stat column is absent -> _phys_expr_for_node returns "NULL" ->
    # fail closed, never emit NULL as if it were the answer.
    agg = _FakeAgg(
        grain_keys=[{"expression_fingerprint": "fp-month", "physical_column": "order_month"}],
        columns=[],  # no revenue__sum column stored
    )
    proof = DerivedServeProof(
        verdict=EXACT, artifact_id=str(agg.id),
        query_key_plans=[DerivedKeyPlan(
            query_key="order_month", plan=DIRECT_EXPRESSION_KEY,
            artifact_key_fingerprint="fp-month",
        )],
        measure_plans=[MeasureRollupPlan("revenue", "sum", SUM_OF_SUM)],
    )
    with pytest.raises(AggregateRewriteUnsupported):
        rewrite_for_derived_exact(bound_query=_BQ(_LQ()), aggregate=agg, proof=proof)


# ---------------------------------------------------------------------------
# Serving adapter: real CandidateManifest lineage from a Phase-3 manifest.
# ---------------------------------------------------------------------------


def test_build_candidate_manifest_carries_real_lineage_and_edges():
    agg = _FakeAgg(
        grain_keys=[{
            "expression_fingerprint": "fp-month",
            "physical_column": "order_month",
            "input_column_ids": ["col-ts"],
            "date_trunc_unit": "month",
        }],
        attribute_edges=[{
            "relationship_id": "rel-1",
            "key_grain_fingerprint": "fp-cid",
            "detail_passenger_column": "passenger_country_name",
            "cardinality": "BIJECTION",
            "manifest_hash": "mh-1",
        }],
        passenger_columns=[{"passenger_column": "passenger_country_name"}],
    )
    cm = build_candidate_manifest(agg=agg, trust_by_relationship={})
    assert "fp-month" in cm.grain_key_fingerprints
    assert cm.grain_key_lineage["fp-month"] == ("col-ts",)
    assert cm.grain_key_units["fp-month"] == "month"
    assert cm.manifest_hash == "mh-1"
    assert cm.attribute_edges[0]["key_grain_fingerprint"] == "fp-cid"


# ---------------------------------------------------------------------------
# Adversarial near-misses at the PROOF layer -> never EXACT (route to source).
# The proof engine is the correctness gate; these assert the Phase-5 servable
# predicate (verdict == EXACT) is FALSE for each.
# ---------------------------------------------------------------------------


def _stale_trust():
    class _Ev:
        id = "ev-stale"
        status = "VERIFIED"
        verifier_version = "v0"
        declaration_hash = "decl-OLD"  # mismatches the live declaration hash
        deployed_version_id = "ver-1"
        deploy_epoch = 1
        artifact_refresh_run_id = "run-1"
        artifact_manifest_hash = "mh-1"
        source_data_version = None
        violation_count = 0
    return TrustInputs(
        declaration_enabled=True, deployed_declaration_hash="decl-NEW",
        evidence=_Ev(), accepted_verifier_version="v0",
        artifact_active_refresh_run_id="run-1", artifact_manifest_hash="mh-1",
        artifact_is_active=True, artifact_is_stale=False,
        bound_deployed_version_id="ver-1", bound_deploy_epoch=1,
        manifest_edge={"cardinality": "BIJECTION", "detail_passenger_column": "p",
                       "key_grain_fingerprint": "fp-cid"},
    )


def test_stale_mapping_is_not_exact_serve():
    agg = _FakeAgg(
        grain_keys=[{"expression_fingerprint": "fp-cid", "physical_column": "country_id"}],
        attribute_edges=[{"relationship_id": "rel-1", "cardinality": "BIJECTION",
                          "detail_passenger_column": "p", "key_grain_fingerprint": "fp-cid",
                          "manifest_hash": "mh-1"}],
    )
    cm = CandidateManifest(
        artifact_id=str(agg.id), is_active=True, is_stale=False,
        active_refresh_run_id="run-1", manifest_hash="mh-1",
        grain_key_fingerprints=frozenset({"fp-cid"}),
        attribute_edges=agg.attribute_edges,
        trust_by_relationship={"rel-1": _stale_trust()},
    )
    proof = build_serve_proof(
        query_keys=[QueryKeyRequest(logical_name="country_name", kind="ATTRIBUTE",
                                    relationship_id="rel-1")],
        measures=[MeasureRequest("revenue", "sum")],
        candidate=cm,
    )
    assert proof.verdict == SOURCE_ONLY  # stale declaration hash -> never served


def test_n1_coarsening_is_not_exact_serve():
    # An N:1 attribute edge is a real coarsening -> ROLLUP, never EXACT (I14).
    # Phase 5 serves EXACT only, so a ROLLUP verdict falls back to source here.
    class _Ev:
        id = "ev1"; status = "VERIFIED"; verifier_version = "v0"
        declaration_hash = "d"; deployed_version_id = "ver-1"; deploy_epoch = 1
        artifact_refresh_run_id = "run-1"; artifact_manifest_hash = "mh-1"
        source_data_version = None; violation_count = 0
    _edge = {"relationship_id": "rel-1", "attribute_key": "attr:rel-1",
             "cardinality": "FUNCTIONAL_N_TO_1",
             "detail_passenger_column": "passenger_country_name",
             "key_grain_key_id": "dim:cid", "declaration_hash": "d"}
    ti = TrustInputs(
        declaration_enabled=True, deployed_declaration_hash="d", evidence=_Ev(),
        accepted_verifier_version="v0", artifact_active_refresh_run_id="run-1",
        artifact_manifest_hash="mh-1", artifact_is_active=True, artifact_is_stale=False,
        bound_deployed_version_id="ver-1", bound_deploy_epoch=1,
        manifest_edge=_edge,
        # Bug-7905: fresh evidence so the trust predicate passes and the proof reaches
        # its N:1 coarsening verdict (this test isolates the ROLLUP path, not the
        # evidence-age gate).
        evidence_age_ok=True,
    )
    cm = CandidateManifest(
        artifact_id="a", is_active=True, is_stale=False, active_refresh_run_id="run-1",
        manifest_hash="mh-1",
        grain_key_ids=frozenset({"dim:cid"}),
        grain_key_lineage_by_id={"dim:cid": ("col-key",)},
        attribute_edges=[_edge], trust_by_relationship={"rel-1": ti},
        edge_by_attribute_key={"attr:rel-1": _edge},
        passenger_by_attribute_key={"attr:rel-1": {"passenger_column": "passenger_country_name",
                                                   "attribute_key": "attr:rel-1",
                                                   "source_column_id": "col-detail"}},
    )
    proof = build_serve_proof(
        query_keys=[QueryKeyRequest(logical_name="country_name", kind="ATTRIBUTE",
                                    relationship_id="rel-1", attribute_key="attr:rel-1",
                                    input_column_ids=("col-key", "col-detail"),
                                    declaration_hash="d")],
        measures=[MeasureRequest("revenue", "sum")],
        candidate=cm,
    )
    assert proof.verdict == ROLLUP  # N:1 is a coarsening; Phase 5 will not serve it
    assert proof.verdict != EXACT


# ---------------------------------------------------------------------------
# Router flag gate: mode OFF -> byte-identical (None); mode SERVE + EXACT ->
# derived RouteDecision. Tests _try_derived_exact_route directly (no full DB).
# ---------------------------------------------------------------------------


class _BDE:
    """Minimal BoundDerivedExpression stand-in with a GROUP_KEY occurrence."""
    def __init__(self, fingerprint, canonical, occ_id, inputs, phys_inputs=None):
        self.occurrence_ids = [occ_id]
        self.canonical_sql = canonical
        self.expression_fingerprint = fingerprint
        self.semantic_context = {}

        class _Ref:
            def __init__(self, cid, phys):
                self.column_id = cid
                self.physical_column = phys
        phys_inputs = phys_inputs or [None] * len(inputs)
        self.inputs = [_Ref(c, p) for c, p in zip(inputs, phys_inputs)]


class _Occ:
    def __init__(self, occ_id, role, output_alias=None):
        self.occurrence_id = occ_id
        self.role = role
        self.output_alias = output_alias


class _Model:
    def __init__(self):
        self.id = uuid.uuid4()
        self.deployed_version_id = "ver-1"
        self.deploy_epoch = 1


def _derived_bound_query(occ_alias=None, phys_inputs=("order_date",)):
    lq = _LQ()
    lq.expression_occurrences = [_Occ("occ-1", "GROUP_KEY", output_alias=occ_alias)]
    bq = _BQ(lq, resolved_measures=[_FakeMeasure("revenue", "revenue__sum", "sum")])
    bq.bound_derived_expressions = [
        _BDE("fp-month", "DATE_TRUNC('month', order_date)", "occ-1",
             ["col-ts"], phys_inputs=list(phys_inputs)),
    ]
    bq.model = _Model()
    return bq


@pytest.mark.asyncio
async def test_router_derived_route_is_noop_when_killswitch_off(monkeypatch):
    """Kill-switch OFF -> _try_derived_exact_route returns None IMMEDIATELY (before
    any aggregate load), byte-identical routing (operational-serving item 1)."""
    from src.routing import router as R

    async def _get_setting(key, **kw):
        if key == "query.derived_expression_serving_enabled":
            return False  # kill-switch OFF
        return "v0"
    monkeypatch.setattr("shared.config.resolver.get_setting", _get_setting)

    async def _load(model_id, db):
        raise AssertionError("must not load aggregates when the kill-switch is off")
    monkeypatch.setattr("src.semantic.binder.load_active_aggregates", _load)

    bq = _derived_bound_query()
    out = await R._try_derived_exact_route(bq, db=object(), target_dialect="postgres")
    assert out is None


@pytest.mark.asyncio
async def test_router_killswitch_defaults_enabled_on_resolver_error(monkeypatch):
    """A resolver error reading the kill-switch defaults to ENABLED (the trust
    predicate remains the fail-closed authority) — a resolver blip must not
    silently disable a healthy, operationally-validated capability. An ORDINARY
    query still returns None via the shape gate, proving we proceeded past the
    kill-switch even when its read failed."""
    from src.routing import router as R

    async def _get_setting(key, **kw):
        raise RuntimeError("resolver unavailable")
    monkeypatch.setattr("shared.config.resolver.get_setting", _get_setting)

    async def _load(model_id, db):
        raise AssertionError("ordinary query must not reach aggregate load")
    monkeypatch.setattr("src.semantic.binder.load_active_aggregates", _load)

    lq = _LQ()
    bq = _BQ(lq, resolved_measures=[_FakeMeasure("revenue", "revenue__sum", "sum")])
    bq.model = _Model()
    bq.bound_derived_expressions = []  # ordinary query -> shape gate returns None
    out = await R._try_derived_exact_route(bq, db=object(), target_dialect="postgres")
    assert out is None


@pytest.mark.asyncio
async def test_router_derived_route_noop_for_ordinary_query(monkeypatch):
    """A non-derived query (no bound derived expression) is never served here,
    even with the flag on — the shape gate returns None before any aggregate load."""
    from src.routing import router as R

    async def _get_setting(key, **kw):
        return True if key == "query.derived_expression_serving_enabled" else "v0"
    monkeypatch.setattr("shared.config.resolver.get_setting", _get_setting)

    async def _load(model_id, db):
        raise AssertionError("must not load aggregates for an ordinary query")
    monkeypatch.setattr("src.semantic.binder.load_active_aggregates", _load)

    lq = _LQ()
    bq = _BQ(lq, resolved_measures=[_FakeMeasure("revenue", "revenue__sum", "sum")])
    bq.model = _Model()
    bq.bound_derived_expressions = []  # ordinary query
    out = await R._try_derived_exact_route(bq, db=object(), target_dialect="postgres")
    assert out is None


@pytest.mark.asyncio
async def test_router_serves_exact_derived_route_when_killswitch_on(monkeypatch):
    """Kill-switch ON (default) + an EXACT candidate -> derived aggregate
    RouteDecision. Serving is authorised by CURRENT health (the EXACT proof), not
    by a stored serve value (operational-serving item 1)."""
    from src.routing import router as R

    async def _get_setting(key, **kw):
        if key == "query.derived_expression_serving_enabled":
            return True  # kill-switch ON
        return "v0"
    monkeypatch.setattr("shared.config.resolver.get_setting", _get_setting)

    agg = _FakeAgg(
        grain_keys=[{
            "expression_fingerprint": "fp-month", "physical_column": "order_month",
            "input_column_ids": ["col-ts"],
        }],
        columns=[_FakeAggColumn("revenue", "sum", "revenue__sum")],
        status="active",
    )
    agg.model_id = None
    agg.target_id = None

    async def _load(model_id, db):
        return [agg]
    monkeypatch.setattr("src.semantic.binder.load_active_aggregates", _load)

    bq = _derived_bound_query()
    out = await R._try_derived_exact_route(bq, db=object(), target_dialect="postgres")
    assert out is not None
    assert out.route_type == "aggregate"
    assert out.aggregate_id == str(agg.id)
    assert "ORDER_MONTH" in out.rewritten_query.upper()
    assert "GROUP BY" not in out.rewritten_query.upper()
    # Hit credit is deferred to the caller (only on successful execution).
    assert out.pending_hit_credit is agg


@pytest.mark.asyncio
async def test_router_derived_route_none_when_candidate_unhealthy_falls_through(monkeypatch):
    """Operational-serving item 4 (router seam): when the sweep has marked a
    relationship BROKEN/STALE, the trust predicate rejects it and
    ``try_build_exact_proof`` returns None for every candidate. ``_try_derived_exact_
    route`` must then return None WITHOUT raising, so the router falls through to
    ordinary aggregate / source routing (never a forced raw source, never a wrong
    number). Kill-switch is ON — proving health, not the switch, is the gate."""
    from src.routing import router as R

    async def _get_setting(key, **kw):
        return True if key == "query.derived_expression_serving_enabled" else "v0"
    monkeypatch.setattr("shared.config.resolver.get_setting", _get_setting)

    agg = _FakeAgg(
        grain_keys=[{
            "expression_fingerprint": "fp-month", "physical_column": "order_month",
            "input_column_ids": ["col-ts"],
        }],
        columns=[_FakeAggColumn("revenue", "sum", "revenue__sum")],
        status="active",
    )
    agg.model_id = None
    agg.target_id = None

    async def _load(model_id, db):
        return [agg]
    monkeypatch.setattr("src.semantic.binder.load_active_aggregates", _load)

    # Simulate an unhealthy candidate: the trust predicate rejected it (BROKEN/STALE
    # evidence), so no EXACT proof is produced for any candidate.
    async def _no_proof(**kw):
        return None
    monkeypatch.setattr(
        "src.routing.derived_serving.try_build_exact_proof", _no_proof,
    )

    bq = _derived_bound_query()
    out = await R._try_derived_exact_route(bq, db=object(), target_dialect="postgres")
    # Relabel refused -> None -> caller continues to ordinary routing (not raw source).
    assert out is None


# ---------------------------------------------------------------------------
# §9.1 CLS: a restricted KEY or DETAIL column denies a bijection relabel edge.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_relabel_edge_denied_when_key_column_restricted():
    """A relabel whose internal KEY column is CLS-restricted must be denied — the
    label must not launder a restricted key (§9.1)."""
    from src.routing.derived_serving import (
        DerivedServeContext,
        load_candidate_trust_inputs,
    )

    class _Rel:
        def __init__(self):
            self.id = "rel-1"
            self.enabled = True
            self.declaration_hash = "d"
            self.key_column_id = "col-key"
            self.detail_column_id = "col-detail"

    class _Ev:
        id = "ev1"; status = "VERIFIED"; verifier_version = "v0"
        declaration_hash = "d"; deployed_version_id = "ver-1"; deploy_epoch = 1
        artifact_refresh_run_id = "run-1"; artifact_manifest_hash = "mh-1"
        source_data_version = None; violation_count = 0

    class _DB:
        async def get(self, model, rid):
            return _Rel()

        async def execute(self, *a, **k):
            class _R:
                def scalar_one_or_none(self_inner):
                    return _Ev()
            return _R()

    agg = _FakeAgg(
        grain_keys=[{"expression_fingerprint": "fp-cid", "physical_column": "country_id"}],
        attribute_edges=[{"relationship_id": "rel-1", "cardinality": "BIJECTION",
                          "detail_passenger_column": "p", "key_grain_fingerprint": "fp-cid",
                          "manifest_hash": "mh-1"}],
    )
    ctx = DerivedServeContext(
        bound_deployed_version_id="ver-1", bound_deploy_epoch=1,
        accepted_verifier_version="v0", security_ok=True,
        restricted_column_ids=frozenset({"col-key"}),  # KEY column restricted
    )
    trust = await load_candidate_trust_inputs(db=_DB(), agg=agg, ctx=ctx)
    assert trust["rel-1"].security_ok is False  # edge denied by CLS

    # And the proof rejects to SOURCE_ONLY (trust predicate rule 7 fails closed).
    cm = build_candidate_manifest(agg=agg, trust_by_relationship=trust)
    proof = build_serve_proof(
        query_keys=[QueryKeyRequest(logical_name="country_name", kind="ATTRIBUTE",
                                    relationship_id="rel-1")],
        measures=[MeasureRequest("revenue", "sum")],
        candidate=cm, security_ok=True,
    )
    assert proof.verdict == SOURCE_ONLY


# ---------------------------------------------------------------------------
# §9.1 CLS for the EXPRESSION-KEY path: grouping by a function of a restricted
# input column must deny the derived route (the alias must not hide the input).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expression_leaf_restricted_denies_derived_route(monkeypatch):
    """A GROUP BY DATE_TRUNC('month', restricted_ts) must NOT serve when the leaf
    column is CLS-restricted (§9.1) — even though a function grain contributes no
    bare grain dimension to the upstream touched-column set."""
    from src.routing import router as R

    async def _get_setting(key, **kw):
        return True if key == "query.derived_expression_serving_enabled" else "v0"
    monkeypatch.setattr("shared.config.resolver.get_setting", _get_setting)

    # Persona restricts the physical column 'order_date' (the expression leaf).
    async def _restricted_ids(persona, db):
        return frozenset({"col-restricted"})
    monkeypatch.setattr(R, "_persona_restricted_column_ids", _restricted_ids)

    async def _restricted_phys(ids, db):
        return {"order_date"}  # the restricted column resolves to this physical name
    monkeypatch.setattr(R, "_restricted_physical_names", _restricted_phys)

    async def _load(model_id, db):
        raise AssertionError("must not reach aggregate load when leaf is restricted")
    monkeypatch.setattr("src.semantic.binder.load_active_aggregates", _load)

    # The served expression's leaf physical column is 'order_date' -> restricted.
    bq = _derived_bound_query(phys_inputs=("order_date",))

    class _Persona:
        id = uuid.uuid4()
    out = await R._try_derived_exact_route(
        bq, db=object(), target_dialect="postgres", persona=_Persona(),
    )
    assert out is None  # denied -> source, never serves the restricted grain


# ---------------------------------------------------------------------------
# §8.2: the served column uses the query's REQUESTED output alias, not the
# canonical SQL text — byte-for-byte column-name parity with the source path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_served_column_uses_requested_output_alias(monkeypatch):
    from src.routing import router as R

    async def _get_setting(key, **kw):
        return True if key == "query.derived_expression_serving_enabled" else "v0"
    monkeypatch.setattr("shared.config.resolver.get_setting", _get_setting)

    agg = _FakeAgg(
        grain_keys=[{"expression_fingerprint": "fp-month", "physical_column": "order_month",
                     "input_column_ids": ["col-ts"]}],
        columns=[_FakeAggColumn("revenue", "sum", "revenue__sum")],
    )
    agg.model_id = None
    agg.target_id = None

    async def _load(model_id, db):
        return [agg]
    monkeypatch.setattr("src.semantic.binder.load_active_aggregates", _load)

    # The query aliased the derived key as "sales_month".
    bq = _derived_bound_query(occ_alias="sales_month")
    out = await R._try_derived_exact_route(bq, db=object(), target_dialect="postgres")
    assert out is not None
    # The projection alias is the requested output alias, not the canonical SQL.
    assert "SALES_MONTH" in out.rewritten_query.upper()
    assert "DATE_TRUNC" not in out.rewritten_query.upper()


# ---------------------------------------------------------------------------
# Fable R1 findings — tuple completeness, identity, stat, complex-SQL gates.
# ---------------------------------------------------------------------------


def _month_agg():
    return _FakeAgg(
        grain_keys=[{"expression_fingerprint": "fp-month", "physical_column": "order_month",
                     "input_column_ids": ["col-ts"]}],
        columns=[_FakeAggColumn("revenue", "sum", "revenue__sum")],
    )


@pytest.mark.asyncio
async def test_mixed_tuple_expr_plus_bare_dim_is_not_exact(monkeypatch):
    """F1: GROUP BY DATE_TRUNC('month', ts), region against a month-only artifact
    must NOT serve — the bare 'region' key is uncovered -> SOURCE_ONLY."""
    from src.routing import router as R

    async def _get_setting(key, **kw):
        return True if key == "query.derived_expression_serving_enabled" else "v0"
    monkeypatch.setattr("shared.config.resolver.get_setting", _get_setting)
    agg = _month_agg()
    agg.model_id = None
    agg.target_id = None
    monkeypatch.setattr("src.semantic.binder.load_active_aggregates",
                        lambda model_id, db: _aslist([agg]))

    bq = _derived_bound_query()
    bq.logical_query.grain = ["region"]  # extra bare group dimension
    out = await R._try_derived_exact_route(bq, db=object(), target_dialect="postgres")
    assert out is None  # mixed tuple -> not exact -> source


@pytest.mark.asyncio
async def test_extra_physical_artifact_key_blocks_exact(monkeypatch):
    """F2: a (month, region)-grain artifact where region has NO fingerprint must
    NOT be served as EXACT for a GROUP BY month query — region is an uncovered
    artifact key, so the set cover fails -> source."""
    from src.routing import router as R

    async def _get_setting(key, **kw):
        return True if key == "query.derived_expression_serving_enabled" else "v0"
    monkeypatch.setattr("shared.config.resolver.get_setting", _get_setting)
    agg = _FakeAgg(
        grain_keys=[
            {"expression_fingerprint": "fp-month", "physical_column": "order_month",
             "input_column_ids": ["col-ts"]},
            {"key_id": "dim:region", "physical_column": "region"},  # NO fingerprint
        ],
        columns=[_FakeAggColumn("revenue", "sum", "revenue__sum")],
    )
    agg.model_id = None
    agg.target_id = None
    monkeypatch.setattr("src.semantic.binder.load_active_aggregates",
                        lambda model_id, db: _aslist([agg]))

    bq = _derived_bound_query()  # GROUP BY month only
    out = await R._try_derived_exact_route(bq, db=object(), target_dialect="postgres")
    assert out is None  # extra artifact key uncovered -> not exact -> source


@pytest.mark.asyncio
async def test_explicit_aggregate_function_overrides_default_agg(monkeypatch):
    """F3: SUM(latency) where latency.default_agg='p50' must request 'sum', not the
    stored median — the explicit query function wins."""
    from src.routing import router as R
    from src.routing.derived_expression_proof import MeasureRequest

    reqs = R._build_measure_requests(_bq_with_explicit_sum())
    assert reqs is not None
    latency = [r for r in reqs if r.measure_name == "latency"]
    assert latency and latency[0].requested_stat == "sum"


@pytest.mark.asyncio
async def test_complex_sql_query_is_not_served(monkeypatch):
    """F4: a CTE / complex-SQL derived query must never take the derived route
    (inner predicates would be silently dropped)."""
    from src.routing import router as R

    async def _get_setting(key, **kw):
        return True if key == "query.derived_expression_serving_enabled" else "v0"
    monkeypatch.setattr("shared.config.resolver.get_setting", _get_setting)
    monkeypatch.setattr("src.semantic.binder.load_active_aggregates",
                        lambda model_id, db: _fail_load())

    bq = _derived_bound_query()
    bq.logical_query.has_complex_sql = True
    out = await R._try_derived_exact_route(bq, db=object(), target_dialect="postgres")
    assert out is None


@pytest.mark.asyncio
async def test_expression_leaf_lineage_mismatch_is_not_exact(monkeypatch):
    """F6: an exact fingerprint match with a DIFFERENT bound leaf column (same
    canonical name, different relation) must NOT serve -> source (§7.3 cond. 2)."""
    from src.routing import router as R

    async def _get_setting(key, **kw):
        return True if key == "query.derived_expression_serving_enabled" else "v0"
    monkeypatch.setattr("shared.config.resolver.get_setting", _get_setting)
    # Artifact key's stored leaf is col-A; the query expression's leaf is col-B.
    agg = _FakeAgg(
        grain_keys=[{"expression_fingerprint": "fp-month", "physical_column": "order_month",
                     "input_column_ids": ["col-A"]}],
        columns=[_FakeAggColumn("revenue", "sum", "revenue__sum")],
    )
    agg.model_id = None
    agg.target_id = None
    monkeypatch.setattr("src.semantic.binder.load_active_aggregates",
                        lambda model_id, db: _aslist([agg]))

    bq = _derived_bound_query()  # BDE leaf column_id is 'col-ts' (!= col-A)
    out = await R._try_derived_exact_route(bq, db=object(), target_dialect="postgres")
    assert out is None  # lineage mismatch -> fail closed


@pytest.mark.asyncio
async def test_empty_expression_leaf_lineage_is_not_exact(monkeypatch):
    """F6: when the binder left the query expression's leaf column IDs empty
    (Phase-1 default), an exact serve must fail closed -> source."""
    from src.routing import router as R

    async def _get_setting(key, **kw):
        return True if key == "query.derived_expression_serving_enabled" else "v0"
    monkeypatch.setattr("shared.config.resolver.get_setting", _get_setting)
    agg = _month_agg()
    agg.model_id = None
    agg.target_id = None
    monkeypatch.setattr("src.semantic.binder.load_active_aggregates",
                        lambda model_id, db: _aslist([agg]))

    bq = _derived_bound_query()
    # Simulate the Phase-1 binder: leaf column_id is empty.
    bq.bound_derived_expressions[0].inputs[0].column_id = ""
    out = await R._try_derived_exact_route(bq, db=object(), target_dialect="postgres")
    assert out is None  # no stable leaf id -> cannot prove §7.3 cond. 2 -> source


# ---------------------------------------------------------------------------
# R2 reachability (real parser, not mocks): the feature's own target shape
# — GROUP BY DATE_TRUNC(...) — must NOT be rejected by the shape/measure gates.
# ---------------------------------------------------------------------------


def test_measure_requests_do_not_bail_on_the_derived_group_key_real_parser():
    """R2 F1/F2: a real ``SELECT DATE_TRUNC('month', ts), SUM(revenue) GROUP BY
    DATE_TRUNC('month', ts)`` parses to a PASSTHROUGH group-key SELECT item; the
    measure builder must SKIP that key item (not bail) and return the SUM measure.
    Driven through the REAL parser so the binder/parser producer contract is
    covered — the mocks hid this in R1."""
    from src.parsing.sql_parser import parse_sql_to_ir
    from src.routing import router as R

    lq = parse_sql_to_ir(
        "SELECT DATE_TRUNC('month', order_date) AS m, SUM(revenue) AS rev "
        "FROM orders GROUP BY DATE_TRUNC('month', order_date)",
        "m1",
    )
    assert lq.has_function_grain is True  # the feature's target shape
    bq = _BQ(lq, resolved_measures=[_FakeMeasure("revenue", "revenue__sum", "sum")])
    reqs = R._build_measure_requests(bq)
    assert reqs is not None  # must NOT bail on the projected derived key
    assert [(r.measure_name, r.requested_stat) for r in reqs] == [("revenue", "sum")]


def test_measure_requests_explicit_stat_over_default_agg_real_parser():
    """R2/F3 through the real parser: SUM(latency) over a p50-default measure
    requests 'sum', never the stored median."""
    from src.parsing.sql_parser import parse_sql_to_ir
    from src.routing import router as R

    lq = parse_sql_to_ir(
        "SELECT DATE_TRUNC('month', order_date) AS m, SUM(latency) AS l "
        "FROM orders GROUP BY DATE_TRUNC('month', order_date)",
        "m1",
    )
    bq = _BQ(lq, resolved_measures=[_FakeMeasure("latency", "latency__p50", "p50")])
    reqs = R._build_measure_requests(bq)
    assert reqs is not None
    assert reqs[0].requested_stat == "sum"  # explicit function wins


def test_measure_requests_explicit_stat_case_insensitive_join_real_parser():
    """Fable R2 F4: SUM(revenue) where the model's canonical measure name is
    'Revenue' (default_agg p50) must request 'sum', not the stored median — the
    query-spelling vs canonical-name join is case-insensitive (binder parity)."""
    from src.parsing.sql_parser import parse_sql_to_ir
    from src.routing import router as R

    lq = parse_sql_to_ir(
        "SELECT DATE_TRUNC('month', order_date) AS m, SUM(revenue) AS total "
        "FROM orders GROUP BY DATE_TRUNC('month', order_date)",
        "m1",
    )
    # Model measure canonically named 'Revenue' (different case) with p50 default.
    bq = _BQ(lq, resolved_measures=[_FakeMeasure("Revenue", "Revenue__p50", "p50")])
    reqs = R._build_measure_requests(bq)
    assert reqs is not None
    assert reqs[0].requested_stat == "sum"  # NOT p50 (median) under an explicit SUM


def test_compound_aggregate_passthrough_still_bails_real_parser():
    """A compound-aggregate passthrough (SUM(a)/SUM(b)) must still bail to source —
    the exact read cannot serve a composed statistic."""
    from src.parsing.sql_parser import parse_sql_to_ir
    from src.routing import router as R

    lq = parse_sql_to_ir(
        "SELECT DATE_TRUNC('month', order_date) AS m, SUM(a) / SUM(b) AS ratio "
        "FROM orders GROUP BY DATE_TRUNC('month', order_date)",
        "m1",
    )
    bq = _BQ(lq, resolved_measures=[_FakeMeasure("a", "a__sum", "sum")])
    reqs = R._build_measure_requests(bq)
    assert reqs is None  # composed/aggregate passthrough -> source


def _real_bq_and_fp(sql):
    """Build a BoundQuery over the REAL parser with a fingerprint-aligned BDE."""
    from src.parsing.sql_parser import parse_sql_to_ir
    from shared.semantic.derived_expression import canonicalise_sql

    lq = parse_sql_to_ir(sql, "m1")
    fp = canonicalise_sql("DATE_TRUNC('MONTH', order_date)",
                          input_dialect=lq.input_dialect).fingerprint

    class _Ref:
        column_id = "col-ts"
        physical_column = "order_date"

    class _RBDE:
        expression_fingerprint = fp
        canonical_sql = "DATE_TRUNC('MONTH', order_date)"
        occurrence_ids = []
        inputs = [_Ref()]
        semantic_context = {}

    bq = _BQ(lq, resolved_measures=[_FakeMeasure("revenue", "revenue__sum", "sum")])
    bq.bound_derived_expressions = [_RBDE()]
    return bq, fp


def _exact_proof(agg_id, fp):
    from src.routing.derived_measure_proof import SUM_OF_SUM as _S
    return DerivedServeProof(
        verdict=EXACT, artifact_id=str(agg_id),
        query_key_plans=[DerivedKeyPlan(query_key="m", plan=DIRECT_EXPRESSION_KEY,
                                        artifact_key_fingerprint=fp)],
        measure_plans=[MeasureRollupPlan("revenue", "sum", _S)],
    )


def test_select_shape_guard_good_shape_serves_real_parser():
    """The clean target shape (one derived key + one bare SUM) serves."""
    bq, fp = _real_bq_and_fp(
        "SELECT DATE_TRUNC('month', order_date) AS m, SUM(revenue) AS rev "
        "FROM orders GROUP BY DATE_TRUNC('month', order_date)")
    agg = _FakeAgg(grain_keys=[{"expression_fingerprint": fp, "physical_column": "order_month"}],
                   columns=[_FakeAggColumn("revenue", "sum", "revenue__sum")])
    sql = rewrite_for_derived_exact(bound_query=bq, aggregate=agg,
                                    proof=_exact_proof(agg.id, fp), target_dialect="postgres")
    assert "ORDER_MONTH" in sql.upper()
    assert "REVENUE__SUM" in sql.upper()
    assert "GROUP BY" not in sql.upper()


def test_select_shape_guard_drops_nothing_extra_projection_bails():
    """F2a: an extra projection (scalar of the grouped key) must route to source
    rather than be silently dropped."""
    bq, fp = _real_bq_and_fp(
        "SELECT DATE_TRUNC('month', order_date) AS m, "
        "CAST(DATE_TRUNC('month', order_date) AS DATE) AS d, SUM(revenue) AS rev "
        "FROM orders GROUP BY DATE_TRUNC('month', order_date)")
    agg = _FakeAgg(grain_keys=[{"expression_fingerprint": fp, "physical_column": "order_month"}],
                   columns=[_FakeAggColumn("revenue", "sum", "revenue__sum")])
    with pytest.raises(AggregateRewriteUnsupported):
        rewrite_for_derived_exact(bound_query=bq, aggregate=agg,
                                  proof=_exact_proof(agg.id, fp), target_dialect="postgres")


def test_select_shape_guard_wrapped_measure_bails():
    """F2c: a wrapped measure ROUND(SUM(x),2) must route to source (wrapper would be
    dropped and the raw component served)."""
    bq, fp = _real_bq_and_fp(
        "SELECT DATE_TRUNC('month', order_date) AS m, ROUND(SUM(revenue), 2) AS r "
        "FROM orders GROUP BY DATE_TRUNC('month', order_date)")
    agg = _FakeAgg(grain_keys=[{"expression_fingerprint": fp, "physical_column": "order_month"}],
                   columns=[_FakeAggColumn("revenue", "sum", "revenue__sum")])
    with pytest.raises(AggregateRewriteUnsupported):
        rewrite_for_derived_exact(bound_query=bq, aggregate=agg,
                                  proof=_exact_proof(agg.id, fp), target_dialect="postgres")


def test_select_shape_guard_hidden_aggregate_bails():
    """F3: a passthrough hiding an aggregate the proof did not map must route to
    source (the aggregate would be silently dropped)."""
    bq, fp = _real_bq_and_fp(
        "SELECT DATE_TRUNC('month', order_date) AS m, ROUND(SUM(price*qty), 2) AS r, "
        "SUM(revenue) AS total FROM orders GROUP BY DATE_TRUNC('month', order_date)")
    agg = _FakeAgg(grain_keys=[{"expression_fingerprint": fp, "physical_column": "order_month"}],
                   columns=[_FakeAggColumn("revenue", "sum", "revenue__sum")])
    with pytest.raises(AggregateRewriteUnsupported):
        rewrite_for_derived_exact(bound_query=bq, aggregate=agg,
                                  proof=_exact_proof(agg.id, fp), target_dialect="postgres")


def test_duplicate_select_items_preserve_arity_and_labels():
    """Spec §3.4: duplicate projections of the same grouped key are emitted ONE per
    SELECT ordinal under their OWN aliases (source arity/labels preserved), not
    merged to one column. The walk reuses the same proven key plan per ordinal, so
    ``a`` and ``b`` both appear and the arity guard passes (bound output count ==
    SELECT-item count)."""
    bq, fp = _real_bq_and_fp(
        "SELECT DATE_TRUNC('month', order_date) AS a, "
        "DATE_TRUNC('month', order_date) AS b, SUM(revenue) AS rev "
        "FROM orders GROUP BY DATE_TRUNC('month', order_date)")
    agg = _FakeAgg(grain_keys=[{"expression_fingerprint": fp, "physical_column": "order_month"}],
                   columns=[_FakeAggColumn("revenue", "sum", "revenue__sum")])
    sql = rewrite_for_derived_exact(bound_query=bq, aggregate=agg,
                                    proof=_exact_proof(agg.id, fp), target_dialect="postgres")
    up = sql.upper()
    # Both duplicate key projections appear under their own aliases + the measure.
    assert '"A"' in up and '"B"' in up and '"REV"' in up
    assert up.count("ORDER_MONTH") == 2  # one per duplicate key ordinal


def test_measure_served_under_requested_alias_not_measure_name():
    """Fable R3 F2: an aliased measure serves under the query's alias (total_rev),
    not the measure name — column-name parity with source."""
    bq, fp = _real_bq_and_fp(
        "SELECT DATE_TRUNC('month', order_date) AS m, SUM(revenue) AS total_rev "
        "FROM orders GROUP BY DATE_TRUNC('month', order_date)")
    agg = _FakeAgg(grain_keys=[{"expression_fingerprint": fp, "physical_column": "order_month"}],
                   columns=[_FakeAggColumn("revenue", "sum", "revenue__sum")])
    sql = rewrite_for_derived_exact(bound_query=bq, aggregate=agg,
                                    proof=_exact_proof(agg.id, fp), target_dialect="postgres")
    assert "TOTAL_REV" in sql.upper()  # requested alias preserved


def test_unaliased_measure_forces_source_label_parity():
    """Fable R3 F2: a measure with no explicit alias has no faithful source label to
    reproduce -> route to source."""
    bq, fp = _real_bq_and_fp(
        "SELECT DATE_TRUNC('month', order_date) AS m, SUM(revenue) "
        "FROM orders GROUP BY DATE_TRUNC('month', order_date)")
    agg = _FakeAgg(grain_keys=[{"expression_fingerprint": fp, "physical_column": "order_month"}],
                   columns=[_FakeAggColumn("revenue", "sum", "revenue__sum")])
    with pytest.raises(AggregateRewriteUnsupported):
        rewrite_for_derived_exact(bound_query=bq, aggregate=agg,
                                  proof=_exact_proof(agg.id, fp), target_dialect="postgres")


@pytest.mark.asyncio
async def test_select_star_derived_query_routes_to_source(monkeypatch):
    """Fable R3 F4: a SELECT * derived-grain query must route to source (it expands
    to every measure at default_agg and the shape guard cannot vet an empty SELECT
    list — serving it would fabricate an answer for possibly-invalid SQL)."""
    from src.routing import router as R

    async def _get_setting(key, **kw):
        return True if key == "query.derived_expression_serving_enabled" else "v0"
    monkeypatch.setattr("shared.config.resolver.get_setting", _get_setting)

    async def _load(model_id, db):
        raise AssertionError("must not load aggregates for a SELECT * query")
    monkeypatch.setattr("src.semantic.binder.load_active_aggregates", _load)

    bq = _derived_bound_query()
    bq.logical_query.select_star = True
    out = await R._try_derived_exact_route(bq, db=object(), target_dialect="postgres")
    assert out is None


def test_uncovered_group_key_occurrence_forces_source():
    """§7.8: a GROUP_KEY expression occurrence with NO bound derived expression
    (binder failed to bind it) must make the tuple uncoverable -> SOURCE_ONLY, so a
    second unbound grouping is never silently dropped."""
    from src.routing.derived_serving import build_query_key_requests

    lq = _LQ()
    # Two GROUP_KEY occurrences, but only ONE is bound (occ-1).
    lq.expression_occurrences = [
        _Occ("occ-1", "GROUP_KEY", output_alias="m"),
        _Occ("occ-2", "GROUP_KEY", output_alias="flag"),  # unbound
    ]
    bq = _BQ(lq)
    bq.bound_derived_expressions = [
        _BDE("fp-month", "DATE_TRUNC('month', order_date)", "occ-1",
             ["col-ts"], phys_inputs=["order_date"]),
    ]
    keys = build_query_key_requests(bq)
    # One real expression key + one unservable placeholder for the unbound occurrence.
    kinds = [(k.kind, k.fingerprint) for k in keys]
    assert ("EXPRESSION", "fp-month") in kinds
    assert any(k.kind == "EXPRESSION" and k.fingerprint is None for k in keys)
    # And the proof over any candidate rejects (a None-fingerprint key can't cover).
    cm = CandidateManifest(
        artifact_id="a", is_active=True, is_stale=False, active_refresh_run_id="run-1",
        manifest_hash=None, grain_key_fingerprints=frozenset({"fp-month"}),
    )
    proof = build_serve_proof(
        query_keys=keys, measures=[MeasureRequest("revenue", "sum")], candidate=cm,
    )
    assert proof.verdict == SOURCE_ONLY


# ---------------------------------------------------------------------------
# F1: binder stable-UUID leaf binding (§7.1) — reachability + fail-closed.
# These drive the REAL parser -> real binder helper so the producer contract
# (not a mock) is covered; the R1/R2 mocks hardcoded column_id, hiding this.
# ---------------------------------------------------------------------------


def test_binder_populates_stable_leaf_column_id_real_parser():
    """§7.1: the binder resolves a derived-expression leaf's physical column NAME to
    its stable ModelColumn id and sets BoundColumnRef.column_id. Driven through the
    REAL parser occurrences so the producer contract is covered. This is the F1
    reachability fix: with a non-empty leaf id, the §7.3 cond. 2 lineage gate
    (_exact_lineage_matches) now PASSES for a real derived query (previously it
    fail-closed for every query because column_id was always "")."""
    from src.parsing.sql_parser import parse_sql_to_ir
    from src.semantic.binder import _build_bound_derived_expressions
    from src.routing.derived_serving import (
        _exact_lineage_matches,
        build_query_key_requests,
    )
    from src.routing.derived_expression_proof import CandidateManifest

    lq = parse_sql_to_ir(
        "SELECT DATE_TRUNC('month', order_date) AS m, SUM(revenue) AS rev "
        "FROM orders GROUP BY DATE_TRUNC('month', order_date)",
        "m1",
    )
    # Real deployed-snapshot name->id map (the leaf physical column resolves here).
    id_map = {"order_date": "col-order-date-uuid", "revenue": "col-revenue-uuid"}
    bdes = _build_bound_derived_expressions(
        lq.expression_occurrences,
        model_id="m1",
        physical_columns={"order_date", "revenue"},
        physical_column_ids=id_map,
    )
    assert bdes, "the DATE_TRUNC group key must bind to a derived expression"
    leaf_ids = [ref.column_id for bde in bdes for ref in bde.inputs]
    # The order_date leaf now carries its STABLE id (not "").
    assert "col-order-date-uuid" in leaf_ids
    assert all(cid for cid in leaf_ids), "no leaf may keep an empty column_id here"

    # And the §7.3 cond. 2 lineage gate now PASSES against a manifest whose stored
    # grain key carries the SAME leaf id (previously impossible with column_id="").
    class _BQReal:
        logical_query = lq
        bound_derived_expressions = bdes

    keys = build_query_key_requests(_BQReal())
    fp = bdes[0].expression_fingerprint
    _kid = f"expr:{fp}"
    candidate = CandidateManifest(
        artifact_id="a", is_active=True, is_stale=False, active_refresh_run_id="run-1",
        manifest_hash=None, grain_key_fingerprints=frozenset({fp}),
        grain_key_lineage={fp: ("col-order-date-uuid",)},
        grain_key_ids=frozenset({_kid}),
        grain_key_id_by_fingerprint={fp: _kid},
        grain_key_lineage_by_id={_kid: ("col-order-date-uuid",)},
    )
    assert _exact_lineage_matches(keys, candidate) is True


def test_binder_empty_id_map_keeps_leaf_unresolved_and_gate_fails_closed():
    """Guard integrity: with NO id map (or an unresolved leaf name), the leaf keeps
    column_id="" and the lineage gate FAILS CLOSED — proving the F1 fix does not
    weaken _exact_lineage_matches (the fingerprint-collision guard). An unresolved
    leaf is safe (source), never a wrong serve."""
    from src.parsing.sql_parser import parse_sql_to_ir
    from src.semantic.binder import _build_bound_derived_expressions
    from src.routing.derived_serving import (
        _exact_lineage_matches,
        build_query_key_requests,
    )
    from src.routing.derived_expression_proof import CandidateManifest

    lq = parse_sql_to_ir(
        "SELECT DATE_TRUNC('month', order_date) AS m, SUM(revenue) AS rev "
        "FROM orders GROUP BY DATE_TRUNC('month', order_date)",
        "m1",
    )
    bdes = _build_bound_derived_expressions(
        lq.expression_occurrences,
        model_id="m1",
        physical_columns={"order_date", "revenue"},
        physical_column_ids={},  # unavailable -> every leaf keeps column_id=""
    )
    assert bdes
    assert all(ref.column_id == "" for bde in bdes for ref in bde.inputs)

    class _BQReal:
        logical_query = lq
        bound_derived_expressions = bdes

    keys = build_query_key_requests(_BQReal())
    fp = bdes[0].expression_fingerprint
    candidate = CandidateManifest(
        artifact_id="a", is_active=True, is_stale=False, active_refresh_run_id="run-1",
        manifest_hash=None, grain_key_fingerprints=frozenset({fp}),
        grain_key_lineage={fp: ("col-order-date-uuid",)},
    )
    # Empty query-side lineage -> fail closed even though the fingerprint matches.
    assert _exact_lineage_matches(keys, candidate) is False


def test_binder_lineage_id_mismatch_fails_closed_collision_guard():
    """§7.3 cond. 2: even with a NON-empty leaf id, a MISMATCH between the query
    leaf id and the manifest grain-key lineage fails closed — the collision guard
    (two same-named columns in different relations) still bites."""
    from src.parsing.sql_parser import parse_sql_to_ir
    from src.semantic.binder import _build_bound_derived_expressions
    from src.routing.derived_serving import (
        _exact_lineage_matches,
        build_query_key_requests,
    )
    from src.routing.derived_expression_proof import CandidateManifest

    lq = parse_sql_to_ir(
        "SELECT DATE_TRUNC('month', order_date) AS m, SUM(revenue) AS rev "
        "FROM orders GROUP BY DATE_TRUNC('month', order_date)",
        "m1",
    )
    bdes = _build_bound_derived_expressions(
        lq.expression_occurrences,
        model_id="m1",
        physical_columns={"order_date"},
        physical_column_ids={"order_date": "orders.created_at.id"},
    )

    class _BQReal:
        logical_query = lq
        bound_derived_expressions = bdes

    keys = build_query_key_requests(_BQReal())
    fp = bdes[0].expression_fingerprint
    # Manifest key was built over a DIFFERENT physical column (same name, other table).
    candidate = CandidateManifest(
        artifact_id="a", is_active=True, is_stale=False, active_refresh_run_id="run-1",
        manifest_hash=None, grain_key_fingerprints=frozenset({fp}),
        grain_key_lineage={fp: ("returns.created_at.id",)},
    )
    assert _exact_lineage_matches(keys, candidate) is False


def test_snapshot_id_map_poisons_ambiguous_cross_table_name():
    """R1 F1 (both reviewers, HIGH): a physical column NAME that exists in >1 table
    (ModelColumn is unique only per (table, name)) must NOT resolve to an arbitrary
    'winner' id — an unqualified leaf cannot be disambiguated, so a query over table
    A could bind table B's id and fake a §7.3 cond. 2 match (wrong-number serve). The
    id map POISONS the ambiguous name (omits it) so the leaf binds column_id="" and
    fails closed to source, deterministically regardless of snapshot iteration
    order. A UNIQUE name still resolves."""
    from src.semantic.snapshot_resolver import _build_shape
    import uuid as _uuid

    model_id = _uuid.uuid4()
    snapshot = {
        "measures": [], "dimensions": [], "hierarchies": [],
        "columns": [
            # Same lowercased name in two different tables -> ambiguous -> poisoned.
            {"id": "id-orders-created", "column_name": "created_at",
             "model_table_id": "t-orders", "is_hidden": False},
            {"id": "id-returns-created", "column_name": "created_at",
             "model_table_id": "t-returns", "is_hidden": False},
            # Unique name -> resolves normally.
            {"id": "id-orders-revenue", "column_name": "revenue",
             "model_table_id": "t-orders", "is_hidden": False},
        ],
    }
    shape = _build_shape(model_id, snapshot)
    # Ambiguous name is NOT in the id map (no arbitrary winner).
    assert "created_at" not in shape.physical_column_ids
    # Unique name resolves to its real id.
    assert shape.physical_column_ids["revenue"] == "id-orders-revenue"
    # Both names still appear in the physical-name sets (name-level behaviour
    # unchanged; only the id map is collision-safe).
    assert {"created_at", "revenue"} <= shape.physical_columns_all


def test_ambiguous_leaf_binds_empty_id_and_fails_closed_end_to_end():
    """End-to-end of R1 F1: a derived expression over an AMBIGUOUS bare leaf name
    binds column_id="" (map poisoned it) and the lineage gate fails closed even
    against a manifest that stored one of the colliding ids — no wrong serve."""
    from src.parsing.sql_parser import parse_sql_to_ir
    from src.semantic.binder import _build_bound_derived_expressions
    from src.semantic.snapshot_resolver import _build_shape
    from src.routing.derived_serving import (
        _exact_lineage_matches,
        build_query_key_requests,
    )
    from src.routing.derived_expression_proof import CandidateManifest
    import uuid as _uuid

    snapshot = {
        "measures": [], "dimensions": [], "hierarchies": [],
        "columns": [
            {"id": "id-A-created", "column_name": "created_at",
             "model_table_id": "t-A", "is_hidden": False},
            {"id": "id-B-created", "column_name": "created_at",
             "model_table_id": "t-B", "is_hidden": False},
        ],
    }
    id_map = _build_shape(_uuid.uuid4(), snapshot).physical_column_ids

    lq = parse_sql_to_ir(
        "SELECT DATE_TRUNC('month', created_at) AS m, SUM(revenue) AS r "
        "FROM orders GROUP BY DATE_TRUNC('month', created_at)",
        "m1",
    )
    bdes = _build_bound_derived_expressions(
        lq.expression_occurrences, model_id="m1",
        physical_columns={"created_at", "revenue"}, physical_column_ids=id_map,
    )
    # The ambiguous leaf got no id.
    assert all(ref.column_id == "" for bde in bdes for ref in bde.inputs)

    class _BQReal:
        logical_query = lq
        bound_derived_expressions = bdes

    keys = build_query_key_requests(_BQReal())
    fp = bdes[0].expression_fingerprint
    # A manifest that stored one of the colliding ids must NOT be matched.
    candidate = CandidateManifest(
        artifact_id="a", is_active=True, is_stale=False, active_refresh_run_id="run-1",
        manifest_hash=None, grain_key_fingerprints=frozenset({fp}),
        grain_key_lineage={fp: ("id-B-created",)},
    )
    assert _exact_lineage_matches(keys, candidate) is False


@pytest.mark.asyncio
async def test_load_physical_column_ids_poisons_ambiguous_name():
    """R2 F5: the FALLBACK id-map loader (_load_physical_column_ids) must also poison
    an ambiguous cross-table name (>1 id for one lowercased name) and cover ALL
    columns — same collision-safe contract as the snapshot builder, since a
    builder-path divergence was the R1 escape."""
    from unittest.mock import AsyncMock, MagicMock
    from src.semantic.binder import _load_physical_column_ids

    _res = MagicMock()
    _res.all.return_value = [
        ("id-A-created", "created_at"),   # ambiguous: same name, two tables
        ("id-B-created", "created_at"),
        ("id-orders-rev", "revenue"),     # unique
    ]
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_res)

    id_map = await _load_physical_column_ids("m1", db)
    assert "created_at" not in id_map       # ambiguous -> poisoned
    assert id_map["revenue"] == "id-orders-rev"


@pytest.mark.asyncio
async def test_live_metadata_bundle_id_map_poisons_ambiguous_name():
    """R2 F5: the LIVE-BUNDLE builder (_load_live_metadata_bundle) must poison an
    ambiguous name too — the third id-map path, so all three are collision-safe."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from src.semantic.binder import _load_live_metadata_bundle

    _res = MagicMock()
    _res.all.return_value = [
        ("id-A-created", "created_at", False),   # ambiguous
        ("id-B-created", "created_at", False),
        ("id-hidden-secret", "secret", True),    # hidden but STILL in the id map
    ]
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_res)

    with patch("src.semantic.binder._load_measures", new=AsyncMock(return_value=[])), \
         patch("src.semantic.binder._load_dimensions", new=AsyncMock(return_value=[])), \
         patch("src.semantic.binder._load_hierarchy_level_dimensions",
               new=AsyncMock(return_value=[])), \
         patch("src.semantic.binder._load_hidden_column_ids",
               new=AsyncMock(return_value=set())):
        bundle = await _load_live_metadata_bundle("m1", db)

    assert "created_at" not in bundle.physical_column_ids       # ambiguous -> poisoned
    # Hidden column's UNIQUE name is still in the id map (vocabulary parity; hidden
    # ACCESS is enforced by CLS, not by the id map).
    assert bundle.physical_column_ids["secret"] == "id-hidden-secret"


def test_source_only_expression_binds_no_leaf_ids():
    """R2 F1: an expression the binder marks source-only (unknown function) must NOT
    carry serving leaf ids even when the leaf names resolve — an unknown/volatile
    function must never serve by exact identity ('unknown function -> source-only')."""
    from src.semantic.binder import _build_bound_derived_expressions
    from src.ir.logical_query import ExpressionOccurrence

    # A GROUP BY over an UNKNOWN function of a known column. NONEXISTENT_FN is not in
    # the semantic registry -> has_unknown_function -> rejection set.
    occ = ExpressionOccurrence(
        occurrence_id="occ-1", role="GROUP_KEY",
        raw_sql="NONEXISTENT_FN(order_date)", input_dialect="postgres",
        ast_json=None, output_alias="x",
    )
    bdes = _build_bound_derived_expressions(
        [occ], model_id="m1",
        physical_columns={"order_date"},
        physical_column_ids={"order_date": "col-order-date"},  # leaf WOULD resolve
    )
    assert bdes
    # rejection is set (unknown function) -> NO leaf id bound despite the resolvable name.
    assert bdes[0].proof_rejection is not None
    assert all(ref.column_id == "" for ref in bdes[0].inputs)


def test_partial_lineage_all_or_nothing_binding():
    """R2 F3: a multi-leaf expression with ONE unresolved (poisoned/absent) leaf must
    bind NO leaf ids at all — never a NON-EMPTY partial set that a same-name-derived
    manifest could match on the partial. All-or-nothing keeps the §7.3 cond.2
    comparison whole."""
    from src.semantic.binder import _build_bound_derived_expressions
    from src.ir.logical_query import ExpressionOccurrence

    # COALESCE over two columns (a KNOWN function -> no unknown-function rejection,
    # so this genuinely exercises the PARTIAL-lineage path, not the source-only
    # path). Only ONE leaf has a resolvable id; the other is absent (poisoned).
    occ = ExpressionOccurrence(
        occurrence_id="occ-1", role="GROUP_KEY",
        raw_sql="COALESCE(ship_date, order_date)", input_dialect="postgres",
        ast_json=None, output_alias="x",
    )
    bdes = _build_bound_derived_expressions(
        [occ], model_id="m1",
        physical_columns={"ship_date", "order_date"},
        # order_date resolves; ship_date is ABSENT (poisoned/ambiguous) -> partial.
        physical_column_ids={"order_date": "col-order-date"},
    )
    assert bdes
    # Known function, but not all leaves resolved -> withhold EVERY id (no partial
    # lineage presented). Confirm the rejection is NOT the unknown-function one, so
    # this test really covers the partial path.
    assert bdes[0].proof_rejection != "DERIVED_FUNCTION_UNKNOWN"
    assert all(ref.column_id == "" for ref in bdes[0].inputs)


def test_case_ambiguous_measure_names_force_source_no_mislabel():
    """R3 F1 (MED, wrong-number): two measures whose names differ ONLY in case
    (Margin / margin — both DB-legal, the uniqueness constraint is case-sensitive)
    fold to one lowercased key in the SELECT-walk's measure lookup. Last-writer-wins
    would serve ONE measure's stored component under the OTHER's alias. The walk must
    POISON the ambiguous lowercased name and route to SOURCE (same discipline as the
    leaf-id maps) — never a mislabelled component."""
    from src.parsing.sql_parser import parse_sql_to_ir
    from shared.semantic.derived_expression import canonicalise_sql
    from src.routing.derived_measure_proof import SUM_OF_SUM as _S

    lq = parse_sql_to_ir(
        "SELECT DATE_TRUNC('month', order_date) AS m, SUM(Margin) AS a, "
        "SUM(margin) AS b FROM orders GROUP BY DATE_TRUNC('month', order_date)",
        "m1",
    )
    fp = canonicalise_sql("DATE_TRUNC('MONTH', order_date)",
                          input_dialect=lq.input_dialect).fingerprint

    class _Ref:
        column_id = "col-ts"
        physical_column = "order_date"

    class _RBDE:
        expression_fingerprint = fp
        canonical_sql = "DATE_TRUNC('MONTH', order_date)"
        occurrence_ids = []
        inputs = [_Ref()]
        semantic_context = {}

    bq = _BQ(lq, resolved_measures=[
        _FakeMeasure("Margin", "Margin__sum", "sum"),
        _FakeMeasure("margin", "margin__sum", "sum"),
    ])
    bq.bound_derived_expressions = [_RBDE()]
    agg = _FakeAgg(
        grain_keys=[{"expression_fingerprint": fp, "physical_column": "order_month"}],
        columns=[
            _FakeAggColumn("Margin", "sum", "Margin__sum"),
            _FakeAggColumn("margin", "sum", "margin__sum"),
        ],
    )
    # Two DISTINCT measures collapsing to lowercased "margin" -> ambiguous plans.
    proof = DerivedServeProof(
        verdict=EXACT, artifact_id=str(agg.id),
        query_key_plans=[DerivedKeyPlan(query_key="m", plan=DIRECT_EXPRESSION_KEY,
                                        artifact_key_fingerprint=fp)],
        measure_plans=[MeasureRollupPlan("Margin", "sum", _S),
                       MeasureRollupPlan("margin", "sum", _S)],
    )
    with pytest.raises(AggregateRewriteUnsupported):
        rewrite_for_derived_exact(bound_query=bq, aggregate=agg, proof=proof,
                                  target_dialect="postgres")


def test_binder_ordinary_query_produces_no_derived_inputs_byte_identical():
    """Byte-identical: an ordinary (no function-grain) query records NO expression
    occurrences, so _build_bound_derived_expressions returns [] regardless of the
    id map — the F1 change touches only derived-expression leaves."""
    from src.parsing.sql_parser import parse_sql_to_ir
    from src.semantic.binder import _build_bound_derived_expressions

    lq = parse_sql_to_ir(
        "SELECT region AS r, SUM(revenue) AS rev FROM orders GROUP BY region", "m1",
    )
    assert lq.has_function_grain is False
    assert not lq.expression_occurrences
    bdes = _build_bound_derived_expressions(
        lq.expression_occurrences,
        model_id="m1",
        physical_columns={"region", "revenue"},
        physical_column_ids={"region": "col-region", "revenue": "col-rev"},
    )
    assert bdes == []


def test_select_walk_preserves_query_order_measure_before_key():
    """SELECT-walk: when the query projects the MEASURE before the KEY, the served
    SELECT keeps that order (measure column first) — parity for positional BI
    consumers. The old keys-then-measures reconstruction always emitted the key
    first, silently reordering columns."""
    bq, fp = _real_bq_and_fp(
        "SELECT SUM(revenue) AS rev, DATE_TRUNC('month', order_date) AS m "
        "FROM orders GROUP BY DATE_TRUNC('month', order_date)")
    agg = _FakeAgg(grain_keys=[{"expression_fingerprint": fp, "physical_column": "order_month"}],
                   columns=[_FakeAggColumn("revenue", "sum", "revenue__sum")])
    sql = rewrite_for_derived_exact(bound_query=bq, aggregate=agg,
                                    proof=_exact_proof(agg.id, fp), target_dialect="postgres")
    up = sql.upper()
    # Measure column appears BEFORE the key column in the projection.
    assert up.index("REVENUE__SUM") < up.index("ORDER_MONTH")
    assert "REV" in up and "GROUP BY" not in up


def test_select_walk_multiple_measures_each_under_own_alias_in_order():
    """SELECT-walk: two measures each serve under their OWN requested alias and in
    query order — the alias/order parity the measure-alias intake called out."""
    from src.parsing.sql_parser import parse_sql_to_ir
    from shared.semantic.derived_expression import canonicalise_sql
    from src.routing.derived_measure_proof import SUM_OF_SUM as _S

    lq = parse_sql_to_ir(
        "SELECT DATE_TRUNC('month', order_date) AS m, SUM(revenue) AS total_rev, "
        "SUM(cost) AS total_cost FROM orders GROUP BY DATE_TRUNC('month', order_date)",
        "m1",
    )
    fp = canonicalise_sql("DATE_TRUNC('MONTH', order_date)",
                          input_dialect=lq.input_dialect).fingerprint

    class _Ref:
        column_id = "col-ts"
        physical_column = "order_date"

    class _RBDE:
        expression_fingerprint = fp
        canonical_sql = "DATE_TRUNC('MONTH', order_date)"
        occurrence_ids = []
        inputs = [_Ref()]
        semantic_context = {}

    bq = _BQ(lq, resolved_measures=[
        _FakeMeasure("revenue", "revenue__sum", "sum"),
        _FakeMeasure("cost", "cost__sum", "sum"),
    ])
    bq.bound_derived_expressions = [_RBDE()]
    agg = _FakeAgg(
        grain_keys=[{"expression_fingerprint": fp, "physical_column": "order_month"}],
        columns=[
            _FakeAggColumn("revenue", "sum", "revenue__sum"),
            _FakeAggColumn("cost", "sum", "cost__sum"),
        ],
    )
    proof = DerivedServeProof(
        verdict=EXACT, artifact_id=str(agg.id),
        query_key_plans=[DerivedKeyPlan(query_key="m", plan=DIRECT_EXPRESSION_KEY,
                                        artifact_key_fingerprint=fp)],
        measure_plans=[MeasureRollupPlan("revenue", "sum", _S),
                       MeasureRollupPlan("cost", "sum", _S)],
    )
    sql = rewrite_for_derived_exact(bound_query=bq, aggregate=agg, proof=proof,
                                    target_dialect="postgres")
    up = sql.upper()
    # Order: key, then total_rev, then total_cost — the query's SELECT order.
    assert up.index("ORDER_MONTH") < up.index("REVENUE__SUM") < up.index("COST__SUM")
    # Each measure under its own requested alias.
    assert "TOTAL_REV" in up and "TOTAL_COST" in up


# ---------------------------------------------------------------------------
# §7.3/§7.4 stage-4 relabel: request builder + SELECT-walk end to end.
# ---------------------------------------------------------------------------


def _relabel_bq(select_sql, *, relabels, projections):
    """A bound query carrying stage-4 relabel + group-key projection records."""
    from src.parsing.sql_parser import parse_sql_to_ir
    lq = parse_sql_to_ir(select_sql, "m1")

    class _BQR:
        logical_query = lq
        bound_derived_expressions = []
        bound_attribute_relabels = relabels
        bound_group_key_projections = projections
    return _BQR()


def test_build_query_key_requests_emits_attribute_and_physical(_ir=None):
    """§3.3/§3.4: a bare detail GROUP BY dimension bound to a relabel emits an
    ATTRIBUTE request (attr:<uuid> + (key,detail) lineage); an unchanged physical
    grain emits a PHYSICAL request (dim:<uuid> + (source_column_id,))."""
    from src.ir.logical_query import BoundAttributeRelabel, BoundGroupKeyProjection

    relabel = BoundAttributeRelabel(
        group_ordinal=0, select_ordinals=[0], query_dimension_id="dq",
        owning_dimension_id="dk", relationship_id="rel-1", attribute_key="attr:rel-1",
        key_column_id="col-key", detail_column_id="col-detail",
        cardinality="BIJECTION", declaration_hash="dh", requested_name="country",
    )
    phys = BoundGroupKeyProjection(
        select_ordinal=1, group_ordinal=1, kind="PHYSICAL",
        query_dimension_id="dim-region", column_id="col-region", key_id="dim:dim-region",
    )
    bq = _relabel_bq(
        "SELECT country, region FROM m1 GROUP BY country, region",
        relabels=[relabel], projections=[phys],
    )
    bq.logical_query.grain = ["country", "region"]
    keys = build_query_key_requests(bq)
    by_kind = {k.kind: k for k in keys}
    assert by_kind["ATTRIBUTE"].attribute_key == "attr:rel-1"
    assert by_kind["ATTRIBUTE"].input_column_ids == ("col-key", "col-detail")
    assert by_kind["ATTRIBUTE"].key_id is None
    assert by_kind["PHYSICAL"].key_id == "dim:dim-region"
    assert by_kind["PHYSICAL"].input_column_ids == ("col-region",)


def test_relabel_walk_projects_passenger_with_reproducible_label():
    """§3.4: a bare unaliased detail projection reproduces the source-equivalent
    label (the terminal spelling, lower-cased when unquoted) and projects the
    manifest passenger — never the generated passenger name as the label."""
    from src.ir.logical_query import BoundGroupKeyProjection

    agg = _FakeAgg(
        grain_keys=[{"key_id": "dim:dk", "physical_column": "country_id",
                     "source_dimension_id": "dk", "input_column_ids": ["col-key"]}],
        attribute_edges=[{"relationship_id": "rel-1", "attribute_key": "attr:rel-1",
                          "cardinality": "BIJECTION", "key_grain_key_id": "dim:dk",
                          "detail_passenger_column": "country__passenger",
                          "manifest_hash": "mh"}],
        passenger_columns=[{"attribute_key": "attr:rel-1", "relationship_id": "rel-1",
                            "passenger_column": "country__passenger"}],
        columns=[_FakeAggColumn("revenue", "sum", "revenue__sum")],
    )
    proj = BoundGroupKeyProjection(
        select_ordinal=0, group_ordinal=0, kind="ATTRIBUTE",
        query_dimension_id="dq", column_id="col-detail", attribute_key="attr:rel-1",
    )
    bq = _relabel_bq(
        "SELECT country, SUM(revenue) AS rev FROM m1 GROUP BY country",
        relabels=[], projections=[proj],
    )
    proof = DerivedServeProof(
        verdict=EXACT, artifact_id=str(agg.id),
        query_key_plans=[DerivedKeyPlan(
            query_key="country", plan=DIRECT_ATTRIBUTE_RELABEL,
            passenger_column="country__passenger", artifact_key_id="dim:dk",
            attribute_key="attr:rel-1")],
        measure_plans=[MeasureRollupPlan("revenue", "sum", SUM_OF_SUM)],
    )
    sql = rewrite_for_derived_exact(bound_query=bq, aggregate=agg, proof=proof,
                                    target_dialect="postgres")
    up = sql.upper()
    # The passenger is projected AS the reproducible detail label ("country"),
    # NOT the generated passenger name.
    assert "COUNTRY__PASSENGER" in up
    assert '"COUNTRY"' in up  # reproducible label (unquoted -> lower-cased)
    assert "COUNTRY_ID" not in up  # never the raw key column
    assert "GROUP BY" not in up


def test_physical_key_resolved_by_key_id_not_name():
    """§3.4: an unchanged PHYSICAL key resolves its built column by canonical key
    id, never by logical name. A manifest whose physical_column differs from the
    logical name proves the resolution is id-based."""
    from src.rewrite.derived_exact import _resolve_key_physical

    agg = _FakeAgg(
        grain_keys=[{"key_id": "dim:dk", "physical_column": "built_region_col",
                     "source_dimension_id": "dk", "input_column_ids": ["col-region"]}],
        columns=[],
    )
    # Resolves by key id to the BUILT name, not the logical "region".
    assert _resolve_key_physical(agg, None, key_id="dim:dk") == "built_region_col"
    # A wrong key id resolves nothing (fail closed).
    assert _resolve_key_physical(agg, None, key_id="dim:other") is None


def test_producer_and_real_binder_yield_identical_ordered_leaf_ids():
    """§7.1 MANDATORY producer-derived contract, END TO END: the production build
    planner and the REAL parser -> binder path produce IDENTICAL ordered
    input_column_ids for the same expression. Uses the real parser + the real
    _build_bound_derived_expressions leaf binding (not a hand-built request)."""
    from shared.semantic.build_manifest_planner import build_grain_key_manifest
    from shared.semantic.grain_resolver import (
        ResolvedAggregateLayout, ResolvedGrainCol,
    )
    from src.parsing.sql_parser import parse_sql_to_ir
    from src.semantic.binder import _build_bound_derived_expressions

    expr = "DATE_TRUNC('month', order_date)"
    col_id = "col-order-date"
    id_map = {"order_date": col_id}

    # Build side (producer): grain-key lineage from the SAME expression.
    layout = ResolvedAggregateLayout(
        grain_cols=[ResolvedGrainCol(
            logical_name="m", dimension_id=uuid.uuid4(), source_table_id=None,
            source_column_name=None, physical_col_name="order_month",
            source_expression=expr,
        )],
        measure_cols=[],
    )
    build_key = build_grain_key_manifest(
        layout=layout, dimension_by_id={}, column_id_by_name=id_map,
    )[0]

    # Query side (real parser + binder), no deployed shape -> the name-map path,
    # which resolves leaves in the SAME canonical order as the producer.
    lq = parse_sql_to_ir(
        f"SELECT {expr} AS m FROM orders GROUP BY {expr}", "m1",
    )
    bdes = _build_bound_derived_expressions(
        lq.expression_occurrences, model_id="m1",
        physical_columns={"order_date"}, physical_column_ids=id_map,
    )
    query_leaf_ids = [ref.column_id for ref in bdes[0].inputs]

    # IDENTICAL ordered id tuples + identical build-time fingerprint.
    assert build_key.input_column_ids == query_leaf_ids == [col_id]
    assert build_key.expression_fingerprint == bdes[0].expression_fingerprint


def test_producer_binder_contract_two_same_named_relations_stay_distinct():
    """§7.1: two relations with the same column name resolve to DIFFERENT ids on
    both sides (the qualifier-aware leaf tuple keeps them distinct), so a
    cross-relation expression can never bind A's query to B's built key."""
    import sqlglot
    from shared.semantic.derived_expression import enumerate_canonical_leaves

    node = sqlglot.parse_one(
        "COALESCE(orders.amount, returns.amount)", read="postgres",
    )
    leaves = enumerate_canonical_leaves(node)
    # Same name, distinct qualifiers, both retained in order.
    assert (leaves[0].qualifier, leaves[0].name) == ("orders", "amount")
    assert (leaves[1].qualifier, leaves[1].name) == ("returns", "amount")
    assert len(leaves) == 2


# ---------------------------------------------------------------------------
# Fable R1 hardening: manifest kind/prefix poison + unprojected physical key.
# ---------------------------------------------------------------------------


def test_kind_prefix_disagreement_poisons_candidate():
    """§2.1: a dim:-prefixed key with kind=ARTIFACT_EXPRESSION (or carrying a
    fingerprint) poisons the candidate -> never EXACT."""
    agg = _FakeAgg(
        grain_keys=[{"key_id": "dim:d1", "kind": "ARTIFACT_EXPRESSION",
                     "physical_column": "c", "input_column_ids": ["col-a"]}],
        columns=[],
    )
    cm = build_candidate_manifest(agg=agg, trust_by_relationship={})
    assert cm.poisoned is True
    proof = build_serve_proof(
        query_keys=[QueryKeyRequest(logical_name="k", kind="PHYSICAL", key_id="dim:d1",
                                    input_column_ids=("col-a",))],
        measures=[], candidate=cm,
    )
    assert proof.verdict != EXACT


def test_dim_key_carrying_fingerprint_is_poisoned():
    agg = _FakeAgg(
        grain_keys=[{"key_id": "dim:d1", "kind": "PHYSICAL_COLUMN",
                     "expression_fingerprint": "fp-x", "physical_column": "c",
                     "input_column_ids": ["col-a"]}],
        columns=[],
    )
    assert build_candidate_manifest(agg=agg, trust_by_relationship={}).poisoned is True


def test_unprojected_physical_group_key_still_carries_identity_in_mixed_tuple():
    """Fable R1 #9: in a mixed tuple (one relabel + one unchanged PHYSICAL key), a
    GROUP BY dimension NOT in SELECT still contributes its PHYSICAL identity
    (dim:<uuid> + (source_column_id,)) to the proof tuple via the sentinel ordinal
    — so the tuple is complete and the extra physical key can be exact-covered."""
    from src.ir.logical_query import (
        BoundAttributeRelabel, BoundGroupKeyProjection,
    )

    relabel = BoundAttributeRelabel(
        group_ordinal=0, select_ordinals=[0], query_dimension_id="dq",
        owning_dimension_id="dk", relationship_id="rel-1", attribute_key="attr:rel-1",
        key_column_id="col-key", detail_column_id="col-detail",
        cardinality="BIJECTION", declaration_hash="dh", requested_name="country",
    )
    # region is grouped (ordinal 1) but NOT projected -> sentinel-ordinal identity.
    phys = BoundGroupKeyProjection(
        select_ordinal=-1, group_ordinal=1, kind="PHYSICAL",
        query_dimension_id="dim-region", column_id="col-region", key_id="dim:dim-region",
    )

    class _BQ2:
        class logical_query:
            grain = ["country", "region"]
        bound_derived_expressions = []
        bound_attribute_relabels = [relabel]
        bound_group_key_projections = [phys]

    keys = build_query_key_requests(_BQ2())
    by_kind = {k.kind: k for k in keys}
    assert by_kind["ATTRIBUTE"].attribute_key == "attr:rel-1"
    assert by_kind["PHYSICAL"].key_id == "dim:dim-region"
    assert by_kind["PHYSICAL"].input_column_ids == ("col-region",)


def test_relabel_and_own_key_dimension_project_distinct_columns():
    """Fable R3 #1: when a relabel (customer_name) AND its own key dimension
    (customer_id) are BOTH grouped+projected, with the relabel listed FIRST, the
    PHYSICAL projection must resolve the built KEY column (not the passenger). Both
    plans share artifact_key_id=dim:dk, so the walk must partition by plan kind."""
    from src.ir.logical_query import BoundGroupKeyProjection

    agg = _FakeAgg(
        grain_keys=[{"key_id": "dim:dk", "kind": "PHYSICAL_COLUMN",
                     "physical_column": "customer_id_col", "source_dimension_id": "dk",
                     "input_column_ids": ["col-key"]}],
        attribute_edges=[{"relationship_id": "rel-1", "attribute_key": "attr:rel-1",
                          "cardinality": "BIJECTION", "key_grain_key_id": "dim:dk",
                          "detail_passenger_column": "customer_name__passenger",
                          "manifest_hash": "mh"}],
        passenger_columns=[{"attribute_key": "attr:rel-1", "relationship_id": "rel-1",
                            "passenger_column": "customer_name__passenger"}],
        columns=[_FakeAggColumn("revenue", "sum", "revenue__sum")],
    )
    # SELECT customer_name (ordinal 0, ATTRIBUTE), customer_id (ordinal 1, PHYSICAL).
    proj0 = BoundGroupKeyProjection(
        select_ordinal=0, group_ordinal=0, kind="ATTRIBUTE",
        query_dimension_id="dq", column_id="col-detail", attribute_key="attr:rel-1",
    )
    proj1 = BoundGroupKeyProjection(
        select_ordinal=1, group_ordinal=1, kind="PHYSICAL",
        query_dimension_id="dk", column_id="col-key", key_id="dim:dk",
    )
    bq = _relabel_bq(
        "SELECT customer_name, customer_id, SUM(revenue) AS rev "
        "FROM m1 GROUP BY customer_name, customer_id",
        relabels=[], projections=[proj0, proj1],
    )
    # Proof carries BOTH plans, both with artifact_key_id=dim:dk (relabel first).
    proof = DerivedServeProof(
        verdict=EXACT, artifact_id=str(agg.id),
        query_key_plans=[
            DerivedKeyPlan(query_key="customer_name", plan=DIRECT_ATTRIBUTE_RELABEL,
                           passenger_column="customer_name__passenger",
                           artifact_key_id="dim:dk", attribute_key="attr:rel-1"),
            DerivedKeyPlan(query_key="customer_id", plan="DIRECT_PHYSICAL_KEY",
                           artifact_key_id="dim:dk"),
        ],
        measure_plans=[MeasureRollupPlan("revenue", "sum", SUM_OF_SUM)],
    )
    sql = rewrite_for_derived_exact(bound_query=bq, aggregate=agg, proof=proof,
                                    target_dialect="postgres")
    up = sql.upper()
    # customer_name projects the passenger; customer_id projects the KEY column —
    # NOT the passenger under the id label (the wrong-numbers bug).
    assert "CUSTOMER_NAME__PASSENGER" in up
    assert "CUSTOMER_ID_COL" in up
    # The passenger must appear exactly once (under customer_name), never twice.
    assert up.count("CUSTOMER_NAME__PASSENGER") == 1


# ---------------------------------------------------------------------------
# Bug-7873c: _reproducible_relabel_label folds an unquoted terminal per the parsed
# dialect (lower for PG/BigQuery, upper for Snowflake), not a hardcoded lower.
# ---------------------------------------------------------------------------


def test_relabel_label_folds_lower_for_postgres():
    from src.rewrite.derived_exact import _reproducible_relabel_label

    # Unquoted PostgreSQL identifier folds to lower case.
    assert _reproducible_relabel_label(None, "Country_Name", None, "postgres") == "country_name"


def test_relabel_label_folds_upper_for_snowflake():
    from src.rewrite.derived_exact import _reproducible_relabel_label

    # Snowflake folds unquoted identifiers to UPPER case — the served label must
    # match what the source engine would have returned.
    assert _reproducible_relabel_label(None, "Country_Name", None, "snowflake") == "COUNTRY_NAME"


def test_relabel_label_explicit_alias_preserved_regardless_of_dialect():
    from src.rewrite.derived_exact import _reproducible_relabel_label

    # An explicit alias is honoured exactly, no folding.
    assert _reproducible_relabel_label(None, "x", "MyAlias", "snowflake") == "MyAlias"


def test_relabel_label_quoted_terminal_preserved_exactly():
    from src.rewrite.derived_exact import _reproducible_relabel_label

    # A simple quoted identifier is preserved exactly (dialect folding does not apply).
    assert _reproducible_relabel_label(None, '"Country_Name"', None, "snowflake") == "Country_Name"


# ---------------------------------------------------------------------------
# Bug-7905: evidence-AGE bound in the trust predicate. A VERIFIED health row older
# than N x the model's sweep cadence is EXPIRED and must NOT serve — it may be a
# stale VERIFIED a rolled-back demotion or a scheduler outage never got to re-check.
# ---------------------------------------------------------------------------

from datetime import (  # noqa: E402
    datetime, datetime as _dt, timedelta, timedelta as _td, timezone, timezone as _tz,
)

from src.routing.derived_trust_predicate import evaluate_trust as _evaluate_trust  # noqa: E402


def _fresh_bijection_trust(*, evidence_age_ok):
    """A trust input that passes every rule EXCEPT (optionally) the age gate."""
    class _Ev:
        id = "ev-age"; status = "VERIFIED"; verifier_version = "v0"
        declaration_hash = "d"; deployed_version_id = "ver-1"; deploy_epoch = 1
        artifact_refresh_run_id = "run-1"; artifact_manifest_hash = "mh-1"
        source_data_version = None; violation_count = 0
    return TrustInputs(
        declaration_enabled=True, deployed_declaration_hash="d", evidence=_Ev(),
        accepted_verifier_version="v0", artifact_active_refresh_run_id="run-1",
        artifact_manifest_hash="mh-1", artifact_is_active=True, artifact_is_stale=False,
        bound_deployed_version_id="ver-1", bound_deploy_epoch=1,
        manifest_edge={"cardinality": "BIJECTION", "detail_passenger_column": "p"},
        evidence_age_ok=evidence_age_ok,
    )


def test_expired_evidence_is_rejected_by_trust_predicate():
    # A VERIFIED row that is beyond the age bound -> reject with EVIDENCE_EXPIRED.
    res = _evaluate_trust(_fresh_bijection_trust(evidence_age_ok=False))
    assert res.admitted is False
    assert res.reason_code == "ATTRIBUTE_EVIDENCE_EXPIRED"


def test_fresh_evidence_passes_the_age_gate():
    # Same evidence, within the age bound -> admitted (every other rule passes).
    res = _evaluate_trust(_fresh_bijection_trust(evidence_age_ok=True))
    assert res.admitted is True
    assert "rule2b_evidence_age_ok" in res.trace


def test_trust_inputs_default_fails_closed_on_age():
    # A caller that forgets to resolve the age bound must FAIL CLOSED (default False).
    ti = _fresh_bijection_trust(evidence_age_ok=True)
    # Rebuild WITHOUT setting evidence_age_ok -> uses the dataclass default.
    default_ti = TrustInputs(
        declaration_enabled=ti.declaration_enabled,
        deployed_declaration_hash=ti.deployed_declaration_hash,
        evidence=ti.evidence, accepted_verifier_version=ti.accepted_verifier_version,
        artifact_active_refresh_run_id=ti.artifact_active_refresh_run_id,
        artifact_manifest_hash=ti.artifact_manifest_hash,
        artifact_is_active=True, artifact_is_stale=False,
        bound_deployed_version_id=ti.bound_deployed_version_id,
        bound_deploy_epoch=ti.bound_deploy_epoch, manifest_edge=ti.manifest_edge,
    )
    assert default_ti.evidence_age_ok is False
    assert _evaluate_trust(default_ti).reason_code == "ATTRIBUTE_EVIDENCE_EXPIRED"


def test_evidence_is_fresh_helper_boundaries():
    from src.routing.derived_serving import _evidence_is_fresh

    now = _dt(2026, 1, 10, tzinfo=_tz.utc)
    bound = _td(hours=72)  # 3 x 24h default cadence

    class _Ev:
        def __init__(self, checked_at):
            self.checked_at = checked_at

    # Within bound -> fresh.
    assert _evidence_is_fresh(_Ev(now - _td(hours=71)), now, bound) is True
    # Exactly at the bound -> still fresh (<=).
    assert _evidence_is_fresh(_Ev(now - _td(hours=72)), now, bound) is True
    # Beyond the bound -> expired.
    assert _evidence_is_fresh(_Ev(now - _td(hours=73)), now, bound) is False
    # Missing checked_at -> fail closed.
    assert _evidence_is_fresh(_Ev(None), now, bound) is False
    # Unresolved bound (None) -> fail closed.
    assert _evidence_is_fresh(_Ev(now), now, None) is False
    # No evidence -> fail closed.
    assert _evidence_is_fresh(None, now, bound) is False
    # Naive checked_at is treated as UTC.
    assert _evidence_is_fresh(_Ev((now - _td(hours=1)).replace(tzinfo=None)), now, bound) is True


class _AgeEv:
    id = "ev-age"; status = "VERIFIED"; verifier_version = "v0"
    declaration_hash = "d"; deployed_version_id = "ver-1"; deploy_epoch = 1
    artifact_refresh_run_id = "run-1"; artifact_manifest_hash = "mh-1"
    source_data_version = None; violation_count = 0
    def __init__(self, checked_at):
        self.checked_at = checked_at


class _AgeRel:
    id = "rel-1"; enabled = True; declaration_hash = "d"
    key_column_id = "col-key"; detail_column_id = "col-detail"


def _age_agg():
    agg = _FakeAgg(
        grain_keys=[{"expression_fingerprint": "fp-cid", "physical_column": "country_id"}],
        attribute_edges=[{"relationship_id": "rel-1", "cardinality": "BIJECTION",
                          "detail_passenger_column": "p", "key_grain_fingerprint": "fp-cid",
                          "manifest_hash": "mh-1"}],
    )
    agg.model_id = "model-1"
    return agg


def _age_db(evidence):
    class _DB:
        async def get(self, _cls, _id):
            return _AgeRel()
        async def execute(self, *a, **k):
            class _R:
                def scalar_one_or_none(self_inner):
                    return evidence
            return _R()
    return _DB()


@pytest.mark.asyncio
async def test_loader_marks_fresh_verified_evidence_age_ok(monkeypatch):
    """A VERIFIED row checked within N x cadence -> evidence_age_ok True (serves)."""
    from src.routing.derived_serving import (
        DerivedServeContext, load_candidate_trust_inputs,
    )

    async def _get_setting(key, **kw):
        return 24  # sweep cadence hours -> 72h age bound
    monkeypatch.setattr("shared.config.resolver.get_setting", _get_setting)

    fresh = _AgeEv(datetime.now(timezone.utc) - timedelta(hours=1))
    ctx = DerivedServeContext(
        bound_deployed_version_id="ver-1", bound_deploy_epoch=1,
        accepted_verifier_version="v0", security_ok=True,
    )
    trust = await load_candidate_trust_inputs(db=_age_db(fresh), agg=_age_agg(), ctx=ctx)
    assert trust["rel-1"].evidence_age_ok is True


@pytest.mark.asyncio
async def test_loader_marks_expired_verified_evidence_not_ok(monkeypatch):
    """A VERIFIED row older than N x cadence -> evidence_age_ok False (not served)."""
    from src.routing.derived_serving import (
        DerivedServeContext, load_candidate_trust_inputs,
    )

    async def _get_setting(key, **kw):
        return 24  # -> 72h bound
    monkeypatch.setattr("shared.config.resolver.get_setting", _get_setting)

    stale = _AgeEv(datetime.now(timezone.utc) - timedelta(hours=100))  # > 72h
    ctx = DerivedServeContext(
        bound_deployed_version_id="ver-1", bound_deploy_epoch=1,
        accepted_verifier_version="v0", security_ok=True,
    )
    trust = await load_candidate_trust_inputs(db=_age_db(stale), agg=_age_agg(), ctx=ctx)
    ti = trust["rel-1"]
    assert ti.evidence_age_ok is False
    # And the trust predicate rejects it with EVIDENCE_EXPIRED.
    ti.manifest_edge = {"cardinality": "BIJECTION", "detail_passenger_column": "p"}
    assert _evaluate_trust(ti).reason_code == "ATTRIBUTE_EVIDENCE_EXPIRED"


@pytest.mark.asyncio
async def test_loader_fails_closed_on_unreadable_cadence(monkeypatch):
    """An unreadable sweep cadence -> age bound None -> evidence_age_ok False."""
    from src.routing.derived_serving import (
        DerivedServeContext, load_candidate_trust_inputs,
    )

    async def _boom(key, **kw):
        raise RuntimeError("resolver down")
    monkeypatch.setattr("shared.config.resolver.get_setting", _boom)

    fresh = _AgeEv(datetime.now(timezone.utc))  # fresh, but cadence unreadable
    ctx = DerivedServeContext(
        bound_deployed_version_id="ver-1", bound_deploy_epoch=1,
        accepted_verifier_version="v0", security_ok=True,
    )
    trust = await load_candidate_trust_inputs(db=_age_db(fresh), agg=_age_agg(), ctx=ctx)
    assert trust["rel-1"].evidence_age_ok is False


@pytest.mark.asyncio
async def test_loader_fails_closed_on_missing_checked_at(monkeypatch):
    """A VERIFIED row with no checked_at -> evidence_age_ok False (fail closed)."""
    from src.routing.derived_serving import (
        DerivedServeContext, load_candidate_trust_inputs,
    )

    async def _get_setting(key, **kw):
        return 24
    monkeypatch.setattr("shared.config.resolver.get_setting", _get_setting)

    no_ts = _AgeEv(None)
    ctx = DerivedServeContext(
        bound_deployed_version_id="ver-1", bound_deploy_epoch=1,
        accepted_verifier_version="v0", security_ok=True,
    )
    trust = await load_candidate_trust_inputs(db=_age_db(no_ts), agg=_age_agg(), ctx=ctx)
    assert trust["rel-1"].evidence_age_ok is False
