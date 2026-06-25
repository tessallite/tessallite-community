# Data Studio Direct And Optional LookML Export Compatibility Spec

**Status:** current scope, updated 2026-05-25
**Companion plan:** `docs/execution/looker-and-looker-cloud-core-support-plan.md`

The filename is retained for existing references. This document supersedes the
earlier assumption that Looker (Google Cloud core) was required for delivery.

## Scope

Tessallite supports two separate concerns:

1. **Looker Studio/Data Studio direct**: the reporting product connects to the
   Tessallite PostgreSQL wire gateway using its built-in PostgreSQL connector.
   It does not run LookML and does not require any Looker license.
2. **Generated LookML artifact**: Tessallite can export `.lkml` files for a
   customer that already operates a compatible Looker instance. The artifact
   is implemented and testable without that instance; executing it is optional
   and deferred until customer access exists.

Looker Cloud Core is not a Tessallite delivery target or validation
prerequisite.

## Semantic Authority

Tessallite models remain canonical. Generated LookML is a one-way adapter:

```text
deployed Tessallite model -> generated LookML ZIP -> customer Git-backed Looker project
```

Hand edits after extraction are customer-owned drift. A re-export includes the
deployed version identifier and deterministic model hash so changed output can
be reviewed.

## Direct Data Studio Path

```text
Looker Studio/Data Studio -> PostgreSQL connector -> gateway :5433
  -> query-router -> source or accelerated route
```

### Contract

| Item | Requirement |
|---|---|
| Relation surface | Normal deployed semantic relations already exposed by the JDBC gateway |
| Authentication | Tessallite user credentials through the gateway |
| TLS | Configure TLS for internet-facing supported deployment validation |
| Query routing | Standard Tessallite router, security and diagnostic path |
| LookML | Not used |
| `LOOKER_GATEWAY_ENABLED` | Not required and must not block a direct connection |

### Evidence Required Before Claiming Live Validation

| ID | Check |
|---|---|
| LS-A-001 | Direct PostgreSQL connection succeeds with redacted evidence |
| LS-A-002 | Expected deployed relations and fields are discoverable |
| LS-A-003 | One-dimension, one-measure report returns correct results |
| LS-A-004 | Date-grain report returns correct buckets |
| LS-A-005 | Invalid credentials fail without returning data |
| LS-A-006 | Aggregate-eligible query preserves results and route evidence |

## LookML Export Artifact

### User Surface

The frontend exposes **Export** -> **LookML (.zip)** for deployed models. The
request supplies the Looker connection name to write into the generated model:

```http
POST /api/v1/projects/{project_id}/models/{model_id}/export/lookml
Content-Type: application/json

{"connection": "tessallite_gateway"}
```

The endpoint emits a ZIP archive with:

- `views/<table>.view.lkml` for exported table views.
- `models/<model>.model.lkml` for explores and declared joins.
- `manifest.lkml` for Tessallite model identity, deployed version and hash.

An offline CLI remains available for snapshot-file export and drift checking:

```bash
tessallite/scripts/tessallite-lookml-export \
  --snapshot-file exported-model.json \
  --project <project-id-or-slug> \
  --model <model-id-or-slug> \
  --out generated/lookml \
  --connection tessallite_gateway
```

### Mapping Contract

| Tessallite metadata | Generated LookML |
|---|---|
| Published physical dimension | `dimension` or time `dimension_group` |
| Declared unique key | `primary_key: yes` |
| Standard physical measure | Supported LookML aggregation |
| Declared joins | Explore joins |
| Supported suggestion metadata | `suggest_dimension` / `suggest_explore` |

The generator deliberately fails or omits unsupported calculated/time-variant
measures, persistent derived tables, Liquid parameter SQL and composite-key
assumptions rather than changing semantics.

## Optional Looker-Hosted Runtime

```text
Looker Studio -> Looker connector -> customer-supplied Looker instance
  -> generated LookML -> gateway generated relations -> query-router
```

This path is deferred. When it is explicitly requested with a compatible
Looker instance:

- Generated relations are enabled using `LOOKER_GATEWAY_ENABLED=true` in a
  controlled validation environment only.
- The Looker PostgreSQL connection uses TLS.
- Generated relations are named
  `public.<model_slug>__<table_alias>` and remain subject to the standard
  router/security contract.
- Unsupported symmetric-aggregate or complex multi-relation window shapes
  return SQLSTATE `0A000` rather than uncertain output.

No documentation or UAT state may claim that Looker renders the generated
artifact until product-derived evidence is captured.

## Non-Goals

- Procuring or validating Looker Cloud Core.
- Bidirectional import/sync of arbitrary LookML projects.
- Persistent Derived Tables hosted on Tessallite.
- Looker Actions.
- A custom Data Studio Community Connector.

## Test Boundary

| Test surface | Environment |
|---|---|
| Emitter, parser and model-hash tests | Local/CI; no Looker license |
| Model-service LookML ZIP endpoint | Local unit tests; no Looker license |
| Frontend export UI build | Local frontend build |
| Gateway catalogue, generated relation, TLS and SQL safety | Service-local tests |
| Direct Data Studio confirmation | Live non-production Data Studio session |
| Optional Looker-hosted rendering | Deferred pending customer-supplied instance |

## References

- Google Data Studio PostgreSQL connector:
  `https://docs.cloud.google.com/data-studio/connect-to-postgresql`
- Google Data Studio Looker connector:
  `https://docs.cloud.google.com/data-studio/connect-to-looker`
- Google LookML introduction:
  `https://docs.cloud.google.com/looker/docs/what-is-lookml`
