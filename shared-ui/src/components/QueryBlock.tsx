import { useState, useCallback } from "react";
import {
  Box,
  Typography,
  IconButton,
  Collapse,
  Tooltip,
} from "@mui/material";
import { ContentCopy, ExpandMore, ExpandLess } from "@mui/icons-material";

interface QueryBlockProps {
  label: string;
  value: unknown;
}

function formatQueryValue(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export function QueryBlock({ label, value }: QueryBlockProps) {
  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState(false);
  const text = formatQueryValue(value);

  const handleCopy = useCallback(() => {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }, [text]);

  return (
    <Box
      sx={{
        mt: 1,
        border: 1,
        borderColor: "divider",
        borderRadius: 1,
        overflow: "hidden",
        minWidth: 0,
      }}
    >
      <Box
        sx={{
          display: "flex",
          alignItems: "center",
          px: 1,
          py: 0.5,
          cursor: "pointer",
          bgcolor: "action.hover",
          gap: 1,
        }}
        onClick={() => setExpanded(!expanded)}
      >
        <Typography
          variant="caption"
          fontWeight={600}
          sx={{ flex: 1, minWidth: 0 }}
          noWrap
        >
          {label}
        </Typography>
        <Tooltip title={copied ? "Copied" : "Copy"}>
          <IconButton
            size="small"
            onClick={(e) => {
              e.stopPropagation();
              handleCopy();
            }}
          >
            <ContentCopy sx={{ fontSize: 14 }} />
          </IconButton>
        </Tooltip>
        <IconButton size="small">
          {expanded ? (
            <ExpandLess sx={{ fontSize: 16 }} />
          ) : (
            <ExpandMore sx={{ fontSize: 16 }} />
          )}
        </IconButton>
      </Box>
      <Collapse in={expanded}>
        <Box
          component="pre"
          sx={{
            p: 1.5,
            m: 0,
            fontSize: 13,
            fontFamily:
              '"JetBrains Mono", "SFMono-Regular", "Consolas", monospace',
            overflowX: "auto",
            whiteSpace: "pre",
            maxWidth: "100%",
            bgcolor: "background.paper",
          }}
        >
          {text}
        </Box>
      </Collapse>
    </Box>
  );
}
