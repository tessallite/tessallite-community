---
title: "LookML Emitter"
audience: modeller
area: Integrations
updated: 2026-05-25
---

## What this covers

Tessallite creates a generated LookML adapter project from a deployed semantic
model. Data Studio direct connections do not use this export. Use it only
when a separate Looker instance will execute a Git-backed LookML project.

## Generate the project

In the web application, open **Export** and select **LookML (.zip)**. Choose a
deployed model, enter the connection name configured in the target Looker
instance, and download the generated project.

The same download is available through the authenticated API:

```text
POST /api/v1/projects/{project_id}/models/{model_id}/export/lookml
{"connection": "tessallite_gateway"}
```

For offline generation and drift checking, export a model snapshot and run the
CLI:

```bash
tessallite/scripts/tessallite-lookml-export \
  --snapshot-file exported-model.json \
  --project <project-id-or-slug> \
  --model <model-id-or-slug> \
  --out generated/lookml \
  --connection tessallite_gateway
```

Output contains:

- `views/<table>.view.lkml` for semantic tables.
- `models/<project>.model.lkml` for explores and declared joins.
- `manifest.lkml` containing model identity, deployed version, and drift hash.

Generated views query governed adapter relations named
`public.<model_slug>__<table_alias>`. The gateway exposes these only when
`LOOKER_GATEWAY_ENABLED=true`.

## Check drift

Re-export after a model change and run the same command with `--check`. A
non-zero result means checked-in generated output no longer matches the
Tessallite model snapshot.

## Contract and limits

The emitter produces published dimensions, standard physical measures,
declared single-column primary keys, joins, drills, and supported suggestion
hints. It intentionally does not generate persistent derived tables,
calculated/time-variant measures, arbitrary Liquid/parameter SQL, or composite
primary keys. Hand edits to generated files are outside the generated contract;
regenerate and review them as owned drift.

## Related

- [Optional Looker-hosted workflow](looker-studio-via-looker-guide.md)
- [Looker Studio Direct Connection](looker-studio-connection-guide.md)

---

<- [Looker Cloud Core - Not Required](looker-cloud-core-connection-guide.md) | [Home](../index.md) | [BI Tool Compatibility Matrix ->](bi-compatibility.md)
