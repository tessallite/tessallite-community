"""Bug-8455 + Bug-8457 — bind the execution-time cache guard to the ADMITTED
generation, and give the aggregate route the guard the pocket route already had.

Bug-8392 closed the pocket admission-to-scan race with an execution-time re-proof
plus a before/after generation stamp. Two holes survived it:

  * **Bug-8455 (pocket, admission-BINDING axis).** The pre-check re-proved
    whatever was LIVE. It could not compare against the generation the MATCHER
    admitted, because ``RouteDecision`` carried no admission-time stamp. The
    matcher's containment proof (``query subset-of pocket``) is deliberately not
    re-run at execution time, so a definition edit plus a COMPLETE refresh
    landing between the matcher's read and the pre-check passed every live check
    and served a row population the containment proof never covered — silently
    MISSING ROWS.

  * **Bug-8457 (aggregate, admission-to-SCAN axis).** The aggregate branch of
    ``execute_routed_query`` had NEITHER half — no re-proof, no stamp — while the
    pocket branch immediately above it had both. Same race class, on the path
    that serves far more production traffic, and with a worse outcome: a rebuild
    that changed the grain or the stored statistic under the same physical column
    names yields silently WRONG NUMBERS, not merely a missed acceleration.

Both are closed by the same three-part construction in
``routing/artifact_generation_guard``: admitted stamp on the decision, live
re-proof bound to it, before/after stamp across the scan.

Test escape: nothing asserted that the route decision carries what the matcher
admitted, and no test executed an AGGREGATE route against a definition whose live
row had moved on since admission. Guard: this file. Tier: T1.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from shared.db.models import AggregateDefinition
from shared.security.predicate_compiler import CompiledPredicate

from src.routing.aggregate_generation_guard import (
    AggregateGenerationChangedError,
    aggregate_generation_of,
    assert_aggregate_route_admissible,
    read_aggregate_generation,
)
from src.routing.aggregate_generation_guard import (
    assert_generation_unchanged as assert_aggregate_generation_unchanged,
)
from src.routing.artifact_generation_guard import (
    ArtifactGeneration,
    ArtifactGenerationChangedError,
)
from src.routing.pocket_generation_guard import (
    PocketGenerationChangedError,
    assert_pocket_route_admissible,
    pocket_generation_of,
)

_VERSION = uuid.uuid4()
_TARGET_ID = uuid.uuid4()
_DEFINING_SQL = "SELECT * FROM payments WHERE region_code = 'NORTH'"


# ---------------------------------------------------------------------------
# Fakes (same shape as the Bug-8392 suite; kept local so neither file's harness
# constrains the other)
# ---------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeDb:
    """Returns the queued rows in order, one per ``execute`` — each guard read
    issues exactly one statement."""

    def __init__(self, rows):
        self._rows = list(rows)
        self.executes = 0

    async def execute(self, _stmt):
        self.executes += 1
        row = self._rows.pop(0) if self._rows else None
        return _FakeResult(row)


def _bound(deployed=_VERSION, epoch=3):
    return types.SimpleNamespace(
        model=types.SimpleNamespace(
            id=uuid.uuid4(), project_id=uuid.uuid4(), slug="m",
            deployed_version_id=deployed, deploy_epoch=epoch,
        )
    )


def _compiled(columns=("region_code",)):
    return CompiledPredicate(
        sql_expression="region_code = 'NORTH'",
        active_rule_ids=("rule-1",),
        security_dimension_columns=tuple(columns),
        mapping_source_ids=(),
    )


# --- aggregate -------------------------------------------------------------

def _agg_row(
    *,
    run_id,
    status="active",
    table="agg_sales_by_region",
    schema="analytics",
    grain=("region_code", "order_month"),
    grain_physical_cols=None,
    built_version=_VERSION,
    built_epoch=3,
    binding=None,
    source_binding=None,
    target_id=None,
    model_id="m-1",
    is_stale=False,
):
    return types.SimpleNamespace(
        status=status,
        # Round-2 review finding 4: the guard cross-checks the recorded
        # binding's model against the LIVE row's, so the fixture must carry it.
        model_id=model_id,
        is_stale=is_stale,
        active_refresh_run_id=run_id,
        physical_table_name=table,
        target_schema=schema,
        target_id=target_id or _TARGET_ID,
        built_for_version_id=built_version,
        built_for_epoch=built_epoch,
        built_for_storage_binding=binding,
        # Bug-8602: the SOURCE half of the routing proof. Defaults to None (no
        # recorded binding), which the guard treats exactly as it treats a
        # missing storage binding: not refused, and covered instead by the
        # control-plane invalidation half.
        built_for_source_binding=source_binding,
        grain=list(grain),
        grain_physical_cols=(
            list(grain_physical_cols) if grain_physical_cols else None
        ),
    )


def _agg_decision(agg_id, *, compiled=None, admitted=None):
    return types.SimpleNamespace(
        route_type="aggregate",
        aggregate_id=agg_id,
        pocket_id=None,
        rewritten_query=(
            'SELECT region_code, SUM(amount__sum) FROM "analytics"'
            '."agg_sales_by_region" GROUP BY region_code'
        ),
        target_dialect="postgres",
        security_compiled=compiled,
        admitted_generation=admitted,
    )


# --- pocket ----------------------------------------------------------------

def _pkt_row(*, run_id, status="fresh", table="pocket_cache", schema="public",
             target_id=None, model_id="m-1"):
    return types.SimpleNamespace(
        status=status,
        active_refresh_run_id=run_id,
        physical_table_name=table,
        target_schema=schema,
        target_id=target_id or _TARGET_ID,
        defining_sql=_DEFINING_SQL,
        row_manifest={
            "source_binding": {
                "model_id": model_id,
                "source_connection_id": "source-connection",
                "source_connection_project_id": "source-project",
                "routing_fingerprint": "source-fingerprint",
            },
        },
        built_for_version_id=_VERSION,
        built_for_epoch=3,
        model_id=model_id,
    )


def _pkt_decision(pocket_id, *, admitted=None):
    return types.SimpleNamespace(
        route_type="pocket",
        pocket_id=pocket_id,
        aggregate_id=None,
        rewritten_query=(
            'SELECT * FROM "public"."pocket_cache" WHERE region_code = \'NORTH\''
        ),
        target_dialect="postgres",
        security_compiled=None,
        admitted_generation=admitted,
    )


# ---------------------------------------------------------------------------
# Bug-8455 — the pocket admission BINDING
# ---------------------------------------------------------------------------


class TestPocketAdmissionBinding:
    @pytest.fixture(autouse=True)
    def _source_binding_matches_live(self):
        """Keep generation tests on their intended axis.

        The dedicated pocket source-binding suite supplies the real binding
        cases; these fixtures model a complete, matching persisted source
        binding so a source guard does not pre-empt the admission assertion.
        """
        with patch(
            "shared.artifact_target_binding.source_build_binding_matches_live",
            AsyncMock(return_value=True),
        ):
            yield

    @pytest.mark.asyncio
    async def test_refresh_between_admission_and_pre_check_is_refused(self):
        """THE Bug-8455 case. Every live check still passes — the pocket is
        ``fresh``, in the same place, built for the deployed version — because a
        COMPLETE refresh restored all of that. Only the run id moved, and only
        the admitted stamp can see it. Before the binding this was accepted and
        served a slice the matcher's containment proof never covered."""
        admitted_run, live_run = uuid.uuid4(), uuid.uuid4()
        pid = uuid.uuid4()
        admitted = pocket_generation_of(_pkt_row(run_id=admitted_run))
        db = _FakeDb([_pkt_row(run_id=live_run)])

        with pytest.raises(PocketGenerationChangedError) as exc:
            await assert_pocket_route_admissible(
                db, bound=_bound(), decision=_pkt_decision(pid, admitted=admitted),
            )
        assert "between the route decision and its execution" in str(exc.value)

    @pytest.mark.asyncio
    async def test_same_generation_is_admitted(self):
        run_id = uuid.uuid4()
        pid = uuid.uuid4()
        admitted = pocket_generation_of(_pkt_row(run_id=run_id))
        db = _FakeDb([_pkt_row(run_id=run_id)])

        gen = await assert_pocket_route_admissible(
            db, bound=_bound(), decision=_pkt_decision(pid, admitted=admitted),
        )
        assert gen == admitted
    @pytest.mark.asyncio
    async def test_absent_binding_keeps_the_bug_8392_behaviour(self):
        """A decision without an admitted-generation stamp keeps live re-proof.

        The persisted pocket fixture still carries the required source binding;
        only the Bug-8455 admission-binding axis is intentionally absent.
        """
        run_id = uuid.uuid4()
        db = _FakeDb([_pkt_row(run_id=run_id)])
        gen = await assert_pocket_route_admissible(
            db, bound=_bound(), decision=_pkt_decision(uuid.uuid4(), admitted=None),
        )
        assert gen.status == "fresh"

    @pytest.mark.asyncio
    async def test_seeded_pocket_with_no_run_pointer_is_not_false_refused(self):
        """A validly bound pocket with no run pointer has a stable stamp."""
        db = _FakeDb([_pkt_row(run_id=None)])
        admitted = pocket_generation_of(_pkt_row(run_id=None))
        gen = await assert_pocket_route_admissible(
            db, bound=_bound(),
            decision=_pkt_decision(uuid.uuid4(), admitted=admitted),
        )
        assert gen == admitted


