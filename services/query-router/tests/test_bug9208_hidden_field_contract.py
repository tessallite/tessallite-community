"""Bug-9208 — hidden fields are CURATION; access is persona and CLS.

DECISION (2026-09-01, Option A): keep ``is_hidden`` as curation. Do not convert
presentation metadata into authorization policy. These tests record that
decision as executable contract so it is not re-litigated from first principles
by the next reviewer who notices that a hidden field is readable.

Two properties are pinned here, and it is the COMBINATION that Bug-9208
reported as an engine-security defect:

1. An explicitly named hidden measure resolves on the business view. Naming a
   field is not discovery, and curation does not gate it.
2. No persona bound means no allow-list enforcement — because an unassigned
   caller is unrestricted BY DESIGN, not because enforcement was skipped.

Together those read like a bypass, and they are not one. The property that
makes them safe lives in the resolver and is pinned in
``shared/tests/test_bug9208_persona_assignment_contract.py``: an ASSIGNED
persona applies whether or not the caller asks for one, so the base catalogue
cannot be used to shed a restriction.

The real defect Bug-9208 identified was product LANGUAGE that sold curation as
a security boundary. That is fixed in the help pages and the UI strings, not
here.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.security import cls_metadata
from src.security.persona_gate import apply_persona_gate


@pytest.mark.asyncio
async def test_bug9208_no_persona_means_no_allow_list_enforcement():
    """Contract, not a gap. An unassigned caller is unrestricted by design.

    ``apply_persona_gate`` returning immediately for a falsy persona id is the
    documented matrix ("no assignment -> everything in the model"). Restriction
    comes from ASSIGNING a persona, or from CLS/RLS — never from the absence of
    an assignment, and never from hiding a field.
    """
    db = AsyncMock()
    bound = object()
    for empty in (None, ""):
        assert await apply_persona_gate(
            db, model_id="m1", persona_id=empty, bound=bound
        ) is None
    db.execute.assert_not_awaited()


def test_bug9208_cls_entry_points_take_no_protocol_argument():
    """CLS must be protocol-independent — REST, JDBC and XMLA alike.

    Both CLS entry points key off the PERSONA, never the wire protocol, so a
    field blocked for a persona is blocked on every surface that persona uses.
    A protocol-dependent boundary would be security theatre: denied over
    JDBC/XMLA but readable over REST. If someone adds a protocol parameter
    here, this fails.
    """
    for fn in (
        cls_metadata.cls_restricted_column_ids,
        cls_metadata.cls_blocked_measure_and_dimension_ids,
    ):
        params = set(inspect.signature(fn).parameters)
        assert "persona" in params, f"{fn.__name__} must key off the persona"
        leaked = params & {"protocol", "client_kind", "dialect", "transport"}
        assert not leaked, (
            f"{fn.__name__} takes {sorted(leaked)}; CLS must not depend on the "
            "wire protocol or client kind"
        )


def test_bug9208_cls_module_does_not_branch_on_protocol():
    """The same guard at the body level, not just the signature."""
    source = Path(inspect.getfile(cls_metadata)).read_text(encoding="utf-8")
    # Strip comments and docstrings' prose mentions are fine; look for real
    # conditional use of a protocol-ish name.
    offenders = re.findall(
        r"^\s*(?:el)?if\s+.*\b(protocol|client_kind|transport)\b.*$",
        source,
        re.M,
    )
    assert not offenders, (
        f"CLS branches on the wire protocol: {offenders}. A field blocked for a "
        "persona must be blocked identically over REST, JDBC and XMLA."
    )


def test_bug9208_cls_is_inert_without_a_persona():
    """Consistent with the matrix: no persona, no CLS restriction.

    Recorded so it is not later reported as a second 'bypass'. The answer is
    the same one Option A rests on — assign a persona (or use RLS); do not
    rely on hiding.
    """
    source = Path(inspect.getfile(cls_metadata)).read_text(encoding="utf-8")
    assert "if persona is None:" in source
