"""F-006-02 — static architecture guard for the single dialect render boundary.

The rewriter authors every statement PostgreSQL-canonical and MUST emit
target-dialect SQL only through the one render boundary in
``src/rewrite/dialects.py`` (``render_tree_for_dialect`` /
``render_expression_for_dialect`` / their private impl ``_render_for_dialect``).
That boundary is where the week-numbering rewrite (Bug-7917 / F-103-03) and the
Spark / T-SQL semi-additive fail-loud guards are applied uniformly, so a caller
that renders a NON-postgres dialect directly with ``tree.sql(dialect=<target>)``
silently skips those guards — the exact F-006-02 defect.

This test statically scans every ``rewrite`` module (except the exact package
path ``src/rewrite/dialects.py``, which owns the boundary) and fails the build
if any ``.sql(dialect=...)`` call targets anything other than the PostgreSQL canonical literal. A
PG-canonical ``.sql(dialect="postgres")`` is allowed: postgres is the identity /
input side of the boundary, not a target-dialect emission.

If a genuinely-new sanctioned exception is ever needed (a tree that is already
target-authored), add it to ``ALLOWED_EXCEPTIONS`` with a one-line reason — do
NOT relax the regex. There are currently none.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REWRITE_DIR = Path(__file__).resolve().parents[1] / "src" / "rewrite"

# The one exact package-relative boundary is allowed to emit a target. A
# basename exemption would also exempt ``nested/dialects.py`` and fail open.
BOUNDARY_RELATIVE_PATH = Path("dialects.py")

# sqlglot dialect strings that are the PostgreSQL canonical / identity side of
# the boundary. Emitting these is a no-op re-serialisation of PG-canonical SQL
# (the input to the boundary), NOT a target-dialect emission, so it is allowed.
_PG_CANONICAL_LITERALS = {"postgres", "postgresql"}

# Narrowly-documented sanctioned exceptions: (module, line, reason). Empty by
# design — every non-postgres emission currently routes through the boundary.
ALLOWED_EXCEPTIONS: dict[tuple[str, int], str] = {}


def _rewrite_module_paths(root: Path = REWRITE_DIR) -> list[Path]:
    """Enumerate the complete rewrite package recursively."""
    return sorted(root.rglob("*.py"))


def _parse_module(path: Path) -> tuple[str, ast.Module | None, list[tuple[int, str]]]:
    """Parse a coverage target and turn any unreadable shape into a violation."""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return "", None, [(0, f"coverage read failure: {exc}")]
    try:
        return source, ast.parse(source, filename=str(path)), []
    except SyntaxError as exc:
        return source, None, [
            (exc.lineno or 0, f"coverage parse failure: {exc.msg}")
        ]


def _is_boundary_module(path: Path, root: Path = REWRITE_DIR) -> bool:
    """Identify only ``<rewrite package>/dialects.py`` as the boundary."""
    return path.resolve() == (root / BOUNDARY_RELATIVE_PATH).resolve()


def _path_label(path: Path, root: Path = REWRITE_DIR) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _dialect_call_violations(path: Path) -> list[tuple[int, str]]:
    """Return (lineno, snippet) for every ``X.sql(dialect=<non-pg>)`` call in
    *path* that is not a PG-canonical emission."""
    source, tree, parse_violations = _parse_module(path)
    if tree is None:
        return parse_violations
    lines = source.splitlines()
    violations: list[tuple[int, str]] = []

    # Bug-5173: a SQLGlot Generator recursively calls ``self.sql(expression)``;
    # that method's first argument is an expression, not a dialect. Identify
    # only the nested Generator owned by our explicit Postgres subclass. A
    # generic "class named Generator" exemption would fail open: a bypass
    # could hide in an unrelated class with that name.
    canonical_generator_ranges: list[tuple[int, int]] = []
    for outer in tree.body:
        if not isinstance(outer, ast.ClassDef):
            continue
        is_postgres_subclass = any(
            isinstance(base, ast.Name) and base.id == "Postgres"
            for base in outer.bases
        )
        if not is_postgres_subclass:
            continue
        for child in outer.body:
            if isinstance(child, ast.ClassDef) and child.name == "Generator":
                canonical_generator_ranges.append(
                    (child.lineno, child.end_lineno or child.lineno)
                )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Match attribute calls named ``.sql(...)``.
        if not (isinstance(func, ast.Attribute) and func.attr == "sql"):
            continue
        if (
            isinstance(func.value, ast.Name)
            and func.value.id == "self"
            and any(start <= node.lineno <= end for start, end in canonical_generator_ranges)
        ):
            continue
        # Find a ``dialect=`` keyword argument OR a positional first argument.
        # Opus review finding 7: ``.sql("tsql")`` (positional) would bypass a
        # keyword-only check. Since ``.sql()``'s first arg is ``dialect``,
        # inspect both forms.
        dialect_keywords = [kw for kw in node.keywords if kw.arg == "dialect"]
        opaque_arguments = any(isinstance(arg, ast.Starred) for arg in node.args) or any(
            keyword.arg is None for keyword in node.keywords
        )
        if opaque_arguments or len(dialect_keywords) > 1:
            violations.append((node.lineno, lines[node.lineno - 1].strip()))
            continue
        dialect_kw = dialect_keywords[0] if dialect_keywords else None
        # Also inspect the first positional argument (``tree.sql("tsql")``).
        dialect_pos = node.args[0] if node.args and not dialect_kw else None
        candidate_values: list = []
        if dialect_kw is not None:
            candidate_values.append(dialect_kw.value)
        if dialect_pos is not None:
            candidate_values.append(dialect_pos)
        if not candidate_values:
            continue
        for value in candidate_values:
            # A string-literal PG-canonical dialect is allowed.
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                if value.value.lower() in _PG_CANONICAL_LITERALS:
                    continue
                # A non-postgres string literal is a direct target emission.
                violations.append((node.lineno, lines[node.lineno - 1].strip()))
                continue
            # A NON-literal dialect argument (e.g. ``dialect=target_dialect``)
            # is a dynamic target emission that must go through the boundary.
            violations.append((node.lineno, lines[node.lineno - 1].strip()))

    return violations


def _transpile_aliases(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Return module/function names that can call SQLGlot ``transpile``."""
    module_names = {"sqlglot"}
    function_names = {"transpile"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sqlglot":
                    module_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "sqlglot":
            for alias in node.names:
                if alias.name == "transpile":
                    function_names.add(alias.asname or alias.name)

    def is_known_reference(value: ast.expr) -> bool:
        return (
            isinstance(value, ast.Name) and value.id in function_names
        ) or (
            isinstance(value, ast.Attribute)
            and value.attr == "transpile"
            and isinstance(value.value, ast.Name)
            and value.value.id in module_names
        )

    # Close over simple local aliases (``emit = sg.transpile``), including
    # alias-to-alias assignments, without attempting dynamic data flow.
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if value is None or not is_known_reference(value):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in function_names:
                    function_names.add(target.id)
                    changed = True
    return module_names, function_names


def _transpile_write_violations(path: Path) -> list[tuple[int, str]]:
    """Return (lineno, snippet) for every ``sqlglot.transpile(... write=<non-pg>)``
    call in *path* that is not a PG-canonical emission (Fable D-adjacent guard)."""
    source, tree, parse_violations = _parse_module(path)
    if tree is None:
        return parse_violations
    lines = source.splitlines()
    violations: list[tuple[int, str]] = []
    module_names, function_names = _transpile_aliases(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Match ``sqlglot.transpile(...)`` or ``transpile(...)`` calls.
        _is_transpile = False
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "transpile"
            and (
                not isinstance(func.value, ast.Name)
                or func.value.id in module_names
            )
        ):
            _is_transpile = True
        elif isinstance(func, ast.Name) and func.id in function_names:
            _is_transpile = True
        if not _is_transpile:
            continue
        write_keywords = [kw for kw in node.keywords if kw.arg == "write"]
        opaque_arguments = any(isinstance(arg, ast.Starred) for arg in node.args) or any(
            keyword.arg is None for keyword in node.keywords
        )
        if opaque_arguments or len(write_keywords) > 1:
            violations.append((node.lineno, lines[node.lineno - 1].strip()))
            continue
        write_kw = write_keywords[0] if write_keywords else None
        write_pos = node.args[2] if len(node.args) >= 3 else None
        if write_kw is not None and write_pos is not None:
            violations.append((node.lineno, lines[node.lineno - 1].strip()))
            continue
        value = write_kw.value if write_kw is not None else write_pos
        if value is None:
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            if value.value.lower() in _PG_CANONICAL_LITERALS:
                continue
        violations.append((node.lineno, lines[node.lineno - 1].strip()))
    return violations


def test_no_direct_target_dialect_render_outside_boundary():
    """No ``rewrite`` module (other than dialects.py) may emit a non-postgres
    dialect via a direct ``.sql(dialect=...)`` call — every such emission must
    route through the ``render_*_for_dialect`` boundary (F-006-02)."""
    offenders: list[str] = []
    for path in _rewrite_module_paths():
        if _is_boundary_module(path):
            continue
        label = _path_label(path)
        for lineno, snippet in _dialect_call_violations(path):
            if ALLOWED_EXCEPTIONS.get((label, lineno)):
                continue
            offenders.append(f"{label}:{lineno}: {snippet}")

    assert not offenders, (
        "Direct target-dialect .sql(dialect=...) call(s) bypass the single "
        "render boundary (F-006-02). Route these through "
        "render_tree_for_dialect / render_expression_for_dialect in dialects.py:\n"
        + "\n".join(offenders)
    )


def test_no_sqlglot_transpile_write_outside_boundary():
    """No ``rewrite`` module (other than dialects.py) may use
    ``sqlglot.transpile(write=...)``
    to emit a non-postgres dialect. This bypasses the render boundary's
    pre-generation transforms (WEEK->ISOWEEK, semi-additive fail-loud).
    Fable review D-adjacent finding."""
    offenders: list[str] = []
    for path in _rewrite_module_paths():
        if _is_boundary_module(path):
            continue
        label = _path_label(path)
        for lineno, snippet in _transpile_write_violations(path):
            offenders.append(f"{label}:{lineno}: {snippet}")

    assert not offenders, (
        "Direct sqlglot.transpile(write=<non-pg>) call(s) bypass the single "
        "render boundary (F-006-02 / Fable D-adjacent). Route these through "
        "render_tree_for_dialect in dialects.py:\n"
        + "\n".join(offenders)
    )


def test_boundary_module_has_the_render_entrypoints():
    """The boundary module exposes the public render entry points the other
    modules must use (guards against a rename silently disabling the guard)."""
    from src.rewrite import dialects

    assert hasattr(dialects, "render_tree_for_dialect")
    assert hasattr(dialects, "render_expression_for_dialect")
    assert hasattr(dialects, "_render_for_dialect")


def test_bug_5173_generator_exemption_is_narrow_and_guard_stays_fail_closed(
    tmp_path: Path,
) -> None:
    """Bug-5173 tool audit: only an actual Postgres Generator is exempt.

    Test escape: the scanner mistook Generator recursion for Expression target
    emission; a broad self.sql exemption would then hide real bypasses. Guard:
    a nested Postgres Generator is clean while an unrelated self.sql(dynamic)
    and a normal tree.sql(dynamic) are both reported. Tier: T3.
    """
    candidate = tmp_path / "candidate.py"
    candidate.write_text(
        "from sqlglot.dialects.postgres import Postgres\n"
        "class Canonical(Postgres):\n"
        "    class Generator(Postgres.Generator):\n"
        "        def render(self, expression):\n"
        "            return self.sql(expression)\n"
        "class NotAGenerator:\n"
        "    def render(self, target):\n"
        "        return self.sql(target)\n"
        "tree.sql(target)\n",
        encoding="utf-8",
    )
    violations = _dialect_call_violations(candidate)
    assert len(violations) == 2, violations


def test_bug_5173_render_guard_enumerates_nested_modules(
    tmp_path: Path,
) -> None:
    """Bug-5173 coverage audit: future nested rewrite modules stay in scope.

    Test escape: top-level ``glob('*.py')`` silently ignored subpackages, so a
    new nested fragment producer could bypass the boundary while the guard
    remained green. Guard: recursive discovery finds a nested direct target
    render, whose AST scanner reports it. Tier: T3.
    """
    nested = tmp_path / "nested"
    nested.mkdir()
    candidate = nested / "producer.py"
    candidate.write_text("tree.sql(dialect=target_dialect)\n", encoding="utf-8")
    discovered = _rewrite_module_paths(tmp_path)
    assert discovered == [candidate]
    assert _dialect_call_violations(discovered[0])


@pytest.mark.parametrize("mod_name", ["uda", "aggregate", "pocket", "source_sql"])
def test_rewrite_modules_import_the_boundary(mod_name):
    """The emission-site modules import a render boundary symbol (so the guard
    is not trivially satisfied by a module that renders nothing)."""
    import importlib

    mod = importlib.import_module(f"src.rewrite.{mod_name}")
    src_text = Path(mod.__file__).read_text(encoding="utf-8")
    # Each of these modules either renders a fragment through the boundary or
    # (pocket / source_sql) uses the private impl; assert at least one boundary
    # symbol is referenced.
    assert any(
        sym in src_text
        for sym in (
            "render_expression_for_dialect",
            "render_tree_for_dialect",
            "_render_for_dialect",
        )
    ), f"{mod_name} does not reference the render boundary"
