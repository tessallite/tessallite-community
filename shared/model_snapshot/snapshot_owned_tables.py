"""The set of tables a model REVERT delete-and-reinserts (Bug-7982 R7).

This is the authoritative definition of "snapshot-owned state": a table whose
rows the rehydrator destroys and rebuilds from the deployed snapshot. Any writer
of such a table that does not serialise against the revert can have its committed
work silently discarded, or can commit stale-structure rows on top of a
just-restored snapshot.

DERIVED, not hand-listed. A hand-maintained list is exactly what let the R6 round
ship with ``optimizer/src/stats/collector.py`` writing three of these tables with
no lock anywhere in the file. The set is read out of ``rehydrator.py`` itself, so
adding a new entity to the revert path automatically extends the guarded set.

WHY THIS MODULE WAS REBUILT (Bug-8439 / Bug-8441)
-------------------------------------------------
``model_write_lock_guard`` moved the DYNAMIC property ("is the lock held at this
write?") to a runtime chokepoint precisely because a static scan that recognises
code SHAPES silently passes every shape it does not recognise. This module was
the one static enumerator left inside that replacement, and it had the identical
bias: its inclusion scan recognised three write shapes and dropped the rest, and
its exclusion lists were hand-written enumerations of a claim nothing checked.
Four consecutive external gates rejected that same root cause. So:

* INCLUSION is conservative and its residual is LOUD. Every write target that
  cannot be resolved to a mapped class is returned in ``unresolved`` instead of
  being dropped, and the recognised positions cover: bare-name statement
  constructors; attribute-form constructors both with an argument
  (``sa.delete(X)``) and WITHOUT one, where the RECEIVER is the target
  (``data_tag_columns.insert()``, ``Measure.__table__.delete()``); session bulk
  persistence (``db.bulk_insert_mappings(X, ...)``); raw ``text()`` DML,
  classified by the same parser the runtime guard uses on live SQL; ORM
  construction in both spellings (``db.add(X(...))`` and ``db.add(models.X(...))``
  — the unit-of-work write shape, which emits its INSERT at flush and so has no
  statement constructor at all); and generic writer helpers resolved PER SCOPE
  rather than by a global parameter-name set. The last four were each proven, by
  review round 1 against the real rehydrator, to be dropped with an EMPTY
  ``unresolved`` — the exact fail-open the four external gates rejected.

  THE RULE IS THE PROPERTY, NOT THE SHAPE (Bug-8721, Bug-8733). Recognising more
  shapes is the move that failed four external gates and four review rounds —
  each found a shape the previous round had not thought of, and each one fell off
  the end of the chain in SILENCE. Round 3 made the chain's end loud for two
  shapes; round 4 broke it again with a plain first-party helper
  (``await purge_rows(Measure, db)``), a NAMED callee that neither rule saw. So
  the final question the scan asks is not "what shape is this call" but:

      did a MAPPED CLASS just reach a callee I cannot attribute?

  If so it is reported, unless the callee is in the short ``_READ_CALLEES``
  allow-list. That inverts the residual: being wrong now means a legitimate read
  is reported (NOISE, and a red test someone must look at), never a write
  vanishing. Measured on the real rehydrator the inversion fires on exactly nine
  call sites, all reads, so it costs nothing today.

  The question is asked of EVERY call, including the ones a shape branch already
  recognised (Bug-8752). It used to be asked only of the calls that fell all the
  way through the shape chain, because the attribute-form ``insert``/``update``
  branch ``continue``d past it — so ``repo.insert(model=Measure, rows=[])`` was
  silent while the byte-identical ``repo.purge(model=Measure)`` was reported. A
  property that a branch can opt out of is a shape rule wearing a property's
  name. There is now ONE implementation of it (``report_unattributed_class``)
  and every branch that does not fully resolve a call site calls it.

  What is still silent — FIVE things, stated plainly rather than claimed away
  (Bug-8743; earlier versions of this paragraph claimed two and then four, and
  BOTH counts were the same overclaim the reviews kept catching — the count has
  now been wrong three times, so treat it as a measurement, not a settled fact):

  1. an attribute-form ``update``/``insert`` where NEITHER the receiver nor ANY
     named argument resolves to a mapped class. That is ``payload.update(extra)``
     — ordinary Python, and reporting it made the derivation permanently
     incomplete, which uncaches the guarded set on every write and re-logs "the
     guard does not cover them" every 300s until an operator switches the guard
     off (Bug-8732). ``delete``/``merge`` are NOT excused this way.

     "ANY named argument" was NOT true when Bug-8752 was filed: the branch read
     the receiver and the FIRST positional argument only, so a mapped class in a
     keyword or in a later positional was dropped with no report — a FIFTH
     silent exit, and not an irreducible one.

     Do NOT restate this as an unbounded "any argument" claim. That sentence has
     now gone stale FOUR times (Bug-8743, Bug-8745, Bug-8752 rounds 2 and 3),
     because the prose asserts a property while the code implements an
     ENUMERATED arm list. The boundary is exactly what ``_named_classes`` walks,
     and it is stated there, not here: the top level is ``args + keywords`` (the
     complete ``ast.Call`` grammar), and inside those positions the recursion
     covers the expressions that FORM a container or SELECT a value without
     naming one directly. Everything else — notably an ORM criterion and a bare
     mapped column — is residual 5. A ``*splat`` of a NAME is residual 3. When
     adding an arm, measure it against the real rehydrator first; the arms
     present today were each measured at zero new reports.
  2. the ORM DIRTY-UPDATE: ``m = await db.get(Measure, id); m.name = x``. There
     is no write call node at all, and ``db.get`` is correctly a read. Latent,
     not live — an AST sweep finds zero such sites in the rehydrator today — but
     it is the fail-open direction, so it is declared. The same shape in a
     model-service HANDLER is a live class and IS detected, by
     ``test_no_allow_listed_endpoint_dirty_updates_a_guarded_entity`` in the
     model-service lock-coverage suite (Bug-8740 was one).
  3. a write whose target class is not NAMED at the call site at all —
     ``db.bulk_save_objects(rows)``, ``purge_all(*targets, db=db)``. Irreducible
     for a source scan: the class only exists at runtime.
  4. a write issued from ANOTHER MODULE into the same tables.
  5. a mapped class named only inside an ORM CRITERION or as a bare mapped
     COLUMN — ``purge_rows(Measure.model_id == mid, db)``,
     ``bulk_update_by(Measure.id, rows)``. This one is a MEASURED trade, not an
     oversight: descending into ``Compare`` and ``Attribute`` chains to catch it
     produces 64 reports on today's rehydrator, 62 of them
     ``.where(Measure.model_id == ...)``. That is a permanently non-empty
     ``unresolved``, i.e. Bug-8732 again. Declared, not chased. If the criterion
     spelling ever becomes the way this file writes, the answer is to attribute
     ``.where``/``.values`` chains to their statement head, not to widen the
     descent.

  All five are the runtime guard's domain: it sees the SQL at the cursor,
  whatever the source looks like. Chasing them here is the open-ended enumeration
  this module has already been rebuilt out of five times — do not.
* EXCLUSION stays an EXPLICIT, small, auditable list — inferring exclusions is
  the fail-OPEN direction and was rightly rejected in R7 (a call-graph walk
  excluded ``personas``, whose loss from this set was the original CLS
  fail-open). What is new is that every entry must now be JUSTIFIED by a
  property derived from ``rehydrator.py`` itself. An entry whose justification
  does not hold is DROPPED at derivation time, so the table becomes guarded —
  noisy, but safe — and ``tests/unit/test_model_write_lock_guard.py`` fails so
  the noise can never reach production silently. The derived check can only ever
  SHRINK the exclusion set, never grow it.

PRESERVE-GATED FAMILIES ARE EXCLUDED (R7 review round 2, B1)
------------------------------------------------------------
The rehydrator's aggregate and pocket teardown is gated on
``if not preserve_aggregates:`` / ``if not preserve_pockets:``. The REVERT path
(``model-service api/versions.py`` — the operation this whole invariant exists to
serialise against) passes ``preserve_aggregates=True, preserve_pockets=True``, so
it does NOT delete-and-reinsert that family; it preserves the rows in place and
merely retires aggregates absent from the reverted-to snapshot. Only the IMPORT
paths pass the flags false, and those rehydrate into a model created in the same
transaction and declare themselves via ``model_write_lock_exempt``.

Including that family made the guarded set describe "tables SOME rehydrate call
can rebuild" rather than the stated invariant, "tables a model REVERT
delete-and-reinserts". The practical consequence was severe and was caught by
review: the aggregate/pocket lifecycle writers (optimizer ``lifecycle/creator``,
``lifecycle/lifecycle_log``, scheduler ``full_refresh``/``incremental_refresh``/
``retirement_sweep``/``pocket_refresh``, ``shared/aggregate_table_ops``) would all
have reported as unlocked violations against a DELETE-REINSERT race that cannot
occur on the revert path — flooding the ``warn`` report with non-defects, which
is exactly how an operator is driven to switch the guard off.

Say "delete-reinsert race", not "race": a revert DOES write this family, as
UPDATEs (retire orphans; mark preserved aggregates/pockets stale), and those
UPDATEs can be lost to a concurrent in-flight refresh that flips ``is_stale``
back from a pre-revert object. Excluding the family removes the only detection
of that, so the exposure is tracked separately as Bug-8431.

DATA SOURCES / TARGETS ARE **NOT** PRESERVE-GATED (Bug-8441 — premise rejected)
------------------------------------------------------------------------------
Bug-8441 reported that ``PRESERVE_GATED_TABLES`` "omits the DataSource/DataTarget
preserve path", implying they should be excluded too. Reading the code says the
opposite, and the derived justification below now PROVES it: on the revert path
``_insert_data_sources_and_targets`` UPSERTS every column of each surviving row
(``on_conflict_do_update``) and ``_reconcile_sources_and_targets`` HARD-DELETES
every live row absent from the snapshot (Bug-7147). A concurrent unlocked
``create_source`` that commits before that reconcile read has its row deleted
outright, and a concurrent unlocked ``update_source`` is overwritten wholesale.
That is the delete/lost-update harm this guard exists for, not the milder
upsert-in-place class the aggregate/pocket exclusion covers. The
lint-versus-guard disagreement is therefore resolved in the fail-CLOSED
direction: these tables stay GUARDED and the five source/target endpoints
acquire the per-model definition lock. ``_JUSTIFY_PRESERVE_GATED`` below fails if
anyone re-adds them.

The exclusion lists are EXPLICIT, not inferred. Inferring them from the
rehydrator call graph was tried and rejected during R7 review round 2: the
analysis excluded ``personas`` — a table whose loss from this set would be the
original CLS fail-open of this bug — on the strength of a call-graph walk nobody
could audit, i.e. its error direction was a HOLE in a safety guard. An
enumeration whose mistakes silently shrink a safety set is not acceptable here,
however clever. The lists below are small, auditable, justified against the
generating construct, and — decisively — proven against a REAL revert by
``test_a_real_revert_writes_only_revert_owned_tables`` in the model-service
live-DB suite, which records every table an actual ``versions.py`` revert writes
and fails if any excluded table appears. Ground truth, not parsing.
"""
from __future__ import annotations

