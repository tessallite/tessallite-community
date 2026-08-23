"""Migration 0194's backfill policy: which joins are rewritten, to what, and
that live + deployed snapshot stay in agreement afterwards.

The last class of test here is the one the whole lane exists for: rewriting the
live token ALONE makes ``definition_closure`` report drift (which refuses every
aggregate/pocket refresh on the model), and rewriting the deployed snapshot in
the same operation is what removes it.
"""
from __future__ import annotations

import pytest

from shared.definition_closure import closure_from_snapshot, compare_closures, ClosureSpec
from shared.semantic.join_keyword import (
    CANONICAL_JOIN_TYPES,
    CARDINALITY_TOKENS,
    ORIENTATION_TOKENS,
    edge_cardinality,
    is_orientation_declared,
    join_keyword,
    normalise_cardinality,
    split_join_token,
)
from shared.semantic.join_orientation_backfill import (
    UNRECOGNISED_TOKEN_JOIN_TYPE,
    JoinBackfill,
    backfill_orientation,
    patch_snapshot_join_types,
    plan_backfills,
)

_EMPTY_SPEC = ClosureSpec.of((), ())

#: Every spelling the vocabulary knows plus the shapes real data has produced:
#: hyphenated YAML cardinalities, padded/mixed case, the deliberately
#: unmapped bare ``outer``, an importer's private label, and empty.
_ALL_TOKENS: tuple[str, ...] = tuple(
    sorted(ORIENTATION_TOKENS | CARDINALITY_TOKENS)
) + (
    "many-to-one",
    "one-to-many",
    "one-to-one",
    "many-to-many",
    " many_to_one ",
    "MANY_TO_ONE",
    " Left Outer ",
    "outer",
    "cross",
    "",
)

#: The ONLY cardinality whose backfill also moves the UN-FLIPPED rendering.
#: See ``join_orientation_backfill``'s "Blast radius" section.
_UNFLIPPED_RENDERING_MOVES: frozenset[str] = frozenset({"one_to_many"})


class TestBackfillOrientation:
    @pytest.mark.parametrize("token", sorted(ORIENTATION_TOKENS))
    def test_declared_orientation_is_left_alone(self, token: str) -> None:
        """A row that already names which relation survives is not touched —
        including a long spelling. Canonicalising ``left_outer`` -> ``left``
        would move a snapshot value on a join whose orientation is not in
        question, which is a different change with its own blast radius."""
        assert backfill_orientation(token) is None

    @pytest.mark.parametrize("token", sorted(CARDINALITY_TOKENS))
    def test_cardinality_token_uses_the_0191_inference(self, token: str) -> None:
        """Derived from ``split_join_token``, not restated here, so the two
        backfills cannot disagree about what ``many_to_one`` means."""
        expected, _ = split_join_token(token)
        assert expected is not None
        assert backfill_orientation(token) == expected

    @pytest.mark.parametrize("token", ["outer", "cross", "", "gibberish", None])
    def test_unrecognised_token_takes_the_documented_fallback(self, token) -> None:
        assert backfill_orientation(token) == UNRECOGNISED_TOKEN_JOIN_TYPE

    def test_fallback_is_part_of_the_canonical_vocabulary(self) -> None:
        assert UNRECOGNISED_TOKEN_JOIN_TYPE in CANONICAL_JOIN_TYPES

    @pytest.mark.parametrize("token", _ALL_TOKENS)
    def test_unflipped_rendering_change_is_confined_to_one_to_many(
        self, token: str
    ) -> None:
        """Pin the blast radius over the WHOLE vocabulary, in BOTH directions.

        Every backfilled token must render the identical keyword for a
        conventionally-drawn (un-flipped) traversal — so a star schema
        traversed from the fact does not move at all — EXCEPT ``one_to_many``,
        whose many side is the modeller's RIGHT table and whose legacy
        un-flipped rendering was therefore preserving the wrong (one) side.

        Asserting the exception must CHANGE, rather than merely tolerating it,
        is what stops a future edit to the inference rule silently widening the
        set of joins whose served numbers move.
        """
        new = backfill_orientation(token)
        if new is None:
            return
        unchanged = join_keyword(token, flipped=False) == join_keyword(
            new, flipped=False
        )
        expected_to_change = normalise_cardinality(token) in _UNFLIPPED_RENDERING_MOVES
        assert unchanged is not expected_to_change, (
            f"{token!r} -> {new!r}: un-flipped rendering "
            f"{'unchanged' if unchanged else 'changed'}, which is not the "
            f"documented blast radius"
        )

    @pytest.mark.parametrize("token", _ALL_TOKENS)
    def test_result_always_declares_an_orientation(self, token: str) -> None:
        """After the backfill no row can still be refused by
        ``pocket_population``'s undeclared-edge check — that refusal is the
        entire reason this migration exists."""
        new = backfill_orientation(token)
        effective = token if new is None else new
        assert is_orientation_declared(effective)


