import { Box, Paper, Typography } from "@mui/material";

interface UserMessageProps {
  text: string;
}

export function UserMessage({ text }: UserMessageProps) {
  return (
    <Box sx={{ display: "flex", justifyContent: "flex-end", mb: 2 }}>
      <Paper
        variant="outlined"
        sx={{ p: 1.25, maxWidth: "75%", bgcolor: "primary.50" }}
      >
        <Typography variant="body2" sx={{ whiteSpace: "pre-wrap" }}>
          {text}
        </Typography>
      </Paper>
    </Box>
  );
}
