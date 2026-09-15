import { Fab } from "@mui/material";
import { KeyboardArrowDown } from "@mui/icons-material";
import { useChatContext } from "../providers/ChatProvider";

interface ScrollToBottomButtonProps {
  onClick: () => void;
  compact?: boolean;
}

export function ScrollToBottomButton({ onClick, compact = false }: ScrollToBottomButtonProps) {
  const { t } = useChatContext();
  return (
    <Fab
      size="small"
      color="default"
      onClick={onClick}
      aria-label={t("chat.scrollToBottomAria")}
      sx={{
        position: "absolute",
        bottom: compact ? 42 : 80,
        ...(compact
          ? {
              right: 8,
              left: "auto",
              transform: "none",
              width: 28,
              height: 28,
              bgcolor: "#fff",
              color: "#217346",
              border: 1,
              borderColor: "#D1D1D1",
              boxShadow: "0 2px 6px rgba(0,0,0,.15)",
            }
          : { left: "50%", transform: "translateX(-50%)" }),
        zIndex: 10,
        opacity: compact ? 1 : 0.8,
        "&:hover": { opacity: 1 },
      }}
    >
      <KeyboardArrowDown sx={{ fontSize: compact ? 16 : undefined }} />
    </Fab>
  );
}
