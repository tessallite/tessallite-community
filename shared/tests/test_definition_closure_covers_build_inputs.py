"""Bug-8250 — the closure's entity list must be DERIVED, not curated.

Round-1 review proved a silent wrong number by execution: ``CalendarTable`` is a
CTAS input (both refresh writers substitute ``tess_cal_{type}_{fiscal_start}``
into the FROM clause, and the fiscal variants disagree on ``year_no`` for the
same date) and it was simply absent from the closure. Nothing failed. The same
audit then found ``DimensionAttributeRelationship`` and ``DataSource`` missing
for the same reason.

The root cause is not those three entities. It is that the closure enumerated
what the COMPARISON AUTHOR listed rather than what the BUILDERS READ, so the
next entity a builder starts querying reopens the hole in silence.

Two guards, because the first one alone has an obvious hole:

1. **Entity classification.** For each builder module, every ORM entity it loads
   must be either covered by the closure or on an explicit exclusion list with a
   stated reason. An unclassified entity fails.
2. **Module discovery.** The builder list itself is checked against the
   TRANSITIVE IMPORT CLOSURE of the three writer entry points. Any reached
   module that reads ORM entities must be classified as a builder or a
   non-builder, with a reason. Round-2 review found two existing modules missing
   from the hand-written list (``variant_columns`` renders window SQL into the
   CTAS; ``aggregate_connection`` decides which database it reads), which is
   guard 1's hole: a curated list cannot notice its own omissions.

Together these fail CLOSED on a shape neither list recognises, which is what
CLAUDE.md's coverage-tool blind-spot rule demands of exactly this kind of guard.

Remaining, stated rather than hidden: the AST sees ``select(Entity)`` and
``db.get(Entity, ...)`` with a NAME argument. A dynamically-resolved entity
(``select(registry[name])``) is invisible to both guards.
"""
from __future__ import annotations

import ast
import io
import os

import pytest

#: Workspace root — this file lives at tessallite/shared/tests/, and the module
#: paths below are workspace-relative.
_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

#: The three writer entry points a build starts from.
WRITER_ENTRY_POINTS = {
    "tessallite/services/scheduler/src/jobs/full_refresh.py": "tessallite/services/scheduler",
    "tessallite/services/scheduler/src/jobs/incremental_refresh.py": "tessallite/services/scheduler",
    "tessallite/services/optimizer/src/lifecycle/creator.py": "tessallite/services/optimizer",
}

#: Modules that assemble an aggregate CTAS or the SQL it embeds. Every ORM
#: entity these load is, by definition, a build input.
BUILDER_MODULES = [
    "tessallite/services/scheduler/src/jobs/full_refresh.py",
    "tessallite/services/scheduler/src/jobs/incremental_refresh.py",
    "tessallite/services/optimizer/src/lifecycle/creator.py",
    "tessallite/shared/semantic/sql_builder.py",
    "tessallite/shared/semantic/hierarchy_resolver.py",
    "tessallite/shared/semantic/passenger_manifest_planner.py",
    # Round-2 review: both meet the criterion above and were missing.
    "tessallite/shared/semantic/variant_columns.py",   # window SQL INTO the CTAS
    "tessallite/shared/aggregate_connection.py",       # which DB the CTAS reads
    "tessallite/shared/semantic/calculated_columns.py",
    "tessallite/shared/calendar_target.py",
    # Found by the widened scanner in round 4: it parses and renders calculated
    # measure expressions INTO the CTAS, and its ORM read is join-shaped, so the
    # select()/get()-only scanner returned an empty set for it -- which
    # EXEMPTED it from classification altogether.
    "tessallite/shared/semantic/calculated_expression.py",
    # Bug-8605: it does not emit SQL, but it decides WHICH table anchors the
    # FROM clause and in what order the joins expand -- and the anchor of a
    # LEFT JOIN chain decides the CTAS's row membership. Its reads shape the
    # CTAS as directly as any renderer's.
    "tessallite/shared/semantic/graph_order.py",
]

