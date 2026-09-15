"""Regression tests for the XMLA Catalog/tenant lockout boundary.

Every Excel XMLA connection sends the model slug as the SOAP ``Catalog``
(e.g. ``modely``). ``auth_basic`` first attempts a TENANT-scoped login using
that Catalog as the tenant id — a login that can never succeed, because the
Catalog is a model slug, not a tenant (the F-002-15 note in
``_is_unknown_tenant`` documents this). model-service recorded a lockout
failure for that bogus tenant scope on every attempt, so five Excel
connections locked scope ``modely`` for 15 minutes.

Bug-9799 keeps the boundaries separate:
  * model-service resolves the tenant BEFORE recording a failure, so a
    model-slug Catalog never poisons a lockout scope;
  * the gateway falls through for an unknown tenant or credential rejection,
    preserving model-slug Catalog discovery;
  * a generic 429 remains a real lockout and must not be reclassified as an
    unknown tenant by the gateway.
"""

import httpx
import pytest

from src.dax.auth_basic import _is_credential_failure, _is_unknown_tenant


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://model-service:8001/api/v1/auth/login")
    response = httpx.Response(code, request=request)
    return httpx.HTTPStatusError(f"{code}", request=request, response=response)


@pytest.mark.parametrize("code", [404, 422])
def test_unknown_tenant_codes_fall_through_to_discovery(code):
    """Only explicit no-such-tenant responses mean the Catalog is unknown."""
    assert _is_unknown_tenant(_status_error(code)) is True


@pytest.mark.parametrize("code", [400, 403, 500, 502, 503])
def test_operational_errors_still_do_not_fall_through(code):
    """The fix must not widen the carve-out into genuine operational faults —
    those must still surface rather than being masked as 'not a tenant'."""
    assert _is_unknown_tenant(_status_error(code)) is False


def test_401_is_a_credential_failure_not_an_unknown_tenant():
    """401 keeps its existing, distinct classification: a real credential
    rejection (which also feeds the failed-login throttle), not a missing
    tenant. Both paths fall through to discovery, but only 401 counts as a
    credential guess."""
    exc = _status_error(401)
    assert _is_credential_failure(exc) is True
    assert _is_unknown_tenant(exc) is False


def test_429_is_not_counted_as_a_credential_guess():
    """A lockout response is neither an unknown tenant nor a credential guess."""
    assert _is_unknown_tenant(_status_error(429)) is False
    assert _is_credential_failure(_status_error(429)) is False
