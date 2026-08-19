"""URL redaction helpers shared by every outbound-webhook dispatcher.

Bug-8350: webhook URLs commonly carry bearer material in userinfo
(``user:pass@host``), query parameters (``?token=...``), or — per the exact
repro that raised this bug — a path segment (``/hooks/<bearer-token>``). Any
record that outlives the endpoint itself — a DLQ row, an audit log entry, a
CSV export, a database backup — must never retain that material. Because the
reported repro puts the secret in the *path*, this module deliberately drops
the path too, keeping only ``scheme://host[:port]`` — a weaker cut (e.g.
keeping the raw path, as model-service's older ``_redact_url_for_audit``
audit-log helper does for a lower-stakes destination) would leave exactly
that case unfixed.
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import quote, urlparse, urlunparse

REDACTED = "<redacted-url>"

# R2 reviewer finding 2 — minimum length for a BARE query string to be treated
# as a redaction candidate on its own. Eight characters is comfortably below
# any real credential (`access_token=...`, `key=<32 chars>`) and comfortably
# above the short `k=v` fragments that collide with ordinary error prose.
_MIN_UNANCHORED_QUERY_LEN = 8

# Bug-8357 — a scrub that only recognises the literal three characters ``://``
# is bypassed by any receiver that echoes the request URL back in an ESCAPED
# rendering, which is the common case rather than the exotic one: a JSON error
# body renders ``https://h/x`` as ``https:\/\/h\/x``, a form-encoded or
# redirect-parameter echo renders it as ``https%3A%2F%2Fh%2Fx``, and an HTML
# error page can render it with character entities. All of those survived the
# old ``https?://\S+`` sweep untouched while the code read as if the text had
# been redacted.
#
# The delimiter is therefore matched as an ALTERNATION of its renderings
# rather than as literal characters. ``re.IGNORECASE`` covers the ``%2f`` /
# ``%2F`` and ``/`` / ``/`` spellings without doubling the
# alternation.
_COLON = r"(?::|%3A|&#58;|&#x3A;|\\u003A)"
_SLASH = r"(?:/|\\/|%2F|&#47;|&#x2F;|\\u002F)"
_URL_PATTERN = re.compile(
    rf"https?{_COLON}{_SLASH}{_SLASH}\S+", re.IGNORECASE
)


def _escaped_renderings(value: str) -> list[str]:
    """Every rendering of *value* this module knows how to recognise.

    Used for the exact-match replacement pass. The generic sweep above already
    catches anything carrying a scheme delimiter; this pass exists for the case
    the sweep cannot see — a receiver echoing only the credential-bearing
    PATH/QUERY of the request, with no scheme at all.
    """
    renderings = {
        value,
        value.replace("/", r"\/"),        # JSON string escape
        value.replace("/", r"\\/"),       # JSON escape nested one level deeper
        quote(value, safe=""),            # fully percent-encoded
        quote(value, safe="").lower(),
        quote(value, safe=":/?#[]@!$&'()*+,;="),  # path/query encoded only
    }
    # Longest first: replacing the raw URL before its percent-encoded form
    # would otherwise leave the encoded copy's tail behind.
    return sorted((r for r in renderings if r), key=len, reverse=True)


def _credential_bearing_suffixes(url: str) -> list[str]:
    """Every scheme-less fragment of *url* that can carry the credential.

    Bug-8350's repro embedded the bearer token in the PATH. A receiver that
    echoes a fragment of the request — no scheme, no host — leaks exactly the
    same material, and no scheme-anchored pattern can see it.

    R1 reviewer F2: an earlier version returned ONE combined ``path?query``
    string, so a receiver echoing just ``/hooks/<token>`` (the query omitted,
    which is the single most likely echo of all — a 404 body naming the route
    it could not find) matched nothing and leaked the token. Each fragment is
    returned separately, longest first so the combined form is replaced before
    its own components are.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return []
    path = parsed.path or ""
    query = parsed.query or ""
    netloc = parsed.netloc or ""
    candidates = [path]
    # R2 reviewer finding 2 — every other candidate SELF-ANCHORS: a path starts
    # with "/", a host+path starts with the hostname. A bare query string
    # anchors on nothing and is applied with str.replace, so it matches
    # mid-word: an endpoint at "?t=1" turned the DLQ message
    # "attempt=1 of 4 failed" into "attemp<redacted-url> of 4 failed", eating
    # the one diagnostic the operator opened the page to read. Over-redaction
    # fails safe, but a scrub that destroys the message is a scrub operators
    # learn to route around. A query shorter than this floor is not carrying a
    # credential worth protecting, and the anchored `path?query` /
    # `host+path?query` forms still cover every echo that includes it.
    if len(query) >= _MIN_UNANCHORED_QUERY_LEN:
        candidates.append(query)
    if path and query:
        candidates.append(f"{path}?{query}")
    # A scheme-stripped echo ("receiver.example/hooks/<token>") is common in
    # proxy and gateway error pages. The host on its own is deliberately NOT a
    # candidate — it is already published as ``target_host`` and redacting it
    # would destroy the operator's only clue about which receiver failed.
    if netloc and path:
        candidates.append(f"{netloc}{path}")
        if query:
            candidates.append(f"{netloc}{path}?{query}")
    # A "/" or a one-character fragment carries nothing and would match half
    # the free text in an ordinary error message.
    kept = {c for c in candidates if len(c.strip("/")) >= 2}
    return sorted(kept, key=len, reverse=True)