# ---------------------------------------------------------------------------
# Bug-8457 — the aggregate guard
# ---------------------------------------------------------------------------

class TestAggregatePreScanReproof:
    @pytest.mark.asyncio
    async def test_admissible_aggregate_returns_its_generation(self):
        run_id = uuid.uuid4()
        db = _FakeDb([_agg_row(run_id=run_id)])
        gen = await assert_aggregate_route_admissible(
            db, bound=_bound(), decision=_agg_decision(uuid.uuid4()),
        )
        assert gen == ArtifactGeneration(
            status="active",
            active_refresh_run_id=str(run_id),
            physical_table_name="agg_sales_by_region",
            target_schema="analytics",
            target_id=str(_TARGET_ID),
        )

    @pytest.mark.asyncio
    async def test_deleted_aggregate_is_refused(self):
        db = _FakeDb([None])
        with pytest.raises(AggregateGenerationChangedError, match="no longer exists"):
            await assert_aggregate_route_admissible(
                db, bound=_bound(), decision=_agg_decision(uuid.uuid4()),
            )

    @pytest.mark.parametrize("status", ["pending", "disabled", "retired", "stale"])
    @pytest.mark.asyncio
    async def test_non_active_status_is_refused(self, status):
        """A rebuild in flight has already committed ``active -> pending`` via
        the Bug-7903 guard, so this is the primary in-flight detector."""
        db = _FakeDb([_agg_row(run_id=uuid.uuid4(), status=status)])
        with pytest.raises(AggregateGenerationChangedError, match="no longer active"):
            await assert_aggregate_route_admissible(
                db, bound=_bound(), decision=_agg_decision(uuid.uuid4()),
            )

    @pytest.mark.asyncio
    async def test_aggregate_staled_after_admission_is_refused(self):
        """Bug-8250 R2: ``is_stale`` is a hard matcher refusal, so admitting a
        row that was staled between admission and execution serves data the
        matcher itself would have declined a moment later.

        The status leg cannot stand in for it: the source-schema drift sweep and
        a superseded-build completion both set ONLY this flag, leaving
        ``status="active"`` and ``active_refresh_run_id`` untouched.
        """
        db = _FakeDb([_agg_row(run_id=uuid.uuid4(), is_stale=True)])
        with pytest.raises(AggregateGenerationChangedError, match="marked stale"):
            await assert_aggregate_route_admissible(
                db, bound=_bound(), decision=_agg_decision(uuid.uuid4()),
            )

    @pytest.mark.asyncio
    async def test_relocated_table_is_refused(self):
        """A rebuild that re-bound the location BEFORE the pre-check would
        otherwise leave us proving one table and scanning another."""
        db = _FakeDb([_agg_row(run_id=uuid.uuid4(), table="agg_sales_v2")])
        with pytest.raises(
            AggregateGenerationChangedError, match="no longer lives where"
        ):
            await assert_aggregate_route_admissible(
                db, bound=_bound(), decision=_agg_decision(uuid.uuid4()),
            )

    @pytest.mark.asyncio
    async def test_build_for_another_deployed_version_is_refused(self):
        db = _FakeDb(
            [_agg_row(run_id=uuid.uuid4(), built_version=uuid.uuid4())]
        )
        with pytest.raises(
            AggregateGenerationChangedError, match="different deployed model"
        ):
            await assert_aggregate_route_admissible(
                db, bound=_bound(), decision=_agg_decision(uuid.uuid4()),
            )

    @pytest.mark.asyncio
    async def test_undeployed_model_skips_the_version_binding_check(self):
        """Mirrors the matcher: the build-binding gate only applies once the
        model has a deployed pointer."""
        db = _FakeDb(
            [_agg_row(run_id=uuid.uuid4(), built_version=None, built_epoch=None)]
        )
        gen = await assert_aggregate_route_admissible(
            db, bound=_bound(deployed=None, epoch=0),
            decision=_agg_decision(uuid.uuid4()),
        )
        assert gen.status == "active"

    @pytest.mark.asyncio
    async def test_rebuild_dropping_a_security_column_is_refused(self):
        """Wrong-numbers/fail-closed leg: the injected predicate references a
        grain column the live build no longer carries."""
        db = _FakeDb([_agg_row(run_id=uuid.uuid4(), grain=("order_month",))])
        with pytest.raises(
            AggregateGenerationChangedError, match="row-security-safe"
        ):
            await assert_aggregate_route_admissible(
                db, bound=_bound(),
                decision=_agg_decision(uuid.uuid4(), compiled=_compiled()),
            )

    @pytest.mark.asyncio
    async def test_rls_safe_aggregate_still_serves(self):
        """Control: the RLS re-proof must not refuse a live build that still
        carries every security column."""
        db = _FakeDb([_agg_row(run_id=uuid.uuid4())])
        gen = await assert_aggregate_route_admissible(
            db, bound=_bound(),
            decision=_agg_decision(uuid.uuid4(), compiled=_compiled()),
        )
        assert gen.status == "active"

    @pytest.mark.asyncio
    async def test_unusable_recorded_storage_binding_is_refused(self):
        """A recorded binding with no routing fingerprint cannot say which
        database its table lives on."""
        db = _FakeDb([_agg_row(run_id=uuid.uuid4(), binding={"target_id": "x"})])
        with pytest.raises(
            AggregateGenerationChangedError, match="different target connection"
        ):
            await assert_aggregate_route_admissible(
                db, bound=_bound(), decision=_agg_decision(uuid.uuid4()),
                target=types.SimpleNamespace(id=_TARGET_ID, target_type="postgresql", config={}),
                conn=types.SimpleNamespace(connection_type="postgresql", config={}),
            )

    @pytest.mark.asyncio
    async def test_legacy_mismatched_bigquery_target_is_refused_before_grain_proof(self):
        db = _FakeDb([_agg_row(run_id=uuid.uuid4())])
        target = types.SimpleNamespace(
            id=_TARGET_ID,
            target_type="postgresql",
            config={"dataset": "analytics"},
        )
        conn = types.SimpleNamespace(
            connection_type="bigquery",
            config={"project_id": "connection-project"},
            encrypted_credentials=None,
        )
        with pytest.raises(AggregateGenerationChangedError, match="unsafe legacy"):
            await assert_aggregate_route_admissible(
                db, bound=_bound(), decision=_agg_decision(uuid.uuid4()),
                target=target, conn=conn,
            )