#: Modules reachable from a writer that read ORM entities but do NOT shape the
#: CTAS. Listed so a NEW ORM-reading module in the build path cannot appear
#: unnoticed; the reason has to say why its reads cannot change a row value.
NON_BUILDER_MODULES = {
    "tessallite/shared/alerting/dispatcher.py": "alert routing; no SQL generation",
    "tessallite/shared/config/resolver.py": "settings precedence chain, not model shape",
    "tessallite/shared/data_quality/validator.py": "post-build rule evaluation",
    "tessallite/shared/db/session.py": "tenant/session plumbing",
    "tessallite/shared/deployed_definition_drift.py": "the drift checker itself",
    "tessallite/shared/model_snapshot/serialiser.py": (
        "writes the DEPLOYED side of this very comparison; its reads define the "
        "snapshot, they do not feed a CTAS"
    ),
    "tessallite/shared/quantile_coverage_producer.py": (
        "records which quantiles an already-built artifact covers"
    ),
    "tessallite/shared/semantic/model_alerts.py": "alert rows, not model shape",
    "tessallite/shared/webhooks/dispatcher.py": "outbound notifications",
    "tessallite/shared/artifact_build_binding.py": (
        "sibling binding: reads the deployed POINTER, not the definitions"
    ),
    "tessallite/shared/artifact_target_binding.py": (
        "sibling binding: reads the source and target ENDPOINTS, not the "
        "definitions"
    ),
    "tessallite/shared/aggregate_refresh_guard.py": (
        "decides the artifact's pending/restore status around a build"
    ),
    "tessallite/shared/aggregate_table_ops.py": (
        "drops and renames physical tables; reads no model definition"
    ),
    "tessallite/shared/connection_scope.py": (
        "resolves and scope-checks the TARGET endpoint"
    ),
    "tessallite/shared/semantic/model_validator.py": (
        "validates an artifact against the model; produces no SQL"
    ),
    "tessallite/shared/semantic/attribute_relationship_deploy_verify.py": (
        "runs AFTER the build to record verification evidence on the artifact "
        "manifest; the declarations it verifies ARE compared, as "
        "attribute_relationships"
    ),
    "tessallite/services/optimizer/src/lifecycle/retirement.py": (
        "retires and purges artifacts; reads no model definition"
    ),
}

#: Entities the closure compares, mapped to the group that carries them.
COVERED = {
    "Measure": "measures",
    "Dimension": "dimensions",
    "ModelTable": "tables",
    "ModelColumn": "columns",
    "Join": "joins",
    "UserDefinedAttribute": "user_defined_attributes",
    "UserDefinedAttributeColumnRef": "uda_column_refs",
    "HierarchyDefinition": "hierarchies",
    "HierarchyLevel": "hierarchies",
    "HierarchyLevelAttribute": "hierarchies",
    "CalendarTable": "calendar_tables",
    "DimensionAttributeRelationship": "attribute_relationships",
    "DataSource": "data_sources",
}

#: Entities a builder reads that are deliberately NOT definition inputs. Each
#: reason has to say what covers it instead, or why nothing needs to.
EXCLUDED = {
    "Model": (
        "the deployed pointer itself, covered by shared/artifact_build_binding.py"
    ),
    "AggregateDefinition": "the artifact being built, not the model definition",
    "AggregateColumn": "the artifact's own column shape",
    "AggregateRefreshPolicy": (
        "refresh cadence and watermark column. Decides WHEN and WHICH SLICE is "
        "re-materialised, never how a value is computed, so it cannot make the "
        "live and deployed definitions disagree"
    ),
    "AggregateRefreshRun": "run history, cannot change a row value",
    "DataTarget": (
        "the storage endpoint the artifact is WRITTEN to, covered by "
        "shared/artifact_target_binding.py"
    ),
    "ProjectConnection": (
        "not a definition, an ENDPOINT -- covered on BOTH sides by "
        "shared/artifact_target_binding.py, which is why it is excluded from "
        "the definition closure rather than compared here. TARGET side "
        "(Bug-8473): built_for_storage_binding + "
        "invalidate_artifacts_for_target. SOURCE side (Bug-8602): "
        "built_for_source_binding, captured by capture_source_build_binding in "
        "all three writers, re-proved under row locks at stamp time by "
        "source_build_binding_matches_live and at serve time by "
        "aggregate_generation_guard._source_binding_matches, with "
        "invalidate_artifacts_for_source_connection folded into "
        "invalidate_artifacts_for_connection so the one control-plane call site "
        "covers both directions. A fingerprint comparison is the right "
        "mechanism here and a snapshot comparison is not: the effective "
        "endpoint can move via persisted source_db.fallback_* settings with the "
        "connection row byte-identical, which no row-versus-row diff can see"
    ),
    "QueryMissLog": "optimizer bookkeeping, never read into generated SQL",
}


