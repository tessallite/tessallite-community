from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from shared.importers.atscale_mapper import map_atscale_to_tessallite
from shared.importers.atscale_parser import SmlParseResult
from shared.importers.cube_mapper import map_cube_to_tessallite
from shared.importers.cube_parser import parse_cube_project
from shared.importers.dbt_mapper import map_dbt_to_tessallite
from shared.importers.dbt_parser import parse_dbt_project
from shared.importers.import_warnings import (
    KNOWN_WARNING_CODES,
    WARNING_CATALOG,
    ImportWarningResponse,
    make_import_warning,
    normalize_import_warnings,
)
from shared.model_snapshot.project_rehydrator import _strip_blocked_import_host

TESSALLITE_ROOT = Path(__file__).resolve().parents[3]
PRODUCER_FILES = (
    TESSALLITE_ROOT / "shared/importers/dbt_parser.py",
    TESSALLITE_ROOT / "shared/importers/dbt_mapper.py",
    TESSALLITE_ROOT / "shared/importers/cube_parser.py",
    TESSALLITE_ROOT / "shared/importers/cube_mapper.py",
    TESSALLITE_ROOT / "shared/importers/atscale_parser.py",
    TESSALLITE_ROOT / "shared/importers/atscale_mapper.py",
    TESSALLITE_ROOT / "shared/model_snapshot/project_rehydrator.py",
    TESSALLITE_ROOT / "services/model-service/src/api/dbt_import.py",
    TESSALLITE_ROOT / "services/model-service/src/api/cube_import.py",
    TESSALLITE_ROOT / "services/model-service/src/api/atscale_import.py",
)


@pytest.mark.parametrize("source", ["dbt", "cube", "atscale", "project_import"])
def test_bug8141_legacy_warning_keeps_diagnostic_only_for_fallback(source: str):
    detail = f"{source} definition needs review"
    warning = normalize_import_warnings([detail], source=source)[0]

    assert warning.code == "legacy.warning"
    assert warning.source == source
    assert warning.severity == "warning"
    assert warning.params == {}
    assert warning.detail == detail
    ImportWarningResponse.model_validate(warning.model_dump())


def test_bug8141_known_warning_is_catalog_derived_and_typed():
    warning = make_import_warning(
        code="dbt.connection_placeholder",
        params={"connection": "dbt import"},
        detail="Configure the imported connection before querying.",
    )

    assert warning.code == "dbt.connection_placeholder"
    assert warning.source == "dbt"
    assert warning.severity == "error"
    assert warning.action == "configure"
    assert warning.element == "dbt import"
    assert warning.params == {"connection": "dbt import"}
    assert normalize_import_warnings([warning], source="dbt") == [warning]


def test_bug8141_known_warning_rejects_param_or_metadata_drift():
    with pytest.raises(ValueError, match="params mismatch"):
        make_import_warning(
            code="dbt.connection_placeholder",
            params={},
            detail="missing typed parameter",
        )
    with pytest.raises(ValueError, match="inconsistent severity"):
        normalize_import_warnings([{
            "code": "dbt.connection_placeholder",
            "severity": "info",
            "source": "dbt",
            "action": "configure",
            "element": "dbt import",
            "params": {"connection": "dbt import"},
            "detail": "diagnostic",
        }], source="dbt")


@pytest.mark.asyncio
async def test_bug8141_real_producers_emit_specific_typed_records():
    dbt = map_dbt_to_tessallite(parse_dbt_project({
        "models.yml": "semantic_models:\n  - name: orders\n    model: ref('orders')\n",
        "bad.yml": "{{invalid yaml",
    }))
    cube = map_cube_to_tessallite(parse_cube_project({
        "cubes.yml": "cubes:\n  - name: orders\n    sql_table: orders\n",
        "bad.yml": "{{invalid yaml",
    }))
    atscale = map_atscale_to_tessallite(SmlParseResult())
    project_warnings: list[ImportWarningResponse] = []
    await _strip_blocked_import_host(
        "postgresql",
        json.dumps({"host": "127.0.0.1"}).encode(),
        {},
        "project import",
        project_warnings,
    )

    cases = (
        (dbt.warnings, "dbt.file_skipped", {"file": "bad.yml", "reason": "invalid_yaml"}),
        (cube.warnings, "cube.file_skipped", {"file": "bad.yml", "reason": "invalid_yaml"}),
        (atscale.warnings, "atscale.connection_type_unresolved", {}),
        (
            project_warnings,
            "project_import.connection_host_removed",
            {"connection": "project import"},
        ),
    )
    for warnings, code, params in cases:
        warning = next(item for item in warnings if item.code == code)
        assert not isinstance(warning, str)
        assert warning.params == params
        assert "detail" not in warning.params
        assert warning.severity in {"info", "warning", "error"}
        assert warning.action
        ImportWarningResponse.model_validate(warning.model_dump())