import ast
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

from sqlalchemy import Table

from shared.db import models as _models

_REHYDRATOR = Path(__file__).with_name("rehydrator.py")

#: CANONICAL SQLAlchemy statement constructors whose single positional argument
#: names the write target. These are the library's own names, not the names the
#: rehydrator happens to bind them to.
#:
#: Bug-8711 (review round 2): the scan used to match the LOCAL name and the list
#: had ``pg_insert`` typed into it by hand — which is not a SQLAlchemy name at
#: all, it is the alias ``rehydrator.py:1414`` binds ``postgresql.insert`` to. So
#: the module was already relying on someone having noticed one alias, and any
#: OTHER alias (``from sqlalchemy import delete as sa_delete``, the live spelling
#: in four other modules of this repo) was matched by nothing, resolved to
#: nothing, and reported as nothing. Local names are now DERIVED from the
#: rehydrator's own import statements — the generating construct, which was
#: sitting in the same AST the whole time.
_WRITE_CONSTRUCTORS = {"insert", "delete", "update", "merge"}

#: SQLAlchemy callables that take RAW SQL. Same derivation.
_RAW_SQL_CONSTRUCTORS = {"text"}

#: Module paths whose members are the SQLAlchemy constructors above. An
#: ``import sqlalchemy as sa`` binds the MODULE, so ``sa.delete(X)`` must be
#: recognised through the alias too.
_SQLALCHEMY_ROOTS = ("sqlalchemy",)

#: Session methods that take raw SQL directly rather than a ``text()`` object.
_RAW_SQL_METHODS = {"exec_driver_sql"}

#: Session methods that persist an ORM instance as an UPSERT rather than an
#: append. Bug-8729: ``db.merge(X(...))`` recorded the same ``construct`` kind as
#: ``db.add(X(...))``, so an overwrite validated the APPEND-ONLY claim.
_UPSERT_METHODS = {"merge"}

#: Session methods that write an ORM INSTANCE, where neither the receiver nor
#: the argument names a mapped class. They are reported unconditionally, because
#: the builtin containers this scan has to coexist with (``dict``/``set``/
#: ``list``) have no ``delete`` or ``merge`` — unlike ``update``/``insert``,
#: which they all have. That is not a claim that NOTHING else is spelled this way
#: (``cache.delete(key)``, a dataframe ``.merge()``); it is a claim that the
#: rehydrator contains no such call today, so the report costs nothing, and if
#: one is ever added the cost is a red test rather than a silent hole.
_SESSION_WRITE_METHODS = {"delete", "merge"}

#: Callables that may legitimately be handed a mapped class WITHOUT writing it.
#: The allow-list for the property inversion in branch 7 (Bug-8733).
#:
#: Measured, not guessed: on today's ``rehydrator.py`` the inversion fires on
#: exactly nine call sites and every one of them is a read — ``select`` x3,
#: ``join`` x4, ``get`` x1, ``select_from`` x1. Being WRONG about an entry here
#: produces NOISE (a real write reported as unresolved is a red test, not a
#: silent hole), which is the same error direction ``_APPEND_ONLY_KINDS`` was
#: deliberately flipped to. Keep it short; do not add a name to silence a report
#: without proving the callee cannot write.
#:
#: SCOPE OF THE CLAIM (Bug-8776): these are SQLAlchemy's names, and the exemption
#: is honoured only where the receiver does not contradict that.
#: ``_has_mapped_class_receiver`` withdraws it for ``<MappedClass>.select(...)``
#: — SQLAlchemy never takes a mapped class as the RECEIVER of any read here, so
#: such a call is project-local code the scan cannot vouch for.
_READ_CALLEES = {
    "select", "join", "outerjoin", "select_from", "get", "exists", "aliased",
    "union", "union_all", "isinstance", "getattr", "hasattr", "issubclass",
}

#: Constructors that DESTROY or CREATE rows. ``update`` is deliberately absent:
#: an in-place UPDATE cannot delete a concurrent writer's row, and the
#: UPDATE-vs-UPDATE reconciliation race is a separate, tracked exposure closed by
#: locking the writers (Bug-8437), not by this set.
_DESTRUCTIVE_KINDS = frozenset({"insert", "upsert", "delete", "construct",
                                "helper", "raw"})

#: The ONLY kinds that PROVE a write is an append — it creates a row and can
#: neither destroy nor overwrite one a concurrent writer committed.
#:
#: An allow-LIST, deliberately (Bug-8720). ``_JUSTIFY_APPEND_ONLY`` used to be a
#: deny-list of ``{delete, update, raw}``, which accepted an UPSERT as an append:
#: a dialect ``insert(...).on_conflict_do_update(...)`` and the rehydrator's own
#: ``_upsert_definition_rows`` both overwrite a live row, and both validated the
#: claim. A deny-list is the enumeration bias again — it is wrong about every
#: kind nobody thought to deny. This one is wrong only about kinds nobody thought
#: to ALLOW, which is the safe direction.
_APPEND_ONLY_KINDS = frozenset({"construct"})

#: Rehydrator-internal helpers that write a mapped class passed as their FIRST
#: positional argument; the scan resolves the class at each CALL SITE.
#:
#: A SEED, not the list. Bug-8711 (review round 2): a hand-listed pair covered the
#: two STATEMENT-style helpers that existed, and was structurally blind to a
#: CONSTRUCTION-style one (``def _rows(model_cls, raw): return [model_cls(**r)
#: for r in raw]``), whose call sites were therefore never followed and whose
#: table silently left the guarded set. :func:`_derive_generic_writer_helpers`
#: now derives the set from the PROPERTY — a function whose first parameter is
#: used as a call target, or handed to a write constructor — and unions it with
#: this seed so removing a name here cannot shrink coverage.
_GENERIC_WRITER_HELPER_SEED = {"_upsert_definition_rows", "_write"}

#: Session bulk-persistence methods whose FIRST argument names the mapped class.
#: They emit INSERT/UPDATE without ever constructing a statement object, so the
#: statement-constructor branches cannot see them (Bug-8702, review round 1).
_BULK_WRITER_METHODS = {"bulk_insert_mappings", "bulk_update_mappings"}

#: The ONLY ``preserve_*`` flags the production REVERT passes True
#: (``model-service api/versions.py`` -> ``rehydrate_into_live(...,
#: preserve_aggregates=True, preserve_pockets=True)``, and ``preserve_targets``
#: which the rehydrator derives from those two).
#:
#: Bug-8703 (review round 1): ``_preserve_gate_spans`` used to accept ANY
#: ``if not <name starting with "preserve_">:`` as "a revert never runs this".
#: That is a NAME-PREFIX inference standing in for a fact about the revert's
#: arguments, and it fails OPEN — a future ``preserve_x`` that the revert passes
#: FALSE would still have its body treated as gated, which is how a wrongly
#: excluded table silently leaves the guarded set. The set is now explicit, and
#: an unrecognised ``preserve_*`` gate is reported as a justification failure
#: (so its tables are guarded, and a test goes red) instead of being trusted.
REVERT_PRESERVE_FLAGS = frozenset({
    "preserve_aggregates",
    "preserve_pockets",
    "preserve_targets",
})

#: Constructors that bind a plain Python collection, used to tell
#: ``some_set.update(...)`` apart from ``sa.update(SomeModel)``. Resolved from an
#: assignment in the same function, never from the receiver's NAME (name-matching
#: a receiver is what R6's checker did, and it is why an unfamiliar session
#: variable was invisible to it).
_COLLECTION_CTORS = {"set", "list", "dict", "tuple", "frozenset", "defaultdict",
                     "Counter", "deque"}

