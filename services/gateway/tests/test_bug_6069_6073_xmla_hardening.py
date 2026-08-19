"""Gateway XMLA hardening regressions.

Bug-6069: a client-supplied SessionId echoed into the SOAP response header must
          be XML-escaped so it cannot inject markup / break out of the
          attribute and forge SOAP structure.
Bug-6073: named-set inlining must not corrupt MDX when a set name collides with
          an MDX keyword/function (e.g. a set literally named ``Order`` would
          otherwise clobber the ``Order(...)`` function calls in the query).
"""
from __future__ import annotations

from src.dax.xmla_server import _soap_response, _inline_named_sets


# ---------------------------------------------------------------------------
# Bug-6069 — SessionId is XML-escaped in the response envelope
# ---------------------------------------------------------------------------

class TestBug6069_SessionIdEscaping:
    def test_benign_session_id_echoed(self):
        resp = _soap_response("<X/>", session_id="abc-123")
        body = resp.body.decode("utf-8")
        assert 'SessionId="abc-123"' in body

    def test_injection_session_id_is_escaped(self):
        # A crafted SessionId attempting to close the attribute + inject a
        # forged header must be neutralised — no raw quote/angle-bracket may
        # survive inside the attribute value.
        malicious = '"/><tns:Session SessionId="forged'
        resp = _soap_response("<X/>", session_id=malicious)
        body = resp.body.decode("utf-8")
        # The raw injection string must not appear verbatim.
        assert malicious not in body
        # Its dangerous characters are escaped.
        assert "&quot;" in body
        assert "&lt;tns:Session" in body
        # Exactly one real Session header element is emitted (no forged one).
        assert body.count("<tns:Session ") == 1

    def test_ampersand_and_angle_escaped(self):
        resp = _soap_response("<X/>", session_id="a&b<c>")
        body = resp.body.decode("utf-8")
        assert "a&amp;b&lt;c&gt;" in body

    def test_no_session_header_when_empty(self):
        resp = _soap_response("<X/>", session_id="")
        body = resp.body.decode("utf-8")
        assert "tns:Session" not in body


# ---------------------------------------------------------------------------
# Bug-6073 — named-set inlining guards against keyword/function collisions
# ---------------------------------------------------------------------------

class TestBug6073_KeywordCollisionGuard:
    def test_bare_keyword_named_set_does_not_clobber_function_call(self):
        # A set literally named "Order" must NOT rewrite the Order(...) call in
        # the query. Only an explicit [Order] bracket reference is inlined.
        mdx = "SELECT Order([Product].Members, [Measures].[Sales], BDESC) ON ROWS FROM [Model]"
        sets = [{"name": "Order", "expression": "TopCount([Product].Members, 5)"}]
        result = _inline_named_sets(mdx, sets)
        # The function call is preserved intact.
        assert "Order([Product].Members" in result
        # The set expression was NOT injected over the function call.
        assert "TopCount([Product].Members, 5)" not in result

    def test_bracketed_keyword_named_set_is_still_inlined(self):
        # The explicit, unambiguous bracket form is safe and must still inline.
        mdx = "SELECT {[Filter]} ON ROWS FROM [Model]"
        sets = [{"name": "Filter", "expression": "TopCount([Customer].Members, 3)"}]
        result = _inline_named_sets(mdx, sets)
        assert "TopCount([Customer].Members, 3)" in result
        assert "[Filter]" not in result

    def test_keyword_guard_is_case_insensitive(self):
        mdx = "SELECT filter([Product].Members, [Measures].[Sales] > 0) ON ROWS FROM [Model]"
        sets = [{"name": "FILTER", "expression": "EXPR_X"}]
        result = _inline_named_sets(mdx, sets)
        assert "filter([Product].Members" in result
        assert "EXPR_X" not in result

    def test_order_direction_flag_named_set_not_clobbered(self):
        # Codex R1: order-direction flags (BDESC/DESC/ASC/BASC) are bare tokens
        # inside Order(...). A set named after one must not rewrite the flag.
        mdx = "SELECT Order([Product].Members, [Measures].[Sales], BDESC) ON ROWS FROM [Model]"
        for flag in ("BDESC", "DESC", "ASC", "BASC"):
            sets = [{"name": flag, "expression": "EXPR_INJECTED"}]
            result = _inline_named_sets(mdx.replace("BDESC", flag), sets)
            assert f"{flag}) ON ROWS" in result
            assert "EXPR_INJECTED" not in result

    def test_descendants_flag_named_set_not_clobbered(self):
        mdx = "SELECT Descendants([Time].[2025], [Time].[Month], SELF_AND_BEFORE) ON ROWS FROM [Model]"
        sets = [{"name": "SELF_AND_BEFORE", "expression": "EXPR_INJECTED"}]
        result = _inline_named_sets(mdx, sets)
        assert "SELF_AND_BEFORE" in result
        assert "EXPR_INJECTED" not in result

    def test_non_keyword_bare_set_still_inlined(self):
        # A normal (non-keyword) bare set name must keep working via the bare
        # replacement path — the guard must not over-block.
        mdx = "SELECT TopProducts ON ROWS FROM [Model]"
        sets = [{"name": "TopProducts", "expression": "TopCount([Product].Members, 5)"}]
        result = _inline_named_sets(mdx, sets)
        assert "TopCount([Product].Members, 5)" in result
