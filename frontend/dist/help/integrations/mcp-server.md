# MCP Server (Model Context Protocol)

The Tessallite MCP server lets AI assistants — Claude Desktop, Cursor, Claude Code, and any MCP-compatible client — query your semantic layer directly. The assistant can list models, inspect structure, run governed queries, view history, and trigger refreshes without custom REST integration.

## What the MCP server can do

| Tool | Description |
|---|---|
| `list_models` | List projects, or list models within a project |
| `describe_model` | Full model structure: dimensions, measures, hierarchies, joins |
| `execute_query` | Run SQL through the semantic layer with aggregate routing |
| `get_query_history` | Recent queries with execution times and routing decisions |
| `list_aggregates` | Materialised aggregates with status, grain, and size |
| `trigger_refresh` | Trigger an aggregate refresh for a specific model (needs a tenant-admin account) |

All queries go through Tessallite's query router. Aggregate routing, persona-based security, and row-level filters are enforced exactly as they are for BI tool queries.

`trigger_refresh` is an administrative action: the scheduler requires a tenant-admin credential. If you configure the MCP server with a viewer or modeler account, every other tool works but `trigger_refresh` returns a per-aggregate "FAILED (403 ...)" line. Use a tenant-admin (or a service account granted the tenant-admin role) if you need refresh.

## Prerequisites

- Python 3.10 or later
- A running Tessallite instance (local or cloud)
- A Tessallite user account with access to the target project

## Installation

From the monorepo:

```bash
cd tessallite/mcp-server
pip install .
```

Standalone:

```bash
pip install tessallite-mcp
```

## How it runs (transport)

The MCP server speaks the MCP **stdio** transport — the assistant launches it as a child process and talks to it over standard input/output. It is not an HTTP service and has no SSE/HTTP listener, so it cannot be deployed as a long-running Cloud Run service; run it locally next to the assistant (the `command`/`args` config below) or via `docker run` with stdio attached.

**Reaching the backend.** `TESSALLITE_URL` must point at the model service and `TESSALLITE_QUERY_URL` at the query router. In a standard local Docker deployment the backend container ports may not be published on the host (nginx fronts them), so `http://localhost:8001` will not resolve — point the variables at the nginx-proxied address your browser uses (for example the app domain, with the service path the reverse proxy exposes) or publish the backend ports for local development. The Endpoints panel in the app pre-fills these values for your deployment.

## Configuration

The server reads configuration from environment variables:

| Variable | Required | Default | Description |
|---|---|---|---|
| `TESSALLITE_URL` | Yes | `http://localhost:8001` | Model service base URL |
| `TESSALLITE_QUERY_URL` | No | `http://localhost:8002` | Query router base URL |
| `TESSALLITE_SCHEDULER_URL` | No | `http://localhost:8004` | Scheduler base URL |
| `TESSALLITE_TENANT_ID` | Yes | — | Tenant slug to authenticate against |
| `TESSALLITE_EMAIL` | Yes | — | User email for JWT login |
| `TESSALLITE_PASSWORD` | Yes | — | User password |
| `TESSALLITE_ROW_LIMIT` | No | `1000` | Maximum rows returned per query |

## Claude Desktop setup

Add to your Claude Desktop configuration (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "tessallite": {
      "command": "tessallite-mcp",
      "env": {
        "TESSALLITE_URL": "http://localhost:8001",
        "TESSALLITE_QUERY_URL": "http://localhost:8002",
        "TESSALLITE_SCHEDULER_URL": "http://localhost:8004",
        "TESSALLITE_TENANT_ID": "acme-demo",
        "TESSALLITE_EMAIL": "admin@acme-demo.com",
        "TESSALLITE_PASSWORD": "acme-demo"
      }
    }
  }
}
```

If installed from source:

```json
{
  "mcpServers": {
    "tessallite": {
      "command": "python",
      "args": ["-m", "src.server"],
      "cwd": "/path/to/tessallite/mcp-server",
      "env": {
        "TESSALLITE_URL": "http://localhost:8001",
        "TESSALLITE_TENANT_ID": "acme-demo",
        "TESSALLITE_EMAIL": "admin@acme-demo.com",
        "TESSALLITE_PASSWORD": "acme-demo"
      }
    }
  }
}
```

## Cursor setup

Add to `.cursor/mcp.json` in your project or global settings:

```json
{
  "mcpServers": {
    "tessallite": {
      "command": "tessallite-mcp",
      "env": {
        "TESSALLITE_URL": "http://localhost:8001",
        "TESSALLITE_TENANT_ID": "acme-demo",
        "TESSALLITE_EMAIL": "admin@acme-demo.com",
        "TESSALLITE_PASSWORD": "acme-demo"
      }
    }
  }
}
```

## Claude Code setup

Add to `.claude/settings.json`:

```json
{
  "mcpServers": {
    "tessallite": {
      "command": "tessallite-mcp",
      "env": {
        "TESSALLITE_URL": "http://localhost:8001",
        "TESSALLITE_TENANT_ID": "acme-demo",
        "TESSALLITE_EMAIL": "admin@acme-demo.com",
        "TESSALLITE_PASSWORD": "acme-demo"
      }
    }
  }
}
```

## Docker

```bash
docker build -t tessallite-mcp tessallite/mcp-server/

docker run --rm \
  -e TESSALLITE_URL=http://host.docker.internal:8001 \
  -e TESSALLITE_QUERY_URL=http://host.docker.internal:8002 \
  -e TESSALLITE_SCHEDULER_URL=http://host.docker.internal:8004 \
  -e TESSALLITE_TENANT_ID=acme-demo \
  -e TESSALLITE_EMAIL=admin@acme-demo.com \
  -e TESSALLITE_PASSWORD=acme-demo \
  tessallite-mcp
```

## Security

- The MCP server authenticates using standard JWT login credentials.
- All queries respect persona-based security and row-level filters.
- Query results are capped at `TESSALLITE_ROW_LIMIT` rows.
- All queries are logged in the query log via the query router.

Use a service account with appropriate permissions rather than a personal admin account in production.

## Troubleshooting

**Connection refused** — Verify the Tessallite services are running and the URLs are correct. For Docker, use `host.docker.internal` to reach the host network.

**Authentication failed** — Check tenant ID, email, and password. The MCP server uses the same login endpoint as the web UI.

**Empty model list** — The authenticated user may not have access to any projects. Check user access bindings in the admin panel.

**Queries return no results** — The user's persona may restrict visible measures or apply row-level filters that exclude all data. Try with a user that has broader access.
