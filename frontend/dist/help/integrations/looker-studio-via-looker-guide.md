---
title: "Optional Looker-hosted LookML Workflow"
audience: modeller
area: Integrations
updated: 2026-05-25
---

## What this covers

This optional path connects Looker Studio to a Looker project generated from a
Tessallite model. Choose it only when you already operate a compatible Looker
instance and need LookML modelling hints while Tessallite remains canonical.

```
Looker Studio -> Looker connector -> Looker instance
  -> generated LookML -> TLS PostgreSQL gateway :5433 -> Tessallite router
```

This is not required for Data Studio direct reporting. Generated files and the
export workflow are tested internally; execution and Studio rendering remain
deferred because no licensed Looker instance is available for validation.

## Setup

1. Enable `LOOKER_GATEWAY_ENABLED=true` and configure gateway TLS in a
   controlled validation environment.
2. In Tessallite, choose **Export** -> **LookML (.zip)** for a deployed model,
   or use the [LookML Emitter](lookml-emitter-guide.md) CLI.
3. Commit generated files to a private repository accessible to Looker.
4. Configure the Looker PostgreSQL-dialect connection to the Tessallite
   gateway with SSL required.
5. Validate the project in Looker, then connect Studio using its Looker
   connector.

## Rendered LookML surface

| Emitted hint | Intended Studio check during capture |
|---|---|
| `drill_fields` | Table drill action opens correct contributing rows. |
| `always_filter` | Required filter is displayed and reaches the gateway query. |
| `conditionally_filter` | Conditional requirement is displayed in applicable explores. |
| `suggest_dimension` / `suggest_explore` | Filter suggestions render from declared suggestion source. |
| `access_filter` | Scoped results are enforced without exposing restricted values. |

Do not treat this table as confirmed Studio rendering behavior until the
screenshots and captured gateway queries are signed off.

## Limits

- No persistent derived tables against Tessallite.
- No bidirectional import or synchronization of arbitrary edited LookML.
- No Looker Action API integration.
- No certification of any Looker-hosted execution path in version 1 until a
  compatible instance is supplied for testing.
- Unsupported symmetric aggregate window shapes fail with a documented
  unsupported-feature message rather than returning uncertain results.

## Related

- [LookML Emitter](lookml-emitter-guide.md)
- [Looker Studio Direct Connection](looker-studio-connection-guide.md)

---

<- [Looker Studio Direct Connection](looker-studio-connection-guide.md) | [Home](../index.md) | [Looker Cloud Core - Not Required ->](looker-cloud-core-connection-guide.md)
