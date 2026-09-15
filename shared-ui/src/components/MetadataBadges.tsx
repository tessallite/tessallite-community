import { Box, Chip, Tooltip } from "@mui/material";
import type { TurnResponse } from "../types/turn";
import { useChatContext } from "../providers/ChatProvider";

interface MetadataBadgesProps {
  turn: TurnResponse;
  compact?: boolean;
}

export function MetadataBadges({ turn, compact = false }: MetadataBadgesProps) {
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

  if (compact) {
    const statusLabel =
      turn.status === "ok"
        ? t("badges.completed")
        : turn.status === "refused" || turn.status === "judge_blocked"
          ? t("badges.blocked")
          : turn.status;
    const statusColor =
      turn.status === "ok"
        ? "#2e7d32"
        : turn.status === "error" || turn.status === "judge_blocked"
          ? "#B33A3A"
          : "#A67C00";
    const routeColor =
      turn.route === "aggregate"
        ? { color: "#2e7d32", bgcolor: "#e8f5e9" }
        : turn.route === "pocket"
          ? { color: "#7b1fa2", bgcolor: "#f3e5f5" }
          : turn.route === "source"
            ? { color: "#f57f17", bgcolor: "#fff8e1" }
            : { color: "#616161", bgcolor: "#F5F5F5" };

    return (
      <Box
        sx={{
          display: "flex",
          alignItems: "center",
          gap: 0.75,
          minHeight: 24,
          px: 1,
          py: 0.5,
          borderBottom: 1,
          borderColor: "divider",
          color: "text.secondary",
          fontSize: 10,
          lineHeight: 1.2,
          whiteSpace: "nowrap",
          overflow: "hidden",
        }}
      >
        {turn.status && (
          <Box component="span" sx={{ color: statusColor, fontWeight: 600 }}>
            ● {statusLabel}
          </Box>
        )}
        {turn.latency_ms != null && (
          <Box component="span">
            {t("badges.seconds", {
              seconds: (turn.latency_ms / 1000).toFixed(1),
            })}
          </Box>
        )}
        {turn.route && (
          <Tooltip title={t("badges.route", { route: turn.route })}>
            <Box
              component="span"
              sx={{
                px: 0.5,
                py: 0.125,
                borderRadius: 0.75,
                fontWeight: 600,
                textTransform: "uppercase",
                ...routeColor,
              }}
            >
              {turn.route}
            </Box>
          </Tooltip>
        )}
        {hasRowCount && (
          <Box component="span">
            {t("badges.rows", {
              count: (turn.query_result_rows as number).toLocaleString(),
            })}
          </Box>
        )}
        {turn.provider && (
          <Box component="span" sx={{ ml: "auto", overflow: "hidden", textOverflow: "ellipsis" }}>
            {turn.provider}
          </Box>
        )}
      </Box>
    );
  }

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
