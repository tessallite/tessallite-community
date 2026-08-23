import { useQuery } from "@tanstack/react-query";
import {
  Alert,
  Box,
  CircularProgress,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Typography,
} from "@mui/material";
import ScheduleIcon from "@mui/icons-material/Schedule";
import { schedulerApiClient } from "../../api/client";
import { useT } from "../../i18n";
import { ui } from "../../theme/tokens";

function formatTime(value: string | null, unavailable: string): string {
  return value ? new Date(value).toLocaleString() : unavailable;
}

function formatStatus(
  value: string | null,
  t: ReturnType<typeof useT>,
): string {
  if (!value) return t("scheduler.jobLedgerNeverRun");
  const keyByStatus: Record<string, string> = {
    started: "scheduler.jobLedgerStatusStarted",
    success: "scheduler.jobLedgerStatusSuccess",
    partial: "scheduler.jobLedgerStatusPartial",
    error: "scheduler.jobLedgerStatusError",
    misfire: "scheduler.jobLedgerStatusMisfire",
    busy: "scheduler.jobLedgerStatusBusy",
  };
  return keyByStatus[value] ? t(keyByStatus[value]) : value;
}

function formatTrigger(
  value: string | null,
  t: ReturnType<typeof useT>,
  unavailable: string,
): string {
  if (!value) return unavailable;
  const keyByTrigger: Record<string, string> = {
    scheduled: "scheduler.jobLedgerTriggerScheduled",
    manual: "scheduler.jobLedgerTriggerManual",
  };
  return keyByTrigger[value] ? t(keyByTrigger[value]) : value;
}

/**
 * Bug-9419 / F-012-10: render the durable scheduler execution ledger already
 * exposed by GET /scheduler/jobs. This is read-only operational evidence, not
 * another job-management surface.
 */
export function SchedulerJobLedger() {
  const t = useT();
  const jobsQuery = useQuery({
    queryKey: ["scheduler-jobs"],
    queryFn: schedulerApiClient.jobs,
    refetchInterval: 30_000,
  });
  const jobs = jobsQuery.data?.jobs ?? [];
  const unavailable = t("scheduler.jobLedgerUnavailable");

  return (
    <Box>
      <Box display="flex" alignItems="center" gap={1} mb={1}>
        <ScheduleIcon fontSize="small" sx={{ color: ui.green }} />
        <Typography variant="subtitle2" fontWeight={700}>
          {t("scheduler.jobLedgerTitle")}
        </Typography>
      </Box>
      <Typography variant="caption" color="text.secondary" display="block" mb={1.5}>
        {t("scheduler.jobLedgerDescription")}
      </Typography>

      {jobsQuery.isLoading && <CircularProgress size={20} />}
      {jobsQuery.isError && (
        <Alert severity="error" variant="outlined">
          {t("scheduler.jobLedgerLoadFailed")}
        </Alert>
      )}
      {!jobsQuery.isLoading && !jobsQuery.isError && (
        <TableContainer sx={{ maxHeight: 360 }}>
          <Table
            size="small"
            stickyHeader
            aria-label={t("scheduler.jobLedgerTableLabel")}
          >
            <TableHead>
              <TableRow>
                <TableCell>{t("scheduler.jobLedgerJob")}</TableCell>
                <TableCell>{t("scheduler.jobLedgerStatus")}</TableCell>
                <TableCell>{t("scheduler.jobLedgerLastFinished")}</TableCell>
                <TableCell>{t("scheduler.jobLedgerError")}</TableCell>
                <TableCell>{t("scheduler.jobLedgerNextRun")}</TableCell>
                <TableCell>{t("scheduler.jobLedgerTrigger")}</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {jobs.length === 0 && (
                <TableRow>
                  <TableCell colSpan={6}>{t("scheduler.jobLedgerEmpty")}</TableCell>
                </TableRow>
              )}
              {jobs.map((job) => (
                <TableRow key={job.job_id}>
                  <TableCell>{job.name}</TableCell>
                  <TableCell>{formatStatus(job.last_status, t)}</TableCell>
                  <TableCell>{formatTime(job.last_finished_at, unavailable)}</TableCell>
                  <TableCell>{job.last_error ?? unavailable}</TableCell>
                  <TableCell>{formatTime(job.next_run_time, unavailable)}</TableCell>
                  <TableCell>
                    {formatTrigger(job.last_trigger_source, t, unavailable)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}
    </Box>
  );
}
