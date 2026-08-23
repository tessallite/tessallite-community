/**
 * Result-surface data-freshness indicator (Bug-8103 / F-104-03).
 *
 * An analyst must be able to tell how fresh a result is before trusting or
 * sharing it. This renders a consumer-facing freshness chip on the pivot
 * result header:
 *   - Live source route  -> "Current live data".
 *   - Accelerated route (aggregate / pocket) with a known materialisation time
 *     -> "As of <timestamp>", flagged when the serve-time overdue gate marked
 *     it stale.
 *
 * Freshness for accelerated routes comes from a backend-authoritative field on
 * the execute response (`freshness`), owned by the query-router/gateway serve
 * path. Until that field is populated this component degrades gracefully:
 * source routes still show "Current live data" (always true for a live route),
 * and accelerated routes without a timestamp show nothing rather than a false
 * claim. See the H1 scope_request for the backend field contract.
 */
import { Chip, Tooltip } from "@mui/material";
import ScheduleIcon from "@mui/icons-material/ScheduleOutlined";
import BoltIcon from "@mui/icons-material/BoltOutlined";
import WarningAmberIcon from "@mui/icons-material/WarningAmberOutlined";
import { useT } from "../../../../i18n";
import type { ResultFreshness } from "../../../../api/types";

function formatTimestamp(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

export default function FreshnessIndicator({
  routeType,
  freshness,
}: {
  routeType: string;
  freshness?: ResultFreshness | null;
}) {
  const t = useT();

  const isLive = freshness?.is_live ?? routeType === "source";
  if (isLive) {
    return (
      <Tooltip title={t("pivot.freshnessLiveTooltip")} placement="top" arrow>
        <Chip
          size="small"
          variant="outlined"
          color="success"
          icon={<BoltIcon sx={{ fontSize: 14 }} />}
          label={t("pivot.freshnessLive")}
        />
      </Tooltip>
    );
  }

  const refreshedAt = freshness?.last_refreshed_at;
  if (!refreshedAt) {
    // Accelerated route but no known age (backend field not yet populated).
    // Show nothing rather than assert a freshness we cannot substantiate.
    return null;
  }

  const stale = freshness?.is_stale === true;
  const label = t("pivot.freshnessAsOf", { time: formatTimestamp(refreshedAt) });
  return (
    <Tooltip
      title={stale ? t("pivot.freshnessStaleTooltip") : t("pivot.freshnessAsOfTooltip")}
      placement="top"
      arrow
    >
      <Chip
        size="small"
        variant="outlined"
        color={stale ? "warning" : "default"}
        icon={
          stale ? (
            <WarningAmberIcon sx={{ fontSize: 14 }} />
          ) : (
            <ScheduleIcon sx={{ fontSize: 14 }} />
          )
        }
        label={stale ? t("pivot.freshnessStale", { time: formatTimestamp(refreshedAt) }) : label}
      />
    </Tooltip>
  );
}
