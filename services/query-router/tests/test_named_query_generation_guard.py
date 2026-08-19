"""Named Query generation guard — admission-to-scan race + live re-proof.

The Named Query materialised fast-path reuses the pocket generation guard
machinery (Bug-8392/Bug-8455/Bug-8473/Bug-8780) verbatim. A Named Query result
table is REUSED in place across refreshes, so the serving decision names a
table, not the generation of it the router proved admissible. Everything the
RLS gate proved — fresh status, version binding, storage binding, row-manifest
column coverage for the projection proof — was proved against the row read at
admission. If a refresh (or a target/source re-point, or an invalidation)
completes before the scan, the query would read a different generation.

Pinned here:

* the before/after stamp across the scan must be equal, or the caller discards
  rows and falls back to live execution;
* ``read_named_query_generation`` returns None when the artifact row is gone
  (fail closed);
* ``assert_named_query_route_admissible`` re-proves EVERY admission gate
  against live state — row exists, still fresh, the rewritten SQL still names
  the live physical location, the build still matches the deployed pointer,
  the live manifest still proves security-column coverage for the admitted
  definition — and binds the result to the ADMITTED generation stamp.

Test escape: no test exercised the Named Query serving guard against an
artifact whose live row had moved on since admission, and the guard module's
unchanged-check import was only exercised at runtime (a wrong symbol name
would surface only on the first materialised serve).
Guard: this file. Tier: T1.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.named_query.population_contract import (
    named_query_population_fingerprint,
)
from src.routing.artifact_generation_guard import (
    ArtifactGeneration,
    ArtifactGenerationChangedError,
)
from src.routing.named_query_generation_guard import (
    NamedQueryGenerationChangedError,
    assert_named_query_generation_unchanged,
    assert_named_query_route_admissible,
    read_named_query_generation,
)

pytestmark = pytest.mark.unit

_VERSION = uuid.uuid4()
_ARTIFACT_ID = uuid.uuid4()
_NQ_ID = uuid.uuid4()
_TARGET_ID = uuid.uuid4()
_MODEL_ID = uuid.uuid4()

# The population-contract fingerprint a current build stamps (NQ-2/Bug-9161
# corrected Phase 1). Every positive-path fixture must carry it; the guard
# re-proves it before the scan, so a None/mismatched fingerprint refuses.
_FP = named_query_population_fingerprint(
    model_id=_MODEL_ID,
    named_query_id=_NQ_ID,
    deployed_version_id=_VERSION,
    deploy_epoch=7,
    definition_sql='SELECT "branch_id", "amount" FROM modely',
)
_POPULATION_MANIFEST = {
    "manifest_version": 2,
    "build_refresh_run_id": "run-1",
    "row_definition_fingerprint": _FP,
}


def _stamp(**overrides) -> ArtifactGeneration:
    base = dict(
        status="fresh",
        active_refresh_run_id="run-1",
        physical_table_name="nq_acme_abc123",
        target_schema="public",
        target_id=str(_TARGET_ID),
    )
    base.update(overrides)
    return ArtifactGeneration(**base)


def _row(**overrides) -> types.SimpleNamespace:
    base = dict(
        id=_ARTIFACT_ID,
        status="fresh",
        active_refresh_run_id="run-1",
        physical_table_name="nq_acme_abc123",
        target_schema="public",
        target_id=_TARGET_ID,
        built_for_version_id=str(_VERSION),
        built_for_epoch=7,
        row_manifest=dict(_POPULATION_MANIFEST),
        model_id=None,
        definition_sql=None,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# read_named_query_generation
# ---------------------------------------------------------------------------


async def test_read_generation_returns_stamp_and_none_when_row_gone() -> None:
    fetch = AsyncMock(return_value=_row())
    with patch(
        "src.routing.named_query_generation_guard.fetch_columns", fetch
    ):
        stamp = await read_named_query_generation(MagicMock(), _ARTIFACT_ID)
    assert stamp == _stamp()

    fetch.return_value = None
    with patch(
        "src.routing.named_query_generation_guard.fetch_columns", fetch
    ):
        assert await read_named_query_generation(MagicMock(), _ARTIFACT_ID) is None


# ---------------------------------------------------------------------------
# assert_named_query_generation_unchanged
# ---------------------------------------------------------------------------


async def test_unchanged_generation_accepted() -> None:
    await assert_named_query_generation_unchanged(
        _stamp(), _stamp(), artifact_id=_ARTIFACT_ID
    )


async def test_changed_generation_refused() -> None:
    with pytest.raises(NamedQueryGenerationChangedError):
        await assert_named_query_generation_unchanged(
            _stamp(), _stamp(active_refresh_run_id="run-2"),
            artifact_id=_ARTIFACT_ID,
        )


async def test_artifact_row_gone_mid_scan_refused() -> None:
    with pytest.raises(NamedQueryGenerationChangedError):
        await assert_named_query_generation_unchanged(
            _stamp(), None, artifact_id=_ARTIFACT_ID
        )


async def test_nq_change_error_is_a_generation_error() -> None:
    assert issubclass(NamedQueryGenerationChangedError, ArtifactGenerationChangedError)


# ---------------------------------------------------------------------------
# assert_named_query_route_admissible — live re-proof legs
# ---------------------------------------------------------------------------

def _decision(**overrides) -> MagicMock:
    base = dict(
        rewritten_query="SELECT * FROM \"public\".\"nq_acme_abc123\"",
        target_dialect="postgresql",
        admitted_generation=_stamp(),
    )
    base.update(overrides)
    return MagicMock(**base)


def _model(**overrides) -> MagicMock:
    base = dict(deployed_version_id=_VERSION, deploy_epoch=7, id=uuid.uuid4())
    base.update(overrides)
    return MagicMock(**base)


async def test_admissible_success_returns_live_generation() -> None:
    with (
        patch(
            "src.routing.named_query_generation_guard.fetch_columns",
            AsyncMock(return_value=_row()),
        ),
        patch(
            "src.routing.named_query_generation_guard._read_named_query_row",
            AsyncMock(
                return_value=MagicMock(
                    model_id=uuid.uuid4(), definition_sql="SELECT * FROM modely",
                )
            ),
        ),
        patch(
            "src.routing.named_query_generation_guard._binding_matches",
            AsyncMock(return_value=True),
        ),
        patch(
            "src.routing.named_query_generation_guard._source_binding_matches",
            AsyncMock(return_value=True),
        ),
        patch(
            "shared.config.source_db.target_connection_authority_is_provable",
            MagicMock(return_value=True),
        ),
    ):
        live = await assert_named_query_route_admissible(
            MagicMock(),
            named_query_id=_NQ_ID,
            artifact_id=_ARTIFACT_ID,
            decision=_decision(),
            model=_model(),
            target=MagicMock(),
            conn=MagicMock(),
            admitted_definition_sql="SELECT * FROM modely",
            expected_population_fingerprint=_FP,
        )
    assert live == _stamp()


async def test_admissible_refuses_missing_artifact_row() -> None:
    fetch = AsyncMock(return_value=None)
    with patch(
        "src.routing.named_query_generation_guard.fetch_columns", fetch
    ):
        with pytest.raises(NamedQueryGenerationChangedError):
            await assert_named_query_route_admissible(
                MagicMock(),
                named_query_id=_NQ_ID,
                artifact_id=_ARTIFACT_ID,
                decision=_decision(),
                model=_model(),
                target=None,
                conn=None,
                expected_population_fingerprint=_FP,
            )


async def test_admissible_refuses_non_fresh_status() -> None:
    fetch = AsyncMock(return_value=_row(status="stale"))
    with patch(
        "src.routing.named_query_generation_guard.fetch_columns", fetch
    ):
        with pytest.raises(NamedQueryGenerationChangedError) as excinfo:
            await assert_named_query_route_admissible(
                MagicMock(),
                named_query_id=_NQ_ID,
                artifact_id=_ARTIFACT_ID,
                decision=_decision(),
                model=_model(),
                target=None,
                conn=None,
                expected_population_fingerprint=_FP,
            )
    assert "no longer fresh" in str(excinfo.value)


async def test_admissible_refuses_moved_physical_location() -> None:
    # The rewritten SQL still names the OLD table while the live row moved.
    fetch = AsyncMock(
        return_value=_row(physical_table_name="nq_acme_OTHER")
    )
    with patch(
        "src.routing.named_query_generation_guard.fetch_columns", fetch
    ):
        with pytest.raises(NamedQueryGenerationChangedError) as excinfo:
            await assert_named_query_route_admissible(
                MagicMock(),
                named_query_id=_NQ_ID,
                artifact_id=_ARTIFACT_ID,
                decision=_decision(),
                model=_model(),
                target=None,
                conn=None,
                expected_population_fingerprint=_FP,
            )
    assert "no longer lives where" in str(excinfo.value)


async def test_admissible_refuses_version_gate_mismatch() -> None:
    # Live row was built for a DIFFERENT deployed pointer than the model now
    # serving — the artifact must not serve after a deploy/revert.
    fetch = AsyncMock(
        return_value=_row(built_for_version_id=str(uuid.uuid4()), built_for_epoch=1)
    )
    with patch(
        "src.routing.named_query_generation_guard.fetch_columns", fetch
    ):
        with pytest.raises(NamedQueryGenerationChangedError) as excinfo:
            await assert_named_query_route_admissible(
                MagicMock(),
                named_query_id=_NQ_ID,
                artifact_id=_ARTIFACT_ID,
                decision=_decision(),
                model=_model(),
                target=None,
                conn=None,
                expected_population_fingerprint=_FP,
            )
    assert "different deployed model version" in str(excinfo.value)


async def test_admissible_reproves_security_proof_on_live_manifest() -> None:
    # The admission-time proof held; the LIVE manifest no longer covers the
    # security column -> refuse, never serve unproven rows. The source-binding
    # leg is patched True (pinned by its own pocket-guard tests) so this test
    # isolates the security-proof re-check.
    fetch = AsyncMock(
        return_value=_row(
            row_manifest={
                **_POPULATION_MANIFEST,
                "columns": [{"logical_name": "other_col"}],
            }
        )
    )
    nq_row = AsyncMock(
        return_value=MagicMock(
            model_id=uuid.uuid4(),
            definition_sql="SELECT * FROM modely WHERE branch_id = 'x'",
        )
    )
    security_compiled = MagicMock(
        mapping_source_ids=(), security_dimension_columns=("branch_id",)
    )
    with (
        patch(
            "src.routing.named_query_generation_guard.fetch_columns", fetch
        ),
        patch(
            "src.routing.named_query_generation_guard._read_named_query_row",
            nq_row,
        ),
        patch(
            "src.routing.named_query_generation_guard._source_binding_matches",
            AsyncMock(return_value=True),
        ),
    ):
        with pytest.raises(NamedQueryGenerationChangedError) as excinfo:
            await assert_named_query_route_admissible(
                MagicMock(),
                named_query_id=_NQ_ID,
                artifact_id=_ARTIFACT_ID,
                decision=_decision(),
                model=_model(),
                target=None,
                conn=None,
                expected_population_fingerprint=_FP,
                admitted_definition_sql=None,
                security_compiled=security_compiled,
            )
    assert "no longer provably" in str(excinfo.value)


async def test_admissible_refuses_missing_source_binding() -> None:
    # A manifest with no source_binding cannot prove which database the rows
    # came from -> refused fail-closed (Bug-8780 contract). No DB touch: the
    # missing key refuses before any live resolution.
    fetch = AsyncMock(return_value=_row())
    nq_row = AsyncMock(
        return_value=MagicMock(
            model_id=uuid.uuid4(), definition_sql="SELECT * FROM modely",
        )
    )
    with (
        patch(
            "src.routing.named_query_generation_guard.fetch_columns", fetch
        ),
        patch(
            "src.routing.named_query_generation_guard._read_named_query_row",
            nq_row,
        ),
    ):
        with pytest.raises(NamedQueryGenerationChangedError) as excinfo:
            await assert_named_query_route_admissible(
                MagicMock(),
                named_query_id=_NQ_ID,
                artifact_id=_ARTIFACT_ID,
                decision=_decision(),
                model=_model(),
                target=None,
                conn=None,
                expected_population_fingerprint=_FP,
                admitted_definition_sql="SELECT * FROM modely",
            )
    assert "different source database" in str(excinfo.value)


async def test_admissible_refuses_admitted_generation_mismatch() -> None:
    # The live row is fine, but it is a DIFFERENT build than the one the
    # serving decision was proved against (Bug-8455 contract).
    fetch = AsyncMock(return_value=_row(active_refresh_run_id="run-99"))
    nq_row = AsyncMock(
        return_value=MagicMock(
            model_id=uuid.uuid4(), definition_sql="SELECT * FROM modely",
        )
    )
    with (
        patch(
            "src.routing.named_query_generation_guard.fetch_columns", fetch
        ),
        patch(
            "src.routing.named_query_generation_guard._read_named_query_row",
            nq_row,
        ),
        patch(
            "src.routing.named_query_generation_guard._source_binding_matches",
            AsyncMock(return_value=True),
        ),
    ):
        with pytest.raises(NamedQueryGenerationChangedError):
            await assert_named_query_route_admissible(
                MagicMock(),
                named_query_id=_NQ_ID,
                artifact_id=_ARTIFACT_ID,
                decision=_decision(),
                model=_model(),
                target=None,
                conn=None,
                expected_population_fingerprint=_FP,
                admitted_definition_sql="SELECT * FROM modely",
            )


def test_admission_columns_are_orm_not_strings() -> None:
    """Bug-9189: ``fetch_columns`` does ``select(*columns)``; a bare string column
    (e.g. ``"status"``) raises SQLAlchemy ArgumentError at runtime, so every
    materialised @nq serve 500'd at the admission guard and fell over. The guard
    columns MUST be ``NamedQueryArtifact`` ORM expressions, never name strings.
    """
    from src.routing import named_query_generation_guard as g

    assert g._GENERATION_COLUMNS, "expected generation columns"
    assert g._ADMISSION_COLUMNS, "expected admission columns"
    for col in g._ADMISSION_COLUMNS:
        assert not isinstance(col, str), (
            f"admission column {col!r} is a str — select(*columns) rejects bare "
            f"strings under SQLAlchemy 2.0; use the NamedQueryArtifact ORM column"
        )


def test_row_with_model_only_reads_selected_columns() -> None:
    """Bug-9189: _row_with_model must read ONLY attributes that _ADMISSION_COLUMNS
    selects. The real column-SELECT row carries exactly those columns, so reading
    any other (e.g. row.id when id was never selected) raises AttributeError on
    every materialised @nq serve. This builds a row with exactly the selected
    column keys and asserts _row_with_model does not touch anything else.
    """
    import types as _types

    from src.routing import named_query_generation_guard as g

    selected_keys = {c.key for c in g._ADMISSION_COLUMNS}
    fake_row = _types.SimpleNamespace(**{k: None for k in selected_keys})
    # Must not raise AttributeError for a column that was not selected.
    ns = g._row_with_model(fake_row, None)
    assert ns is not None
