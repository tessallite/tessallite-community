"""Canonical ``QueryLog.client_kind`` domain (Bug-8070 / Bug-7451).

``client_kind`` answers "which surface produced this query" — the difference
between a BI client on the JDBC wire, an Excel plugin cell, a drill-through, an
agent turn, and a scheduled KPI evaluation. Operators read it to separate
workloads in top-user, latency and route analyses, so a workload that does not
declare its origin is not merely unlabelled: it is silently counted as BI
traffic and distorts every one of those figures.

The value was previously spelled out independently in the query-router request
model and in the model-service log filter. They drifted twice — "headless",
"agent" and "mcp" became writable long before the filter accepted them, so
filtering by those origins returned 422 (Bug-7451), and the KPI bridge shipped
with no origin at all (Bug-8070). One tuple, imported by both, removes the class
of defect rather than its latest instance.

Adding a value means:
  1. add it here;
  2. add the ``diagnostics.clientKindLabel.<value>`` and ``diagnostics.client<Value>``
     keys to ``frontend/src/i18n/.../en.json``;
  3. add the ``MenuItem`` to ``DiagnosticsPanel.tsx``'s client filter;
  4. add it to the finite domain list in ``frontend/src/i18n/i18n.test.ts``.
The frontend cannot import this module, so steps 2-4 are the manual half of the
contract; the i18n parity test is what fails when they are skipped.
"""
from __future__ import annotations

from typing import Literal

# Ordered for display: BI clients, then Tessallite's own surfaces.
QUERY_LOG_CLIENT_KINDS: tuple[str, ...] = (
    "looker_studio",   # gateway, from the JDBC application_name
    "looker_cloud",    # gateway, from the JDBC application_name
    "plugin",          # Excel plugin (query-router /plugin)
    "drill",           # drill-through (query-router /drill)
    "headless",        # headless embedding API (query-router /headless)
    "agent",           # conversational agent (agent-service exec)
    "mcp",             # MCP tool call (SQL over HTTP)
    "kpi",             # KPI evaluation bridge (model-service -> /execute)
)

# Runtime-constructed Literal so FastAPI/Pydantic validate exactly this domain.
# ``Literal[<tuple>]`` expands the tuple to its members at runtime.
ClientKindLiteral = Literal[QUERY_LOG_CLIENT_KINDS]  # type: ignore[valid-type]

# The subset a CALLER may declare on a query-router request body. The gateway
# derives its own labels from the wire, and internal callers pass client_kind
# through the function signature rather than the body, so those are excluded
# from what an external body may assert about itself.
REQUEST_DECLARABLE_CLIENT_KINDS: tuple[str, ...] = (
    "looker_studio",
    "looker_cloud",
    "agent",
    "drill",
    "kpi",
)

RequestClientKindLiteral = Literal[REQUEST_DECLARABLE_CLIENT_KINDS]  # type: ignore[valid-type]
