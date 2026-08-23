"""Product defaults for a NEWLY CREATED model.

One definition per default, imported by every producer of a new model — the ORM
column default, the create schema, and each importer/mapper that constructs a
model from a foreign format. A default spelled once in six places is how the ORM
and the importers came to disagree.
"""
from __future__ import annotations

#: Whether a NEW model materialises every model measure into each aggregate.
#:
#: Bug-9409 (user decision F-102-26, Choice A, 2026-08-17): FALSE.
#:
#: Two individually correct features composed into a machine that never hits.
#: With the flag on, the optimizer materialised every measure at a grain; the
#: query-router's row-population proof (Bug-8664) then REFUSED to serve that
#: artifact for any query whose own plan would not join the relations those extra
#: measures dragged in. `modely` carried ~50 aggregates and a 24-hour hit rate of
#: zero. The producer-side narrowing (F-102-01/Bug-9410) reduces the over-build,
#: but the simple product path is to materialise what the workload asks for and
#: let the flywheel add the rest — so opting IN to all-measure aggregates is an
#: explicit choice, not the silent default.
#:
#: This changes the default for models created FROM NOW ON only. Every existing
#: model keeps its persisted value: the migration alters the column DEFAULT and
#: rewrites no rows.
DEFAULT_INCLUDE_ALL_MEASURES = False