class TestAggregateSourceBindingReproof:
    """Bug-8602 — the aggregate must also still be reading the database it was
    BUILT FROM.

    A cross-database aggregate (source connection A, target connection B) has no
    ``DataTarget`` on A at all, so the storage binding is structurally silent
    about it. If A is re-pointed, the cached rows came from a database the model
    no longer reads while the source-route fallback for the same question reads
    the new one — two routes, two answers, no error.

    Only the DB-reading layer (``current_source_build_binding``) is stubbed
    here. The recorded-dict handling, the fail-closed comparison and the guard's
    refusal path all execute exactly as in production.
    """

    _RECORDED = {
        "model_id": "m-1",
        "source_connection_id": "conn-a",
        "source_connection_project_id": "proj-1",
        "routing_fingerprint": "fp-old",
    }

    @staticmethod
    def _live(monkeypatch, *, binding=None, raises=False):
        from shared import artifact_target_binding as atb

        async def _fake(_db, _model_id, **_kw):
            if raises:
                raise RuntimeError("source unresolvable")
            return binding

        monkeypatch.setattr(atb, "current_source_build_binding", _fake)

    @pytest.mark.asyncio
    async def test_moved_source_endpoint_is_refused(self, monkeypatch):
        """THE Bug-8602 case: same connection id, different endpoint."""
        from shared.artifact_target_binding import ArtifactSourceBuildBinding

        self._live(
            monkeypatch,
            binding=ArtifactSourceBuildBinding("m-1", "conn-a", "proj-1", "fp-new"),
        )
        db = _FakeDb([_agg_row(run_id=uuid.uuid4(), source_binding=self._RECORDED)])
        with pytest.raises(
            AggregateGenerationChangedError, match="different source database"
        ):
            await assert_aggregate_route_admissible(
                db, bound=_bound(), decision=_agg_decision(uuid.uuid4()),
            )

    @pytest.mark.asyncio
    async def test_datasource_repointed_to_another_connection_is_refused(
        self, monkeypatch
    ):
        from shared.artifact_target_binding import ArtifactSourceBuildBinding

        self._live(
            monkeypatch,
            binding=ArtifactSourceBuildBinding("m-1", "conn-b", "proj-1", "fp-old"),
        )
        db = _FakeDb([_agg_row(run_id=uuid.uuid4(), source_binding=self._RECORDED)])
        with pytest.raises(AggregateGenerationChangedError):
            await assert_aggregate_route_admissible(
                db, bound=_bound(), decision=_agg_decision(uuid.uuid4()),
            )

    @pytest.mark.asyncio
    async def test_connection_that_moved_project_is_refused(self, monkeypatch):
        """Bug-5325 parity at serve time (round-1 review finding 2).

        The endpoint is byte-identical and the connection id is unchanged; only
        the connection's PROJECT moved. A rebuild would now be refused outright
        by ``assert_connection_in_project``, so the cached answer must not keep
        serving either.
        """
        from shared.artifact_target_binding import ArtifactSourceBuildBinding

        self._live(
            monkeypatch,
            binding=ArtifactSourceBuildBinding(
                "m-1", "conn-a", "proj-2", "fp-old"
            ),
        )
        db = _FakeDb([_agg_row(run_id=uuid.uuid4(), source_binding=self._RECORDED)])
        with pytest.raises(AggregateGenerationChangedError):
            await assert_aggregate_route_admissible(
                db, bound=_bound(), decision=_agg_decision(uuid.uuid4()),
            )

    @pytest.mark.asyncio
    async def test_a_binding_missing_the_project_leg_is_refused(self, monkeypatch):
        """A record written by an older binding version cannot prove the
        isolation leg. Refused WITHOUT a live read."""
        called = []
        from shared import artifact_target_binding as atb

        async def _fake(_db, _model_id, **_kw):
            called.append(1)
            return None

        monkeypatch.setattr(atb, "current_source_build_binding", _fake)
        legacy = {
            "model_id": "m-1", "source_connection_id": "conn-a",
            "routing_fingerprint": "fp-old",
        }
        db = _FakeDb([_agg_row(run_id=uuid.uuid4(), source_binding=legacy)])
        with pytest.raises(AggregateGenerationChangedError):
            await assert_aggregate_route_admissible(
                db, bound=_bound(), decision=_agg_decision(uuid.uuid4()),
            )
        assert not called

    @pytest.mark.asyncio
    async def test_a_binding_naming_another_model_is_refused(self, monkeypatch):
        """Round-2 review finding 4. The binding is self-contained by design —
        it names its own model — so a binding left behind from a different
        model would send the guard to resolve the WRONG model's source and then
        approve. Refuse without a live read."""
        called = []
        from shared import artifact_target_binding as atb

        async def _fake(_db, _model_id, **_kw):
            called.append(1)
            return None

        monkeypatch.setattr(atb, "current_source_build_binding", _fake)
        db = _FakeDb([
            _agg_row(
                run_id=uuid.uuid4(),
                model_id="m-2",
                source_binding=self._RECORDED,
            )
        ])
        with pytest.raises(AggregateGenerationChangedError):
            await assert_aggregate_route_admissible(
                db, bound=_bound(), decision=_agg_decision(uuid.uuid4()),
            )
        assert not called

    @pytest.mark.asyncio
    async def test_unchanged_source_still_serves(self, monkeypatch):
        """Control: the source re-proof must not refuse an unchanged build, or
        it would silently drop every aggregate route to source."""
        from shared.artifact_target_binding import ArtifactSourceBuildBinding

        self._live(
            monkeypatch,
            binding=ArtifactSourceBuildBinding("m-1", "conn-a", "proj-1", "fp-old"),
        )
        db = _FakeDb([_agg_row(run_id=uuid.uuid4(), source_binding=self._RECORDED)])
        gen = await assert_aggregate_route_admissible(
            db, bound=_bound(), decision=_agg_decision(uuid.uuid4()),
        )
        assert gen.status == "active"

    @pytest.mark.asyncio
    async def test_unresolvable_live_source_is_refused(self, monkeypatch):
        """Fail closed: an unprovable source identity is not a servable one."""
        self._live(monkeypatch, raises=True)
        db = _FakeDb([_agg_row(run_id=uuid.uuid4(), source_binding=self._RECORDED)])
        with pytest.raises(AggregateGenerationChangedError):
            await assert_aggregate_route_admissible(
                db, bound=_bound(), decision=_agg_decision(uuid.uuid4()),
            )

    @pytest.mark.asyncio
    async def test_model_with_no_resolvable_source_is_refused(self, monkeypatch):
        """``None`` means no source connection or a multi-source model — both
        states a build refuses, so neither may prove a cached build."""
        self._live(monkeypatch, binding=None)
        db = _FakeDb([_agg_row(run_id=uuid.uuid4(), source_binding=self._RECORDED)])
        with pytest.raises(AggregateGenerationChangedError):
            await assert_aggregate_route_admissible(
                db, bound=_bound(), decision=_agg_decision(uuid.uuid4()),
            )

    @pytest.mark.asyncio
    async def test_unusable_recorded_source_binding_is_refused(self, monkeypatch):
        """A recorded binding with no fingerprint cannot say which database its
        rows came from. Refused WITHOUT a live read (which must not even be
        attempted on an unusable record)."""
        called = []
        from shared import artifact_target_binding as atb

        async def _fake(_db, _model_id, **_kw):
            called.append(1)
            return None

        monkeypatch.setattr(atb, "current_source_build_binding", _fake)
        db = _FakeDb([
            _agg_row(run_id=uuid.uuid4(), source_binding={"model_id": "m-1"})
        ])
        with pytest.raises(AggregateGenerationChangedError):
            await assert_aggregate_route_admissible(
                db, bound=_bound(), decision=_agg_decision(uuid.uuid4()),
            )
        assert not called

    @pytest.mark.asyncio
    async def test_aggregate_without_a_recorded_source_binding_still_serves(
        self, monkeypatch
    ):
        """Backward compatibility, identical to the storage-binding rule: an
        aggregate built before the column existed relies on the control-plane
        invalidation half and must not be refused, or upgrading the platform
        would drop all aggregate acceleration until every artifact rebuilds."""
        called = []
        from shared import artifact_target_binding as atb

        async def _fake(_db, _model_id, **_kw):
            called.append(1)
            return None

        monkeypatch.setattr(atb, "current_source_build_binding", _fake)
        db = _FakeDb([_agg_row(run_id=uuid.uuid4(), source_binding=None)])
        gen = await assert_aggregate_route_admissible(
            db, bound=_bound(), decision=_agg_decision(uuid.uuid4()),
        )
        assert gen.status == "active"
        assert not called, (
            "an aggregate with no recorded source binding must not pay for a "
            "live source resolution on every query"
        )