class TestPlanBackfills:
    def test_selects_only_undeclared_rows_and_keeps_the_raw_token(self) -> None:
        plans = plan_backfills(
            [
                ("j1", "m1", "many_to_one"),
                ("j2", "m1", "left"),
                ("j3", "m2", " one_to_many "),
                ("j4", "m2", "inner"),
                ("j5", "m2", "outer"),
            ]
        )
        assert [(p.join_id, p.model_id, p.old_token, p.new_token) for p in plans] == [
            ("j1", "m1", "many_to_one", "left"),
            ("j3", "m2", " one_to_many ", "right"),
            ("j5", "m2", "outer", "left"),
        ]

    def test_is_idempotent(self) -> None:
        """Re-planning over the post-backfill values finds nothing, which is
        what makes a second ``alembic upgrade`` a no-op."""
        rows = [("j1", "m1", "many_to_one"), ("j2", "m1", "outer")]
        first = plan_backfills(rows)
        assert first
        rewritten = [(p.join_id, p.model_id, p.new_token) for p in first]
        assert plan_backfills(rewritten) == []

    def test_ids_are_stringified_for_snapshot_matching(self) -> None:
        import uuid

        jid, mid = uuid.uuid4(), uuid.uuid4()
        (plan,) = plan_backfills([(jid, mid, "many_to_one")])
        assert plan.join_id == str(jid)
        assert plan.model_id == str(mid)


def _bf(join_id: str, old: str, new: str = "left") -> JoinBackfill:
    return JoinBackfill(join_id=join_id, model_id="m1", old_token=old, new_token=new)