#: Sentinel for a write target whose argument is not resolvable to a mapped
#: class. R7 review round 3, N2: these used to be dropped SILENTLY, so a future
#: rehydrator written in that style would shrink the guarded set with no signal —
#: the one error direction this module's docstring says is unacceptable. They now
#: surface in ``unresolved``, which a unit test asserts is empty.
_UNRESOLVABLE = "<non-name-write-target>"


def _table_name(obj) -> str | None:
    """The table name of an ORM class or Core ``Table``, else None."""
    if isinstance(obj, Table):
        return obj.name
    name = getattr(obj, "__tablename__", None)
    return name if isinstance(name, str) else None


def _resolve_table(name: str | None) -> str | None:
    if not name or name == _UNRESOLVABLE:
        return None
    return _table_name(getattr(_models, name, None))


def _arg_name(call: ast.Call) -> str | None:
    """The NAME of a write constructor's first positional argument.

    ``insert(X)`` -> ``"X"``; ``delete(models.X)`` -> ``"X"`` (an attribute-form
    argument is resolved by its attribute, so a module-qualified mapped class is
    not mistaken for an unresolvable shape); anything else -> the fail-loud
    sentinel.
    """
    if not call.args:
        return None
    # ``insert(Measure.__table__)`` targets Measure. Bug-8707: ``_receiver_name``
    # unwrapped ``__table__`` and this did not, so the same expression resolved
    # in one position and produced a permanent false ``unresolved`` in the other.
    return _receiver_name(call.args[0]) or _UNRESOLVABLE


def _named_classes(call: ast.Call) -> list[str]:
    """Every mapped-class NAME this call site mentions, in any argument position.

    Bug-8745: the property inversion asked "did a mapped class reach a callee I
    cannot attribute?" of ``node.args`` only, so the same first-party helper
    respelled ``purge_rows(model=Measure, session=db)`` — arguably the more
    idiomatic spelling — was silent again, as was ``purge_all([Measure], db)``.

    The TOP LEVEL is complete, not an enumeration: ``ast.Call`` has exactly two
    argument containers, so ``args + keywords`` is the whole grammar of
    "arguments this call site names".

    The recursion INSIDE those positions is an enumerated arm list, and saying
    otherwise is what kept going stale. Three separate review rounds each found
    an arm missing — dict KEYS (``purge_all({Measure: ids}, db)`` silent while
    ``{1: Measure}`` reported), then the container-FORMING expressions
    (``[Measure] + extra``, a comprehension, ``Measure if f else Persona``, a
    walrus), then ``BoolOp`` (``target or Measure``, the commoner spelling of
    the ``IfExp`` arm that had just been added). The arms are therefore listed
    explicitly below, each one measured against the real ``rehydrator.py`` at
    zero new reports before being added.

    Where the recursion STOPS is also deliberate and measured: a generic descent
    into every child node produces 64 reports on today's rehydrator, 62 of them
    from ordinary ``.where(Measure.model_id == ...)`` clauses — a permanently
    non-empty ``unresolved``, i.e. Bug-8732. An ORM criterion and a bare mapped
    column are therefore module-docstring residual 5, and a ``*splat`` of a NAME
    (or a subscripted ``TARGETS[0]``) is residual 3. Add an arm only after
    measuring it the same way.
    """
    out: list[str] = []

    def visit(node: ast.AST | None) -> None:
        if node is None:
            return
        if isinstance(node, ast.Starred):
            visit(node.value)
            return
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            for element in node.elts:
                visit(element)
            return
        if isinstance(node, ast.Dict):
            # Bug-8752 review round 1: KEYS were not visited, so a class-keyed
            # teardown map — ``purge_all({Measure: ids}, db)``, an ordinary
            # spelling for a batched delete — was silent while the value-keyed
            # ``{1: Measure}`` was reported. ``node.keys`` holds None for
            # ``{**other}``; ``visit(None)`` returns immediately.
            for key in node.keys:
                visit(key)
            for value in node.values:
                visit(value)
            return
        # Bug-8752 review round 2: a LITERAL container is not the only way a
        # caller spells "these classes". ``[Measure] + extra``, ``[c for c in
        # TARGETS]``, ``Measure if flag else Persona`` and ``(t := Measure)``
        # are each ONE refactor away from the literal this already handles, and
        # each was fully silent while the bare ``[Measure]`` was reported —
        # the same shape-enumeration bias, one level down.
        #
        # The recursion stops at container-FORMING expressions on purpose.
        # Descending generically into every child (Compare, Attribute chains)
        # was MEASURED against the real rehydrator and produced 64 new reports,
        # 62 of them ``.where(Measure.model_id == ...)`` — a permanently
        # non-empty ``unresolved``, which is the Bug-8732 operator-turns-the-
        # guard-off dynamic. An ORM CRITERION and a bare mapped COLUMN are
        # therefore still part of the declared residual, stated in the module
        # docstring rather than claimed away.
        if isinstance(node, ast.BinOp):
            visit(node.left)
            visit(node.right)
            return
        if isinstance(node, ast.IfExp):
            visit(node.body)
            visit(node.orelse)
            return
        if isinstance(node, ast.BoolOp):
            # Bug-8752 review round 3: ``x or Measure`` / ``flag and Measure``
            # is the SAME select-a-value shape as the ``IfExp`` arm above and
            # the more common spelling of it, and it was silent — including
            # inside a list literal and in a branch-4 keyword. The "64 reports"
            # measurement that justifies stopping at Compare/Attribute does NOT
            # apply here: measured on the real rehydrator, this arm produces
            # zero new reports and zero benign-idiom noise.
            for value in node.values:
                visit(value)
            return
        if isinstance(node, ast.Subscript):
            # ``BY_CLASS[Measure]`` names the class in the SLICE. The subscripted
            # VALUE is deliberately not visited: ``TARGETS[0]`` names nothing at
            # the call site and is residual 3, not this.
            visit(node.slice)
            return
        if isinstance(node, ast.Lambda):
            # ``run_all(lambda: Measure)`` — a deferred teardown still names the
            # class at this call site.
            visit(node.body)
            return
        if isinstance(node, ast.NamedExpr):
            visit(node.value)
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            visit(node.elt)
            for generator in node.generators:
                visit(generator.iter)
            return
        if isinstance(node, ast.DictComp):
            visit(node.key)
            visit(node.value)
            for generator in node.generators:
                visit(generator.iter)
            return
        name = _receiver_name(node)
        if name is not None:
            out.append(name)

    for arg in call.args:
        visit(arg)
    for keyword in call.keywords:
        visit(keyword.value)
    return out


