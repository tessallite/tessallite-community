"""Bug-9552 — login-lockout and system-audit helpers must tolerate a schema
behind migration 0212 so the login-gated migration path can bootstrap.

The migrate-then-serve deploy gate authenticates via the system-login
endpoint before migrations run. ``assert_not_locked`` / ``system_audit``
touched tables created by migration 0212 with no tolerance for an undefined
table, so every login 500'd and migrations could never run on a schema-behind
system (demonstrated live on GCP 2026-08-23).

Contract under test:
- Lockout helpers fail OPEN (skip, with a session rollback) only for
  undefined-table errors; every other database error still raises.
- ``system_audit`` skips the event when the audit table is absent AND the
  schema is still in the pre-migration bootstrap window, using a probe that
  never touches the caller's transaction; it writes normally when the table
  exists.
- The probe names the table migration 0212 actually creates
  (``tess_system.system_audit_events``), asserted as a full qualified name
  against the ORM and against the migration itself (SOL-R1-A01).
- Bug-9821: outside that bootstrap window the platform plane is FAIL-CLOSED
  like the tenant plane — a missing audit store raises ``AuditWriteError`` so
  the caller's mutation rolls back instead of committing without evidence.
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import ProgrammingError

from shared.audit.logger import AuditWriteError
from shared.audit.system import (
    _AUDIT_TABLE_REVISION,
    _revisions_before_audit_table,
    system_audit,
)
from shared.auth import lockout as lockout_module
from shared.auth.lockout import (
    assert_not_locked,
    record_login_failure,
    record_login_success,
)
from shared.db.models import SystemAuditEvent


class _FakeUndefinedTable:
    """Mimics asyncpg.exceptions.UndefinedTableError (SQLSTATE 42P01)."""

    sqlstate = "42P01"

    def __str__(self) -> str:
        return 'relation "tess_system.login_lockouts" does not exist'


class _FakeProgrammingError(Exception):
    sqlstate = "XX000"

    def __str__(self) -> str:
        return "some other database error"


def _undefined_table_error() -> ProgrammingError:
    return ProgrammingError("SELECT ...", {}, _FakeUndefinedTable())


def _other_programming_error() -> ProgrammingError:
    return ProgrammingError("SELECT ...", {}, _FakeProgrammingError())


def _db_raising(exc: Exception) -> AsyncMock:
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=exc)
    db.rollback = AsyncMock()
    db.commit = AsyncMock()
    return db


@pytest.fixture
def lockout_enabled(monkeypatch):
    """Bug-10060: the account lock defaults to OFF and short-circuits before any
    query. These tests exercise the pre-migration DATABASE path, so they switch
    it on explicitly to reach the code they are guarding."""
    monkeypatch.setattr(lockout_module, "_MAX_FAILURES", 5)


@pytest.mark.asyncio
async def test_bug9552_assert_not_locked_fails_open_on_missing_table(lockout_enabled):
    db = _db_raising(_undefined_table_error())
    await assert_not_locked(db, "__system__", "admin@example.com")
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_bug9552_record_login_failure_fails_open_on_missing_table():
    db = _db_raising(_undefined_table_error())
    await record_login_failure(db, "__system__", "admin@example.com")
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_bug9552_record_login_success_fails_open_on_missing_table():
    db = _db_raising(_undefined_table_error())
    await record_login_success(db, "__system__", "admin@example.com")
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_bug9552_lockout_helpers_still_raise_on_other_errors(lockout_enabled):
    for helper in (
        assert_not_locked,
        record_login_failure,
        record_login_success,
    ):
        db = _db_raising(_other_programming_error())
        with pytest.raises(ProgrammingError):
            await helper(db, "__system__", "admin@example.com")
        db.rollback.assert_not_awaited()


# The one place the expected system-audit table name is written down in this
# module. Every assertion below is checked against the ORM and the migration,
# so this constant cannot drift silently the way the probe literal did.
EXPECTED_AUDIT_TABLE = "tess_system.system_audit_events"


EXPECTED_VERSION_TABLE = "tess_system.alembic_version"


def _probe_call(db):
    """The ``to_regclass`` presence probe call (always the first execute)."""
    return db.execute.await_args_list[0]


def _probed_target(db) -> str:
    """The qualified audit-table name the probe actually asked about.

    Reads the bound parameter when the probe is parameterised and falls back to
    an inlined ``to_regclass('...')`` literal, so the assertion is about the
    TARGET TABLE rather than the SQL spelling.
    """
    call = _probe_call(db)
    if len(call.args) > 1 and isinstance(call.args[1], dict) and call.args[1]:
        params = call.args[1]
        if "audit_table" in params:
            return str(params["audit_table"])
        return str(next(iter(params.values())))
    match = re.search(r"to_regclass\(\s*'([^']+)'\s*\)", str(call.args[0]))
    return match.group(1) if match else ""


def _audit_db(audit_table, version_table=None, stamps=()):
    """A session whose schema reports the given audit/version-table presence.

    ``audit_table`` / ``version_table`` are the ``to_regclass`` results (a
    qualified name when the relation exists, ``None`` when it does not).
    ``stamps`` are the ``alembic_version`` rows read only when the audit table
    is absent and the stamp table exists.
    """
    db = AsyncMock()

    probe_row = MagicMock()
    probe_row.__getitem__ = lambda _self, index: (audit_table, version_table)[index]
    probe_result = MagicMock()
    probe_result.first.return_value = probe_row
    # Retained so a probe implementation that reads a single scalar still sees
    # the audit table's own presence rather than a bare Mock.
    probe_result.scalar.return_value = audit_table

    stamp_result = MagicMock()
    stamp_result.scalars.return_value.all.return_value = list(stamps)

    async def _execute(statement, *args, **kwargs):
        if "to_regclass" in str(statement):
            return probe_result
        return stamp_result

    db.execute = AsyncMock(side_effect=_execute)
    db.add = MagicMock()
    db.flush = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_bug9552_system_audit_skips_event_when_table_missing():
    # No stamp table at all: the first-deploy bootstrap window.
    db = _audit_db(None, None)
    event = await system_audit(db, action="a", severity="warn")
    assert event is None
    db.add.assert_not_called()
    db.flush.assert_not_awaited()
    # The probe must be a read, and must not disturb the caller transaction.
    probe_stmt = str(_probe_call(db).args[0])
    assert "to_regclass" in probe_stmt
    assert _probed_target(db) == EXPECTED_AUDIT_TABLE


@pytest.mark.asyncio
async def test_bug9552_system_audit_writes_when_table_present():
    db = _audit_db(EXPECTED_AUDIT_TABLE, EXPECTED_VERSION_TABLE)
    event = await system_audit(db, action="a", severity="warn")
    assert event is not None
    db.add.assert_called_once()
    db.flush.assert_awaited_once()
    # The normal path costs exactly one round trip: no stamp lookup.
    assert db.execute.await_count == 1


@pytest.mark.asyncio
async def test_bug9552_system_audit_probe_failure_fails_closed():
    # A probe that errors must NOT silently disable auditing: it reports
    # present and the normal insert path runs as before.
    db = AsyncMock()
    db.add = MagicMock()  # plain mock: system_audit never awaits db.add
    db.execute = AsyncMock(side_effect=ProgrammingError("SELECT", {}, Exception("x")))
    db.flush = AsyncMock()
    event = await system_audit(db, action="a", severity="warn")
    assert event is not None
    db.add.assert_called_once()
    db.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_bug9552_token_version_defaults_to_zero_on_missing_table():
    from unittest.mock import patch

    from shared.auth.middleware import get_system_admin_token_version

    async def _gen(db):
        yield db

    db = _db_raising(_undefined_table_error())
    with patch("shared.db.session.get_system_db", lambda: _gen(db)):
        version = await get_system_admin_token_version()
    assert version == 0
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_bug9552_token_version_still_raises_on_other_errors():
    from unittest.mock import patch

    from shared.auth.middleware import get_system_admin_token_version

    async def _gen(db):
        yield db

    db = _db_raising(_other_programming_error())
    with patch("shared.db.session.get_system_db", lambda: _gen(db)):
        with pytest.raises(ProgrammingError):
            await get_system_admin_token_version()
    db.rollback.assert_not_awaited()


# ---------------------------------------------------------------------------
# SOL-R1-A01 — the probe must name the table migration 0212 actually creates.
#
# The probe literal had drifted to the TENANT table name (``audit_events``)
# while 0212 creates ``system_audit_events``. ``to_regclass`` returns NULL for
# a missing table instead of raising, so the drifted probe reported "absent" on
# every schema — including fully migrated ones — and ``system_audit`` silently
# dropped every event for all of its callers (model auth, tenant operations,
# admin operations).
#
# The previous guard asserted ``"audit_events" in probe_stmt``. That substring
# is present in BOTH the wrong and the right name, so it could never fail on
# this defect. These tests compare the full qualified name instead.
# ---------------------------------------------------------------------------


def _migration_0212_source() -> str:
    """Source of the revision the module names as the audit-table creator.

    Globbed by ``_AUDIT_TABLE_REVISION`` rather than a literal so the runtime
    constant and the migration cannot drift apart (Bug-9821 reads the same
    constant to decide when the bootstrap concession has expired).
    """
    versions = Path(__file__).resolve().parents[1] / "db" / "migrations" / "versions"
    matches = sorted(versions.glob(f"{_AUDIT_TABLE_REVISION}_*.py"))
    assert matches, (
        f"migration {_AUDIT_TABLE_REVISION} not found under {versions}"
    )
    return matches[0].read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_bug9552_sol_r1_a01_probe_targets_the_system_audit_table():
    """The pre-migration probe asks about the ORM's own table, exactly."""
    db = _audit_db(None, None)
    await system_audit(db, action="a", severity="warn")
    assert _probed_target(db) == EXPECTED_AUDIT_TABLE
    # Anchored to the ORM so the constant above cannot drift on its own.
    assert EXPECTED_AUDIT_TABLE == (
        f"{SystemAuditEvent.__table__.schema}.{SystemAuditEvent.__table__.name}"
    )
    # The tenant table name must not be what is probed.
    assert _probed_target(db) != "tess_system.audit_events"


