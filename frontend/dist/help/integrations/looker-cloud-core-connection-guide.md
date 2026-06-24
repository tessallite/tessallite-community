---
title: "Looker Cloud Core - Not Required"
audience: modeller
area: Integrations
updated: 2026-05-25
---

## Status

Looker Cloud Core is not required for Tessallite's Looker Studio/Data Studio
direct integration and is not an active delivery or validation target.

Use [Looker Studio Direct Connection](looker-studio-connection-guide.md) for
the supported PostgreSQL connector path. If a customer later supplies a
compatible Looker instance, generated LookML can be evaluated through the
[optional Looker-hosted workflow](looker-studio-via-looker-guide.md).

## Architecture

```
Looker Studio -> PostgreSQL connector
  -> PostgreSQL connection to Tessallite Gateway on port 5433
  -> Tessallite query router
  -> source warehouse
```

This direct path does not consume or execute LookML.

## Related

- [JDBC Connection Guide](jdbc-connection-guide.md)
- [Looker Studio Direct Connection](looker-studio-connection-guide.md)
- [Optional Looker-hosted workflow](looker-studio-via-looker-guide.md)
- [LookML Emitter](lookml-emitter-guide.md)
- [BI Tool Compatibility Matrix](bi-compatibility.md)

---

<- [Looker Studio Direct Connection](looker-studio-connection-guide.md) | [Home](../index.md) | [LookML Emitter ->](lookml-emitter-guide.md)
