"""Bug-8453 — the conversational agent must not tell a business user "there is
no data for that" when the truthful answer is "your row-security policy grants
you no rows".

This is the most user-visible of the four remaining ``/execute`` consumers: the
other three render a blank surface, but the agent makes a positive FALSE
ASSERTION about the business in natural language, and hides a possibly
misconfigured policy from the person best placed to report it.

Two legs, because a producer fix with an unwired consumer is the exact gap this
codebase keeps hitting:
  1. ``QueryExecution`` actually carries the denial off the wire.
  2. The narration prompt actually branches on it.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

from shared.security.execute_contract import ROW_SECURITY_DENY_ALL_RULE_ID
from src.narrate.narrate import _build_format_block, _build_narrate_prompt


def _execution(rows, *, denied: bool, columns=("region", "revenue")):
    from src.exec.query import QueryExecution

    return QueryExecution(
        sql="SELECT 1",
        columns=list(columns),
        rows=list(rows),
        rows_returned=len(rows),
        route_type="source",
        routed_sql=None,
        aggregate_id=None,
        pocket_id=None,
        execution_ms=1,
        truncated=False,
        security_rules_applied=(
            (ROW_SECURITY_DENY_ALL_RULE_ID,) if denied else ()
        ),
        row_security_denied=denied,
    )


class TestQueryExecutionCarriesTheDenial:
    def test_defaults_are_backwards_compatible(self):
        ex = _execution([], denied=False)
        assert ex.security_rules_applied == ()
        assert ex.row_security_denied is False

    def test_denial_is_recorded(self):
        ex = _execution([], denied=True)
        assert ex.row_security_denied is True
        assert ROW_SECURITY_DENY_ALL_RULE_ID in ex.security_rules_applied


class TestNarrationBranchesOnTheDenial:
    def _block(self, *, denied: bool, is_empty: bool = True) -> str:
        return _build_format_block(
            columns=["region", "revenue"],
            sample_rows=[],
            output_format="text",
            is_empty=is_empty,
            row_security_denied=denied,
        )

    def test_denied_prompt_names_the_permissions_restriction(self):
        block = self._block(denied=True).lower()
        assert "permission" in block
        assert "row-level security" in block or "row level security" in block

    def test_denied_prompt_forbids_asserting_the_data_does_not_exist(self):
        """The whole defect: the agent asserted absence of data. The prompt must
        explicitly forbid that framing, not merely omit it."""
        block = self._block(denied=True)
        assert "no data was found" not in block.lower()
        assert "does not exist" in block.lower()

    def test_denied_prompt_does_not_leak_the_rule_contents(self):
        """The agent is given rule IDS only; it must not be invited to describe
        the row-security policy itself."""
        block = self._block(denied=True).lower()
        assert "do not describe the security rules" in block

    def test_undenied_empty_result_keeps_the_plain_no_data_wording(self):
        """Regression guard the other way: a genuinely empty result must NOT
        start claiming a permissions problem."""
        block = self._block(denied=False).lower()
        assert "no data was found" in block
        assert "permission" not in block

    def test_denial_takes_priority_over_a_nonempty_row_set(self):
        """A deny-all can still return a row (COUNT(*) over WHERE 0 = 1 gives
        0), so the denial branch must not be gated on is_empty."""
        block = self._block(denied=True, is_empty=False).lower()
        assert "permission" in block


class TestNarratePromptAnswerBlock:
    def _prompt(self, *, denied: bool) -> str:
        _system, user = _build_narrate_prompt(
            project_system_prompt="sys",
            user_message="what were sales?",
            execution=_execution([], denied=denied),
        )
        return user

    def test_denied_answer_block_states_a_permissions_restriction(self):
        text = self._prompt(denied=True).lower()
        assert "permissions restriction" in text
        assert "not an absence of data" in text

    def test_undenied_answer_block_states_no_data(self):
        text = self._prompt(denied=False).lower()
        assert "no data was found for this query" in text
        assert "permissions restriction" not in text


# ---------------------------------------------------------------------------
# R2 deep-review findings B2 + S1, applied.
#
# S1: every test above builds ``QueryExecution`` DIRECTLY, so the producer half
# was unproven — deleting the two lines in ``execute_query`` that derive the
# denial left this file fully green. These exercise the real wire path.
#
# B2: ``execute_query`` is the single agent-service /execute chokepoint with
# THREE call sites (direct query, compound step, recipe step) and only the
# direct one consulted the denial. A denied step returned COUNT(*) = 0 from
# ``WHERE 0 = 1`` straight into a combine expression, so the agent stated a
# fabricated business figure. The invariant now lives at the chokepoint.
# ---------------------------------------------------------------------------

import types as _types
import uuid as _uuid
from unittest.mock import AsyncMock, MagicMock, patch

from src.exec.query import (
    QueryExecutionError,
    RowSecurityDeniedQueryError,
    execute_query,
)
from src.tools.spec import QueryToolCall

_MODEL = _uuid.uuid4()


def _wire_client(payload):
    class _Resp:
        status_code = 200

        def json(self):
            return payload

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            return _Resp()

    return _Client


async def _execute_with_payload(payload, **kw):
    db = AsyncMock()
    db.get = AsyncMock(
        return_value=_types.SimpleNamespace(id=_MODEL, slug="test-model")
    )
    meas = MagicMock()
    meas.all.return_value = [("revenue", "SUM")]
    db.execute = AsyncMock(return_value=meas)
    call = QueryToolCall(
        model_id=str(_MODEL), measures=["revenue"],
        dimensions=[], where=[], having=[], sort=[],
    )
    with patch("src.exec.query.httpx.AsyncClient", _wire_client(payload)):
        return await execute_query(
            db, call, "jwt",
            allowed_model_ids={_MODEL},
            persona_scopes=None,
            **kw,
        )


_DENIED_WIRE = {
    # The realistic deny-all shape: a row IS returned and it contains 0.
    "rows": [{"revenue": 0}],
    "columns": ["revenue"],
    "route_type": "source",
    "security_rules_applied": [ROW_SECURITY_DENY_ALL_RULE_ID],
}


class TestDenialIsReadOffTheWire:
    """The producer half: execute_query must derive the denial from the
    /execute payload, and a returned row must NOT suppress it."""

    @pytest.mark.asyncio
    async def test_deny_all_sentinel_is_read_from_the_payload(self):
        ex = await _execute_with_payload(
            _DENIED_WIRE, allow_row_security_denial=True,
        )
        assert ex.row_security_denied is True
        assert ROW_SECURITY_DENY_ALL_RULE_ID in ex.security_rules_applied

    @pytest.mark.asyncio
    async def test_narrowing_rule_is_carried_but_is_not_a_denial(self):
        ex = await _execute_with_payload({
            "rows": [{"revenue": 5}], "columns": ["revenue"],
            "route_type": "source", "security_rules_applied": ["region-rule"],
        })
        assert ex.row_security_denied is False
        assert ex.security_rules_applied == ("region-rule",)

    @pytest.mark.asyncio
    async def test_absent_field_is_not_a_denial(self):
        ex = await _execute_with_payload(
            {"rows": [], "columns": [], "route_type": "source"}
        )
        assert ex.row_security_denied is False
        assert ex.security_rules_applied == ()


class TestChokepointFailsClosed:
    """B2: the invariant lives at the chokepoint, so a caller that has NOT been
    taught to render a denial cannot consume one as data."""

    @pytest.mark.asyncio
    async def test_denial_raises_by_default(self):
        with pytest.raises(RowSecurityDeniedQueryError) as exc:
            await _execute_with_payload(_DENIED_WIRE)
        # The message must name the restriction, not blame the query.
        assert "permissions restriction" in str(exc.value).lower()
        assert "not an absence of data" in str(exc.value).lower()

    @pytest.mark.asyncio
    async def test_the_denial_error_is_a_query_execution_error(self):
        """Compound steps and recipe steps already catch QueryExecutionError
        and REFUSE, so subclassing it is what makes them fail closed without
        each having to be taught about row security."""
        assert issubclass(RowSecurityDeniedQueryError, QueryExecutionError)

    @pytest.mark.asyncio
    async def test_only_an_opted_in_caller_receives_a_denied_execution(self):
        ex = await _execute_with_payload(
            _DENIED_WIRE, allow_row_security_denial=True,
        )
        assert ex.row_security_denied is True

    @pytest.mark.asyncio
    async def test_a_narrowed_result_is_never_refused(self):
        """Rows the caller IS permitted to see are correct data; refusing them
        would break every legitimately row-restricted user."""
        ex = await _execute_with_payload({
            "rows": [{"revenue": 5}], "columns": ["revenue"],
            "route_type": "source", "security_rules_applied": ["region-rule"],
        })
        assert ex.rows == [{"revenue": 5}]


@pytest.mark.asyncio
async def test_direct_query_path_opts_in_so_it_can_narrate_the_denial():
    """The one caller allowed to proceed on a denial is the direct-query path,
    because narrate.py has a branch that tells the user their access is
    restricted. If that opt-in is ever removed the user would get a bare
    refusal instead of the explanation this lane added."""
    import inspect

    import src.pipeline as pipeline

    src = inspect.getsource(pipeline)
    assert "allow_row_security_denial=True" in src, (
        "the direct-query path no longer opts in; a denied query would refuse "
        "instead of narrating the restriction"
    )
    # And exactly ONE caller opts in -- compound/recipe must stay fail-closed.
    assert src.count("allow_row_security_denial=True") == 1
    import src.exec.recipe as recipe

    assert "allow_row_security_denial" not in inspect.getsource(recipe), (
        "the recipe step must NOT opt out of the chokepoint refusal"
    )


def test_recipe_denial_names_the_permissions_restriction():
    """R3 finding B-3. Subclassing QueryExecutionError made the recipe path
    fail CLOSED (no fabricated zero reaches evaluate_combine) but NOT truthful:
    the reason ladder had no branch for it, so it fell to "recipe_failed" and
    told the user "The recipe could not be executed. Please try again" --
    advising a retry for something that did not fail and never will. Exactly
    the misattribution the compound branch got a dedicated handler to avoid."""
    import inspect
    import re

    import src.pipeline as pipeline

    src_text = inspect.getsource(pipeline)
    handler = src_text.split("except RecipeExecutionError as exc:")[1][:3000]
    assert "RowSecurityDeniedQueryError" in handler, (
        "the recipe reason ladder does not recognise a row-security denial"
    )
    assert "row_security_denied" in handler
    # The denial branch must not be the generic retry advice. Bound the slice
    # at the NEXT branch so the following else: (which legitimately says "try
    # again" for a real failure) is not read as part of it.
    denial_msg = handler.split('elif reason == "row_security_denied":')[1]
    denial_msg = re.split(r"\n        (?:elif|else)\b", denial_msg)[0]
    assert "please try again" not in denial_msg.lower(), denial_msg
    assert "permissions restriction" in denial_msg.lower()
    assert "not an absence of data" in denial_msg.lower()


@dataclass(frozen=True)
class _ExecuteQueryCallSite:
    path: str
    line: int
    enclosing: tuple[str, ...]
    keywords: frozenset[str]


class _ExecuteQueryScanError(RuntimeError):
    """The caller guard cannot prove the production call graph."""


_EXECUTE_QUERY_MODULE = ("src", "exec", "query")
_EXECUTE_QUERY_SYMBOL = _EXECUTE_QUERY_MODULE + ("execute_query",)

# The inventory is a release guard, not a best-effort static analyser.  The
# only production imports that are allowed to establish an execute_query
# binding are the two files whose callers are explicitly listed below.  A
# module-qualified import, alias, re-export, container, or getattr indirection
# is rejected even when a human could resolve it: silently accepting a new
# shape would let an un-inventoried caller bypass the persona-scope contract.
_SANCTIONED_EXECUTE_QUERY_FILES = frozenset({"pipeline.py", "exec/recipe.py"})


def _module_for_path(root: Path, path: Path) -> tuple[str, ...]:
    relative = path.relative_to(root).with_suffix("")
    parts = relative.parts
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ("src",) + parts


def _resolve_relative_import(
    current_module: tuple[str, ...], node: ast.ImportFrom,
) -> tuple[str, ...] | None:
    if node.level == 0:
        if not node.module:
            return None
        return tuple(node.module.split("."))
    package = list(current_module[:-1])
    if node.level - 1 > len(package):
        return None
    package = package[: len(package) - (node.level - 1)]
    if node.module:
        package.extend(node.module.split("."))
    return tuple(package)


def _raise_scan(path: Path, node: ast.AST, detail: str) -> None:
    raise _ExecuteQueryScanError(
        f"{detail} in {path}:{getattr(node, 'lineno', '?')}"
    )


def _check_execute_query_imports(
    tree: ast.Module,
    current_module: tuple[str, ...],
    *,
    path: Path,
) -> None:
    """Reject every import shape except the two direct production imports."""
    relative_path = path.as_posix()
    direct_allowed = relative_path in _SANCTIONED_EXECUTE_QUERY_FILES
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported = tuple(alias.name.split("."))
                if imported == _EXECUTE_QUERY_MODULE:
                    _raise_scan(
                        path,
                        node,
                        "module-qualified execute_query import is not allowed",
                    )
                if imported[: len(_EXECUTE_QUERY_MODULE)] == _EXECUTE_QUERY_MODULE:
                    _raise_scan(
                        path,
                        node,
                        "ambiguous execute_query module import is not allowed",
                    )
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        imported_module = _resolve_relative_import(current_module, node)
        if imported_module is None:
            _raise_scan(path, node, "unresolvable import")
        for alias in node.names:
            if alias.name == "*":
                _raise_scan(path, node, "star import cannot resolve execute_query")
            if alias.name == "execute_query":
                if (
                    imported_module != _EXECUTE_QUERY_MODULE
                    or not direct_allowed
                    or alias.asname is not None
                ):
                    _raise_scan(
                        path,
                        node,
                        "execute_query aliases/re-exports are not allowed",
                    )
                continue
            if imported_module == ("src", "exec") and alias.name == "query":
                _raise_scan(
                    path,
                    node,
                    "module-qualified execute_query import is not allowed",
                )


class _ExecuteQueryCallVisitor(ast.NodeVisitor):
    def __init__(
        self,
        *,
        path: Path,
        direct_allowed: bool,
    ) -> None:
        self.path = path
        self.direct_allowed = direct_allowed
        self.enclosing: list[str] = []
        self.calls: list[_ExecuteQueryCallSite] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.enclosing.append(node.name)
        self.generic_visit(node)
        self.enclosing.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.enclosing.append(node.name)
        self.generic_visit(node)
        self.enclosing.pop()

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id == "execute_query":
            if not self.direct_allowed:
                _raise_scan(self.path, node, "unauthorized execute_query caller")
            self.calls.append(
                _ExecuteQueryCallSite(
                    path=self.path.as_posix(),
                    line=node.lineno,
                    enclosing=tuple(self.enclosing),
                    keywords=frozenset(
                        keyword.arg if keyword.arg is not None else "**"
                        for keyword in node.keywords
                    ),
                )
            )
            # Do not visit the direct function name again, but inspect every
            # argument: passing the callable onward is indirection, not a
            # caller that the inventory can certify.
            for argument in node.args:
                self.visit(argument)
            for keyword in node.keywords:
                self.visit(keyword.value)
            return
        if isinstance(node.func, ast.Attribute) and node.func.attr == "execute_query":
            _raise_scan(
                self.path,
                node,
                "qualified execute_query call is not allowed",
            )
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "execute_query"
        ):
            _raise_scan(
                self.path,
                node,
                "getattr execute_query indirection is not allowed",
            )
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == "execute_query":
            _raise_scan(
                self.path,
                node,
                "execute_query callable indirection is not allowed",
            )

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == "execute_query":
            _raise_scan(
                self.path,
                node,
                "qualified execute_query reference is not allowed",
            )
        self.generic_visit(node)


def _scan_execute_query_callers(root: Path) -> list[_ExecuteQueryCallSite]:
    """Find every production execute_query call through static AST binding.

    This intentionally scans the complete agent-service ``src`` tree instead
    of a hand-picked pair of modules. UTF-8-sig accommodates Windows-authored
    files, and parse/import/binding ambiguity raises rather than certifying a
    potentially missed caller.
    """
    calls: list[_ExecuteQueryCallSite] = []
    for path in sorted(root.rglob("*.py")):
        try:
            source = path.read_text(encoding="utf-8-sig")
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError, UnicodeError) as exc:
            raise _ExecuteQueryScanError(
                f"cannot parse production file {path}: {exc}"
            ) from exc
        current_module = _module_for_path(root, path)
        relative_path = path.relative_to(root)
        _check_execute_query_imports(
            tree,
            current_module,
            path=relative_path,
        )
        visitor = _ExecuteQueryCallVisitor(
            path=relative_path,
            direct_allowed=relative_path.as_posix()
            in _SANCTIONED_EXECUTE_QUERY_FILES,
        )
        visitor.visit(tree)
        calls.extend(visitor.calls)
    return calls


_SANCTIONED_EXECUTE_QUERY_CALLERS = {
    ("pipeline.py", "run_turn"): "direct",
    ("pipeline.py", "_run_compound_query_branch"): "compound",
    ("exec/recipe.py", "execute_recipe"): "recipe",
}


def test_every_execute_query_call_site_is_accounted_for():
    """The repository-wide AST inventory protects the shared chokepoint.

    Every current caller must carry the local ProjectPersona-derived
    ``persona_scopes`` keyword and must not carry the project-persona UUID as
    query-router's model ``persona_id``. The direct caller is the only one that
    may render a row-security denial; compound and recipe remain fail-closed.
    """
    root = Path(__file__).resolve().parents[1] / "src"
    calls = _scan_execute_query_callers(root)
    actual = {(site.path, site.enclosing[-1]) for site in calls}
    assert actual == set(_SANCTIONED_EXECUTE_QUERY_CALLERS), (
        "execute_query caller inventory drifted: "
        f"expected {sorted(_SANCTIONED_EXECUTE_QUERY_CALLERS)}, found {sorted(actual)}"
    )
    assert len(calls) == len(_SANCTIONED_EXECUTE_QUERY_CALLERS)
    for site in calls:
        assert "persona_scopes" in site.keywords, (
            f"{site.path}:{site.line} must pass persona_scopes explicitly"
        )
        assert "persona_id" not in site.keywords, (
            f"{site.path}:{site.line} must not pass a project-persona UUID to "
            "query-router's model persona_id"
        )
    direct = next(
        site for site in calls
        if (site.path, site.enclosing[-1]) == ("pipeline.py", "run_turn")
    )
    assert "allow_row_security_denial" in direct.keywords
    for site in calls:
        if site is not direct:
            assert "allow_row_security_denial" not in site.keywords


def _write_scan_fixture(root: Path, source: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "fixture.py").write_text(source, encoding="utf-8-sig")


def test_execute_query_ast_scan_fails_closed_on_alias_indirection(tmp_path):
    """Mutation fixtures must never create an un-inventoried caller.

    The old resolver followed only bindings visible in one file.  These
    mutations are deliberately shaped like the cross-module re-export,
    wrapper alias, callable-container, and getattr patterns that can hide a
    new production caller from a local AST walk.  The guard rejects each
    shape before it can be certified.
    """
    cases = {
        "cross_file_reexport": {
            "bridge.py": "from src.exec.query import execute_query as forwarded\n",
            "caller.py": (
                "from bridge import forwarded as run_query\n"
                "async def caller(db, call, token, scopes):\n"
                "    return await run_query(db, call, token, persona_scopes=scopes)\n"
            ),
        },
        "wrapper_alias": {
            "fixture.py": (
                "from src.exec.query import execute_query as run_query\n"
                "async def wrapper(db, call, token, scopes):\n"
                "    return await run_query(db, call, token, persona_scopes=scopes)\n"
            ),
        },
        "callable_container": {
            # ``pipeline.py`` is the only fixture path allowed to import the
            # callable directly; the visitor must still reject storing it.
            "pipeline.py": (
                "from src.exec.query import execute_query\n"
                "handlers = [execute_query]\n"
            ),
        },
        "getattr": {
            "fixture.py": (
                "query_module = object()\n"
                "handler = getattr(query_module, 'execute_query')\n"
            ),
        },
    }
    for name, files in cases.items():
        root = tmp_path / name
        for relative, source in files.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source, encoding="utf-8-sig")
        with pytest.raises(_ExecuteQueryScanError, match="execute_query"):
            _scan_execute_query_callers(root)


def test_execute_query_ast_scan_fails_closed_on_parse_or_import_ambiguity(tmp_path):
    _write_scan_fixture(
        tmp_path / "bad_parse",
        "async def broken(:\n    pass\n",
    )
    with pytest.raises(_ExecuteQueryScanError, match="cannot parse"):
        _scan_execute_query_callers(tmp_path / "bad_parse")

    _write_scan_fixture(
        tmp_path / "bad_import",
        """
from src.exec.query import *

async def hidden_call(db, call, token, scopes):
    return await execute_query(db, call, token, persona_scopes=scopes)
""",
    )
    with pytest.raises(_ExecuteQueryScanError, match="star import"):
        _scan_execute_query_callers(tmp_path / "bad_import")