def test_bug9552_sol_r1_a01_migration_0212_creates_the_orm_audit_table():
    """Migration contract: 0212 creates the table the ORM and probe expect.

    This is the assertion that ties the three restatements together — ORM
    table, migration ``create_table``, and the runtime probe — so a rename in
    any one of them fails here instead of silently disabling auditing.
    """
    source = _migration_0212_source()
    table = SystemAuditEvent.__table__
    created = set(re.findall(r"op\.create_table\(\s*[\"']([^\"']+)[\"']", source))
    assert table.name in created, (
        f"migration {_AUDIT_TABLE_REVISION} creates {sorted(created)}; "
        f"ORM declares {table.name!r}"
    )
    assert f'_SCHEMA = "{table.schema}"' in source


# ---------------------------------------------------------------------------
# Bug-9821 — the fail-open window is the migration phase and nothing else.
#
# ``system_audit`` was fail-open BY OMISSION: whenever the probe reported the
# audit table absent it returned None and the caller committed its mutation
# with no durable evidence and no error. That tolerance exists only so the
# login-gated migration gate can authenticate and run migrations on a schema
# that predates the revision creating the table (Bug-9552). Past that window
# the platform plane now matches the tenant plane's fail-closed posture
# (``shared.audit.logger.audit_required``).
#
# The window is decided by the schema's own Alembic stamp, read through the
# shipped migration graph — NOT by revision-number arithmetic. The chain has
# two branches whose numbers interleave (0213 is on the tenant branch and is
# NOT "after" the system-branch 0212), so comparing numbers would misclassify.
# ---------------------------------------------------------------------------


