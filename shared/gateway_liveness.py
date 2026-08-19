"""The single proof that the gateway's JDBC accept loop is ALIVE (Bug-8974).

Why this module exists
----------------------
Every JDBC liveness probe in this repo used to open a TCP connection and call
that healthy. A TCP connection proves the socket is BOUND, nothing more: the
kernel completes the three-way handshake into the listen backlog (asyncio's
default is 100) whether or not any process is accepting, and
``asyncio.Server.is_serving()`` stays ``True`` right through
``asyncio/selector_events.py::_accept_connection``'s EMFILE/ENFILE/ENOBUFS/
ENOMEM remove-reader-and-retry loop. So a wedged gateway passed every probe
until the backlog filled -- which is exactly the "healthy while dead" incident
recorded as Bug-8533 consequence (2).

The only thing that can observe the accept loop is an application-layer
exchange. The PostgreSQL v3 ``SSLRequest`` is the smallest one that exists: 8
bytes out, one byte (``S`` or ``N``) back, answered pre-auth, no credentials,
no dependency on model-service. That byte can only be produced by a task the
accept loop dequeued and handed to the real handler.

Liveness vs readiness -- these are different questions, deliberately
--------------------------------------------------------------------
* LIVENESS (this module): the accept loop is alive. Depends on nothing. A
  liveness failure means restarting the gateway would help.
* READINESS (``READINESS_PROBE_SQL`` below, run by callers that hold
  credentials): the whole path works -- auth via model-service, routing, query
  execution. A readiness probe SHOULD fail when model-service is down; a
  liveness probe must NOT, because restarting the gateway does not fix a
  model-service outage. Collapsing the two turns a model-service blip into a
  gateway restart loop.

Deliberately stdlib-only and dependency-free so host-side deploy smoke checks
can import it without the service venv. Do not add imports that pull in
``shared.config`` -- callers pass host/port/timeout in.
"""
from __future__ import annotations

import asyncio
import contextlib
import socket
import ssl
import struct

__all__ = [
    "SSL_REQUEST",
    "READINESS_PROBE_SQL",
    "probe_jdbc_accept_loop",
    "probe_jdbc_accept_loop_async",
]

# PostgreSQL v3 SSLRequest: int32 length (8) + int32 request code (80877103).
SSL_REQUEST = struct.pack("!II", 8, 80877103)

# Answers that prove a task the accept loop dequeued reached the real handler.
# 'S' = TLS accepted, 'N' = TLS denied, 'E' = the first byte of an
# ErrorResponse. 'E' counts: the per-IP admission governor refuses a connection
# with a FATAL ErrorResponse BEFORE reading the startup frame, so a probe that
# arrives while the loopback address is at its concurrency cap gets 'E'. That
# is still the application answering, so treating it as dead would be a false
# NEGATIVE -- and a false negative restarts a working gateway, which is worse
# than the false positive this module exists to remove. The set is enumerated
# rather than "any byte" so a foreign process squatting on the port is not
# mistaken for the gateway.
_SSL_REPLIES = (b"S", b"N", b"E")

# The readiness statement, fixed by the user 2026-08-11. It is what BI clients
# commonly send as their own connection test, so a readiness probe using it
# exercises the same shape real clients use rather than a synthetic one the
# gateway might treat differently. Verified against a live gateway on
# 2026-08-11: returns a single row ``(1,)``. Callers that hold credentials
# execute this over an ordinary JDBC connection; there is no separate readiness
# mechanism and no special-casing of it anywhere in the gateway.
READINESS_PROBE_SQL = "SELECT 1 LIMIT 1"


def probe_jdbc_accept_loop(
    host: str,
    port: int,
    timeout: float = 3.0,
) -> tuple[bool, str]:
    """Return ``(alive, detail)`` for the JDBC accept loop at *host*:*port*.

    Blocking/stdlib. ``alive`` is True only when the server answered the
    ``SSLRequest`` with ``S`` or ``N``. A TCP connection that is accepted by
    the kernel but never dequeued by the application times out here, which is
    the entire point of this function.

    When the answer is ``S`` the TLS handshake is completed before closing.
    Not for the probe's benefit -- liveness is already proven by the byte --
    but so the gateway's handler sees an ordinary client that went away rather
    than a half-open TLS negotiation, which it logs at ERROR.
    """
    sock: socket.socket | None = None
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)
        sock.sendall(SSL_REQUEST)
        reply = sock.recv(1)
        if reply not in _SSL_REPLIES:
            if not reply:
                return False, (
                    f"{host}:{port} accepted the TCP connection but closed it "
                    f"without answering the PostgreSQL SSLRequest"
                )
            return False, (
                f"{host}:{port} answered the PostgreSQL SSLRequest with "
                f"{reply!r}, which is not an 'S'/'N'/'E' reply — something "
                f"other than the Tessallite gateway is on this port"
            )
        if reply == b"S":
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            with contextlib.suppress(Exception):
                sock = context.wrap_socket(sock, server_hostname=host)
        return True, f"{host}:{port} answered the SSLRequest with {reply.decode()!r}"
    except socket.timeout:
        return False, (
            f"{host}:{port} did not answer the PostgreSQL SSLRequest within "
            f"{timeout}s — the port is bound but nothing is accepting "
            f"(the connection is sitting in the kernel listen backlog)"
        )
    except OSError as exc:
        return False, f"{host}:{port} — {exc}"
    finally:
        if sock is not None:
            with contextlib.suppress(Exception):
                sock.close()


async def probe_jdbc_accept_loop_async(
    host: str,
    port: int,
    timeout: float = 3.0,
) -> tuple[bool, str]:
    """Async twin of :func:`probe_jdbc_accept_loop`, for the gateway's own
    ``/health`` handler, which must not block its event loop.

    Runs the blocking implementation in a worker thread rather than
    re-expressing the exchange in asyncio primitives, so there is exactly ONE
    definition of what "the accept loop is alive" means. A second async
    implementation is how the two would silently drift.

    ``timeout`` bounds the exchange twice: inside the socket calls, and again
    around the thread, so a thread wedged in ``recv`` cannot hold ``/health``
    open past the budget.
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(probe_jdbc_accept_loop, host, port, timeout),
            timeout=timeout + 1.0,
        )
    except asyncio.TimeoutError:
        return False, (
            f"{host}:{port} — the JDBC liveness probe itself did not return "
            f"within {timeout + 1.0}s"
        )
