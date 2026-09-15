"""Bug-9133 — every ``tess_system`` table must be created on the SYSTEM branch.

The Alembic chain has two intentional heads, one per branch label (``system``
rooted at 0001, ``tenant`` rooted at 0002). Deployments never run a bare
``alembic upgrade head``; they run ``system@head`` and ``tenant@head``
separately so tenant tables never land in ``tess_system`` and vice versa.

That makes branch placement a silent correctness boundary: a ``tess_system``
table created by a revision that is chained on the TENANT branch is never
created by ``system@head``, so the table simply does not exist on a live system
schema and every read or write against it 500s. Nothing in the migration
itself reports the mistake — 0142/0174 even documented themselves as "system
branch" while being chained on the tenant root.

This has now happened three times (``revoked_embed_tokens`` re-parented by
0128; ``sso_states`` and ``saml_assertion_replay`` re-parented by 0206), which
is why the invariant is asserted here rather than left to review.

Coverage-tool discipline: the revision graph is read through Alembic's own
``ScriptDirectory`` — the parser Alembic uses at runtime — so this check cannot
develop a blind spot for a declaration style a hand-rolled regex would miss
(``revision: str = "0200"`` is exactly such a case). The create-site scan then
FAILS CLOSED: a system table whose creating revision cannot be identified is a
failure, never a silent pass.
"""
from __future__ import annotations

import pathlib
import re

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from shared.db.models import SystemBase

_MIGRATIONS_DIR = (
    pathlib.Path(__file__).resolve().parents[1] / "db" / "migrations"
)
_SYSTEM_ROOT = "0001"
_TENANT_ROOT = "0002"


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    return ScriptDirectory.from_config(cfg)


def _branch_revisions(script: ScriptDirectory) -> dict[str, set[str]]:
    """Map ``system``/``tenant`` to the revisions reachable from that head."""
    branches: dict[str, set[str]] = {}
    for head in script.get_heads():
        chain = {rev.revision for rev in script.iterate_revisions(head, "base")}
        if _SYSTEM_ROOT in chain:
            branches.setdefault("system", set()).update(chain)
        elif _TENANT_ROOT in chain:
            branches.setdefault("tenant", set()).update(chain)
    return branches


def _creating_revisions(script: ScriptDirectory, table: str) -> set[str]:
    """Revisions containing a ``create_table`` for ``table``.

    Handles both the direct form ``op.create_table("name", ...)`` and the
    module-constant form ``_NAME = "name"`` / ``op.create_table(_NAME, ...)``
    used by the system-schema revisions.
    """
    direct = re.compile(r'create_table\(\s*["\']' + re.escape(table) + r'["\']')
    const_def = re.compile(
        r'^(_[A-Z0-9_]+)\s*=\s*["\']' + re.escape(table) + r'["\']', re.M
    )
    found: set[str] = set()
    for revision in script.walk_revisions():
        source = pathlib.Path(revision.path).read_text(
            encoding="utf-8", errors="replace"
        )
        if direct.search(source):
            found.add(revision.revision)
            continue
        for match in const_def.finditer(source):
            if re.search(r"create_table\(\s*" + match.group(1) + r"\b", source):
                found.add(revision.revision)
                break
    return found


def test_bug9133_migration_graph_has_exactly_the_two_documented_heads():
    """A third head, or a collapsed one, breaks MIGRATE_MODE schema isolation."""
    script = _script_directory()
    heads = set(script.get_heads())
    assert len(heads) == 2, f"expected exactly 2 heads, found {sorted(heads)}"
    branches = _branch_revisions(script)
    assert set(branches) == {"system", "tenant"}, (
        f"heads {sorted(heads)} do not resolve to one system and one tenant "
        f"root; resolved {sorted(branches)}"
    )


@pytest.mark.parametrize(
    "table", sorted(t.name for t in SystemBase.metadata.tables.values())
)
def test_bug9133_system_table_is_created_on_the_system_branch(table: str):
    """Each ``tess_system`` ORM table is created by a system-branch revision.

    ``sso_states`` and ``saml_assertion_replay`` failed this before 0206
    re-parented them: both were created only by 0142/0174, which are chained on
    the tenant root, so ``system@head`` created neither and durable SSO state
    could not exist on a migratable deployment.
    """
    script = _script_directory()
    system_revisions = _branch_revisions(script).get("system", set())
    creators = _creating_revisions(script, table)

    # Fail closed: an unattributable system table is a failure, not a pass.
    assert creators, (
        f"no migration creates system table {table!r}; the create-site scan "
        "found nothing, so branch placement cannot be verified"
    )
    assert creators & system_revisions, (
        f"system table {table!r} is created only by {sorted(creators)}, none of "
        "which is on the system branch — `alembic upgrade system@head` would "
        "never create it"
    )
