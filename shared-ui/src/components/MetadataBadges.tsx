import { Box, Chip, Tooltip } from "@mui/material";
import type { TurnResponse } from "../types/turn";
import { useChatContext } from "../providers/ChatProvider";

interface MetadataBadgesProps {
  turn: TurnResponse;
}

export function MetadataBadges({ turn }: MetadataBadgesProps) {
  const { t } = useChatContext();
  const hasRowCount =
    typeof turn.query_result_rows === "number" && turn.query_result_rows >= 0;
  const hasAny =
    turn.latency_ms || turn.route || turn.judge_verdict || turn.status || hasRowCount;
  if (!hasAny) return null;

  const statusColors: Record<
    string,
    "success" | "warning" | "error" | "info" | "default"
  > = {
    ok: "success",
    refused: "warning",
    clarify: "info",
    error: "error",
    judge_blocked: "error",
  };

  return (
    <Box
      sx={{
        display: "flex",
        gap: 0.5,
        mb: 1,
        flexWrap: "wrap",
        alignItems: "center",
      }}
    >
      {turn.status && (
        <Chip
          label={
            turn.status === "ok"
              ? t("badges.completed")
              : turn.status === "refused"
                ? t("badges.blocked")
                : turn.status
          }
          size="small"
          color={statusColors[turn.status] || "default"}
          variant="outlined"
          sx={{ fontSize: 11, height: 20 }}
        />
      )}
      {turn.latency_ms != null && (
        <Chip
          label={t("badges.seconds", {
            seconds: (turn.latency_ms / 1000).toFixed(1),
          })}
          size="small"
          variant="outlined"
          sx={{ fontSize: 11, height: 20 }}
        />
      )}
      {turn.route && (
        <Tooltip title={t("badges.route", { route: turn.route })}>
          <Chip
            label={turn.route}
            size="small"
            variant="outlined"
            sx={{ fontSize: 11, height: 20 }}
          />
        </Tooltip>
      )}
      {turn.judge_verdict && (
        <Chip
          label={t("badges.judge", { verdict: turn.judge_verdict })}
          size="small"
          variant="outlined"
          sx={{ fontSize: 11, height: 20 }}
        />
      )}
      {hasRowCount && (
        <Chip
          label={t("badges.rows", {
            count: (turn.query_result_rows as number).toLocaleString(),
          })}
          size="small"
          variant="outlined"
          sx={{ fontSize: 11, height: 20 }}
        />
      )}
    </Box>
  );
}
