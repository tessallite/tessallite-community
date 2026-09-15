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
  compact?: boolean;
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

function StepRow({ step, compact }: { step: CompoundStep; compact: boolean }) {
  const { t } = useChatContext();
  const [expanded, setExpanded] = useState(false);
  const hasDetails = !!step.preview_row;

  return (
    <Box
      sx={{
        display: "flex",
        alignItems: "flex-start",
        gap: compact ? 0.625 : 1,
        py: compact ? 0.25 : 0.75,
        px: compact ? 0.75 : 1,
        borderRadius: compact ? 0 : 1,
        bgcolor: step.status === "failed" ? "error.50" : "transparent",
        "&:not(:last-child)": { borderBottom: 1, borderColor: "divider" },
      }}
    >
      <Box sx={{ mt: 0.25, flexShrink: 0 }}>
        <Box sx={{ transform: compact ? "scale(.8)" : undefined, transformOrigin: "left center" }}>
          <StepIcon status={step.status} />
        </Box>
      </Box>

      <Box sx={{ flex: 1, minWidth: 0 }}>
        <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
          <Typography
            variant="caption"
              fontWeight={600}
              sx={compact ? { fontSize: 10 } : undefined}
            color="text.secondary"
          >
            {t("steps.step", { n: String(step.step_number) })}
          </Typography>
          {step.title && (
            <Typography variant="caption" noWrap sx={{ flex: 1, ...(compact ? { fontSize: 11 } : {}) }}>
              {step.title}
            </Typography>
          )}
          {step.row_count !== undefined && (
            <Typography variant="caption" color="text.secondary" sx={compact ? { fontSize: 10 } : undefined}>
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
                  fontSize: compact ? 10 : 11,
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
          sx={{ flexShrink: 0, ...(compact ? { width: 20, height: 20, p: 0 } : {}) }}
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

export function InlineStepCard({ steps, compact = false }: InlineStepCardProps) {
  const { t } = useChatContext();

  return (
    <Box
      sx={{
        mt: compact ? 0 : 1,
        border: 1,
        borderColor: "divider",
        borderRadius: compact ? 0 : 1,
        overflow: "hidden",
      }}
    >
      <Box sx={{ px: 1, py: compact ? 0.25 : 0.5, bgcolor: "action.hover" }}>
        <Typography variant="caption" fontWeight={600} sx={compact ? { fontSize: 10 } : undefined}>
          {t("steps.header", { n: String(steps.length) })}
        </Typography>
      </Box>
      <Box sx={{ px: compact ? 0 : 0.5 }}>
        {steps.map((step) => (
          <StepRow key={step.step_number} step={step} compact={compact} />
        ))}
      </Box>
      <style>{`@keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }`}</style>
    </Box>
  );
}
