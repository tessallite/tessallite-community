"""The shared join-keyword contract and the Bug-8628 rebuild predicate.

The RENDER contract (which keyword each token produces, flipped and unflipped)
is exercised end-to-end through both builders in
``query-router/tests/test_bug_7775_outer_join_types.py`` and
``query-router/tests/test_from_builder_cross_builder_equivalence.py``. This
file covers the surface those cannot reach: the orientation/cardinality SPLIT
and the artifact-invalidation predicate that decides which stored artifacts
the orientation fix makes wrong.
"""
from __future__ import annotations

import importlib.util
import types

import pytest

from shared.semantic.join_keyword import (
    CANONICAL_JOIN_TYPES,
    is_orientation_declared,
    join_keyword,
    normalise_cardinality,
    split_join_token,
)
from shared.semantic.join_orientation_invalidation import (
    creator_ctas_rendering_changed,
    creator_model_ids_needing_rebuild,
    ctas_rendering_changed,
    model_ids_needing_rebuild,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Orientation / cardinality split (contract invariant 3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("token", CANONICAL_JOIN_TYPES)
def test_an_orientation_token_declares_no_cardinality(token):
    join_type, cardinality = split_join_token(token)
    assert join_type == token
    assert cardinality is None, (
        "an orientation token must not be read as a fan-out declaration"
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("left_outer", "left"),
        ("LEFT OUTER", "left"),
        ("right_outer", "right"),
        ("full outer", "full"),
        ("  Inner ", "inner"),
    ],
)
def test_long_spellings_fold_onto_the_canonical_four(raw, expected):
    join_type, cardinality = split_join_token(raw)
    assert (join_type, cardinality) == (expected, None)


@pytest.mark.parametrize(
    "raw,expected_join_type,expected_cardinality",
    [
        # The many side is the modeller's LEFT table -> preserve it with LEFT.
        ("many_to_one", "left", "many_to_one"),
        ("many-to-one", "left", "many_to_one"),
        # The many side is the modeller's RIGHT table -> preserve it with RIGHT.
        ("one_to_many", "right", "one_to_many"),
        ("one-to-many", "right", "one_to_many"),
        ("one_to_one", "left", "one_to_one"),
        ("many_to_many", "left", "many_to_many"),
    ],
)
def test_a_cardinality_token_is_moved_out_of_the_join_type_field(
    raw, expected_join_type, expected_cardinality
):
    """The whole point of the split: a fan-out label must never remain in the
    field that decides which rows survive."""
    join_type, cardinality = split_join_token(raw)
    assert cardinality == expected_cardinality
    assert join_type == expected_join_type
    assert join_type in CANONICAL_JOIN_TYPES


@pytest.mark.parametrize("raw", ["", None, "banana", "cross", "outer"])
def test_an_unclassifiable_token_yields_neither_field(raw):
    """The caller keeps its own default and the raw value is left for the
    renderer to coerce — never guessed at."""
    assert split_join_token(raw) == (None, None)


@pytest.mark.parametrize("raw", ["banana", "many_to_one", "", None, "outer"])
def test_normalise_cardinality_rejects_non_cardinalities(raw):
    if raw == "many_to_one":
        assert normalise_cardinality(raw) == "many_to_one"
    else:
        assert normalise_cardinality(raw) is None


def test_orientation_declared_matches_the_render_flip():
    """``is_orientation_declared`` must be true for exactly the tokens whose
    rendered keyword is independent of which side the traversal arrived from.

    These two are separate functions in separate consumers (the renderer, and
    the pocket row-population proof). A token one of them treats as declared
    while the other renders direction-dependently is a silent wrong-number
    hole, so the relationship is asserted rather than assumed.
    """
    for token in [
        "inner", "left", "right", "full",
        "left_outer", "right_outer", "full_outer",
        "many_to_one", "one_to_many", "banana", "", "outer",
    ]:
        declared = is_orientation_declared(token)
        unflipped = join_keyword(token, flipped=False)
        flipped = join_keyword(token, flipped=True)
        preserved_side_is_stable = (
            # Symmetric keywords preserve the same rows either way; LEFT/RIGHT
            # swap precisely so the same PHYSICAL relation stays preserved.
            unflipped == flipped
            if unflipped in ("INNER JOIN", "FULL OUTER JOIN")
            else {unflipped, flipped} == {"LEFT JOIN", "RIGHT JOIN"}
        )
        assert declared == preserved_side_is_stable, (
            f"{token!r}: is_orientation_declared={declared} but the renderer "
            f"emits {unflipped} / {flipped}"
        )


# ---------------------------------------------------------------------------
# Bug-8628 artifact invalidation (contract invariant 6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    [
        # Flip-sensitive: the pre-fix CTAS never flipped these.
        "left", "right", "left_outer", "right_outer",
        # Spelling-sensitive: the pre-fix map keyed only on the literal
        # "full"/"right", so these fell through to its LEFT JOIN default.
        "full_outer", "full outer", "fullouter", "full join",
        "left join", "right join", "leftouter", "rightouter",
    ],
)
def test_tokens_whose_rendering_moved_are_invalidated(token):
    assert ctas_rendering_changed(token) is True