class TestPatchSnapshotJoinTypes:
    def test_patches_an_agreeing_join(self) -> None:
        snap = {"joins": [{"id": "j1", "join_type": "many_to_one", "cardinality": "many_to_one"}]}
        out, changed = patch_snapshot_join_types(snap, {"j1": _bf("j1", "many_to_one")})
        assert changed == 1
        assert out["joins"][0]["join_type"] == "left"
        # Every other field of the join survives untouched.
        assert out["joins"][0]["cardinality"] == "many_to_one"

    def test_matches_on_normalised_token_not_raw_string(self) -> None:
        snap = {"joins": [{"id": "j1", "join_type": " Many_To_One "}]}
        out, changed = patch_snapshot_join_types(snap, {"j1": _bf("j1", "many_to_one")})
        assert changed == 1
        assert out["joins"][0]["join_type"] == "left"

    def test_leaves_a_disagreeing_snapshot_token_alone(self) -> None:
        """The snapshot already differed from live, so the model has un-deployed
        join edits and the closure already refuses its refreshes. Overwriting it
        would change what the ROUTER binds — a different edit, not a
        correction."""
        snap = {"joins": [{"id": "j1", "join_type": "inner"}]}
        out, changed = patch_snapshot_join_types(snap, {"j1": _bf("j1", "many_to_one")})
        assert changed == 0
        assert out["joins"][0]["join_type"] == "inner"

    def test_ignores_joins_absent_from_the_plan(self) -> None:
        snap = {"joins": [{"id": "j1", "join_type": "many_to_one"}, {"id": "j2", "join_type": "many_to_one"}]}
        out, changed = patch_snapshot_join_types(snap, {"j1": _bf("j1", "many_to_one")})
        assert changed == 1
        assert out["joins"][1]["join_type"] == "many_to_one"

    def test_never_mutates_the_input_snapshot(self) -> None:
        join = {"id": "j1", "join_type": "many_to_one"}
        snap = {"measures": [{"name": "m"}], "joins": [join]}
        out, changed = patch_snapshot_join_types(snap, {"j1": _bf("j1", "many_to_one")})
        assert changed == 1
        assert join["join_type"] == "many_to_one"
        assert snap["joins"][0] is join
        assert out is not snap
        assert out["measures"] is snap["measures"]

    @pytest.mark.parametrize("joins", [None, "not-a-list", 7])
    def test_malformed_joins_key_is_a_no_op_copy(self, joins) -> None:
        snap = {"joins": joins}
        out, changed = patch_snapshot_join_types(snap, {"j1": _bf("j1", "many_to_one")})
        assert changed == 0
        assert out == snap and out is not snap

    def test_missing_joins_key_is_a_no_op_copy(self) -> None:
        out, changed = patch_snapshot_join_types({}, {"j1": _bf("j1", "many_to_one")})
        assert (out, changed) == ({}, 0)

    def test_a_snapshot_join_with_no_join_type_key_is_patched(self) -> None:
        """The ONE skip branch ``definition_closure`` cannot cover.

        ``_compare_group`` diffs only the fields the DEPLOYED row carries, so a
        snapshot join that omits ``join_type`` entirely is never compared on it.
        Skipping it here would leave live ``left`` (flip-aware: LEFT/RIGHT)
        against a snapshot the router hydrates with no token at all
        (``join_keyword(None)`` -> un-flipped LEFT in BOTH directions) — a
        live/deployed divergence this migration CREATED and that no guard can
        see. Before the backfill the two agreed (a legacy token also renders
        un-flipped LEFT both ways), so patching is what PRESERVES the agreement,
        not a convenience.
        """
        snap = {"joins": [{"id": "j1", "left_table_id": "t1"}]}
        out, changed = patch_snapshot_join_types(
            snap, {"j1": _bf("j1", "many_to_one")}
        )
        assert changed == 1
        assert out["joins"][0]["join_type"] == "left"
        assert out["joins"][0]["left_table_id"] == "t1"

    def test_the_drift_guard_is_blind_to_a_snapshot_join_missing_join_type(
        self,
    ) -> None:
        """Why the case above must be patched rather than skipped: the guard
        that would otherwise catch the divergence reports nothing at all."""
        deployed = {"joins": [{"id": "j1", "model_id": "m1"}]}
        live = {"joins": [{"id": "j1", "model_id": "m1", "join_type": "left"}]}
        assert (
            compare_closures(
                closure_from_snapshot(live, _EMPTY_SPEC),
                closure_from_snapshot(deployed, _EMPTY_SPEC),
            )
            == []
        )
        # ...yet the two render different rows on a flipped traversal.
        assert join_keyword("left", flipped=True) == "RIGHT JOIN"
        assert join_keyword(None, flipped=True) == "LEFT JOIN"

    @pytest.mark.parametrize("value", [5, True, ["left"], {"a": 1}, 3.5])
    @pytest.mark.parametrize("field", ["join_type", "cardinality"])
    def test_a_non_string_snapshot_value_is_skipped_not_raised(
        self, field: str, value
    ) -> None:
        """A malformed snapshot join must not abort the whole tenant upgrade.

        ``_restore_version_history`` persists a schema_version>=2 bundle's own
        ``snapshot_json`` after PK re-keying only, ``shared_pk_map`` gives that
        join the SAME id as the live row, and ``_validate_snapshot_for_deploy``
        never inspects join fields — so a non-string value is deployable.
        ``normalise_token`` then raises ``AttributeError`` and ``alembic
        upgrade`` dies for EVERY model in the schema, not just this one. The
        function's own policy for a value it cannot read is to leave the join
        alone, which is what it must do here.
        """
        join = {"id": "j1", "join_type": "many_to_one", "cardinality": None}
        join[field] = value
        (plan,) = plan_backfills([("j1", "m1", "many_to_one")])
        patched, changed = patch_snapshot_join_types(
            {"joins": [join]}, {plan.join_id: plan}
        )
        assert changed == 0
        assert patched["joins"][0] == join

    def test_non_dict_join_entries_survive(self) -> None:
        snap = {"joins": ["junk", {"id": "j1", "join_type": "many_to_one"}]}
        out, changed = patch_snapshot_join_types(snap, {"j1": _bf("j1", "many_to_one")})
        assert changed == 1
        assert out["joins"][0] == "junk"