class TestAggregateAdmissionBinding:
    @pytest.mark.asyncio
    async def test_rebuild_between_admission_and_pre_check_is_refused(self):
        """THE Bug-8457 case. A complete rebuild restores ``active``, the same
        location and the same deployed-version binding, so every live check
        passes; only the run id moved. Without the admitted stamp the query is
        served from a generation whose grain/statistics were never matched."""
        admitted_run, live_run = uuid.uuid4(), uuid.uuid4()
        admitted = aggregate_generation_of(_agg_row(run_id=admitted_run))
        db = _FakeDb([_agg_row(run_id=live_run)])

        with pytest.raises(AggregateGenerationChangedError) as exc:
            await assert_aggregate_route_admissible(
                db, bound=_bound(),
                decision=_agg_decision(uuid.uuid4(), admitted=admitted),
            )
        assert "between the route decision and its execution" in str(exc.value)

    @pytest.mark.asyncio
    async def test_same_generation_is_admitted(self):
        run_id = uuid.uuid4()
        admitted = aggregate_generation_of(_agg_row(run_id=run_id))
        db = _FakeDb([_agg_row(run_id=run_id)])
        gen = await assert_aggregate_route_admissible(
            db, bound=_bound(),
            decision=_agg_decision(uuid.uuid4(), admitted=admitted),
        )
        assert gen == admitted