def _orm_entities(path: str) -> set[str]:
    """Every ORM entity a module loads, by name.

    An entity is a name IMPORTED FROM a ``db.models`` module in this file, not a
    name that merely looks like a class. Round-4 review pushed the scan past
    ``select(E)`` / ``db.get(E, ...)`` to also cover ``.join(E)``,
    ``.select_from(E)``, ``.outerjoin(E)`` and ``.where(E.x == ...)``, because a
    module whose only read is join-shaped reported an EMPTY set and an empty set
    EXEMPTS a module from classification entirely — the CalendarTable failure
    mode through an unwritten shape. Widening the call sites made a CapWords
    heuristic untenable (``WHITE.join(...)`` on a string constant is not an
    entity read), so provenance replaces the heuristic: it is more precise in
    both directions, and it cannot mistake a local for an entity.
    """
    tree = ast.parse(io.open(path, encoding="utf-8").read())

    entity_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.endswith("db.models") or node.module.endswith(".models"):
                entity_names |= {a.asname or a.name for a in node.names}

    names: set[str] = set()

    def _add(node):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            names.add(node.value.id)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "select":
            for arg in node.args:
                _add(arg)
        elif isinstance(func, ast.Attribute) and node.args:
            if func.attr in ("get", "join", "outerjoin", "select_from", "where"):
                for arg in node.args:
                    _add(arg)
    return names & entity_names


#: Top-level import roots belonging to this repository. Anything else is a
#: third-party dependency the walk has no reason to follow. Asserted against the
#: roots actually imported, so a NEW first-party root cannot be silently treated
#: as third-party -- the previous hard-coded pair failed open on exactly that.
FIRST_PARTY_ROOTS = ("shared", "src", "tessallite")


def _is_first_party(module: str) -> bool:
    return module.split(".", 1)[0] in FIRST_PARTY_ROOTS


def _module_base(module: str, service_root: str) -> str | None:
    """Workspace-relative path stem for a dotted first-party module.

    Bug-8677: also handles the ``tessallite.`` prefix (an absolute import such
    as ``import tessallite.shared.foo``). Stripping the prefix and
    re-dispatching covers the two inner roots (``shared``, ``src``) reached
    through the absolute package name. The fail-closed fix is in
    ``_import_closure`` (a ``None`` return now produces an ``unresolved`` entry
    rather than a silent skip), but recognising the prefix avoids false-positive
    unresolved entries for a legitimate import style.
    """
    # Strip the repository-level package prefix so absolute imports resolve
    # through the same two inner-root branches (shared, src) as relative ones.
    if module.startswith("tessallite."):
        module = module[len("tessallite."):]
    if module.startswith("shared"):
        return "tessallite/" + module.replace(".", "/")
    if module.startswith("src"):
        return service_root + "/" + module.replace(".", "/")
    return None


def _resolve_relative(rel: str, node) -> str | None:
    """Turn ``from . import x`` / ``from ..y import z`` into a path stem."""
    parts = os.path.dirname(rel).split("/")
    up = node.level - 1
    if up:
        if up >= len(parts):
            return None
        parts = parts[:-up]
    stem = "/".join(parts)
    if node.module:
        stem = stem + "/" + node.module.replace(".", "/")
    return stem


