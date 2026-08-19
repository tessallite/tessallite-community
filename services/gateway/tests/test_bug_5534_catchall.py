"""Bug-5534 (remaining Power BI Desktop half) — malformed XMLA URL diagnosis.

Power BI Desktop's data-source dialog can nest the whole gateway URL after the
base path. The live GCP gateway log (2026-06-25, registry Bug-5534) recorded
the request line verbatim:

    POST /api/v1/xmlahttps%3A//sql.cloud.tessallite.io%3A8080/api/v1/xmla

Note there is no separator between ``xmla`` and ``https``. No concrete XMLA
route matches that, so FastAPI answered a bare 404 "Not Found" and the user
saw a connection failure with nothing pointing at the URL they typed.

The gateway now recognises the shape and answers with an actionable 404 naming
the correct URL forms. It deliberately does NOT dispatch the request to a
tenant guessed out of the appended text — the recorded Bug-5534 decision (see
``_TENANT_SLUG_RE`` and ``test_bug_5534_xmla_fixes.py``) is that a malformed
XMLA URL must be surfaced, because the earlier half-working behaviour hid the
typo from the BI client instead of getting it corrected.

Test escape: the URL-append shape only appears with a real Power BI client, and
no test ever asserted what an unmatched ``/xmla...`` path returns — so the bare
404 went unnoticed through several rounds of live diagnosis. These tests pin
the routing and the response at the app boundary (real route table, so a route
that never matches is caught). Tier: T1.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from src.dax.xmla_server import router as xmla_router


@pytest.fixture(scope="module")
def client() -> TestClient:
    """App with the SAME double include as ``src.main`` (prefixed + bare).

    The auth middleware is intentionally absent: these cases are decided by the
    route table before any handler work, and mounting the real middleware would
    turn a routing assertion into an auth assertion.
    """
    app = FastAPI()
    app.include_router(xmla_router, prefix="/api/v1")
    app.include_router(xmla_router)
    return TestClient(app)


# The exact live-logged path, and the variants seen alongside it.
_MALFORMED_PATHS = [
    "/api/v1/xmlahttps%3A//sql.cloud.tessallite.io%3A8080/api/v1/xmla",
    "/api/v1/xmlahttps://sql.cloud.tessallite.io:8080/api/v1/xmla",
    "/api/v1/xmla/https://sql.cloud.tessallite.io:8080/api/v1/xmla/acme-demo",
    "/xmlahttps://sql.cloud.tessallite.io:8080/api/v1/xmla",
]


@pytest.mark.parametrize("path", _MALFORMED_PATHS)
def test_appended_url_gets_an_actionable_404(client, path):
    resp = client.post(path, content=b"<Envelope/>")
    assert resp.status_code == 404
    text = resp.text
    # It must name the shape, both correct forms, and the concrete example —
    # a bare "Not Found" is what left this half of Bug-5534 undiagnosable.
    assert "A full URL appears to have been appended" in text
    assert "/api/v1/xmla/<workspace>" in text
    assert "/api/v1/xmla/acme-demo" in text


def test_appended_url_is_never_dispatched_to_a_guessed_tenant(client, monkeypatch):
    """The trailing ``/acme-demo`` must NOT be mined out and served."""
    from src.dax import xmla_server

    async def _boom(*_a, **_kw):
        raise AssertionError(
            "a malformed XMLA URL must not reach the request handler"
        )

    monkeypatch.setattr(xmla_server, "_handle_xmla_request", _boom)
    resp = client.post(
        "/api/v1/xmla/https://sql.cloud.tessallite.io:8080/api/v1/xmla/acme-demo",
        content=b"<Envelope/>",
    )
    assert resp.status_code == 404


def test_embedded_credentials_are_never_echoed_or_logged(client, caplog):
    """A pasted connection URL can carry ``user:password@host``. Neither the
    404 body nor the warning log may repeat it."""
    import logging

    with caplog.at_level(logging.WARNING):
        resp = client.post(
            "/api/v1/xmlahttps://admin%40acme-demo.com:hunter2@sql.example.com:8080/api/v1/xmla",
            content=b"<Envelope/>",
        )
    assert resp.status_code == 404
    assert "hunter2" not in resp.text
    assert "[REDACTED]@" in resp.text
    assert "hunter2" not in caplog.text


@pytest.mark.parametrize(
    "path",
    [
        # Encoded ``/`` inside the password: after decoding, the userinfo holds
        # ``s3cr3t/tail@``, so a regex applied to the DECODED path stops at the
        # ``/`` before it ever reaches the ``@`` and redacts nothing.
        "/api/v1/xmlahttps://alice:s3cr3t%2Ftail@sql.example.com/api/v1/xmla",
        # Encoded whitespace fails the same way.
        "/api/v1/xmlahttps://alice:s3cr3t%20tail@sql.example.com/api/v1/xmla",
        # ...and with the scheme separators encoded too, which is the exact
        # shape the live Power BI Desktop log recorded (``https%3A//``).
        "/api/v1/xmlahttps%3A//alice:s3cr3t%2Ftail@sql.example.com/api/v1/xmla",
    ],
)
def test_encoded_userinfo_delimiter_is_never_echoed_review_f_cr_03(
    client, caplog, path,
):
    """sol review F-CR-03: the credential must be redacted from the RAW encoded
    path, before decoding can hide the authority boundary."""
    import logging

    with caplog.at_level(logging.WARNING):
        resp = client.post(path, content=b"<Envelope/>")
    assert resp.status_code == 404
    assert "s3cr3t" not in resp.text
    assert "s3cr3t" not in caplog.text
    assert "[REDACTED]@" in resp.text
    # ...and the host is still named, so the 404 stays diagnosable.
    assert "sql.example.com" in resp.text


def test_credential_is_redacted_without_a_raw_path_review_f_cr_03():
    """``raw_path`` is optional in the ASGI spec. On a server that omits it the
    handler only has the DECODED path, where the authority boundary is already
    destroyed — the blunt scheme-to-last-``@`` net must still remove it."""
    from src.dax.xmla_server import _safe_malformed_path

    safe = _safe_malformed_path(
        "https://alice:s3cr3t/tail@sql.example.com/api/v1/xmla"
    )
    assert safe == "https://[REDACTED]@sql.example.com/api/v1/xmla"


def test_a_password_ending_in_the_redaction_marker_still_redacts_review_f_cr_03():
    """The residual check compares the WHOLE userinfo, not a suffix — a
    password crafted to end in ``[REDACTED]`` must not smuggle the rest of the
    credential through the decoded-path fallback."""
    from src.dax.xmla_server import _safe_malformed_path

    safe = _safe_malformed_path(
        "https://alice:s3cr3t/[REDACTED]@sql.example.com/api/v1/xmla"
    )
    assert "s3cr3t" not in safe
    assert safe == "https://[REDACTED]@sql.example.com/api/v1/xmla"


def test_a_pasted_blob_is_capped_before_being_echoed(client):
    resp = client.post(
        "/api/v1/xmlahttps://sql.example.com/" + "z" * 4000, content=b"<Envelope/>"
    )
    assert resp.status_code == 404
    # The echoed path is truncated, so the body cannot be inflated by input.
    assert "z" * 4000 not in resp.text
    assert len(resp.text) < 600


def test_garbage_path_without_an_appended_url_still_explains_itself(client):
    resp = client.post("/api/v1/xmla/acme-demo/extra/segments", content=b"<Envelope/>")
    assert resp.status_code == 404
    # No false "appended URL" claim when the path carries no scheme.
    assert "A full URL appears to have been appended" not in resp.text
    assert "Malformed XMLA endpoint path" in resp.text


# ---------------------------------------------------------------------------
# The catch-all must not shadow any real XMLA route. A GET probe short-circuits
# in each concrete handler before any network work, so a 200 proves the request
# reached that handler and not the catch-all.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/xmla",
        "/api/v1/xmla/",
        "/api/v1/xmla/acme-demo",
        "/api/v1/xmla/msmdpump.dll",
        "/api/v1/msmdpump.dll",
        "/xmla",
        "/xmla/acme-demo",
    ],
)
def test_real_xmla_routes_are_not_shadowed(client, path):
    resp = client.get(path)
    assert resp.status_code == 200, f"{path} was swallowed by the catch-all"


def test_malformed_tenant_slug_keeps_its_own_404_message(client):
    """``/api/v1/xmla/acme-demo,`` is a single segment, so the tenant route —
    not the catch-all — still owns it (Bug-5534 trailing-comma diagnosis)."""
    resp = client.get("/api/v1/xmla/acme-demo,")
    assert resp.status_code == 404
    assert "Unknown workspace path segment" in resp.text
    assert "Malformed XMLA endpoint path" not in resp.text