class TestAggregatePostScanStamp:
    def _gen(self, run_id, **over):
        base = dict(
            status="active",
            active_refresh_run_id=str(run_id),
            physical_table_name="agg_sales_by_region",
            target_schema="analytics",
            target_id=str(_TARGET_ID),
        )
        base.update(over)
        return ArtifactGeneration(**base)

    def test_unchanged_generation_passes(self):
        run_id = uuid.uuid4()
        assert_aggregate_generation_unchanged(
            self._gen(run_id), self._gen(run_id), aggregate_id="a"
        )

    @pytest.mark.parametrize(
        "field,value",
        [
            ("status", "pending"),
            ("physical_table_name", "agg_sales_v2"),
            ("target_schema", "analytics_eu"),
            ("target_id", str(uuid.uuid4())),
        ],
    )
    def test_any_field_moving_across_the_scan_discards_the_result(self, field, value):
        run_id = uuid.uuid4()
        with pytest.raises(AggregateGenerationChangedError, match="while its query"):
            assert_aggregate_generation_unchanged(
                self._gen(run_id), self._gen(run_id, **{field: value}),
                aggregate_id="a",
            )

    def test_new_run_id_across_the_scan_discards_the_result(self):
        with pytest.raises(AggregateGenerationChangedError):
            assert_aggregate_generation_unchanged(
                self._gen(uuid.uuid4()), self._gen(uuid.uuid4()), aggregate_id="a",
            )

    def test_vanished_row_across_the_scan_discards_the_result(self):
        with pytest.raises(AggregateGenerationChangedError):
            assert_aggregate_generation_unchanged(
                self._gen(uuid.uuid4()), None, aggregate_id="a",
            )

    @pytest.mark.asyncio
    async def test_read_returns_none_for_a_deleted_aggregate(self):
        assert await read_aggregate_generation(_FakeDb([None]), "a") is None