def test_bug9821_migration_graph_places_the_audit_table_revision():
    """The bootstrap window is derived from a graph this build can read.

    If the graph cannot be read the runtime fails closed, so this asserts the
    supported deployment shape: the migrations ship, the revision is in them,
    and the set genuinely excludes the creating revision while including a
    real system-branch ancestor of it.
    """
    before = _revisions_before_audit_table()
    assert before is not None, (
        "the shipped migration graph must be readable; a build that cannot "
        "read it fails every platform-plane audit closed"
    )
    assert _AUDIT_TABLE_REVISION not in before
    # 0128 is a real system-branch ancestor (it re-parented revoked_embed_tokens
    # onto the system chain) and is the stamp a 1.1.3-era system schema carries.
    assert "0128" in before
    # 0223 is the TENANT head. Number-wise it looks "after" 0212; by ancestry it
    # is on the other branch and must never be read as pre-migration.
    assert "0223" not in before


@pytest.mark.asyncio
async def test_bug9821_tolerates_missing_store_during_the_migration_phase():
    """A schema stamped before the creating revision is still the gate window."""
    db = _audit_db(None, EXPECTED_VERSION_TABLE, stamps=("0128",))
    event = await system_audit(db, action="auth.system_login_success", severity="warn")
    assert event is None
    db.add.assert_not_called()
    db.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_bug9821_tolerates_missing_store_on_an_unstamped_schema():
    """``alembic_version`` present but empty means the schema is at base."""
    db = _audit_db(None, EXPECTED_VERSION_TABLE, stamps=())
    event = await system_audit(db, action="tenant.create", severity="warn")
    assert event is None
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_bug9821_fails_closed_once_the_migration_phase_has_passed():
    """Stamped AT the creating revision: the store must exist, so refuse."""
    db = _audit_db(None, EXPECTED_VERSION_TABLE, stamps=(_AUDIT_TABLE_REVISION,))
    with pytest.raises(AuditWriteError):
        await system_audit(db, action="tenant.delete", severity="critical")
    db.add.assert_not_called()
    db.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_bug9821_fails_closed_on_a_stamp_this_build_cannot_place():
    """An unplaceable stamp is an unsupported shape, not a bootstrap licence."""
    db = _audit_db(None, EXPECTED_VERSION_TABLE, stamps=("not-a-revision",))
    with pytest.raises(AuditWriteError):
        await system_audit(db, action="license.install", severity="warn")
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_bug9821_fails_closed_when_any_stamp_is_past_the_window():
    """A branched stamp set is pre-migration only if EVERY stamp is."""
    db = _audit_db(
        None, EXPECTED_VERSION_TABLE, stamps=("0128", _AUDIT_TABLE_REVISION)
    )
    with pytest.raises(AuditWriteError):
        await system_audit(db, action="auth.logout", severity="info")
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_bug9821_platform_plane_matches_the_tenant_plane_exception_type():
    """Both planes refuse with the same error, so callers handle one contract."""
    from shared.audit import logger as tenant_audit

    db = _audit_db(None, EXPECTED_VERSION_TABLE, stamps=(_AUDIT_TABLE_REVISION,))
    with pytest.raises(tenant_audit.AuditWriteError):
        await system_audit(db, action="tenant.update", severity="warn")
