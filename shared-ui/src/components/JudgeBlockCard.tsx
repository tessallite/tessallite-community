import { Box, Button, Stack, Typography } from "@mui/material";
import { Replay } from "@mui/icons-material";
import type { TurnResponse } from "../types/turn";
import { useChatContext } from "../providers/ChatProvider";

interface JudgeBlockCardProps {
  turn: TurnResponse;
  onResend?: (prompt: string) => void;
  disabled?: boolean;
}

export function JudgeBlockCard({
  onResend,
  disabled = false,
}: JudgeBlockCardProps) {
  const { t } = useChatContext();

  const handleResend = () => {
    onResend?.(t("judge.correctPreviousNoReason"));
  };

  return (
    <Box
      sx={{
        mt: 1,
        borderRadius: 1,
        border: 1,
        borderColor: "error.200",
        bgcolor: "background.paper",
      }}
    >
      <Stack
        direction="row"
        alignItems="center"
        spacing={1}
        sx={{ px: 1.25, py: 0.75 }}
      >
        <Box
          sx={{
            width: 8,
            height: 8,
            borderRadius: "50%",
            bgcolor: "error.main",
            flexShrink: 0,
          }}
        />
        <Typography
          sx={{
            fontSize: 12,
            fontWeight: 500,
            color: "text.primary",
            flex: 1,
          }}
        >
          {t("judge.didNotApprove")}
        </Typography>
        {onResend && (
          <Button
            size="small"
            variant="outlined"
            startIcon={<Replay fontSize="small" />}
            disabled={disabled}
            onClick={handleResend}
            sx={{ fontSize: 11, py: 0.25, px: 1, whiteSpace: "nowrap" }}
          >
            {t("judge.sendBackToCorrect")}
          </Button>
        )}
      </Stack>
    </Box>
  );
}