# ---------------------------------------------------------------------------
# Shared-primitive wiring: the two guards must stay one mechanism, and every
# production route-decision site must populate the stamp.
# ---------------------------------------------------------------------------

def _is_route_decision(func) -> bool:
    """Match both ``RouteDecision(...)`` and ``mod.RouteDecision(...)``.

    Deep-review finding 8: a bare-name-only match is a blind spot — an
    attribute-form construction anywhere in the service would be invisible to
    the enumeration guard, which is precisely the "the verification tool's own
    enumeration logic has a blind spot" category CLAUDE.md asks reviewers to
    hunt. None exist today; this keeps it that way.
    """
    import ast

    if isinstance(func, ast.Name):
        return func.id == "RouteDecision"
    if isinstance(func, ast.Attribute):
        return func.attr == "RouteDecision"
    return False


class TestSharedPrimitiveWiring:
    def test_both_errors_share_one_recovery_type(self):
        """``execute_with_observation`` and the member-discovery path catch the
        BASE error, so a new artifact kind inherits the recovery automatically
        instead of escaping as a 502."""
        assert issubclass(PocketGenerationChangedError, ArtifactGenerationChangedError)
        assert issubclass(
            AggregateGenerationChangedError, ArtifactGenerationChangedError
        )

    def test_the_stamp_is_one_type_for_both_kinds(self):
        run_id = uuid.uuid4()
        pkt = pocket_generation_of(_pkt_row(run_id=run_id, table="t", schema="s"))
        agg = aggregate_generation_of(
            _agg_row(run_id=run_id, table="t", schema="s", status="fresh")
        )
        assert type(pkt) is type(agg) is ArtifactGeneration
        assert pkt == agg

    # Sites that build a cache-typed RouteDecision WITHOUT an admitted
    # generation, each with the reason it is sound. Anything not on this list
    # must carry the stamp. Keyed by (module suffix, function) so moving a site
    # re-opens the question rather than silently inheriting an exemption.
    #
    # This allow-list is the fail-CLOSED half of the enumeration guard: an
    # unrecognised site is a FAILURE, never a pass. CLAUDE.md calls out
    # "the verification tool's own enumeration logic has a blind spot" as a
    # first-class finding category, and the first version of this guard had
    # exactly that — it scanned ``routing/router`` alone, so a cache-serving
    # decision built anywhere else in the service would not have been seen.
    _STAMP_EXEMPT = {
        # Result-cache HIT reconstructions. These never reach
        # ``execute_routed_query`` — they exist only to feed
        # ``_result_freshness`` and ``record_query_success`` with the
        # route_type/artifact id the ORIGINAL execution used. No scan happens,
        # so there is no admission-to-scan window to bind.
        ("api/routes.py", "_cached_response_with_current_freshness"),
        ("api/routes.py", "record_query_cache_hit"),
    }

    @staticmethod
    def _cannot_reach_a_cache_scan(kw) -> bool:
        """True when the decision hard-codes BOTH artifact ids to None.

        ``execute_routed_query`` enters its aggregate branch only on
        ``route_type == "aggregate" and decision.aggregate_id`` (and the pocket
        branch likewise), so a decision that literally passes
        ``aggregate_id=None, pocket_id=None`` can never scan a cache table
        whatever its route_type resolves to. Deriving the exemption from the
        SAME condition the executor branches on makes it self-maintaining —
        unlike a name-keyed entry, it cannot go stale when a function is
        renamed, and it tightens automatically if the executor's condition is
        ever changed to not require an id.
        """
        import ast

        def _is_none(name):
            node = kw.get(name)
            return isinstance(node, ast.Constant) and node.value is None

        return _is_none("aggregate_id") and _is_none("pocket_id")

    def test_every_cache_serving_route_decision_populates_the_stamp(self):
        """The binding is only worth anything if the PRODUCTION creation sites
        set it. Any cache-typed ``RouteDecision`` anywhere in the query-router
        must carry an ``admitted_generation`` or be explicitly exempted above,
        or the guard silently degrades to the live-only re-proof for that path —
        the exact blind spot Bug-8455 was.
        """
        import ast
        import pathlib

        import src

        root = pathlib.Path(src.__file__).resolve().parent
        unbound: list[str] = []
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:  # pragma: no cover - defensive
                continue
            # Map each node to its INNERMOST enclosing function. ``ast.walk``
            # is breadth-first, so a plain ``setdefault`` would attribute a
            # call inside a nested function to the OUTER one and could mis-key
            # an exemption (deep-review finding 8). Walking outer-to-inner and
            # overwriting gives the innermost name.
            enclosing: dict[int, str] = {}

            def _assign(node, name):
                for sub in ast.iter_child_nodes(node):
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        _assign(sub, sub.name)
                    else:
                        enclosing[id(sub)] = name
                        _assign(sub, name)

            for fn in ast.iter_child_nodes(tree):
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    _assign(fn, fn.name)
                else:
                    _assign(fn, "<module>")
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and _is_route_decision(node.func)):
                    continue
                kw = {k.arg: k.value for k in node.keywords}
                route_type = kw.get("route_type")
                # A non-literal route_type (a variable) is NOT assumed safe:
                # it may resolve to "aggregate"/"pocket" at runtime, so it must
                # be exempted explicitly like any other unstamped site.
                literal_cache = (
                    isinstance(route_type, ast.Constant)
                    and route_type.value in ("aggregate", "pocket")
                )
                dynamic = not isinstance(route_type, ast.Constant)
                if not (literal_cache or dynamic):
                    continue
                if "admitted_generation" in kw:
                    continue
                if self._cannot_reach_a_cache_scan(kw):
                    continue
                key = (rel, enclosing.get(id(node), "<module>"))
                if key in self._STAMP_EXEMPT:
                    continue
                unbound.append(f"{rel}:{node.lineno} in {key[1]}()")
        assert unbound == [], (
            "cache-serving RouteDecision(s) with no admitted_generation and no "
            f"documented exemption: {unbound}. The execution guard would "
            "re-prove whatever is live instead of the admitted build."
        )

    def test_the_exemptions_still_point_at_real_sites(self):
        """A stale exemption is a silent hole: the site it excused could be
        renamed or moved and a NEW unstamped site take its place."""
        import ast
        import pathlib

        import src

        root = pathlib.Path(src.__file__).resolve().parent
        live: set[tuple[str, str]] = set()
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:  # pragma: no cover - defensive
                continue
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for sub in ast.walk(fn):
                    if isinstance(sub, ast.Call) and _is_route_decision(sub.func):
                        live.add((rel, fn.name))
        stale = sorted(self._STAMP_EXEMPT - live)
        assert stale == [], f"exemptions no longer match a real site: {stale}"

    def test_the_enumeration_guard_can_actually_see_a_gap(self):
        """Coverage-tool blind-spot check (CLAUDE.md): prove the AST scan above
        FAILS on an unbound site rather than silently finding nothing."""
        import ast

        src = (
            "def f():\n"
            "    return RouteDecision(route_type='aggregate', aggregate_id=x)\n"
        )
        tree = ast.parse(src)
        found = [
            n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and getattr(n.func, "id", None) == "RouteDecision"
            and any(
                k.arg == "route_type" and isinstance(k.value, ast.Constant)
                and k.value.value in ("aggregate", "pocket")
                for k in n.keywords
            )
            and "admitted_generation" not in {k.arg for k in n.keywords}
        ]
        assert found == [2]