@pytest.mark.parametrize(
    "token",
    [
        # "JOIN" -> "INNER JOIN" is a spelling change, identical rows.
        "inner",
        # Rendered FULL OUTER before and after, and it is flip-symmetric.
        "full",
        # Legacy tokens still render as an un-flipped LEFT JOIN (invariant 4).
        "many_to_one", "one_to_many", "banana", "outer", "", None,
    ],
)
def test_tokens_whose_rows_did_not_move_are_not_invalidated(token):
    assert ctas_rendering_changed(token) is False, (
        "staling an artifact whose rows did not change costs a rebuild for "
        "nothing"
    )


def test_a_hardcoded_left_right_list_would_have_missed_these():
    """Guard against the obvious wrong implementation.

    The natural predicate — ``join_type IN ('left','right')`` — is what a
    reader would write. It misses four real cases, so this test pins the
    difference rather than trusting the reader.
    """
    naive = {"left", "right"}
    missed = [
        t for t in ("right_outer", "full_outer", "left join", "fullouter")
        if ctas_rendering_changed(t) and t not in naive
    ]
    assert missed == ["right_outer", "full_outer", "left join", "fullouter"]


def test_model_ids_needing_rebuild_selects_only_affected_models():
    rows = [
        ("model-a", "inner"),
        ("model-a", "many_to_one"),      # neither changes -> model-a untouched
        ("model-b", "inner"),
        ("model-b", "left"),             # one changed edge -> model-b rebuilds
        ("model-c", "full_outer"),       # spelling fell through pre-fix
    ]
    assert model_ids_needing_rebuild(rows) == {"model-b", "model-c"}


def test_model_ids_needing_rebuild_is_empty_for_a_clean_model():
    assert model_ids_needing_rebuild([("m", "inner"), ("m", "full")]) == set()


@pytest.mark.parametrize("padded", [" full ", " inner ", "\tleft ", "  right"])
def test_legacy_emulation_matches_the_shipped_lookup_byte_for_byte(padded):
    """The shipped pre-fix lookup was ``_JOIN_SQL.get(j.join_type.lower(), "LEFT JOIN")``
    -- lower() only, NO strip. A padded token therefore rendered as the LEFT JOIN
    default, while the corrected renderer strips and renders the real keyword, so
    its rows MOVE and the artifact must be staled (Bug-8649).

    Promoted from the round-1 deep review, which caught the emulation using
    ``normalise_token`` (strip + lower) and therefore reporting "unchanged" for
    exactly the rows whose population changed.
    """
    shipped = {"inner": "JOIN", "left": "LEFT JOIN", "right": "RIGHT JOIN",
               "full": "FULL OUTER JOIN"}.get(padded.lower(), "LEFT JOIN")
    corrected = {join_keyword(padded, flipped=f) for f in (False, True)}
    rendering_moved = corrected != {("INNER JOIN" if shipped == "JOIN" else shipped)}
    assert ctas_rendering_changed(padded) is rendering_moved


