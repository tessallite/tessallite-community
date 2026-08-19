import type { RefreshPolicyCreate } from "../../api/types";
import type { RebuildMethod } from "./RebuildMethodPicker";

interface PolicyPayloadInput {
  method: RebuildMethod;
  cron: string | null | undefined;
  incrementalColumn: string;
  lookbackDays: number;
  fullRebuildIntervalDays: number | null;
}

/** Canonical producer for both aggregate refresh-policy authoring surfaces. */
export function buildAggregateRefreshPolicy({
  method,
  cron,
  incrementalColumn,
  lookbackDays,
  fullRebuildIntervalDays,
}: PolicyPayloadInput): RefreshPolicyCreate {
  const incremental = method === "incremental";
  return {
    refresh_mode: incremental ? "incremental" : "scheduled",
    cron_expression: cron ?? null,
    incremental_column: incremental ? incrementalColumn || null : null,
    incremental_lookback: incremental ? lookbackDays : null,
    incremental_append_only: incremental,
    full_rebuild_interval_days: incremental ? fullRebuildIntervalDays : null,
    is_enabled: Boolean(cron),
  };
}