# ---------------------------------------------------------------------------
# Through the REAL execution chokepoint (execute_routed_query)
#
# The guard being correct in isolation is not the property that matters: the
# aggregate branch previously had no guard call at all, so a unit-level guard
# test would pass while production stayed exposed. These exercise the real
# ``routes.execute_routed_query`` aggregate branch.
# ---------------------------------------------------------------------------

from unittest.mock import AsyncMock, patch  # noqa: E402

from src.api import routes as routes_mod  # noqa: E402


class _AggExecDb(_FakeDb):
    """Adds the ORM ``get`` calls ``execute_routed_query`` makes."""

    def __init__(self, rows, aggregate, target):
        super().__init__(rows)
        self._aggregate = aggregate
        self._target = target

    async def get(self, cls, key):
        if cls.__name__ == "AggregateDefinition":
            return self._aggregate
        if cls.__name__ == "DataTarget":
            return self._target
        return None


def _exec_aggregate(run_id):
    return types.SimpleNamespace(
        id=uuid.uuid4(), target_id=_TARGET_ID,
        physical_table_name="agg_sales_by_region", target_schema="analytics",
        active_refresh_run_id=run_id, status="active",
    )


def _exec_target():
    return types.SimpleNamespace(
        id=_TARGET_ID, project_connection_id=uuid.uuid4(),
        target_type="postgresql",
        config={"schema": "analytics"},
    )


def _exec_connection():
    return types.SimpleNamespace(connection_type="postgresql", config={})


@pytest.mark.asyncio
async def test_aggregate_execution_returns_rows_when_the_generation_holds():
    run_id = uuid.uuid4()
    agg = _exec_aggregate(run_id)
    target = _exec_target()
    db = _AggExecDb([_agg_row(run_id=run_id), _agg_row(run_id=run_id)], agg, target)
    decision = _agg_decision(
        agg.id, admitted=aggregate_generation_of(_agg_row(run_id=run_id))
    )

    with patch.object(
        routes_mod, "resolve_endpoint_connection",
        AsyncMock(return_value=_exec_connection()),
    ), patch.object(
        routes_mod, "execute_on_connection",
        AsyncMock(return_value=([{"region_code": "NORTH"}], 10, ["region_code"])),
    ):
        rows, _bytes, cols, endpoint = await routes_mod.execute_routed_query(
            _bound(), decision, db
        )

    assert rows == [{"region_code": "NORTH"}]
    assert cols == ["region_code"]
    assert endpoint is target


@pytest.mark.asyncio
async def test_aggregate_execution_discards_rows_when_a_rebuild_lands_mid_scan():
    """Bug-8457's post-scan half. The pre-scan proof passed, the scan ran, and
    the aggregate was rebuilt underneath it. The rows must NEVER be returned:
    they may have been read from a generation with a different grain or a
    different stored statistic — silently wrong numbers."""
    run_id = uuid.uuid4()
    agg = _exec_aggregate(run_id)
    db = _AggExecDb(
        [_agg_row(run_id=run_id), _agg_row(run_id=uuid.uuid4())],
        agg, _exec_target(),
    )
    decision = _agg_decision(agg.id)

    wrong_rows = [{"region_code": "NORTH", "amount": 999}]
    with patch.object(
        routes_mod, "resolve_endpoint_connection",
        AsyncMock(return_value=_exec_connection()),
    ), patch.object(
        routes_mod, "execute_on_connection",
        AsyncMock(return_value=(wrong_rows, 10, ["region_code", "amount"])),
    ):
        with pytest.raises(AggregateGenerationChangedError):
            await routes_mod.execute_routed_query(_bound(), decision, db)


