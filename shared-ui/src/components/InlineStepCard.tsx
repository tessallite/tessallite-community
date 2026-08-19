import { useState } from "react";
import {
  Box,
  Typography,
  IconButton,
  Collapse,
} from "@mui/material";
import {
  CheckCircle,
  Error as ErrorIcon,
  HourglassEmpty,
  Sync,
  ExpandMore,
  ExpandLess,
} from "@mui/icons-material";
import type { CompoundStep } from "../types/streaming";
import { useChatContext } from "../providers/ChatProvider";

interface InlineStepCardProps {
  steps: CompoundStep[];
}

function StepIcon({ status }: { status: string }) {
  switch (status) {
    case "complete":
      return <CheckCircle sx={{ fontSize: 16, color: "success.main" }} />;
    case "failed":
      return <ErrorIcon sx={{ fontSize: 16, color: "error.main" }} />;
    case "running":
      return (
        <Sync
          sx={{
            fontSize: 16,
            color: "info.main",
            animation: "spin 1.5s linear infinite",
          }}
        />
      );
    default:
      return (
        <HourglassEmpty sx={{ fontSize: 16, color: "text.disabled" }} />
      );
  }
}

function StepRow({ step }: { step: CompoundStep }) {
  const { t } = useChatContext();
  const [expanded, setExpanded] = useState(false);
  const hasDetails = !!step.preview_row;

  return (
    <Box
      sx={{
        display: "flex",
        alignItems: "flex-start",
        gap: 1,
        py: 0.75,
        px: 1,
        borderRadius: 1,
        bgcolor: step.status === "failed" ? "error.50" : "transparent",
        "&:not(:last-child)": { borderBottom: 1, borderColor: "divider" },
      }}
    >
      <Box sx={{ mt: 0.25, flexShrink: 0 }}>
        <StepIcon status={step.status} />
      </Box>

      <Box sx={{ flex: 1, minWidth: 0 }}>
        <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
          <Typography
            variant="caption"
            fontWeight={600}
            color="text.secondary"
          >
            {t("steps.step", { n: String(step.step_number) })}
          </Typography>
          {step.title && (
            <Typography variant="caption" noWrap sx={{ flex: 1 }}>
              {step.title}
            </Typography>
          )}
          {step.row_count !== undefined && (
            <Typography variant="caption" color="text.secondary">
              {t("steps.rows", { count: step.row_count.toLocaleString() })}
            </Typography>
          )}
        </Box>

        {hasDetails && (
          <Collapse in={expanded}>
            {step.preview_row && (
              <Box
                component="pre"
                sx={{
                  fontSize: 11,
                  fontFamily: '"JetBrains Mono", monospace',
                  bgcolor: "action.hover",
                  p: 0.75,
                  borderRadius: 0.5,
                  mt: 0.5,
                  overflowX: "auto",
                  maxHeight: 120,
                }}
              >
                {JSON.stringify(step.preview_row, null, 2)}
              </Box>
            )}
          </Collapse>
        )}
      </Box>

      {hasDetails && (
        <IconButton
          size="small"
          onClick={() => setExpanded(!expanded)}
          sx={{ flexShrink: 0 }}
        >
          {expanded ? (
            <ExpandLess sx={{ fontSize: 14 }} />
          ) : (
            <ExpandMore sx={{ fontSize: 14 }} />
          )}
        </IconButton>
      )}
    </Box>
  );
}

export function InlineStepCard({ steps }: InlineStepCardProps) {
  const { t } = useChatContext();

  return (
    <Box
      sx={{
        mt: 1,
        border: 1,
        borderColor: "divider",
        borderRadius: 1,
        overflow: "hidden",
      }}
    >
      <Box sx={{ px: 1, py: 0.5, bgcolor: "action.hover" }}>
        <Typography variant="caption" fontWeight={600}>
          {t("steps.header", { n: String(steps.length) })}
        </Typography>
      </Box>
      <Box sx={{ px: 0.5 }}>
        {steps.map((step) => (
          <StepRow key={step.step_number} step={step} />
        ))}
      </Box>
      <style>{`@keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }`}</style>
    </Box>
  );
}