# ---------------------------------------------------------------------------
# Bug-8660 — the rollout must consider BUILD-time tokens, not only live ones
# ---------------------------------------------------------------------------


def test_live_rows_alone_miss_an_artifact_built_under_an_edited_join():
    """An artifact's rows reflect the tokens in force WHEN IT WAS BUILT.

    Edit a join from an affected token to an unaffected one after the build
    and without redeploying, and the LIVE graph looks clean while the stored
    artifact still holds pre-fix rows. It cannot self-repair either: the
    definition closure refuses its refresh precisely because live and snapshot
    now differ. Migration 0191 therefore feeds this predicate the UNION of the
    live join rows and every stored version snapshot's join tokens.
    """
    live_only = [("model-e", "inner")]
    assert model_ids_needing_rebuild(live_only) == set(), (
        "the live row on its own looks clean — this is the trap"
    )

    snapshot_tokens = [("model-e", "right")]
    assert model_ids_needing_rebuild(live_only + snapshot_tokens) == {"model-e"}


def test_the_rollout_migration_unions_the_version_snapshots():
    """Canary for the one-shot rollout step.

    ``upgrade()`` runs once against a real tenant schema, so no unit test can
    observe its effect. What IS checkable, and is exactly what Bug-8660 got
    wrong, is that it reads the snapshot side at all. Proven by execution on a
    throwaway tenant schema: with the union, an aggregate whose live join says
    ``inner`` but whose stored snapshot said ``right`` is staled; without it,
    the same aggregate stays fresh while holding pre-fix rows.
    """
    from pathlib import Path

    migration = (
        Path(__file__).resolve().parents[3]
        / "shared" / "db" / "migrations" / "versions"
        / "0191_join_cardinality_and_orientation_rebuild.py"
    )
    source = migration.read_text(encoding="utf-8")
    upgrade_body = source.split("def upgrade()", 1)[1].split("def downgrade()", 1)[0]
    assert "model_versions" in upgrade_body, (
        "the rollout stales artifacts from the LIVE join rows only; a join "
        "edited after its artifact was built (and not redeployed) then leaves "
        "that artifact fresh while it still holds pre-fix rows"
    )
    assert "snapshot_json" in upgrade_body


def test_the_rollout_reads_snapshots_when_there_are_no_live_joins(monkeypatch):
    """Deleted live joins must not hide an affected deployed snapshot."""
    from pathlib import Path

    migration = (
        Path(__file__).resolve().parents[3]
        / "shared" / "db" / "migrations" / "versions"
        / "0191_join_cardinality_and_orientation_rebuild.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0191_test", migration)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    statements: list[str] = []

    class _Rows:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class _Bind:
        def execute(self, statement, params=None):
            sql = str(statement)
            statements.append(sql)
            if "SELECT id, model_id, join_type FROM joins" in sql:
                return _Rows([])
            if "snapshot_json" in sql and "model_versions" in sql:
                # Matches both the legacy `SELECT model_id, snapshot_json FROM
                # model_versions` form and the current deployed-version JOIN form
                # (`... FROM models m JOIN model_versions mv ON mv.id =
                # m.deployed_version_id WHERE mv.snapshot_unavailable IS NOT TRUE`,
                # introduced by efd06f85) — the rollout reads the DEPLOYED snapshot.
                return _Rows([
                    types.SimpleNamespace(
                        model_id="model-snapshot-only",
                        snapshot_json={"joins": [{"join_type": "right"}]},
                    )
                ])
            raise AssertionError(f"unexpected migration statement: {sql}")

    bind = _Bind()
    monkeypatch.setattr(
        module,
        "_table_exists",
        lambda name: name in {"joins", "model_versions"},
    )
    monkeypatch.setattr(module, "_has_column", lambda table, column: True)
    monkeypatch.setattr(module.op, "get_bind", lambda: bind)
    monkeypatch.setattr(module.op, "alter_column", lambda *args, **kwargs: None)

    module.upgrade()

    assert any("model_versions" in statement for statement in statements), (
        "an empty live joins table skipped snapshot-only artifacts, leaving "
        "their pre-fix row population servable"
    )