def redact_url_for_display(url: str) -> str:
    """Return ``scheme://host[:port]`` only — no userinfo, path, query, or
    fragment. Safe to persist or show to an operator; deliberately does not
    round-trip to a usable URL and deliberately drops the path (webhook
    receivers commonly embed bearer tokens in path segments, not just query
    parameters — see the Bug-8350 repro).

    Bug-8349 R2: ``parsed.port`` raises ``ValueError`` for a malformed port
    (e.g. ``https://host:notaport/hook``) — a case that reaches this helper
    whenever a legacy row or a URL that predates ``validate_webhook_url``'s
    port check gets DLQ'd. That must not crash the caller (the whole point
    of this function is to be the *safe* fallback path); return the same
    unparseable-URL placeholder used for the ``urlparse`` failure above.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return "<unparseable-url>"
    netloc = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError:
        return "<unparseable-url>"
    if port:
        netloc = f"{netloc}:{port}"
    return urlunparse((parsed.scheme, netloc, "", "", "", ""))


# Bug-8407 — ``hash_url_secure`` / ``verify_url_hash`` and the
# ``agent_webhook_dlq.target_url_hash`` column they fed are GONE (migration
# 0188). Bug-8350 R2 MED-1 had already replaced the original unsalted SHA-256
# with bcrypt, which closed the offline-brute-force half of the finding, but
# the other half stood: NOTHING in the codebase ever read the column. It was a
# persisted, per-row derivative of a secret-bearing URL that bought the product
# nothing, cost a ~0.3s bcrypt call on every DLQ write, and could only ever be
# a liability in a stolen backup. The correct answer to "a hash of a secret
# with no consumer" is not a slower hash, it is not storing it. Coarse
# cross-row correlation still works through ``target_host``.


def scrub_url_from_text(text: Optional[str], url: Optional[str] = None) -> Optional[str]:
    """Strip URL(s) out of free text before it is persisted or returned.

    Three passes, in order:

    1. an exact-match replacement of every rendering of *url* this module
       knows (raw, JSON-escaped, percent-encoded) — the common case of an
       HTTP client or a receiver echoing the request URL back;
    2. an exact-match replacement of *url*'s ``path[?query]`` suffix, for a
       receiver that echoes only the credential-bearing tail with no scheme
       (Bug-8350's own repro put the bearer token in the path, so the tail
       alone is a full leak and no scheme-anchored pattern can see it);
    3. a generic sweep for any REMAINING ``http(s)`` URL, whose scheme
       delimiter is matched in escaped renderings too (Bug-8357 — the old
       literal ``https?://`` sweep was bypassed by a JSON-escaped or
       percent-encoded echo, so the defence-in-depth backstop provided false
       assurance to anyone reading the code).

    ``None``/empty input is returned unchanged. Never raises: this runs on the
    persistence path of a DLQ row, and a redaction helper that can throw would
    turn a leak into a lost failure record.

    Known limit, stated rather than papered over: pass 2 needs *url*. Called
    with no *url* (a read-time backstop over a historical row), a receiver echo
    of ONLY the credential-bearing path — ``/hooks/<token>`` with no scheme —
    is indistinguishable from ordinary free text and survives. Both read-time
    backstops therefore pass the endpoint's currently-configured URL rather
    than calling this bare (``agent-service/src/api/webhooks.list_dlq``,
    ``model-service/src/api/webhooks._redact_delivery``). A row whose target
    was a DIFFERENT, since-replaced URL and which was written before the
    Bug-8350 write-time scrub existed is the one residual case; migration 0188
    re-scrubs those rows once with this matcher.
    """
    if not text:
        return text
    scrubbed = text
    if url is not None and not isinstance(url, str):
        # "Never raises" is a hard contract here: this runs on the DLQ
        # persistence path, where an exception converts a leak into a LOST
        # failure record — the worse of the two outcomes. A caller handing us
        # a non-string (an ORM column object, a mock, a UUID) still gets the
        # generic sweep rather than a TypeError out of urllib.quote.
        url = None
    if url:
        for rendering in _escaped_renderings(url):
            scrubbed = scrubbed.replace(rendering, REDACTED)
        for suffix in _credential_bearing_suffixes(url):
            for rendering in _escaped_renderings(suffix):
                scrubbed = scrubbed.replace(rendering, REDACTED)
    return _URL_PATTERN.sub(REDACTED, scrubbed)
