import { useState } from "react";
import {
  Box,
  CircularProgress,
  Collapse,
  IconButton,
  Stack,
  Typography,
} from "@mui/material";
import { ExpandMore, ExpandLess } from "@mui/icons-material";
import type { TurnResponse } from "../types/turn";
import { useChatContext } from "../providers/ChatProvider";

const WARN_VERDICT = {
  dot: "warning.main",
  border: "warning.200",
  key: "judge.concerned",
};

const VERDICT_META: Record<string, typeof WARN_VERDICT> = {
  pass: {
    dot: "success.main",
    border: "success.200",
    key: "judge.approves",
  },
  warn: WARN_VERDICT,
  fail: {
    dot: "error.main",
    border: "error.200",
    key: "judge.didNotApprove",
  },
  unknown: {
    dot: "grey.500",
    border: "grey.300",
    key: "judge.unavailable",
  },
};

export function JudgeVerdictStrip({ turn }: { turn: TurnResponse }) {
  const { t } = useChatContext();
  const [open, setOpen] = useState(false);
  const pending = turn.judge_pending && !turn.judge_verdict;
  const verdict = turn.judge_verdict;
  if (!pending && !verdict) return null;

  if (pending) {
    return (
      <Box
        sx={{
          mt: 1,
          px: 1.25,
          py: 0.75,
          display: "flex",
          alignItems: "center",
          gap: 1,
          fontSize: 12,
          color: "text.secondary",
          bgcolor: "grey.50",
          borderRadius: 1,
          border: 1,
          borderColor: "divider",
        }}
      >
        <CircularProgress size={12} />
        {t("judge.evaluating")}
      </Box>
    );
  }

  const meta = VERDICT_META[verdict!] ?? WARN_VERDICT;
  const metrics = turn.judge_metrics;
  const hasDetails =
    (metrics && Object.keys(metrics).length > 0) ||
    Boolean(turn.judge_reasoning);

  return (
    <Box
      sx={{
        mt: 1,
        borderRadius: 1,
        border: 1,
        borderColor: meta.border,
        bgcolor: "background.paper",
      }}
    >
      <Stack
        direction="row"
        alignItems="center"
        spacing={1}
        sx={{
          px: 1.25,
          py: 0.75,
          cursor: hasDetails ? "pointer" : "default",
        }}
        onClick={() => hasDetails && setOpen((v) => !v)}
      >
        {hasDetails && (
          <IconButton size="small" sx={{ p: 0 }} tabIndex={-1}>
            {open ? (
              <ExpandLess fontSize="small" />
            ) : (
              <ExpandMore fontSize="small" />
            )}
          </IconButton>
        )}
        <Box
          sx={{
            width: 8,
            height: 8,
            borderRadius: "50%",
            bgcolor: meta.dot,
            flexShrink: 0,
          }}
        />
        <Typography
          sx={{ fontSize: 12, fontWeight: 500, color: "text.primary" }}
        >
          {t(meta.key)}
        </Typography>
      </Stack>
      {hasDetails && (
        <Collapse in={open} timeout="auto" unmountOnExit>
          <Box sx={{ px: 1.75, pb: 1 }}>
            {metrics && Object.keys(metrics).length > 0 && (
              <Box
                sx={{
                  display: "flex",
                  gap: 1.5,
                  flexWrap: "wrap",
                  mb: 0.5,
                }}
              >
                {Object.entries(metrics).map(([k, v]) => (
                  <Typography
                    key={k}
                    component="span"
                    sx={{ fontSize: 11, color: "text.secondary" }}
                  >
                    {k.replace(/_/g, " ")}: {Math.round(v * 5)}/5
                  </Typography>
                ))}
              </Box>
            )}
            {turn.judge_reasoning && (
              <Typography sx={{ fontSize: 11, color: "text.secondary" }}>
                {turn.judge_reasoning}
              </Typography>
            )}
          </Box>
        </Collapse>
      )}
    </Box>
  );
}