class TestLiveDeployedAgreement:
    """The property migration 0194 exists to hold: after the backfill the live
    graph and the deployed snapshot still agree on ``joins.join_type``, so
    ``definition_closure`` permits refreshes instead of refusing every one.
    """

    JOIN = {
        "id": "j1",
        "model_id": "m1",
        "left_table_id": "t1",
        "right_table_id": "t2",
        "left_column_id": "c1",
        "right_column_id": "c2",
        "join_type": "many_to_one",
    }

    @staticmethod
    def _drift(live_joins, deployed_joins) -> list[str]:
        return compare_closures(
            closure_from_snapshot({"joins": live_joins}, _EMPTY_SPEC),
            closure_from_snapshot({"joins": deployed_joins}, _EMPTY_SPEC),
        )

    def test_no_drift_before_the_backfill(self) -> None:
        assert self._drift([dict(self.JOIN)], [dict(self.JOIN)]) == []

    def test_live_only_rewrite_refuses_every_refresh(self) -> None:
        """Mutation proof that 0191's objection was real: this is exactly the
        state a live-only backfill would leave, and a non-empty reason list is
        a hard refusal of every aggregate/pocket refresh on the model."""
        live = dict(self.JOIN, join_type="left")
        reasons = self._drift([live], [dict(self.JOIN)])
        assert reasons
        assert any("join_type changed" in r for r in reasons)

    def test_coordinated_rewrite_leaves_no_drift(self) -> None:
        """Both sides move together, INCLUDING the fan-out.

        The live row is modelled as ``0191`` leaves it — ``cardinality``
        backfilled, ``join_type`` still legacy — and the deployed snapshot as
        it was written before ``0191`` existed, with no ``cardinality`` key.
        The migration must land them on the same value for BOTH fields: the
        snapshot gains the fan-out its own token encoded (or the many-to-many
        guard fails open) and the live row keeps the one 0191 gave it.
        """
        plans = plan_backfills(
            [(self.JOIN["id"], self.JOIN["model_id"], self.JOIN["join_type"])]
        )
        (plan,) = plans
        # 0191's live state: cardinality split out, join_type untouched.
        live = dict(
            self.JOIN, join_type=plan.new_token, cardinality=plan.new_cardinality
        )
        deployed, changed = patch_snapshot_join_types(
            {"joins": [dict(self.JOIN)]}, {plan.join_id: plan}
        )
        assert changed == 1
        assert self._drift([live], deployed["joins"]) == []
        assert is_orientation_declared(live["join_type"])
        assert edge_cardinality(live) == edge_cardinality(deployed["joins"][0])
        assert edge_cardinality(deployed["joins"][0]) == "many_to_one"

    def test_live_cardinality_written_by_the_migration_matches_the_snapshot(
        self,
    ) -> None:
        """A row that arrived AFTER 0191 (an old bundle imported post-0191,
        Bug-8698) has a NULL live ``cardinality``, so the migration writes it
        on BOTH sides from the same token. Writing only the snapshot side would
        move the drift from ``join_type`` to ``cardinality`` rather than
        removing it."""
        (plan,) = plan_backfills(
            [(self.JOIN["id"], self.JOIN["model_id"], self.JOIN["join_type"])]
        )
        assert plan.new_cardinality == "many_to_one"
        live = dict(
            self.JOIN, join_type=plan.new_token, cardinality=plan.new_cardinality
        )
        deployed, _ = patch_snapshot_join_types(
            {"joins": [dict(self.JOIN)]}, {plan.join_id: plan}
        )
        assert self._drift([live], deployed["joins"]) == []


