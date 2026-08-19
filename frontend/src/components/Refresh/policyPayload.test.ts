import { describe, expect, it } from "vitest";
import { buildAggregateRefreshPolicy } from "./policyPayload";

describe("aggregate refresh policy payload", () => {
  it("makes selecting incremental the explicit append-only declaration", () => {
    expect(buildAggregateRefreshPolicy({
      method: "incremental",
      cron: "0 2 * * *",
      incrementalColumn: "business_date",
      lookbackDays: 7,
      fullRebuildIntervalDays: 30,
    })).toEqual({
      refresh_mode: "incremental",
      cron_expression: "0 2 * * *",
      incremental_column: "business_date",
      incremental_lookback: 7,
      incremental_append_only: true,
      full_rebuild_interval_days: 30,
      is_enabled: true,
    });
  });

  it("clears incremental authority when full rebuild is selected", () => {
    expect(buildAggregateRefreshPolicy({
      method: "full",
      cron: null,
      incrementalColumn: "business_date",
      lookbackDays: 7,
      fullRebuildIntervalDays: 30,
    })).toEqual({
      refresh_mode: "scheduled",
      cron_expression: null,
      incremental_column: null,
      incremental_lookback: null,
      incremental_append_only: false,
      full_rebuild_interval_days: null,
      is_enabled: false,
    });
  });
});
