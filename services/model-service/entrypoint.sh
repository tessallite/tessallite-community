#!/bin/sh
set -eu

# The model-service must not serve a tenant against a stale schema. This gate
# runs for every container start, including direct Compose refreshes that do
# not pass through an installation wrapper. A tenant that cannot be migrated
# is logged and rejected by the shared session boundary; healthy tenants remain
# available. The serving process rechecks the authoritative row and revision,
# so no startup handoff file or clear operation is needed after repair.
python -m src.startup_migrations
exec "$@"