class TestSnapshotFanOutIsPreserved:
    """The deployed snapshot's fan-out must survive the orientation rewrite.

    ``0191`` backfilled ``joins.cardinality`` on LIVE rows only, so a snapshot
    join still holding a cardinality token predates the split and carries no
    ``cardinality`` key: that token IS its fan-out. Every snapshot consumer —
    both ``field_compatibility`` loaders and the drill-through path classifier
    — resolves fan-out through ``edge_cardinality`` against the snapshot the
    router binds, so losing it makes the many-to-many guard fail OPEN.
    """

    def test_patching_a_legacy_snapshot_join_preserves_the_fan_out(self) -> None:
        for token, expected in (
            ("many_to_many", "many_to_many"),
            ("many_to_one", "many_to_one"),
            ("one_to_many", "one_to_many"),
            ("one_to_one", "one_to_one"),
        ):
            snapshot = {"joins": [{"id": "j1", "join_type": token}]}
            plans = plan_backfills([("j1", "m1", token)])
            patched, changed = patch_snapshot_join_types(
                snapshot, {p.join_id: p for p in plans}
            )
            assert changed == 1
            join = patched["joins"][0]
            assert is_orientation_declared(join["join_type"])
            assert edge_cardinality(join) == expected, (
                f"patching {token!r} left the deployed snapshot resolving "
                f"edge_cardinality={edge_cardinality(join)!r}; the fan-out the "
                f"snapshot itself encoded was discarded"
            )

    def test_never_overwrites_a_cardinality_the_snapshot_already_carries(
        self,
    ) -> None:
        """A snapshot deployed AFTER 0191 carries its own ``cardinality``. That
        is the deployed truth and must not be re-derived from the legacy
        token."""
        snapshot = {
            "joins": [
                {"id": "j1", "join_type": "many_to_one", "cardinality": "one_to_many"}
            ]
        }
        plans = plan_backfills([("j1", "m1", "many_to_one")])
        patched, changed = patch_snapshot_join_types(
            snapshot, {p.join_id: p for p in plans}
        )
        assert changed == 1
        assert patched["joins"][0]["cardinality"] == "one_to_many"

    def test_a_join_with_no_join_type_key_gains_no_invented_cardinality(self) -> None:
        """That snapshot encoded no fan-out at all, so writing one would invent
        a preservation claim the deployed contract never made (invariant 3)."""
        snapshot = {"joins": [{"id": "j1"}]}
        plans = plan_backfills([("j1", "m1", "many_to_one")])
        patched, changed = patch_snapshot_join_types(
            snapshot, {p.join_id: p for p in plans}
        )
        assert changed == 1
        assert patched["joins"][0]["join_type"] == "left"
        assert "cardinality" not in patched["joins"][0]

    def test_an_unrecognised_token_yields_no_cardinality(self) -> None:
        """``outer`` / ``''`` / an importer's private label carry no fan-out."""
        for token in ("outer", "", "vendor_private_label"):
            snapshot = {"joins": [{"id": "j1", "join_type": token}]}
            plans = plan_backfills([("j1", "m1", token)])
            patched, changed = patch_snapshot_join_types(
                snapshot, {p.join_id: p for p in plans}
            )
            assert changed == 1
            assert patched["joins"][0]["join_type"] == UNRECOGNISED_TOKEN_JOIN_TYPE
            assert edge_cardinality(patched["joins"][0]) is None

    def test_a_snapshot_carrying_an_explicit_null_cardinality_gains_the_fan_out(
        self,
    ) -> None:
        """The post-0191 shape of Bug-8698 — which is the ONLY shape it can have.

        ``row_to_snapshot_dict`` emits EVERY ORM column, so any snapshot written
        after 0191 added ``joins.cardinality`` carries the key with a JSON
        ``null`` for a row whose live cardinality is NULL. A join can only
        appear in a snapshot written AFTER the row was created, so a join that
        arrived post-0191 with a legacy token (a pre-0194 bundle imported, or a
        revert to a pre-0191 version, then deployed) ALWAYS has a snapshot
        carrying ``"cardinality": null``.

        A key-PRESENCE check therefore skips exactly the case the live
        ``cardinality`` write in ``_apply_live_backfill`` exists for: the live
        row gains the fan-out, the snapshot does not, ``definition_closure``
        reports ``cardinality changed`` and refuses every aggregate/pocket
        refresh on the model, and for ``many_to_many`` the snapshot loses its
        fan-out so ``field_compatibility``'s guard stops refusing a fanning
        path. The predicate must be "does this snapshot RESOLVE a fan-out",
        not "is the key present".
        """
        for token in ("many_to_one", "many_to_many", "one_to_many", "one_to_one"):
            snapshot = {
                "joins": [{"id": "j1", "join_type": token, "cardinality": None}]
            }
            (plan,) = plan_backfills([("j1", "m1", token)])
            patched, changed = patch_snapshot_join_types(
                snapshot, {plan.join_id: plan}
            )
            assert changed == 1
            join = patched["joins"][0]
            assert edge_cardinality(join) == token, (
                f"a deployed snapshot carrying an explicit null cardinality "
                f"lost the fan-out its own {token!r} token encoded: {join!r}"
            )
            # ...and the live row 0194 writes in the same transaction must
            # agree, or the closure refuses every refresh on the model.
            live = {
                "id": "j1",
                "model_id": "m1",
                "join_type": plan.new_token,
                "cardinality": plan.new_cardinality,
            }
            assert (
                compare_closures(
                    closure_from_snapshot({"joins": [live]}, _EMPTY_SPEC),
                    closure_from_snapshot(
                        {"joins": [dict(join, model_id="m1")]}, _EMPTY_SPEC
                    ),
                )
                == []
            ), f"0194 left live and deployed disagreeing on cardinality for {token!r}"

    @pytest.mark.parametrize("stored", ["", " ", "N:N", "unknown", "1:N"])
    def test_a_snapshot_cardinality_that_resolves_nothing_still_gains_the_fan_out(
        self, stored: str
    ) -> None:
        """"Resolves no fan-out" is NOT "is null" — the fourth layer of this bug.

        ``rehydrator._insert_joins`` writes a bundle's ``cardinality`` VERBATIM.
        It is the only field on that insert with no coercion (``population_
        participation`` is coerced twelve lines above it, for exactly this threat
        model), and ``POST .../import`` validates only ``schema_version``. So a
        non-null string the vocabulary rejects is a producible stored value.

        For such a row the legacy ``join_type`` token is STILL the only working
        carrier of fan-out: ``edge_cardinality`` falls THROUGH the unrecognised
        value to it. Rewriting ``join_type`` without re-encoding therefore drops
        ``edge_cardinality`` to None on the deployed side, ``field_compatibility``
        stops refusing a fanning path, and the pair double-counts — with NO drift
        signal at all, because both sides still agree on the unrecognised string.
        """
        snapshot = {
            "joins": [{"id": "j1", "join_type": "many_to_many", "cardinality": stored}]
        }
        (plan,) = plan_backfills([("j1", "m1", "many_to_many")])
        patched, changed = patch_snapshot_join_types(snapshot, {plan.join_id: plan})
        assert changed == 1
        assert edge_cardinality(patched["joins"][0]) == "many_to_many", (
            f"a deployed snapshot whose stored cardinality {stored!r} resolves "
            f"nothing lost the fan-out its own many_to_many token encoded: "
            f"{patched['joins'][0]!r}"
        )

    def test_a_snapshot_carrying_cardinality_without_join_type_stays_in_lockstep(
        self,
    ) -> None:
        """The missing-``join_type`` branch must move BOTH fields or neither.

        A hand-authored or imported ``snapshot_json`` can carry ``cardinality``
        without ``join_type`` — ``_row_to_dict`` cannot produce that shape, but
        the import path does no join validation at all. In that branch the patch
        publishes the LIVE plan's orientation into the snapshot; the live row is
        simultaneously gaining the plan's fan-out. Publishing one without the
        other leaves ``definition_closure`` reporting ``cardinality changed`` and
        refusing every aggregate/pocket refresh on the model — the exact
        unattended regression 0191 stopped short to avoid.
        """
        snapshot = {"joins": [{"id": "j1", "left_table_id": "t1", "cardinality": None}]}
        (plan,) = plan_backfills([("j1", "m1", "many_to_one")])
        patched, changed = patch_snapshot_join_types(snapshot, {plan.join_id: plan})
        assert changed == 1
        deployed = dict(patched["joins"][0], model_id="m1")
        live = {
            "id": "j1",
            "model_id": "m1",
            # The live row carries every column the snapshot names; only the two
            # fields this migration writes are under test here.
            "left_table_id": "t1",
            "join_type": plan.new_token,
            "cardinality": plan.new_cardinality,
        }
        assert (
            compare_closures(
                closure_from_snapshot({"joins": [live]}, _EMPTY_SPEC),
                closure_from_snapshot({"joins": [deployed]}, _EMPTY_SPEC),
            )
            == []
        ), f"0194 created a cardinality drift on a model that had none: {deployed!r}"
