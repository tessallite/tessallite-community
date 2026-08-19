"""Tests for shared.artifact_version_gate (F-013-02 / Bug-8250).

Verifies the comparison rules used by both matchers and the deploy staling
helper to decide whether a materialised artifact is compatible with the model's
currently-deployed version.

Two things are proven here:

1. The Python comparator fails CLOSED on every NULL input, including a NULL
   epoch. The pre-fix version coerced ``None`` to ``0``, and this file's
   ``test_epoch_none_vs_zero`` asserted that coercion as CORRECT — a test
   actively locking in the unsafe behaviour on a wrong-numbers gate. It now
   asserts the opposite.
2. The SQL encoding used by deploy/revert staling and the Python encoding used
   by the runtime matchers agree on the WHOLE truth table. Deploy staling
   cannot call a per-row Python predicate, so a second encoding is unavoidable;
   proving the two agree is what makes "single source of truth" a fact rather
   than a claim. The parity test runs the real SQLAlchemy expression against a
   real SQL engine, so SQL's three-valued NULL logic is exercised, not assumed.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, select

from shared.artifact_version_gate import (
    artifact_built_for_current,
    artifact_incompatible_sql,
)


def test_matching_version_and_epoch():
    assert artifact_built_for_current("v1", 0, "v1", 0) is True


def test_matching_uuid_version():
    vid = uuid.uuid4()
    assert artifact_built_for_current(vid, 3, vid, 3) is True


def test_str_vs_uuid_match():
    vid = uuid.uuid4()
    assert artifact_built_for_current(str(vid), 1, vid, 1) is True


def test_version_mismatch():
    assert artifact_built_for_current("v1", 0, "v2", 0) is False


def test_epoch_mismatch():
    assert artifact_built_for_current("v1", 0, "v1", 1) is False


def test_null_built_for():
    assert artifact_built_for_current(None, None, "v1", 0) is False


def test_null_deployed():
    assert artifact_built_for_current("v1", 0, None, 0) is False


def test_both_null():
    assert artifact_built_for_current(None, None, None, None) is False


def test_null_built_epoch_is_incompatible():
    """Bug-8250 re-gate: a half-written binding must never serve.

    Every writer stamps version and epoch together through
    ``artifact_build_binding``, so a non-NULL version beside a NULL epoch is a
    row of unknown provenance. Coercing it to epoch 0 let it serve against a
    never-redeployed model — fail OPEN on a wrong-numbers gate.
    """
    assert artifact_built_for_current("v1", None, "v1", 0) is False


def test_null_deployed_epoch_is_incompatible():
    """``Model.deploy_epoch`` is NOT NULL DEFAULT 0; a NULL is refused, not coerced."""
    assert artifact_built_for_current("v1", 0, "v1", None) is False


def test_epoch_string_normalisation():
    assert artifact_built_for_current("v1", "2", "v1", 2) is True


def test_uninterpretable_epoch_is_incompatible():
    assert artifact_built_for_current("v1", "not-a-number", "v1", 0) is False


# ---------------------------------------------------------------------------
# SQL / Python parity
# ---------------------------------------------------------------------------

_VID_A = "11111111-1111-1111-1111-111111111111"
_VID_B = "22222222-2222-2222-2222-222222222222"

#: (built_for_version_id, built_for_epoch) rows spanning every shape a stored
#: artifact binding can take, including the half-written and NULL ones.
_ARTIFACT_ROWS = [
    (_VID_A, 0),
    (_VID_A, 1),
    (_VID_A, None),
    (_VID_B, 0),
    (_VID_B, None),
    (None, 0),
    (None, None),
]

#: Deployed pointers to evaluate every artifact row against, including undeploy.
_DEPLOYED = [
    (_VID_A, 0),
    (_VID_A, 1),
    (_VID_B, 0),
    (None, 0),
]


@pytest.fixture(scope="module")
def artifact_table():
    metadata = MetaData()
    table = Table(
        "artifacts",
        metadata,
        Column("rowid", Integer, primary_key=True),
        Column("built_for_version_id", String),
        Column("built_for_epoch", Integer),
    )
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            table.insert(),
            [
                {"rowid": i, "built_for_version_id": v, "built_for_epoch": e}
                for i, (v, e) in enumerate(_ARTIFACT_ROWS)
            ],
        )
    yield engine, table
    engine.dispose()


@pytest.mark.parametrize("deployed_version_id,deploy_epoch", _DEPLOYED)
def test_sql_and_python_agree_on_the_whole_truth_table(
    artifact_table, deployed_version_id, deploy_epoch
):
    """The staling UPDATE must select EXACTLY the rows the matcher refuses.

    A disagreement in either direction is a live defect: SQL-stales-but-Python-
    serves means a matcher routes to an artifact deploy already declared
    superseded; Python-refuses-but-SQL-skips means a permanently unusable
    artifact is never marked for rebuild.
    """
    engine, table = artifact_table
    predicate = artifact_incompatible_sql(
        table.c.built_for_version_id,
        table.c.built_for_epoch,
        deployed_version_id,
        deploy_epoch,
    )
    with engine.connect() as conn:
        sql_incompatible = {
            row[0] for row in conn.execute(select(table.c.rowid).where(predicate))
        }

    python_incompatible = {
        i
        for i, (vid, epoch) in enumerate(_ARTIFACT_ROWS)
        if not artifact_built_for_current(vid, epoch, deployed_version_id, deploy_epoch)
    }
    assert sql_incompatible == python_incompatible, (
        f"SQL and Python disagree for deployed=({deployed_version_id}, "
        f"{deploy_epoch}): sql_only={sorted(sql_incompatible - python_incompatible)} "
        f"python_only={sorted(python_incompatible - sql_incompatible)}"
    )


def test_sql_null_epoch_row_is_selected_not_skipped(artifact_table):
    """Regression guard for the three-valued-logic trap.

    ``built_for_epoch != 0`` is NULL — not TRUE — for a NULL epoch, so a
    predicate built only from ``!=`` silently SKIPS the half-written rows. Those
    are exactly the rows the Python gate refuses, so skipping them would leave a
    permanently unservable artifact unmarked for rebuild.
    """
    engine, table = artifact_table
    predicate = artifact_incompatible_sql(
        table.c.built_for_version_id, table.c.built_for_epoch, _VID_A, 0
    )
    with engine.connect() as conn:
        selected = {
            row[0] for row in conn.execute(select(table.c.rowid).where(predicate))
        }
    # index 2 is (_VID_A, None) — matching version, NULL epoch.
    assert 2 in selected


def test_sql_undeploy_selects_every_artifact(artifact_table):
    engine, table = artifact_table
    predicate = artifact_incompatible_sql(
        table.c.built_for_version_id, table.c.built_for_epoch, None, 0
    )
    with engine.connect() as conn:
        selected = {
            row[0] for row in conn.execute(select(table.c.rowid).where(predicate))
        }
    assert selected == set(range(len(_ARTIFACT_ROWS)))