# ---------------------------------------------------------------------------
# Bug-8639 — optimizer/lifecycle/creator.py is a THIRD renderer with its OWN
# pre-fix history, distinct from sql_builder.py's (contract invariant 6)
# ---------------------------------------------------------------------------


def test_creator_and_sql_builder_legacy_histories_disagree_on_bare_full():
    """Pin the exact token where the two pre-fix renderers' histories differ.

    ``sql_builder.py`` already rendered ``FULL OUTER JOIN`` for a bare
    ``"full"`` pre-fix (only the flip was missing), so its shaped comparison
    reports "unchanged". ``creator.py`` collapsed the SAME token onto
    ``LEFT JOIN`` pre-fix, so its rows DID move. Reusing the sql_builder-
    shaped predicate for a creator-built aggregate would silently miss
    exactly this case.
    """
    assert ctas_rendering_changed("full") is False
    assert creator_ctas_rendering_changed("full") is True


@pytest.mark.parametrize(
    "token",
    [
        "left", "right", "full",
        "left_outer", "right_outer", "full_outer",
        "full outer", "fullouter", "full join",
        "left join", "right join", "leftouter", "rightouter",
    ],
)
def test_creator_tokens_whose_rendering_moved_are_invalidated(token):
    assert creator_ctas_rendering_changed(token) is True


@pytest.mark.parametrize(
    "token",
    [
        "inner",
        # Legacy/cardinality tokens rendered LEFT JOIN before AND after
        # (invariant 4); creator.py's old default was also LEFT JOIN for any
        # non-"inner" token, so these coincide with the fixed rendering.
        "many_to_one", "one_to_many", "banana", "outer", "", None,
    ],
)
def test_creator_tokens_whose_rows_did_not_move_are_not_invalidated(token):
    assert creator_ctas_rendering_changed(token) is False, (
        "staling an artifact whose rows did not change costs a rebuild for "
        "nothing"
    )


def test_creator_model_ids_needing_rebuild_selects_only_affected_models():
    rows = [
        ("model-a", "inner"),
        ("model-a", "many_to_one"),  # neither changes -> model-a untouched
        ("model-b", "full"),         # creator's own bug: full -> LEFT JOIN
    ]
    assert creator_model_ids_needing_rebuild(rows) == {"model-b"}


def test_the_creator_rollout_migration_unions_the_version_snapshots():
    """Canary for the Bug-8639 one-shot rollout step, mirroring 0191's own
    canary (``test_the_rollout_migration_unions_the_version_snapshots``)."""
    from pathlib import Path

    migration = (
        Path(__file__).resolve().parents[3]
        / "shared" / "db" / "migrations" / "versions"
        / "0192_creator_join_orientation_rebuild.py"
    )
    source = migration.read_text(encoding="utf-8")
    upgrade_body = source.split("def upgrade()", 1)[1].split("def downgrade()", 1)[0]
    assert "model_versions" in upgrade_body, (
        "the rollout stales aggregates from the LIVE join rows only; a join "
        "edited after its aggregate was built (and not redeployed) then "
        "leaves that aggregate fresh while it still holds pre-fix rows"
    )
    assert "snapshot_json" in upgrade_body
    assert "creator_model_ids_needing_rebuild" in source, (
        "must use the creator-specific predicate, not the sql_builder-shaped "
        "one — they disagree on a bare 'full' token"
    )
    assert "pocket_definitions" not in upgrade_body, (
        "creator.py builds aggregates only, never pockets"
    )
