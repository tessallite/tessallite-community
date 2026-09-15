"""Bug-9893 — a materialised named list is served only where its context applies.

Audit second pass row R6. ``model-service`` ``refresh_named_list`` computes a
dynamic Named List's members under whoever pressed Refresh — frequently a
modeller holding ``bypass_row_security`` — and the stored membership was then
expanded as fixed literals into EVERY persona's query. That is a materialised
artifact served to a persona whose row-security filters were never applied to
it, which SQL generation rule 4 forbids.

The rule these tests pin:

* a list whose membership is AUTHORED (``fixedMembers``) is model content, not
  a projection of source rows, and is always served;
* a caller that row security does not narrow is served the stored membership;
* an RLS-narrowed caller is served the stored membership only when the refresh
  ran under that caller's own persona AND principal without a bypass;
* anything else — including a list refreshed before the context was recorded —
  is inadmissible, so the caller evaluates the list live instead.
"""
from __future__ import annotations

import pytest

from src.params.named_list_resolver import (
    _ResolvedList,
    _extract_lists_from_snapshot,
    expand_named_lists,
    named_lists_referenced,
    refresh_probe_sql,
    stored_membership_admissible,
)
from src.params.resolver import ParameterError


MODELLER_CTX = {
    "principal_id": "modeller@acme-demo.com",
    "persona_id": None,
    "bypass_row_security": True,
    "refreshed_at": "2026-09-06T10:00:00+00:00",
    "probe_sql": 'SELECT DISTINCT "region" FROM "modely"',
}
VIEWER_PERSONA = "11111111-1111-1111-1111-111111111111"
VIEWER_CTX = {
    "principal_id": "viewer@acme-demo.com",
    "persona_id": VIEWER_PERSONA,
    "bypass_row_security": False,
    "refreshed_at": "2026-09-06T10:00:00+00:00",
    "probe_sql": 'SELECT DISTINCT "region" FROM "modely"',
}


def _dynamic_list(refresh_context=None, builder_type: str = "topN"):
    return _ResolvedList(
        name="TopRegions",
        data_type="string",
        members=["EMEA", "APAC", "AMER"],
        list_type="sql_fixed",
        builder_type=builder_type,
        refresh_context=refresh_context,
    )


class TestStoredMembershipAdmissibility:
    def test_bug9893_bypass_refreshed_list_is_not_served_to_an_rls_persona(self):
        admissible, reason = stored_membership_admissible(
            _dynamic_list(MODELLER_CTX),
            persona_id=VIEWER_PERSONA,
            principal_identity="viewer@acme-demo.com",
            row_security_active=True,
        )
        assert admissible is False
        assert reason == "refreshed_with_row_security_bypassed"

    def test_bug9893_stored_membership_still_serves_a_caller_without_row_security(self):
        admissible, reason = stored_membership_admissible(
            _dynamic_list(MODELLER_CTX),
            persona_id=VIEWER_PERSONA,
            principal_identity="viewer@acme-demo.com",
            row_security_active=False,
        )
        assert admissible is True
        assert reason == "no_row_security_for_caller"

    def test_bug9893_refresh_under_the_callers_own_context_is_served(self):
        admissible, reason = stored_membership_admissible(
            _dynamic_list(VIEWER_CTX),
            persona_id=VIEWER_PERSONA,
            principal_identity="viewer@acme-demo.com",
            row_security_active=True,
        )
        assert admissible is True
        assert reason == "refreshed_under_this_caller_context"

    def test_bug9893_another_principal_in_the_same_persona_is_not_served(self):
        admissible, _reason = stored_membership_admissible(
            _dynamic_list(VIEWER_CTX),
            persona_id=VIEWER_PERSONA,
            principal_identity="someone.else@acme-demo.com",
            row_security_active=True,
        )
        assert admissible is False

    def test_bug9893_unrecorded_context_fails_closed(self):
        admissible, reason = stored_membership_admissible(
            _dynamic_list(None),
            persona_id=VIEWER_PERSONA,
            principal_identity="viewer@acme-demo.com",
            row_security_active=True,
        )
        assert admissible is False
        assert reason == "refresh_context_unknown"

    def test_bug9893_authored_fixed_members_are_always_served(self):
        admissible, reason = stored_membership_admissible(
            _dynamic_list(None, builder_type="fixedMembers"),
            persona_id=VIEWER_PERSONA,
            principal_identity="viewer@acme-demo.com",
            row_security_active=True,
        )
        assert admissible is True
        assert reason == "static_membership"


class TestRefreshContextTravelsInTheSnapshot:
    def test_bug9893_snapshot_carries_the_refresh_context(self):
        lists = _extract_lists_from_snapshot({
            "named_sets": [{
                "name": "TopRegions",
                "list_type": "sql_fixed",
                "builder_definition": {
                    "type": "topN",
                    "data_type": "string",
                    "members": ["EMEA"],
                    "refresh_context": MODELLER_CTX,
                },
            }],
        })
        nlist = lists["@topregions"]
        assert nlist.refresh_context == MODELLER_CTX
        assert refresh_probe_sql(nlist) == MODELLER_CTX["probe_sql"]

    def test_bug9893_missing_refresh_context_reads_as_none(self):
        lists = _extract_lists_from_snapshot({
            "named_sets": [{
                "name": "TopRegions",
                "list_type": "sql_fixed",
                "builder_definition": {
                    "type": "topN", "data_type": "string", "members": ["EMEA"],
                },
            }],
        })
        assert lists["@topregions"].refresh_context is None
        assert refresh_probe_sql(lists["@topregions"]) is None


class TestLiveMembershipReplacesTheStoredOne:
    def _lists(self):
        nlist = _dynamic_list(MODELLER_CTX)
        return {"@topregions": nlist}

    def test_bug9893_named_lists_referenced_finds_the_placeholder(self):
        found = named_lists_referenced(
            'SELECT "region" FROM "modely" WHERE "region" IN (@TopRegions)',
            self._lists(),
        )
        assert [nl.name for nl in found] == ["TopRegions"]

    def test_bug9893_override_is_expanded_instead_of_the_stored_members(self):
        sql = 'SELECT "region" FROM "modely" WHERE "region" IN (@TopRegions)'
        out, audit = expand_named_lists(
            sql, self._lists(),
            member_overrides={"@topregions": ["EMEA"]},
        )
        assert "'EMEA'" in out
        assert "APAC" not in out
        assert audit == ["TopRegions(1 members)"]

    def test_bug9893_stored_members_are_expanded_when_no_override_is_given(self):
        sql = 'SELECT "region" FROM "modely" WHERE "region" IN (@TopRegions)'
        out, _audit = expand_named_lists(sql, self._lists())
        assert "'APAC'" in out

    def test_bug9893_an_empty_live_membership_is_refused_as_a_permissions_result(self):
        sql = 'SELECT "region" FROM "modely" WHERE "region" IN (@TopRegions)'
        with pytest.raises(ParameterError) as exc:
            expand_named_lists(
                sql, self._lists(), member_overrides={"@topregions": []},
            )
        message = str(exc.value)
        assert "row-level security" in message
        # It must NOT tell the analyst to press Refresh: nothing is broken.
        assert "Refresh" not in message