@pytest.mark.asyncio
async def test_aggregate_execution_refuses_before_scanning_an_unproven_build():
    """A rebuild that completed BEFORE the scan started is caught by the
    pre-check, so no query is ever sent to the aggregate table."""
    agg = _exec_aggregate(uuid.uuid4())
    db = _AggExecDb(
        [_agg_row(run_id=uuid.uuid4(), status="pending")], agg, _exec_target()
    )
    decision = _agg_decision(agg.id)

    exec_mock = AsyncMock(return_value=([], 0, []))
    with patch.object(
        routes_mod, "resolve_endpoint_connection",
        AsyncMock(return_value=_exec_connection()),
    ), patch.object(routes_mod, "execute_on_connection", exec_mock):
        with pytest.raises(AggregateGenerationChangedError):
            await routes_mod.execute_routed_query(_bound(), decision, db)

    exec_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_aggregate_execution_refuses_a_generation_the_matcher_never_admitted():
    """Bug-8457's admission-binding half, through the real chokepoint: the live
    aggregate is perfectly healthy and passes every live gate, but it is not the
    build the matcher's grain/measure coverage proof was made against."""
    agg = _exec_aggregate(uuid.uuid4())
    live_run = uuid.uuid4()
    db = _AggExecDb([_agg_row(run_id=live_run)], agg, _exec_target())
    decision = _agg_decision(
        agg.id, admitted=aggregate_generation_of(_agg_row(run_id=uuid.uuid4()))
    )

    exec_mock = AsyncMock(return_value=([], 0, []))
    with patch.object(
        routes_mod, "resolve_endpoint_connection",
        AsyncMock(return_value=_exec_connection()),
    ), patch.object(routes_mod, "execute_on_connection", exec_mock):
        with pytest.raises(AggregateGenerationChangedError):
            await routes_mod.execute_routed_query(_bound(), decision, db)

    exec_mock.assert_not_awaited()


class TestStorageBindingProducerConsumerParity:
    """Deep-review finding 12. ``_binding_matches`` compares a fingerprint
    RECORDED by the optimizer/scheduler process against one RE-DERIVED here in
    the query-router process. If those two ever computed it differently, EVERY
    aggregate carrying a recorded binding would be refused and all aggregate
    acceleration would silently collapse to source — a total, invisible
    performance regression on the highest-traffic route.

    They cannot diverge because both sides funnel through ONE helper. That is a
    structural property, so it is pinned structurally rather than left to a
    live probe.
    """

    def test_the_producer_captures_through_the_same_helper_the_guard_reads(self):
        import inspect

        from shared import artifact_target_binding as atb

        producer_src = inspect.getsource(atb.capture_target_build_binding)
        assert "resolve_target_binding_dict(" in producer_src, (
            "the build-time capture no longer routes through "
            "resolve_target_binding_dict; the serve-time guard re-derives with "
            "that helper, so the two would compute different fingerprints and "
            "refuse every aggregate"
        )

        from src.routing import aggregate_generation_guard as agg_guard
        from src.routing import pocket_generation_guard as pkt_guard

        for mod in (agg_guard, pkt_guard):
            guard_src = inspect.getsource(mod._binding_matches)
            assert "resolve_target_binding_dict(" in guard_src, mod.__name__

    def test_the_source_producer_and_guard_share_one_fingerprint_helper(self):
        """Bug-8602: the same structural property, for the source side.

        ``capture_source_build_binding`` (optimizer/scheduler) and the
        query-router's re-derivation must both funnel through
        ``resolve_source_binding_dict``. If they forked, every aggregate
        carrying a recorded SOURCE binding would be refused and aggregate
        acceleration would collapse to source invisibly.
        """
        import inspect

        from shared import artifact_target_binding as atb

        assert "resolve_source_binding_dict(" in inspect.getsource(
            atb.capture_source_build_binding
        )
        assert "capture_source_build_binding(" in inspect.getsource(
            atb.current_source_build_binding
        ), (
            "the live source read no longer builds its binding through the "
            "capture helper, so the recorded and live fingerprints can diverge"
        )
        from src.routing import aggregate_generation_guard as agg_guard

        assert "source_build_binding_matches_live(" in inspect.getsource(
            agg_guard._source_binding_matches
        )

    def test_the_source_guard_is_actually_called_by_the_admission_gate(self):
        """A binding nobody reads is a write-only column. Pin the wiring: the
        admission gate must invoke the source re-proof, not merely define it."""
        import inspect

        from src.routing import aggregate_generation_guard as agg_guard

        src = inspect.getsource(agg_guard.assert_aggregate_route_admissible)
        assert "_source_binding_matches(db, row)" in src
        assert (
            AggregateDefinition.built_for_source_binding
            in agg_guard._ADMISSION_COLUMNS
        ), (
            "built_for_source_binding is not read by the admission query, so "
            "the guard would raise AttributeError or read a stale ORM value"
        )

    def test_both_guards_pass_the_same_session_arguments(self):
        """The helper falls back to a system session for persisted
        ``source_db.fallback_*`` values. The aggregate guard must not resolve
        with a different session set than the pocket guard, which has been in
        production since Bug-8473."""
        import inspect
        import re

        from src.routing import aggregate_generation_guard as agg_guard
        from src.routing import pocket_generation_guard as pkt_guard

        pattern = re.compile(r"resolve_target_binding_dict\((.*?)\)\n", re.S)

        def _kwargs(mod):
            src = inspect.getsource(mod._binding_matches)
            call = pattern.search(src)
            assert call, "could not read the call in " + mod.__name__
            return set(re.findall(r"(\w+)=", call.group(1)))

        assert _kwargs(agg_guard) == _kwargs(pkt_guard)