def _import_closure(unresolved=None) -> set[str]:
    """Every first-party module transitively imported by the writer entry points.

    Resolves three shapes, because resolving only the first is how round-3
    review found this guard failing OPEN:

    * ``import shared.x`` / ``from shared.x import name`` -> ``shared/x.py``
    * ``from src.ddl import postgres_ddl`` -> ``src/ddl/postgres_ddl.py``. The
      imported NAME can be a module, and ``src/ddl.py`` does not exist, so the
      whole dialect-CTAS package was skipped silently.
    * ``from .postgres_ddl import ...`` -> relative, previously dropped outright
      by the ``node.level == 0`` condition.

    Anything first-party it cannot place is appended to ``unresolved`` so the
    caller can fail closed instead of quietly shrinking the scan.
    """
    reached: set[str] = set()
    stack = list(WRITER_ENTRY_POINTS.items())
    while stack:
        rel, service_root = stack.pop()
        if rel in reached:
            continue
        absolute = os.path.join(_REPO, rel)
        if not os.path.exists(absolute):
            continue
        reached.add(rel)
        tree = ast.parse(io.open(absolute, encoding="utf-8").read())
        stems: list[tuple[str, list[str]]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_first_party(alias.name):
                        base = _module_base(alias.name, service_root)
                        if base:
                            stems.append((base, []))
                        elif unresolved is not None:
                            # Bug-8677: a first-party import that
                            # ``_module_base`` cannot place must surface as
                            # unresolved rather than vanish silently. This is
                            # the root-cause fix -- the previous code's
                            # ``if base:`` gate dropped every unplaceable
                            # first-party import, including the
                            # ``tessallite.``-prefixed style, without ever
                            # reporting it to the fail-closed assertion.
                            unresolved.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = _resolve_relative(rel, node)
                elif node.module and _is_first_party(node.module):
                    base = _module_base(node.module, service_root)
                else:
                    continue
                if base:
                    stems.append((base, [a.name for a in node.names]))
                elif unresolved is not None:
                    # Bug-8677: same fail-closed reporting for ImportFrom.
                    unresolved.append(node.module or rel)
        for base, names in stems:
            # Round-4 review: an imported NAME can be a SUBPACKAGE directory,
            # not only a module file. Omitting ``base/<n>/__init__.py`` meant
            # such a subpackage was never walked AND never reported, because
            # ``placed`` was already True from the parent package's own
            # ``__init__`` -- so the fail-closed assertion could not see it.
            candidates = [base + ".py", base + "/__init__.py"]
            for n in names:
                candidates.append(base + "/" + n + ".py")
                candidates.append(base + "/" + n + "/__init__.py")
            placed = False
            for candidate in candidates:
                if os.path.exists(os.path.join(_REPO, candidate)):
                    stack.append((candidate, service_root))
                    placed = True
            if not placed and unresolved is not None:
                unresolved.append(base)
    return reached


@pytest.mark.parametrize("module", BUILDER_MODULES)
def test_builder_module_exists(module):
    """A rename must fail loudly instead of silently shrinking the scan."""
    assert os.path.exists(os.path.join(_REPO, module)), (
        f"{module} is in BUILDER_MODULES but does not exist; the closure "
        f"coverage check is scanning nothing for it"
    )


@pytest.mark.parametrize("module", BUILDER_MODULES)
def test_every_entity_a_builder_reads_is_classified(module):
    """Covered by the closure, or explicitly excluded with a reason. No third state.

    An unclassified entity is the CalendarTable defect repeating: a builder reads
    something, the comparison does not, and a live edit to it produces numbers
    the deployed model does not describe -- with no error and no staleness.
    """
    entities = _orm_entities(os.path.join(_REPO, module))
    unclassified = sorted(entities - set(COVERED) - set(EXCLUDED))
    assert not unclassified, (
        f"{module} loads ORM entities the definition closure neither compares "
        f"nor excludes: {unclassified}. Add each to COVERED (and to "
        f"shared/definition_closure.py + shared/deployed_definition_drift.py) "
        f"if it can change what the CTAS produces, or to EXCLUDED with the "
        f"reason it cannot."
    )


def test_import_closure_follows_package_and_relative_imports():
    """The discovery walk must not fail OPEN on the two commonest import shapes.

    ``creator.py`` reaches the dialect CTAS emitters as
    ``from src.ddl import bigquery_ddl, postgres_ddl, spark_ddl`` -- a PACKAGE
    import. Resolving only ``src.ddl`` -> ``src/ddl.py`` found nothing and
    skipped them silently, so the most load-bearing modules in the build path
    (``postgres_ddl.build_pg_ctas`` IS the CTAS builder) sat outside the guard
    while every test was green. Round-3 review measured 79 modules reached
    versus 89 with a correct resolver.
    """
    reached = {r.replace(chr(92), "/") for r in _import_closure()}
    for expected in (
        "tessallite/services/optimizer/src/ddl/postgres_ddl.py",
        "tessallite/services/optimizer/src/ddl/bigquery_ddl.py",
        "tessallite/services/scheduler/src/ddl/dialect_map.py",
    ):
        assert expected in reached, (
            f"{expected} is imported by a writer but the discovery walk never "
            f"reached it; the guard fails OPEN on that import shape"
        )


def test_import_closure_places_every_first_party_import():
    """Fail CLOSED on a first-party import the resolver cannot place.

    Silently skipping an unresolvable module is exactly what let the package
    shape go unnoticed, so an unplaceable first-party import is a test failure
    and a new import style gets surfaced instead of shrinking the scan.
    """
    unresolved: list[str] = []
    _import_closure(unresolved)
    assert not sorted(set(unresolved)), (
        f"first-party imports the discovery walk could not place: "
        f"{sorted(set(unresolved))}. Extend _module_base / the candidate list "
        f"rather than letting the walk skip them."
    )


def test_orm_scanner_sees_a_join_shaped_read():
    """An entity loaded only via ``.join()`` must not report as no read at all.

    This is the guard on the guard. An EMPTY entity set EXEMPTS a module from
    builder/non-builder classification, so a scanner blind to a read shape does
    not merely under-report — it removes the module from the contract entirely.
    Round-4 review demonstrated it live on ``shared/aggregate_connection.py``,
    whose only ORM read is ``.join(ModelTable)``; widening the scan then
    immediately surfaced ``calculated_expression`` as an unclassified builder,
    which had been invisible for the same reason.
    """
    entities = _orm_entities(
        os.path.join(_REPO, "tessallite/shared/aggregate_connection.py")
    )
    assert "ModelTable" in entities, (
        "the scanner reports no ORM read for a module whose only read is "
        "join-shaped; an empty set exempts it from classification"
    )


def test_orm_scanner_ignores_a_lookalike_that_is_not_an_entity():
    """Provenance, not CapWords: a string constant's ``.join`` is not a read.

    Widening the call sites made the old CapWords heuristic report ``WHITE`` from
    a ``WHITE.join(...)``. Keying on names imported from a ``db.models`` module
    is precise in both directions and cannot mistake a local for an entity.
    """
    entities = _orm_entities(
        os.path.join(_REPO, "tessallite/shared/semantic/calculated_expression.py")
    )
    assert all(e[0].isupper() for e in entities)
    assert "WHITE" not in entities


def test_first_party_roots_cover_every_root_a_writer_imports():
    """A new first-party top-level package must not be silently third-party.

    ``_is_first_party`` decides what the walk follows at all. A root missing
    from it drops every module beneath it with no report -- the same fail-open
    shape as the package-import miss, one level further up.
    """
    roots: set[str] = set()
    for rel in _import_closure():
        absolute = os.path.join(_REPO, rel)
        tree = ast.parse(io.open(absolute, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots |= {a.name.split(".", 1)[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                roots.add(node.module.split(".", 1)[0])
    search_dirs = ["tessallite"] + list(WRITER_ENTRY_POINTS.values())
    on_disk = {
        r for r in roots
        if any(os.path.isdir(os.path.join(_REPO, d, r)) for d in search_dirs)
    }
    missing = sorted(on_disk - set(FIRST_PARTY_ROOTS))
    assert not missing, (
        f"first-party import roots the walk does not follow: {missing}. Add "
        f"them to FIRST_PARTY_ROOTS -- a root it does not recognise is dropped "
        f"silently, not reported."
    )


def test_every_orm_reading_module_in_the_build_path_is_classified():
    """Closes guard 1's own hole: a curated list cannot notice its omissions.

    Walks the transitive import closure of the three writer entry points and
    requires every reached module that reads ORM entities to be declared either
    a builder or a non-builder. Round-2 review found ``variant_columns`` (window
    SQL rendered INTO the CTAS) and ``aggregate_connection`` (which database the
    CTAS reads) already missing from BUILDER_MODULES while every test was green.
    """
    builders = set(BUILDER_MODULES)
    unclassified: list[str] = []
    for rel in sorted(_import_closure()):
        normalised = rel.replace("\\", "/")
        if normalised in builders or normalised in NON_BUILDER_MODULES:
            continue
        if _orm_entities(os.path.join(_REPO, normalised)):
            unclassified.append(normalised)
    assert not unclassified, (
        "these modules are reachable from an aggregate build and read ORM "
        f"entities, but are declared neither a builder nor a non-builder: "
        f"{unclassified}. Add each to BUILDER_MODULES if its reads shape the "
        f"CTAS, or to NON_BUILDER_MODULES with the reason they cannot."
    )


@pytest.mark.parametrize("module", sorted(NON_BUILDER_MODULES))
def test_non_builder_module_exists_and_has_a_reason(module):
    assert os.path.exists(os.path.join(_REPO, module)), (
        f"{module} is in NON_BUILDER_MODULES but does not exist"
    )
    assert NON_BUILDER_MODULES[module].strip(), f"{module} has no stated reason"


def test_every_covered_entity_is_actually_in_the_closure():
    """COVERED must not drift from the dataclass it claims to describe.

    Without this, someone could silence the entity check by adding a name to
    COVERED without adding the comparison -- turning the guard into a rubber
    stamp, which is worse than not having it.
    """
    from shared.definition_closure import DefinitionClosure

    groups = set(DefinitionClosure().__dict__)
    missing = sorted({g for g in COVERED.values() if g not in groups})
    assert not missing, (
        f"COVERED names closure groups that do not exist: {missing}"
    )


def test_every_closure_group_is_claimed_by_some_entity():
    """The inverse: a group nobody produces is dead comparison weight."""
    from shared.definition_closure import DefinitionClosure

    groups = set(DefinitionClosure().__dict__)
    unclaimed = sorted(groups - set(COVERED.values()))
    assert not unclaimed, (
        f"closure groups no builder entity maps to: {unclaimed}"
    )


def test_calendar_table_specifically_is_covered():
    """Named regression guard for the round-1 finding.

    Kept separate from the generic scan so the specific defect that produced a
    proven wrong number cannot be silently reclassified into EXCLUDED.
    """
    assert COVERED.get("CalendarTable") == "calendar_tables"
    assert "CalendarTable" not in EXCLUDED


# -----------------------------------------------------------------------
# Bug-8677 regression: tessallite.-prefixed imports
# -----------------------------------------------------------------------


def test_module_base_resolves_tessallite_dot_prefix():
    """Bug-8677. ``_module_base`` must resolve ``tessallite.shared.foo`` the
    same way it resolves ``shared.foo``, so the transitive walk does not
    silently lose the module and the fail-closed assertion can see it.
    """
    result = _module_base("tessallite.shared.semantic.sql_builder", "tessallite/services/scheduler")
    assert result == "tessallite/shared/semantic/sql_builder", (
        f"_module_base returned {result!r} for a tessallite.-prefixed import; "
        "it must strip the prefix and resolve through the inner roots"
    )


def test_module_base_resolves_tessallite_dot_src_prefix():
    """Bug-8677. Same rule for ``tessallite.src.…`` prefixed modules."""
    result = _module_base("tessallite.src.jobs.full_refresh", "tessallite/services/scheduler")
    assert result == "tessallite/services/scheduler/src/jobs/full_refresh", (
        f"_module_base returned {result!r} for a tessallite.src. import; "
        "it must strip the prefix and resolve through the inner roots"
    )


def test_unplaceable_first_party_import_lands_in_unresolved():
    """Bug-8677 root-cause fix. A first-party import that ``_module_base``
    cannot place must appear in ``unresolved``, not vanish silently.

    Before this fix, a ``None`` return from ``_module_base`` was silently
    dropped by the ``if base:`` gate, so the fail-closed assertion
    (``assert not sorted(set(unresolved))``) could never see it. This is the
    FOURTH instance of the coverage-tool enumeration blind-spot class in this
    file — each one was the same fail-OPEN direction.

    Tested by injecting a synthetic module whose import ``_module_base`` cannot
    resolve, rather than depending on a real codebase import pattern.
    """
    import tempfile

    # A tiny module that imports from a first-party root the resolver does not
    # handle, but which IS in FIRST_PARTY_ROOTS (so _is_first_party returns
    # True). ``tessallite.nonexistent`` fits: ``tessallite`` is a first-party
    # root, but after stripping the prefix ``nonexistent`` matches neither
    # ``shared`` nor ``src``, so _module_base returns None.
    snippet = 'from tessallite.nonexistent import something\n'
    with tempfile.NamedTemporaryFile(
        suffix=".py", dir=os.path.join(_REPO, "tessallite/shared"),
        mode="w", delete=False, encoding="utf-8",
    ) as f:
        f.write(snippet)
        f.flush()
        probe_path = f.name

    try:
        rel = os.path.relpath(probe_path, _REPO).replace("\\", "/")
        # Build a small closure that starts from the probe file.
        old_entry = dict(WRITER_ENTRY_POINTS)
        WRITER_ENTRY_POINTS.clear()
        WRITER_ENTRY_POINTS[rel] = "tessallite/shared"
        try:
            unresolved: list[str] = []
            _import_closure(unresolved)
            assert any("nonexistent" in u for u in unresolved), (
                f"expected the tessallite.nonexistent import to land in "
                f"unresolved; got {unresolved}"
            )
        finally:
            WRITER_ENTRY_POINTS.clear()
            WRITER_ENTRY_POINTS.update(old_entry)
    finally:
        os.unlink(probe_path)


# ---------------------------------------------------------------------------
# Bug-8602 — an EXCLUDED reason must be TRUE, not merely present
# ---------------------------------------------------------------------------
#
# The Bug-8250 round-2 reason for ProjectConnection read "the storage endpoint,
# covered by shared/artifact_target_binding.py". Present, well-formed, and
# false: that module covered only the TARGET endpoint, and the SOURCE endpoint
# behind DataSource.project_connection_id was covered by nothing at all. The
# guard therefore rubber-stamped the exact gap it exists to find — the
# coverage-tool blind-spot class in CLAUDE.md, this time in the reason text
# rather than in the enumeration.
#
# A prose reason cannot be verified in general. What CAN be verified is that
# every mechanism the reason NAMES still exists and is still wired, so a later
# refactor cannot leave a true-sounding reason standing over a removed guard.


def test_the_projectconnection_exclusion_names_mechanisms_that_exist():
    from shared import artifact_target_binding as atb
    from shared.db.models import AggregateDefinition

    reason = EXCLUDED["ProjectConnection"]

    for symbol in (
        "capture_source_build_binding",
        "source_build_binding_matches_live",
        "invalidate_artifacts_for_source_connection",
        "invalidate_artifacts_for_connection",
        "invalidate_artifacts_for_target",
    ):
        assert symbol in reason, (
            f"the exclusion reason no longer names {symbol}; state what "
            f"covers the SOURCE endpoint or move ProjectConnection to COVERED"
        )
        assert hasattr(atb, symbol), (
            f"the exclusion reason claims {symbol} covers ProjectConnection, "
            f"but shared/artifact_target_binding.py no longer defines it"
        )

    for column in ("built_for_storage_binding", "built_for_source_binding"):
        assert column in reason
        assert hasattr(AggregateDefinition, column), (
            f"the exclusion reason claims {column} records the binding, but "
            f"the ORM no longer has that column"
        )


def test_the_source_invalidator_is_reachable_from_the_only_call_site():
    """The reason claims the source invalidator is "folded into"
    ``invalidate_artifacts_for_connection``. That is the whole basis for
    excluding ProjectConnection from the closure, because ``connections.py``
    makes exactly ONE invalidation call. Pin the fold."""
    import inspect

    from shared import artifact_target_binding as atb

    src = inspect.getsource(atb.invalidate_artifacts_for_connection)
    assert "invalidate_artifacts_for_source_connection(" in src, (
        "the control-plane entry point no longer invalidates the SOURCE side; "
        "a connection that is a model's source but nobody's target would stale "
        "nothing (Bug-8602)"
    )


def test_the_source_guard_is_wired_into_the_query_router():
    """The reason also claims a serve-time re-proof. That lives in another
    service, so it is checked as text rather than imported."""
    path = os.path.join(
        _REPO,
        "tessallite/services/query-router/src/routing/aggregate_generation_guard.py",
    )
    src = io.open(path, encoding="utf-8").read()
    assert "aggregate_generation_guard._source_binding_matches" in EXCLUDED[
        "ProjectConnection"
    ]
    assert "async def _source_binding_matches(" in src
    assert "_source_binding_matches(db, row)" in src, (
        "the aggregate serve-time guard defines the source re-proof but never "
        "calls it; built_for_source_binding would be a write-only column"
    )