def _receiver_name(node: ast.AST) -> str | None:
    """The mapped-class name an expression denotes, or None if it denotes nothing.

    Used for BOTH a write constructor's first argument and a zero-argument
    attribute constructor's receiver.

    ``data_tag_columns.insert()`` and ``Measure.__table__.delete()`` name their
    write target through the RECEIVER, not through an argument. Bug-8702: both
    were dropped without a trace, because ``_arg_name`` returns None for a
    no-argument call and the branch simply moved on. ``data_tag_columns.insert()``
    is not hypothetical — it is the spelling this repo already uses in the
    model-service live-DB fixture, so rewriting the rehydrator's two
    ``insert(data_tag_columns)`` sites into it would have silently removed the
    column-level-security membership table from the guarded set.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        # ``X.__table__`` is still X.
        if node.attr == "__table__":
            return _receiver_name(node.value)
        return node.attr
    return None


def _has_mapped_class_receiver(node: ast.Call) -> bool:
    """``<MappedClass>.<attr>(...)`` — the call's RECEIVER is a mapped class.

    Bug-8776, and the sixth instance of this module's recurring root cause:
    a claim tested by NAME with no evidence the name means what the claim
    assumes. ``_READ_CALLEES`` is a statement about SQLAlchemy's OWN API, but it
    was matched against the callee's bare attribute name and nothing else, so
    ``Measure.select(Persona)`` and ``Measure.union(Persona)`` were exempted as
    "safe SA reads" and ``Persona`` left the guarded set in total silence — the
    one error direction this module's docstring calls unacceptable.

    The discriminator is structural, not a list: for every read in
    ``_READ_CALLEES`` SQLAlchemy takes the mapped class as an ARGUMENT
    (``select(Measure)``, ``session.get(Measure, id)``, ``join(Measure, ...)``,
    ``aliased(Measure)``) and never as the RECEIVER. A mapped-class receiver is
    therefore project-local code by construction, and an exemption written about
    SQLAlchemy must not cover it.

    Cost measured, not assumed: today's ``rehydrator.py`` contains no
    mapped-class-receiver call at all, so this narrowing adds zero reports. The
    WIDER form of the same blindness — ``repo.get(Persona)``, a project-local
    method that merely SHARES a name with an SA read — is still exempt, because
    the receiver there is an unnameable variable and this module deliberately
    refuses to name-match receivers. That residual is filed, not silently
    accepted.
    """
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    return _resolve_table(_receiver_name(func.value)) is not None


class _Bindings:
    """What the scanned module's own ``import`` statements bind, and to what.

    Bug-8711: matching a write constructor by its LOCAL name is the same
    enumeration bias four external gates rejected, one level down — the list of
    local names is unbounded (any alias), so every alias nobody typed in was
    invisible. The bindings are read out of the module's own import statements
    instead, which is the construct that GENERATES those names.

    Covers ``from sqlalchemy import delete``, ``... import delete as sa_delete``,
    ``from sqlalchemy.dialects.postgresql import insert as pg_insert``,
    ``import sqlalchemy as sa`` and ``import sqlalchemy.dialects.postgresql``.
    """

    def __init__(self, tree: ast.AST) -> None:
        self.write_ctors: dict[str, str] = {}   # local name -> canonical
        self.raw_ctors: dict[str, str] = {}     # local name -> canonical
        self.module_aliases: set[str] = set()   # names bound to a sqlalchemy module
        self.upsert_ctors: set[str] = set()     # local names from a dialects module
        self.star_imports: list[int] = []       # linenos of ``from sqlalchemy import *``
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if not self._is_sqlalchemy(node.module):
                    continue
                dialect = ".dialects." in f".{node.module}."
                for alias in node.names:
                    if alias.name == "*":
                        # Bug-8721: a star import binds every constructor under a
                        # name no statement records. Nothing here can enumerate
                        # what it brought in, so say so rather than guess.
                        self.star_imports.append(node.lineno)
                        continue
                    local = alias.asname or alias.name
                    if alias.name in _WRITE_CONSTRUCTORS:
                        self.write_ctors[local] = alias.name
                        if dialect:
                            # Bug-8720: a dialect INSERT is the upsert spelling
                            # (``on_conflict_do_update``). Canonicalising it to
                            # "insert" destroyed the only signal that tells an
                            # overwrite apart from an append.
                            self.upsert_ctors.add(local)
                    elif alias.name in _RAW_SQL_CONSTRUCTORS:
                        self.raw_ctors[local] = alias.name
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if self._is_sqlalchemy(alias.name):
                        self.module_aliases.add(alias.asname or alias.name.split(".")[0])
        # Bug-8721: an ``import`` is only ONE construct that binds a constructor
        # name. ``_DEL = delete`` and ``_DEL = sa.delete`` bind another, and the
        # round-2 derivation could not see either. Follow plain aliasing
        # assignments to a fixpoint (they can chain).
        for _pass in range(4):
            grew = False
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                    continue
                target, value = node.targets[0], node.value
                if not isinstance(target, ast.Name):
                    continue
                src = None
                if isinstance(value, ast.Name):
                    src = value.id
                elif (isinstance(value, ast.Attribute)
                      and getattr(value.value, "id", None) in self.module_aliases):
                    src = value.attr
                    if src in _WRITE_CONSTRUCTORS and target.id not in self.write_ctors:
                        self.write_ctors[target.id] = src
                        grew = True
                    elif src in _RAW_SQL_CONSTRUCTORS and target.id not in self.raw_ctors:
                        self.raw_ctors[target.id] = src
                        grew = True
                    continue
                if src in self.write_ctors and target.id not in self.write_ctors:
                    self.write_ctors[target.id] = self.write_ctors[src]
                    if src in self.upsert_ctors:
                        self.upsert_ctors.add(target.id)
                    grew = True
                elif src in self.raw_ctors and target.id not in self.raw_ctors:
                    self.raw_ctors[target.id] = self.raw_ctors[src]
                    grew = True
            if not grew:
                break

    @staticmethod
    def _is_sqlalchemy(module: str | None) -> bool:
        if not module:
            return False
        head = module.split(".")[0]
        return head in _SQLALCHEMY_ROOTS

    def canonical_write(self, local: str | None) -> str | None:
        if not local:
            return None
        canonical = self.write_ctors.get(local)
        if canonical == "insert" and local in self.upsert_ctors:
            return "upsert"
        return canonical

    def is_raw_sql(self, local: str | None) -> bool:
        return bool(local) and local in self.raw_ctors


def _derive_generic_writer_helpers(tree: ast.AST, bindings: _Bindings) -> frozenset[str]:
    """Functions that write a mapped class handed to them as their FIRST parameter.

    DERIVED from the property, not listed (Bug-8711). A function qualifies when
    its first parameter is, inside its own body, either

      * the first argument of a statement constructor — ``insert(model_cls)``,
        the STATEMENT-style helper the hand list already covered; or
      * the callee of a call — ``model_cls(**row)`` — the CONSTRUCTION-style
        helper the hand list was structurally blind to, whose call sites were
        never followed and whose table therefore left the guarded set silently.

    Seeded with :data:`_GENERIC_WRITER_HELPER_SEED` so deleting a name from that
    constant cannot shrink coverage either.
    """
    out: set[str] = set(_GENERIC_WRITER_HELPER_SEED)
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not fn.args.args:
            continue
        first = fn.args.args[0].arg
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            callee = getattr(node.func, "id", None)
            if callee == first:
                out.add(fn.name)
                break
            if bindings.canonical_write(callee) is not None:
                arg = node.args[0] if node.args else None
                if isinstance(arg, ast.Name) and arg.id == first:
                    out.add(fn.name)
                    break
    return frozenset(out)


# ---------------------------------------------------------------------------
# Scope + shape analysis over rehydrator.py
# ---------------------------------------------------------------------------

class _Scopes:
    """Enclosing-function chain and per-function collection bindings.

    The generic-helper parameter excuse is resolved PER SCOPE. The previous
    version kept one flat set of "first parameter names of the declared
    helpers", so a BRAND NEW helper whose first parameter happened to be called
    ``model_cls`` or ``model`` had its write target dropped from ``unresolved``
    silently — Bug-8439 shape 1. A name is now excused only when it is the first
    parameter of a DECLARED generic helper that lexically encloses the write.
    """

    def __init__(self, tree: ast.AST) -> None:
        self.parent: dict[ast.AST, ast.AST] = {}
        self.func_of: dict[ast.AST, ast.AST | None] = {}
        self._collections: dict[ast.AST, set[str]] = {}

        def walk(node: ast.AST, enclosing: ast.AST | None) -> None:
            self.func_of[node] = enclosing
            is_func = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            nxt = node if is_func else enclosing
            for child in ast.iter_child_nodes(node):
                self.parent[child] = node
                walk(child, nxt)

        walk(tree, None)

    def chain(self, node: ast.AST) -> list[ast.AST]:
        """Every function definition lexically enclosing ``node``, innermost first."""
        out: list[ast.AST] = []
        cur = self.func_of.get(node)
        while cur is not None:
            out.append(cur)
            cur = self.func_of.get(cur)
        return out

    def top_level_func(self, node: ast.AST) -> str | None:
        chain = self.chain(node)
        return chain[-1].name if chain else None

    def excused_helper_param(
        self, node: ast.AST, name: str, helpers: frozenset[str]
    ) -> bool:
        for fn in self.chain(node):
            if fn.name in helpers and fn.args.args:
                if fn.args.args[0].arg == name:
                    return True
        return False

    def collection_names(self, node: ast.AST) -> set[str]:
        """Names PROVABLY bound to a plain Python collection in an enclosing scope."""
        out: set[str] = set()
        for fn in self.chain(node):
            cached = self._collections.get(fn)
            if cached is None:
                cached = _collection_bindings(fn)
                self._collections[fn] = cached
            out |= cached
        return out


def _collection_bindings(fn: ast.AST) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(fn):
        value = target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            target, value = node.target, node.value
        if not isinstance(target, ast.Name) or value is None:
            continue
        if isinstance(value, (ast.Set, ast.List, ast.Dict, ast.Tuple,
                              ast.SetComp, ast.ListComp, ast.DictComp)):
            out.add(target.id)
        elif isinstance(value, ast.Call) and getattr(value.func, "id", None) in _COLLECTION_CTORS:
            out.add(target.id)
    return out


def _preserve_gate_spans(
    tree: ast.AST,
) -> tuple[tuple[tuple[int, int], ...], tuple[str, ...]]:
    """``(spans, unrecognised_flags)`` for every ``if not preserve_*:`` body.

    Only the flags in :data:`REVERT_PRESERVE_FLAGS` create a span, because only
    those are the ones the production revert passes True. Bug-8703: matching on
    the ``preserve_`` NAME PREFIX was an enumeration standing in for a fact about
    the revert's arguments, and it failed OPEN — a new ``preserve_x`` the revert
    passes False would still have had its body treated as never-run-by-a-revert.
    An unrecognised flag is returned so the caller can report it, which guards the
    tables inside it rather than excusing them.
    """
    spans: list[tuple[int, int]] = []
    unrecognised: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or not node.body:
            continue
        test = node.test
        if not (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)):
            continue
        operand = test.operand
        if not (isinstance(operand, ast.Name) and operand.id.startswith("preserve_")):
            continue
        if operand.id not in REVERT_PRESERVE_FLAGS:
            unrecognised.add(operand.id)
            continue
        end = max((c.end_lineno or c.lineno) for c in node.body)
        spans.append((node.body[0].lineno, end))
    return tuple(spans), tuple(sorted(unrecognised))


def _in_spans(lineno: int, spans: tuple[tuple[int, int], ...]) -> bool:
    return any(start <= lineno <= end for start, end in spans)


def _gate_only_called(tree: ast.AST, spans: tuple[tuple[int, int], ...]) -> frozenset[str]:
    """Module-level rehydrator functions whose EVERY invocation is preserve-gated.

    One level of call-graph propagation, deliberately. A function that is never
    called inside this module is NOT reported as gate-only (an empty ``all()`` is
    vacuously true, which would be a silent hole), and a function reached through
    a shape this does not model simply looks ungated — which fails the
    justification check LOUDLY rather than widening an exclusion.

    Bug-8703 (review round 1) closed two ways this could still answer "gated"
    wrongly, which is the fail-OPEN direction:

    * a function referenced as a VALUE (``_STEPS = {"tear": _tear}``, a decorator,
      a ``functools.partial``) is invoked through a name this scan never sees, so
      any bare ``Name`` load outside the callee position now counts as an
      UNGATED invocation;
    * a PUBLIC function can be called from another module entirely, which parsing
      one file can never see, so only module-private (``_``-prefixed) functions
      are eligible at all.

    Bug-8729 (source Bug-8768) closed the THIRD invisible-invocation shape: an
    attribute-form call ``self._x()`` / ``obj._x()``. A bare-``Name``-only scan
    saw it as neither a callee nor a value load, so an UNGATED attribute-form
    call outside every preserve gate never counted against ``_x`` and left it
    wrongly gate-only — the same fail-OPEN the two shapes above closed. An
    attribute-form call outside a preserve gate now counts as an ungated
    invocation, and the shape is treated fail-CLOSED (see the loop body).
    """
    defined = {
        n.name for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name.startswith("_")
    }
    sites: dict[str, list[bool]] = defaultdict(list)
    callee_nodes: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            if func.id in defined:
                callee_nodes.add(id(func))
                sites[func.id].append(_in_spans(node.lineno, spans))
        elif isinstance(func, ast.Attribute) and func.attr in defined:
            # Bug-8729: an ATTRIBUTE-FORM callee (``self._x()`` / ``obj._x()``)
            # was the third invisible-invocation shape. A bare-``Name``-only scan
            # saw it as NEITHER a callee nor a value load, so an UNGATED
            # ``obj._x()`` OUTSIDE every preserve gate could not disqualify ``_x``
            # from gate-only — the fail-OPEN direction, in which a guarded table
            # is wrongly excluded because one of its writer's real invocations
            # was never counted. The attribute callee cannot be PROVEN to bind
            # this module's module-level ``_x`` (it may be a like-named method or
            # an unrelated object's attribute), so — exactly as a value reference
            # is treated below — its invocation path is untrackable and an
            # attribute-form call outside every preserve gate is recorded as an
            # UNGATED invocation. That can only DISQUALIFY ``_x``, never qualify
            # it, so this shape it cannot classify fails CLOSED: the table stays
            # guarded. An attribute-form call INSIDE a preserve gate is left
            # unrecorded — it could only be additional gated-looking evidence,
            # and gate-only membership still requires a positive gated call the
            # scan CAN see (a bare-name call), so ignoring it grants no membership
            # and hides no ungated call.
            if not _in_spans(node.lineno, spans):
                sites[func.attr].append(False)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Name) or id(node) in callee_nodes:
            continue
        if node.id in defined and isinstance(node.ctx, ast.Load):
            # Referenced as a value: the real invocation site is invisible here.
            sites[node.id].append(False)
    return frozenset(name for name, gated in sites.items() if gated and all(gated))


def _scan(tree: ast.AST, origin: str = "rehydrator.py") -> tuple[list[tuple[str, str, bool]], list[str]]:
    """Every rehydrator write, as ``(table, kind, preserve_gated)`` + unresolved.

    ``kind`` is one of ``insert`` / ``delete`` / ``update`` / ``pg_insert`` /
    ``helper`` (a generic writer helper call site) / ``construct`` (an ORM
    instance built for the session's unit of work — ``db.add(X(...))``) /
    ``raw`` (a table named inside a ``text()`` DML literal).
    """
    scopes = _Scopes(tree)
    bindings = _Bindings(tree)
    helpers = _derive_generic_writer_helpers(tree, bindings)
    spans, unrecognised_flags = _preserve_gate_spans(tree)
    gate_only = _gate_only_called(tree, spans)

    writes: list[tuple[str, str, bool]] = []
    unresolved: list[str] = []
    for flag in unrecognised_flags:
        unresolved.append(
            f"if not {flag}: — an unrecognised preserve gate. Add it to "
            "REVERT_PRESERVE_FLAGS only if the production revert passes it True"
        )
    for lineno in bindings.star_imports:
        unresolved.append(
            f"from sqlalchemy import * at {origin}:{lineno} — a star import "
            "binds constructors under names no statement records, so this scan "
            "cannot tell which calls are writes"
        )

    def record(table: str, kind: str, node: ast.AST) -> None:
        gated = _in_spans(node.lineno, spans)
        if not gated:
            enclosing = scopes.top_level_func(node)
            gated = enclosing is not None and enclosing in gate_only
        writes.append((table, kind, gated))

    # Helpers that are actually CALLED somewhere in this file. Bug-8722: the
    # parameter excuse below says "its CALL SITES carry the class" — and nothing
    # checked one exists. ``_gate_only_called`` refuses exactly this vacuous
    # ``all()`` reasoning for the preserve gates; this path did not, so a helper
    # called only from a sibling module took its table out of the guarded set
    # with no signal at all.
    called_helpers: set[str] = set()
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and getattr(n.func, "id", None) in helpers):
            continue
        # Bug-8731: a helper calling ITSELF is not evidence that anything supplies
        # its mapped class. Recursion satisfied the check and put the silence
        # straight back.
        if n.func.id in {fn.name for fn in scopes.chain(n)}:
            continue
        called_helpers.add(n.func.id)

    def resolve_or_report(name: str | None, kind: str, node: ast.AST, what: str) -> None:
        """The ONLY way a write target is consumed. Never silently drops."""
        table = _resolve_table(name)
        if table is not None:
            record(table, kind, node)
            return
        if name is not None and scopes.excused_helper_param(node, name, helpers):
            enclosing = {fn.name for fn in scopes.chain(node)} & helpers
            if enclosing & called_helpers:
                return  # a generic helper's parameter; its CALL SITES carry the class
            unresolved.append(
                f"{what} at {origin}:{node.lineno} — inside generic writer "
                f"helper(s) {sorted(enclosing)} which are never CALLED in this "
                "file, so no call site can supply the mapped class"
            )
            return
        unresolved.append(f"{what} at {origin}:{node.lineno}")

    def report_unattributed_class(
        node: ast.Call,
        callee_text: str | None,
        attributed: tuple[str, ...] = (),
    ) -> None:
        """THE PROPERTY: did a mapped class reach a callee I cannot attribute?

        Factored out of branch 7(c) so it is ONE rule with ONE implementation
        (Bug-8752). Branch 4 used to ``continue`` past 7(c) unconditionally, so
        ``repo.insert(model=Measure)`` — a mapped class in a position branch 4
        does not read — was dropped in total silence while the identical call
        under a callee branch 4 does not recognise (``repo.purge(model=Measure)``)
        was reported. That is the shape-over-property regression this module has
        been rebuilt out of five times, reintroduced by a ``continue``.

        Why branch 4 CALLS this rather than literally falling through to 7:
        7(a) fires first, and it reports any callee merely SPELLED like a
        statement constructor. Every benign ``payload.update(extra)`` is spelled
        that way, so a literal fall-through re-opens the exact noise floor
        Bug-8732 closed (proven by mutation: deleting branch 4's ``continue``
        turns seven of the eight ordinary-Python cases in
        ``test_ordinary_python_collection_mutation_is_not_reported_as_a_write``
        red). The PROPERTY is what branch 4 must reach; the SHAPE check is
        what it must keep skipping.

        ``attributed`` names the class this call site was already resolved to, so
        an attributed write is not also reported as unattributable.
        """
        # Bug-8776: the read exemption is a claim about SQLAlchemy's API, so it
        # is withdrawn the moment the receiver proves the callee is NOT
        # SQLAlchemy's. See ``_has_mapped_class_receiver``.
        if callee_text in _READ_CALLEES and not _has_mapped_class_receiver(node):
            return
        if not any(
            name not in attributed and _resolve_table(name) is not None
            for name in _named_classes(node)
        ):
            return
        unresolved.append(
            f"{callee_text or chr(60) + 'computed callee' + chr(62)}(...) at "
            f"{origin}:{node.lineno} — a mapped class reached a callee "
            "this scan cannot attribute as a read or a write"
        )

    # Module-level string constants, so ``db.execute(SQL_CONST)`` and
    # ``text(SQL_CONST)`` can be classified rather than shrugged at (Bug-8733).
    str_constants: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt, val = node.targets[0], node.value
            if (isinstance(tgt, ast.Name) and isinstance(val, ast.Constant)
                    and isinstance(val.value, str)):
                str_constants[tgt.id] = val.value

    def literal_of(arg) -> object:
        if isinstance(arg, ast.Constant):
            return arg.value
        if isinstance(arg, ast.Name):
            return str_constants.get(arg.id)
        return None

    def scan_raw_sql(literal, node: ast.AST, what: str) -> None:
        from shared.db.model_write_lock_guard import written_tables

        if not isinstance(literal, str):
            unresolved.append(
                f"{what} at {origin}:{node.lineno} — a raw statement whose "
                "SQL cannot be read at parse time"
            )
            return
        for name in sorted(written_tables(literal)):
            if name in _known_table_names():
                record(name, "raw", node)
            else:
                unresolved.append(
                    f"{what} writes {name!r} at {origin}:{node.lineno} — no "
                    "mapped class for it in shared.db.models"
                )

    # Bug-8711: a generic writer helper referenced as a VALUE
    # (``functools.partial(_write, Measure)``, a dict of steps, a decorator) is
    # invoked through a name this scan never sees, so its call sites cannot be
    # followed. Report it rather than let the class it writes disappear.
    helper_callee_nodes: set[int] = {
        id(n.func) for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) in helpers
    }
    for node in ast.walk(tree):
        if (isinstance(node, ast.Name) and node.id in helpers
                and isinstance(node.ctx, ast.Load)
                and id(node) not in helper_callee_nodes):
            unresolved.append(
                f"the generic writer helper {node.id!r} is used as a VALUE at "
                f"{origin}:{node.lineno}; its real call site cannot be "
                "followed, so the class it writes cannot be resolved"
            )

    # Bug-8729: ``db.merge(X(...))`` persists as an UPSERT. Its inner
    # construction is otherwise indistinguishable from ``db.add(X(...))``, which
    # IS a genuine append, so the kind is decided by the ENCLOSING call.
    upsert_arg_nodes: dict[int, str] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _UPSERT_METHODS):
            continue
        for arg in node.args:
            for inner in [arg, *ast.walk(arg)]:
                if isinstance(inner, ast.Call):
                    upsert_arg_nodes[id(inner)] = "upsert"

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        bare = getattr(func, "id", None) if isinstance(func, ast.Name) else None
        attr = func.attr if isinstance(func, ast.Attribute) else None
        receiver = (
            getattr(func.value, "id", None) if isinstance(func, ast.Attribute) else None
        )

        # EVERY shape branch below consumes the call with a ``continue``, and
        # every one of them reads at most ONE position — the receiver, or the
        # first positional argument, or a literal SQL string. So every one of
        # them owes the property (``report_unattributed_class``) an answer about
        # the positions it did NOT read. Bug-8752 was branch 4 not paying that
        # debt; branches 1, 2, 3, 5 and 6 have the identical structure, so the
        # rule is applied uniformly rather than to the one branch an external
        # gate happened to name — fixing only the named branch and waiting for
        # the next gate to find its sibling is this module's actual failure
        # history. Measured on the real rehydrator: zero additional reports, and
        # a byte-identical derived table set.
        #
        # 1. Bare-name statement constructors and generic writer helpers. The
        #    LOCAL name is resolved through the module's own imports, so an alias
        #    (``delete as sa_delete``) is matched by derivation, not by having
        #    been typed into a constant.
        canonical = bindings.canonical_write(bare)
        if canonical is not None or bare in helpers:
            kind = "helper" if canonical is None else canonical
            first = _arg_name(node)
            resolve_or_report(first, kind, node, f"{bare}(...)")
            report_unattributed_class(node, bare, (first,) if first else ())
            continue

        # 2. Raw SQL constructors — ``text(...)`` and any alias of it.
        if bindings.is_raw_sql(bare):
            scan_raw_sql(
                literal_of(node.args[0]) if node.args else None,
                node, f"{bare}(...)",
            )
            report_unattributed_class(node, bare)
            continue

        if attr is not None:
            # 3. ``sa.delete(X)`` / ``sa.text(...)`` — the constructor reached
            #    through a bound sqlalchemy MODULE rather than a bound name.
            if receiver in bindings.module_aliases:
                if attr in _WRITE_CONSTRUCTORS:
                    first = _arg_name(node)
                    resolve_or_report(first, attr, node, f"{receiver}.{attr}(...)")
                    report_unattributed_class(node, attr, (first,) if first else ())
                    continue
                if attr in _RAW_SQL_CONSTRUCTORS:
                    scan_raw_sql(
                        literal_of(node.args[0]) if node.args else None,
                        node, f"{receiver}.{attr}(...)",
                    )
                    report_unattributed_class(node, attr)
                    continue

            # 4. Receiver-named constructors — ``data_tag_columns.insert()``,
            #    ``Measure.__table__.delete()``. A receiver PROVABLY bound to a
            #    plain Python collection (``some_set.update(...)``) is the one
            #    silent exit in this scan, and it is silent only because it is
            #    demonstrably not a database write.
            if attr in _WRITE_CONSTRUCTORS:
                # POSITIVE EVIDENCE required (Bug-8732). Reporting every
                # unresolvable ``<recv>.update(...)`` made ordinary Python
                # scream: ``payload.update(extra)`` on a PARAMETER,
                # ``self.state.update(extra)``, a module-level ``_SEEN = {}``,
                # ``items.insert(0, x)``. A permanently non-empty ``unresolved``
                # blocks legitimate edits AND re-logs "the guard does NOT cover
                # them" every 300s in production — the operator-turns-it-off
                # dynamic this module exists to avoid. A DB write in this
                # position always names a mapped class, as the receiver or as the
                # argument; a call that names neither is not one.
                #
                # Bug-8752: this branch reads exactly TWO positions — the
                # receiver and the FIRST positional argument — and it used to
                # ``continue`` unconditionally, so every OTHER position was
                # dropped before the property inversion in 7(c) could see it.
                # Each exit below therefore asks the property of the positions
                # this branch did not itself resolve.
                receiver_target = _receiver_name(func.value)
                arg_target = _arg_name(node)
                if _resolve_table(receiver_target) is not None:
                    resolve_or_report(
                        receiver_target, attr, node,
                        f"{receiver_target}.{attr}(...)",
                    )
                    report_unattributed_class(node, attr, (receiver_target,))
                    continue
                if _resolve_table(arg_target) is not None or (
                    arg_target is not None
                    and scopes.excused_helper_param(node, arg_target, helpers)
                ):
                    resolve_or_report(
                        arg_target, attr, node,
                        f"{receiver or chr(60) + 'expr' + chr(62)}.{attr}({arg_target})",
                    )
                    report_unattributed_class(node, attr, (arg_target,))
                    continue
                if not node.args and not node.keywords:
                    # A ZERO-ARGUMENT receiver-form constructor whose receiver
                    # does not resolve — ``_TABLES["measures"].delete()``. The
                    # positive-evidence rule above cannot see it, and it is NOT
                    # the plain-collection shape: ``set.update()`` and
                    # ``dict.update()`` with no arguments are no-ops and
                    # ``list.insert()`` requires them, so nothing benign is
                    # spelled this way. Report it.
                    #
                    # Bug-8752 review round 3: "nothing benign" is true of the
                    # BUILTIN collections only. A custom object with a no-argument
                    # ``update()`` — ``self.state.update()``, ``pbar.update()`` —
                    # IS reported here, and that is kept deliberately: narrowing
                    # it would silence ``_TABLES["measures"].delete()``, the shape
                    # this arm exists for. The accepted noise is pinned by
                    # ``test_a_zero_argument_write_constructor_is_reported_even_
                    # when_benign`` so it is a stated cost, not a surprise.
                    #
                    # Bug-8744: KEYWORDS count as arguments here. ``node.args`` is
                    # empty for ``payload.update(**extra)`` and
                    # ``payload.update(name=x)``, which are ordinary Python, so
                    # this branch was re-opening the very noise floor the
                    # positive-evidence rule above exists to close.
                    unresolved.append(
                        f"{receiver or chr(60) + 'expr' + chr(62)}.{attr}() at "
                        f"{origin}:{node.lineno} — a zero-argument write "
                        "constructor whose receiver cannot be resolved to a "
                        "mapped class"
                    )
                elif attr in _SESSION_WRITE_METHODS:
                    # ``db.delete(instance)`` / ``db.merge(instance)``. The
                    # positive-evidence rule cannot see these — neither the
                    # session nor the instance variable names a mapped class —
                    # but unlike ``update``/``insert`` these are NOT plain-Python
                    # collection methods at all (``dict``/``set``/``list`` have
                    # no ``delete`` or ``merge``), so reporting them costs no
                    # false positives and closes a genuine ORM write shape.
                    unresolved.append(
                        f"{receiver or chr(60) + 'expr' + chr(62)}.{attr}"
                        f"({arg_target}) at {origin}:{node.lineno} — an ORM "
                        "instance write whose mapped class is not named by the "
                        "receiver or the first positional argument"
                    )
                    # Bug-8752 review round 1: the message above is about the two
                    # positions this branch reads. If a mapped class sits in some
                    # OTHER position it must still be named, and every exit must
                    # ask the property uniformly or the structural guard below
                    # has an exception to reason about.
                    report_unattributed_class(node, attr)
                else:
                    # Bug-8752: the ONLY remaining exit — an attribute-form
                    # ``insert``/``update`` with arguments where neither the
                    # receiver nor the first positional names a mapped class.
                    # This used to be the end of the chain and it was SILENT, so
                    # ``repo.insert(model=Measure, rows=[])``,
                    # ``repo.update(target=Measure, values={})`` and
                    # ``repo.insert(0, Measure)`` all vanished — while the same
                    # call under an unrecognised callee name
                    # (``repo.purge(model=Measure)``) was reported by 7(c). Ask
                    # the property here instead: silence is now conditional on
                    # NO mapped class being named anywhere in the call, which is
                    # what makes ``payload.update(extra)`` genuinely benign
                    # rather than merely unrecognised.
                    #
                    # A receiver PROVABLY bound to a plain collection is NOT
                    # excused here, deliberately. ``targets.insert(0, Measure)``
                    # puts a mapped class somewhere this scan cannot follow, and
                    # 7(c) already reports the same thing on ``pending.append(
                    # Measure)``; excusing it by receiver would make the property
                    # depend on the callee's spelling again.
                    report_unattributed_class(node, attr)
                continue

            # 5. Session bulk persistence and driver-level raw SQL.
            if attr in _BULK_WRITER_METHODS:
                first = _arg_name(node)
                resolve_or_report(first, "insert", node, f"{attr}(...)")
                report_unattributed_class(node, attr, (first,) if first else ())
                continue
            if attr in _RAW_SQL_METHODS:
                scan_raw_sql(
                    literal_of(node.args[0]) if node.args else None,
                    node, f"{attr}(...)",
                )
                report_unattributed_class(node, attr)
                continue
            # A bare SQL string handed to ``.execute`` — as a literal, or as a
            # module-level constant, which round 4 proved was silent.
            if attr == "execute" and node.args:
                sql = literal_of(node.args[0])
                if isinstance(sql, str):
                    scan_raw_sql(sql, node, "execute(<raw SQL>)")
                    report_unattributed_class(node, attr)
                    continue

        # 6. ORM instance construction — ``db.add(X(...))``, ``db.add(models.X(...))``,
        #    ``db.add_all([X(...)])``. The unit of work emits the INSERT at flush,
        #    so no statement constructor ever appears in the source. Construction
        #    is treated as a write unconditionally: over-guarding a class that is
        #    built but never added is the safe direction.
        constructed = bare if bare is not None else attr
        if constructed is not None:
            table = _resolve_table(constructed)
            if table is not None:
                record(table, upsert_arg_nodes.get(id(node), "construct"), node)
                report_unattributed_class(node, constructed, (constructed,))
                continue
            # Bug-8731: ``db.add(model_cls(**row))`` inside a generic writer
            # helper. The class is the helper PARAMETER, so it resolves to
            # nothing and used to fall through into silence — the very shape
            # Bug-8711 was filed about, left uncovered by Bug-8722 guard.
            if scopes.excused_helper_param(node, constructed, helpers):
                resolve_or_report(
                    constructed, "construct", node, f"{constructed}(...)",
                )
                report_unattributed_class(node, constructed, (constructed,))
                continue

        # 7. THE DEFAULT PATH IS LOUD (Bug-8721). Everything above recognises a
        #    shape; four external gates and three internal review rounds each
        #    found another shape nobody had recognised, because whatever fell off
        #    the end of the chain fell off in SILENCE. Enumerating a fifth batch
        #    of shapes is the move that keeps failing. Instead, the two ways a
        #    write can hide behind an unrecognised CALLEE are reported outright:
        #
        #    (a) the callee is spelled like a statement constructor but is not
        #        bound to one by any import or aliasing assignment — a first-party
        #        re-export (``from shared.db.sqlhelpers import delete``), a star
        #        import, or a binding this scan cannot follow;
        #    (b) the callee is COMPUTED (``OPS["delete"](X)``, ``(a if c else
        #        b)(X)``, ``globals()["delete"](X)``) and is handed a mapped
        #        class, which is exactly the signature of a write nobody can
        #        attribute.
        #
        #    Both are noise-free on today's rehydrator (measured: zero hits) and
        #    turn the residual from silence into a red test.
        callee_text = bare if bare is not None else attr
        if callee_text in (_WRITE_CONSTRUCTORS | _RAW_SQL_CONSTRUCTORS):
            if receiver is not None and receiver in scopes.collection_names(node):
                # Bug-8752 review round 1: this exit consumed the call and asked
                # NOTHING, which is the same defect as the branch-4 short-circuit
                # one branch over. The round-1 implementer argued it was
                # unreachable (branch 4 consumes every attribute-form call whose
                # ``attr`` is a write constructor, and a bare-name callee has no
                # receiver), so the only shape that reaches it is
                # ``<provable collection>.text(X)`` — narrow, but NOT unreachable,
                # and ``buf.text(Measure)`` was executed and returned total
                # silence. "Argued unreachable" is how five of this module's
                # silent exits got there. The skip stays (a proven collection is
                # not a database write); the property does not.
                report_unattributed_class(node, callee_text)
                continue  # a plain Python collection: not a database write
            unresolved.append(
                f"{callee_text}(...) at {origin}:{node.lineno} — spelled like "
                "a SQLAlchemy statement constructor but bound by no import or "
                "aliasing assignment this scan can follow"
            )
            continue
        # (c) THE PROPERTY, not the shape (Bug-8733). Rounds 1-3 each reported a
        #     newly-remembered SHAPE and each left the next one silent; round 4
        #     broke it again with a plain first-party helper
        #     (``await purge_rows(Measure, db)``) — a NAMED callee, so neither
        #     7(a) nor a computed-callee rule saw it. The question that actually
        #     matters is not "what shape is this call" but "did a mapped class
        #     just reach a callee I cannot attribute?". Ask that, and allow-list
        #     the callees that legitimately take one without writing.
        #
        #     Bug-8752: the rule lives in ``report_unattributed_class`` so that
        #     branch 4 asks the IDENTICAL question of the argument positions it
        #     does not resolve. A second copy of this test is how the property
        #     silently became branch-specific in the first place.
        report_unattributed_class(node, callee_text)

    # (d) A statement constructor used as a VALUE rather than called — the
    #     symmetry of the generic-writer-helper report above. ``partial(delete,
    #     Measure)`` binds the write somewhere this scan cannot follow.
    ctor_callee_nodes: set[int] = {
        id(n.func) for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", None) in bindings.write_ctors
    }
    for node in ast.walk(tree):
        if (isinstance(node, ast.Name)
                and node.id in bindings.write_ctors
                and isinstance(node.ctx, ast.Load)
                and id(node) not in ctor_callee_nodes):
            unresolved.append(
                f"the statement constructor {node.id!r} is used as a VALUE at "
                f"{origin}:{node.lineno}; the call it is eventually made "
                "through cannot be followed"
            )

    return writes, unresolved


@lru_cache(maxsize=1)
def _known_table_names() -> frozenset[str]:
    """Every table name reachable from ``shared.db.models``."""
    out: set[str] = set()
    for attr_name in dir(_models):
        table = _table_name(getattr(_models, attr_name, None))
        if table:
            out.add(table)
    return frozenset(out)


def analyse_source(
    source: str, origin: str = "rehydrator.py",
) -> tuple[tuple[tuple[str, str, bool], ...], tuple[str, ...]]:
    """Run the rehydrator write scan over arbitrary source text.

    The supported entry point for tests and mutation proofs: it lets a guard test
    feed the scan a synthetic write shape without rewriting ``rehydrator.py`` on
    a shared working tree.
    """
    writes, unresolved = _scan(ast.parse(source), origin)
    return tuple(writes), tuple(sorted(set(unresolved)))


@lru_cache(maxsize=1)
def _analysis() -> tuple[tuple[tuple[str, str, bool], ...], tuple[str, ...]]:
    # ``utf-8-sig`` (Bug-8539): a BOM-prefixed source file makes ``ast.parse``
    # raise on an invisible character, which would take the whole guard down.
    return analyse_source(_REHYDRATOR.read_text(encoding="utf-8-sig"))


# ---------------------------------------------------------------------------
# Explicit, justified exclusions
# ---------------------------------------------------------------------------

#: The aggregate/pocket family. The rehydrator tears these down only inside
#: ``if not preserve_aggregates:`` / ``if not preserve_pockets:``, and the REVERT
#: path (``model-service api/versions.py``) passes BOTH flags True — it preserves
#: these rows in place and merely retires aggregates absent from the reverted-to
#: snapshot. They are therefore NOT revert-owned. Their milder
#: upsert-overwrite reconciliation exposure is tracked as Bug-8431.
#:
#: Justified by ``_JUSTIFY_PRESERVE_GATED``: every row-destroying/creating write
#: the rehydrator makes to these tables must be reachable only through a
#: ``if not preserve_*:`` branch.
PRESERVE_GATED_TABLES = frozenset({
    "aggregate_definitions",
    "aggregate_columns",
    "aggregate_refresh_policies",
    "aggregate_refresh_runs",
    "aggregate_lifecycle_events",
    "quantile_coverage",
    "pocket_definitions",
    "pocket_predicates",
    "pocket_refresh_policies",
    "pocket_refresh_runs",
})

#: R7 review round 4 (B3). ``models`` entered the derived set only because the
#: rehydrator contains ``update(Model)`` — but a revert UPDATEs the model row's
#: scalars IN PLACE and never delete-reinserts it, so it does not meet this
#: module's stated definition. Guarding it made the production default emit a
#: permanent false ERROR: ``bump_data_epoch`` runs on EVERY successful refresh
#: (scheduler full/incremental refresh, pocket refresh), so a healthy tenant
#: re-emitted the ``{models}`` report every re-arm window forever and kept that
#: key claimed ~100% of the time — the exact self-muting the rate-limit exists to
#: prevent.
#:
#: Bug-8437 asked whether removing ``models`` removed the only DETECTION of the
#: revert-versus-``update_model`` lost-update race. It did — and detection was
#: never the right instrument. A lost update is closed by mutual EXCLUSION, so
#: ``models.py::update_model``, ``targets.py::create_target`` and
#: ``targets.py::delete_target`` (which write ``models.target_id``) now acquire
#: the per-model definition lock and are no longer allow-listed by
#: ``model_lock_coverage``. ``bump_data_epoch`` remains safe without the lock: it
#: is an atomic ``SET data_epoch = data_epoch + 1`` on a column the revert is
#: forbidden to write at all (``_MODEL_SCALAR_EXCLUDE``).
#:
#: Caller enumeration for that new invariant (CLAUDE.md shared-primitive
#: hardening discipline), recorded here rather than implied. Every writer of a
#: REHYDRATED ``models`` scalar outside the three endpoints above:
#:   * ``shared/model_refresh_epoch.bump_data_epoch`` — excluded column, atomic
#:     increment: satisfies the invariant by construction. VERIFIED, not asserted
#:     (Bug-8988 challenged this line against Bug-8414, which claimed a
#:     concurrent deploy could lose-update ``data_epoch`` and pin the KPI result
#:     cache to a pre-deploy value; the claim does not hold on this tree, and
#:     Bug-8414's own intake names this check as its resolution path (c)). The
#:     proof is two facts, both cheap to re-check:
#:       - ``data_epoch`` is in ``rehydrator._MODEL_SCALAR_EXCLUDE``, so no
#:         revert, deploy or import writes it AT ALL — pinned by
#:         ``test_a_revert_never_regresses_models_data_epoch``; and
#:       - enumerating every durable writer of the VALUE (not every caller of
#:         the symbol) finds exactly one, ``bump_data_epoch``, whose
#:         ``SET data_epoch = data_epoch + 1`` cannot lose a concurrent
#:         increment. Migration ``0176`` only adds and drops the column.
#:     There is therefore no read-modify-write anywhere and no race to
#:     serialise. Taking the lock here would be actively wrong: the docstring
#:     requires the call INSIDE the refresh transaction, so the lock would be
#:     held for the whole of a CTAS that runs for minutes and would starve every
#:     Save/deploy/revert for that model.
#:   * ``model-service api/versions.py`` deploy/undeploy/revert — writes only
#:     excluded columns AND holds the lock.
#:   * seed/bootstrap scripts — build the model inside one transaction, so no
#:     concurrent reader of it exists yet.
#:   * optimizer ``lifecycle/resolve_target.py`` (backfills ``target_id``),
#:     ``api/predictive_routes.py`` and ``lifecycle/predictive_sweep.py`` (stamp
#:     ``predictive_built_for_version_id`` / ``predictive_built_for_epoch``) —
#:     Bug-8694 FIXED: each now calls ``acquire_model_definition_lock`` before
#:     the rehydrated-scalar write. ``resolve_target`` takes the lock around the
#:     backfill, ``db.refresh``-es the column under it (a genuine re-read), and
#:     commits immediately to release the xact lock before the caller's slow
#:     source work. The two stamp writers acquire it LATE — after the slow build,
#:     never across the CTAS; their ``db.get`` re-fetches the live ORM instance
#:     but the serialisation guarantee comes from the advisory lock plus the
#:     column-scoped stamp UPDATE, not the fetch. They are not model-service
#:     routes, so the route-scoped lint cannot see them, and ``models`` is
#:     deliberately absent from the runtime guarded set — hence they are
#:     enumerated and verified here rather than by the lint.
#:
#: Justified by ``_JUSTIFY_UPDATE_IN_PLACE``: the rehydrator must make NO
#: row-destroying/creating write to these tables at all.
UPDATE_IN_PLACE_TABLES = frozenset({
    "models",
})

#: Tables the rehydrator only ever APPENDS to. ``model_alerts`` is the one: a
#: revert INSERTs a ``governance_revert`` alert and an import INSERTs an
#: ``aggregate_import`` one, but no rehydrate path ever deletes or updates an
#: existing alert row, so no concurrent alert writer's row can be destroyed or
#: overwritten by a revert. ``model_lock_coverage``'s allow-list already says the
#: same thing about ``dismiss_alert_endpoint``; the two layers agree.
#:
#: This entry exists because the new ORM-construction shape (Bug-8439 shape 3)
#: makes ``model_alerts`` visible to the derivation for the first time. Excluding
#: it keeps the guarded set behaviourally identical to before while the SHAPE is
#: now covered — a future ``tenant_db.add(Measure(...))`` in the rehydrator would
#: be caught.
#:
#: Justified by ``_JUSTIFY_APPEND_ONLY``: the rehydrator must make no ``delete``
#: and no ``update`` write to these tables.
APPEND_ONLY_TABLES = frozenset({
    "model_alerts",
})


def _writes_by_table() -> dict[str, list[tuple[str, bool]]]:
    out: dict[str, list[tuple[str, bool]]] = defaultdict(list)
    for table, kind, gated in _analysis()[0]:
        out[table].append((kind, gated))
    return out


def _JUSTIFY_PRESERVE_GATED(entries: list[tuple[str, bool]]) -> str | None:
    ungated = [k for k, gated in entries if k in _DESTRUCTIVE_KINDS and not gated]
    if ungated:
        return (
            "the rehydrator makes row-destroying/creating writes to it OUTSIDE "
            f"any `if not preserve_*:` branch ({sorted(set(ungated))}), so a "
            "REVERT does destroy or recreate its rows"
        )
    return None


def _JUSTIFY_UPDATE_IN_PLACE(entries: list[tuple[str, bool]]) -> str | None:
    destructive = [k for k, _gated in entries if k in _DESTRUCTIVE_KINDS]
    if destructive:
        return (
            "the rehydrator does not only UPDATE it in place — it also "
            f"{sorted(set(destructive))}s rows"
        )
    return None


def _JUSTIFY_APPEND_ONLY(entries: list[tuple[str, bool]]) -> str | None:
    # An ALLOW-list of proven appends, not a deny-list of known mutations
    # (Bug-8720): an UPSERT is neither a delete nor an update by kind, yet it
    # overwrites a live row, so a deny-list validated it as an append.
    mutating = [k for k, _gated in entries if k not in _APPEND_ONLY_KINDS]
    if mutating:
        return (
            "the rehydrator does not only APPEND to it — it also writes it as "
            f"{sorted(set(mutating))}, which can destroy or overwrite a row a "
            "concurrent writer committed"
        )
    return None


#: The row/column-level security governance family. Eligibility for an exclusion
#: is NECESSARY, never SUFFICIENT — the derived predicates prove only that the
#: rehydrator's syntax is consistent with the claim, not that excluding the table
#: is a good idea. Review round 1 showed the sharpest case: ``personas`` IS
#: preserve-gate-eligible today (a revert with ``restore_governance=False``
#: genuinely does not rewrite it), so nothing in the derived machinery would stop
#: someone adding it — and its loss from the guarded set is the ORIGINAL CLS
#: fail-open this whole mechanism exists to prevent.
#:
#: These tables may therefore never be excluded, whatever the syntax says. The
#: check is deliberately dumb, security-scoped, and impossible to satisfy by
#: rewording a rehydrator branch.
NEVER_EXCLUDE_TABLES = frozenset({
    "personas",
    "persona_tag_restrictions",
    "row_security_rules",
    "data_tags",
    "data_tag_columns",
})


def _exclusion_specs():
    """The three exclusion lists, read from module state at CALL time.

    Deliberately not a module-level constant: a mutation proof that rebinds one
    of the lists must actually change the derivation, otherwise the test would
    pass against a frozen snapshot of the old value and prove nothing.
    """
    return (
        ("PRESERVE_GATED_TABLES", PRESERVE_GATED_TABLES, _JUSTIFY_PRESERVE_GATED),
        ("UPDATE_IN_PLACE_TABLES", UPDATE_IN_PLACE_TABLES, _JUSTIFY_UPDATE_IN_PLACE),
        ("APPEND_ONLY_TABLES", APPEND_ONLY_TABLES, _JUSTIFY_APPEND_ONLY),
    )


@lru_cache(maxsize=1)
def exclusion_justification_failures() -> tuple[str, ...]:
    """Exclusion entries the rehydrator itself contradicts.

    An entry listed here is NOT applied by :func:`derive` — the table is guarded
    instead. The failure direction is deliberate: a wrong exclusion produces
    NOISE (a report an operator can see and a test that fails), never SILENCE.
    """
    writes = _writes_by_table()
    out: list[str] = []
    for list_name, tables, justify in _exclusion_specs():
        for table in sorted(tables):
            if table in NEVER_EXCLUDE_TABLES:
                out.append(
                    f"{list_name}[{table}]: this table carries row/column-level "
                    "security governance and may never be excluded from the "
                    "guarded set, however the rehydrator is written "
                    "(NEVER_EXCLUDE_TABLES)"
                )
                continue
            entries = writes.get(table)
            if not entries:
                out.append(
                    f"{list_name}[{table}]: the rehydrator does not write this "
                    "table at all — the entry is stale and excludes nothing"
                )
                continue
            reason = justify(entries)
            if reason is not None:
                out.append(f"{list_name}[{table}]: {reason}")
    return tuple(out)


def _justified_exclusions() -> frozenset[str]:
    unjustified = {
        entry.split("[", 1)[1].split("]", 1)[0]
        for entry in exclusion_justification_failures()
    }
    everything: set[str] = set()
    for _name, tables, _justify in _exclusion_specs():
        everything |= set(tables)
    return frozenset(everything) - unjustified


def gated_families() -> frozenset[str]:
    """Every table deliberately kept OUT of the guarded set, with its reason
    recorded above: the preserve-gated aggregate/pocket family, the
    update-in-place model row, and the append-only alert table.

    This reports what the exclusion lists CLAIM (so the live ground-truth revert
    test still checks every one of them is never torn down). What the derivation
    actually applies is the justified subset — see
    :func:`exclusion_justification_failures`.
    """
    return frozenset(
        table for _n, tables, _j in _exclusion_specs() for table in tables
    )


@lru_cache(maxsize=1)
def derive() -> tuple[frozenset[str], tuple[str, ...]]:
    """Return ``(revert_owned_tables, unresolved_target_names)``."""
    writes, unresolved = _analysis()
    tables = {table for table, _kind, _gated in writes}
    return frozenset(tables) - _justified_exclusions(), unresolved


@lru_cache(maxsize=1)
def snapshot_owned_tables() -> frozenset[str]:
    """Table names the revert path delete-and-reinserts."""
    return derive()[0]


def reset_cache() -> None:
    """Test hook: re-read ``rehydrator.py`` (mutation proofs rewrite it)."""
    for fn in (_analysis, exclusion_justification_failures, derive,
               snapshot_owned_tables):
        fn.cache_clear()