def test_bug8141_catalog_and_english_locale_are_fail_closed_and_exact():
    locale_path = TESSALLITE_ROOT / "frontend/src/i18n/en/importExport.json"
    locale = json.loads(locale_path.read_text(encoding="utf-8"))
    locale_codes = {
        key.removeprefix("importWarning.")
        for key in locale
        if key.startswith("importWarning.")
        and key not in {
            "importWarning.diagnosticFallback",
            "importWarning.legacy",
        }
    }

    assert locale_codes == KNOWN_WARNING_CODES
    for code in KNOWN_WARNING_CODES:
        assert "{{detail}}" not in locale[f"importWarning.{code}"]
        spec = WARNING_CATALOG[code]
        assert spec.source == code.split(".", 1)[0]


def _is_warning_receiver(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Name) and node.id == "warnings"
    ) or (
        isinstance(node, ast.Attribute) and node.attr == "warnings"
    )


def _is_allowed_warning_initializer(node: ast.expr | None) -> bool:
    """A warning receiver may only start empty or as a dataclass field.

    Anything else is a raw transfer around the typed validator: a straight
    ``warnings = list(parsed.warnings)`` (or any other initializer) can carry
    strings or unknown codes into the output without ``extend_known_import_warnings``
    ever seeing them.
    """
    if isinstance(node, ast.List) and not node.elts:
        return True
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "field"
    )


def test_bug8141_all_current_warning_append_producers_use_catalog_constructor():
    """Fail closed across append/extend shapes and the complete code domain."""
    violations: list[str] = []
    discovered = 0
    producer_codes: set[str] = set()
    for path in PRODUCER_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "make_import_warning"
            ):
                code_value = next(
                    (kw.value for kw in node.keywords if kw.arg == "code"), None
                )
                if not (
                    isinstance(code_value, ast.Constant)
                    and isinstance(code_value.value, str)
                ):
                    violations.append(f"{path.name}:{node.lineno}:dynamic-code")
                else:
                    producer_codes.add(code_value.value)
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"append", "extend"}
                and _is_warning_receiver(node.func.value)
            ):
                continue
            discovered += 1
            if node.func.attr == "extend":
                violations.append(f"{path.name}:{node.lineno}:raw-extend")
                continue
            first = node.args[0] if node.args else None
            if not (
                isinstance(first, ast.Call)
                and isinstance(first.func, ast.Name)
                and first.func.id == "make_import_warning"
            ):
                violations.append(f"{path.name}:{node.lineno}")
            continue

        for node in ast.walk(tree):
            # Bug-8141 r3: the guard used to see only append/extend call
            # shapes, which is how all three mappers kept a raw
            # ``warnings = list(parsed.warnings)`` seam open.  Assignment and
            # augmented-assignment to a warning receiver are equally raw
            # transfers and are violations unless the initializer is a fresh
            # empty list or a dataclass field default.
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value:
                targets = (
                    node.targets if isinstance(node, ast.Assign)
                    else [node.target]
                )
                if any(
                    _is_warning_receiver(target) for target in targets
                ) and not _is_allowed_warning_initializer(node.value):
                    violations.append(f"{path.name}:{node.lineno}:raw-copy")
            if (
                isinstance(node, ast.AugAssign)
                and _is_warning_receiver(node.target)
            ):
                violations.append(f"{path.name}:{node.lineno}:raw-augassign")

    assert discovered >= 40, "producer discovery unexpectedly narrowed"
    assert producer_codes == KNOWN_WARNING_CODES
    assert violations == []
