import { Box, Typography } from "@mui/material";
import { useChatContext } from "../providers/ChatProvider";

export function EmptyChatState() {
  const { t } = useChatContext();

  return (
    <Box
      sx={{
        height: "100%",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        flexDirection: "column",
        gap: 1,
        color: "text.secondary",
      }}
    >
      <Typography variant="h6" color="text.secondary">
        {t("chat.emptyTitle")}
      </Typography>
      <Typography variant="body2" color="text.secondary">
        {t("chat.emptyHint")}
      </Typography>
    </Box>
  );
}
