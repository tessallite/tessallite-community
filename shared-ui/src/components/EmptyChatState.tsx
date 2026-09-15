import { Box, Chip, Typography } from "@mui/material";
import { AutoAwesomeOutlined } from "@mui/icons-material";
import { useChatContext } from "../providers/ChatProvider";

export function EmptyChatState({ compact = false, onSelectExample }: { compact?: boolean; onSelectExample?: (text: string) => void }) {
  const { t } = useChatContext();

  return (
    <Box
      sx={{
        height: "100%",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        flexDirection: "column",
        gap: compact ? 0.5 : 1,
        color: "text.secondary",
        ...(compact ? { px: 2, textAlign: "center" } : {}),
      }}
    >
      {compact && <AutoAwesomeOutlined sx={{ fontSize: 40, color: "#A67C00" }} />}
      <Typography
        variant="h6"
        color="text.secondary"
        sx={compact ? { fontSize: 14, fontWeight: 600 } : undefined}
      >
        {compact ? t("chat.compactEmptyTitle") : t("chat.emptyTitle")}
      </Typography>
      <Typography
        variant="body2"
        color="text.secondary"
        sx={compact ? { fontSize: 12 } : undefined}
      >
        {compact ? t("chat.compactEmptyHint") : t("chat.emptyHint")}
      </Typography>
      {compact && onSelectExample && (
        <Box sx={{ display: "flex", flexWrap: "wrap", justifyContent: "center", gap: 0.5, mt: 1 }}>
          {[t("chat.exampleRevenue"), t("chat.exampleCustomers")].map((example) => (
            <Chip key={example} label={example} variant="outlined" onClick={() => onSelectExample(example)}
              sx={{ height: 20, fontSize: 11, borderRadius: "2px" }} />
          ))}
        </Box>
      )}
    </Box>
  );
}
