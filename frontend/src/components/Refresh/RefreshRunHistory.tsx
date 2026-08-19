import {
  Table,
  TableBody,
  TableCell,
  TableRow,
  Tooltip,
  Typography,
} from "@mui/material";
import { statusColor } from "../../theme/tokens";
import { useT } from "../../i18n";
import { runStatusLabel } from "../../utils/runStatus";

interface Run {
  id: string;
  status: string;
  started_at: string;
  completed_at?: string | null;
  rows_written?: number | null;
  triggered_by?: string;
  error_message?: string | null;
}

interface Props {
  runs: Run[];
  limit?: number;
}

export default function RefreshRunHistory({ runs, limit = 5 }: Props) {
  const t = useT();
  const display = runs.slice(0, limit);

  function friendlyTriggeredBy(value: string | undefined): string {
    if (!value) return "—";
    switch (value) {
      case "scheduler":
      case "scheduled":
        return t("refreshHistory.scheduled");
      case "manual":
      case "manual_test":
        return t("refreshHistory.manual");
      case "api":
        return t("refreshHistory.api");
      case "test":
        return t("refreshHistory.test");
      default:
        return value.charAt(0).toUpperCase() + value.slice(1);
    }
  }

  function friendlyStatus(status: string): string {
    switch (status) {
      case "completed":
        return t("refreshHistory.completed");
      case "failed":
        return t("refreshHistory.failed");
      case "in_progress":
      case "running":
        return t("refreshHistory.running");
      default: {
        // F-559-04: the shared run-status labels cover the statuses this
        // panel's own keys do not (notably "queued"). Anything neither knows
        // keeps the original capitalised passthrough rather than a raw key.
        const shared = runStatusLabel(status, t);
        return shared === status
          ? status.charAt(0).toUpperCase() + status.slice(1)
          : shared;
      }
    }
  }

  if (display.length === 0) {
    return (
      <Typography variant="body2" color="text.secondary">
        {t("refreshHistory.noHistory")}
      </Typography>
    );
  }

  return (
    <Table size="small" data-testid="refresh-run-history">
      <TableBody>
        {display.map((r) => (
          <TableRow key={r.id}>
            <TableCell sx={{ py: 0.25, pl: 0, border: 0 }}>
              <Typography variant="caption" color="text.secondary">
                {new Date(r.started_at).toLocaleString()}
              </Typography>
            </TableCell>
            <TableCell sx={{ py: 0.25, border: 0 }}>
              <Tooltip
                title={r.status === "failed" && r.error_message ? r.error_message : ""}
                placement="top"
              >
                <Typography
                  variant="caption"
                  fontWeight={600}
                  sx={{ color: statusColor(r.status).fg }}
                >
                  {friendlyStatus(r.status)}
                </Typography>
              </Tooltip>
            </TableCell>
            <TableCell sx={{ py: 0.25, border: 0 }} align="right">
              <Typography variant="caption" color="text.secondary">
                {r.rows_written != null
                  ? t("refreshHistory.rowsWritten", { count: r.rows_written.toLocaleString() })
                  : "—"}
              </Typography>
            </TableCell>
            <TableCell sx={{ py: 0.25, pr: 0, border: 0 }} align="right">
              <Typography variant="caption" color="text.secondary">
                {friendlyTriggeredBy(r.triggered_by)}
              </Typography>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
