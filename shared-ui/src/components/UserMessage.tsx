import { Box, Paper, Typography } from "@mui/material";

/**
 * Brand colours are fixed here rather than resolved from the host theme on
 * purpose. The prompt bubble has to read identically in every host and in both
 * light and dark mode — these turns are captured for presentations — and
 * off-white on the dark-theme green (`#2DA860`) would not clear contrast.
 * `#006C35` / `#E8EDE9` is ~7:1.
 */
const BUBBLE_BG = "#006C35";
const BUBBLE_TEXT = "#E8EDE9";

interface UserMessageProps {
  text: string;
  compact?: boolean;
}

export function UserMessage({ text, compact = false }: UserMessageProps) {
  return (
    <Box sx={{ display: "flex", justifyContent: "flex-end", mb: compact ? 1 : 2 }}>
      <Paper
        elevation={0}
        sx={{
          px: compact ? 1.25 : 2,
          py: compact ? 0.625 : 1.5,
          maxWidth: compact ? "88%" : "75%",
          bgcolor: compact ? "#217346" : BUBBLE_BG,
          borderRadius: compact ? "8px 8px 2px 8px" : 2,
        }}
      >
        <Typography
          sx={{
            whiteSpace: "pre-wrap",
            color: compact ? "#fff" : BUBBLE_TEXT,
            fontSize: compact ? 13 : { xs: 16, sm: 17 },
            lineHeight: compact ? 1.4 : 1.5,
            fontWeight: compact ? 600 : 500,
          }}
        >
          {text}
        </Typography>
      </Paper>
    </Box>
  );
}
