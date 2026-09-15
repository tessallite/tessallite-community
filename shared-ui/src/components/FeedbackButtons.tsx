import { IconButton, Stack } from "@mui/material";
import {
  ThumbUpAltOutlined,
  ThumbDownAltOutlined,
} from "@mui/icons-material";
import { useChatContext } from "../providers/ChatProvider";

interface FeedbackButtonsProps {
  onFeedback: (vote: "up" | "down") => void;
  compact?: boolean;
}

export function FeedbackButtons({ onFeedback, compact = false }: FeedbackButtonsProps) {
  const { t } = useChatContext();
  return (
    <Stack direction="row" spacing={0.25} alignItems="center">
      <IconButton
        size="small"
        onClick={() => onFeedback("up")}
        aria-label={t("feedback.helpful")}
        title={t("feedback.helpful")}
        sx={compact ? { width: 24, height: 24, p: 0 } : undefined}
      >
        <ThumbUpAltOutlined sx={{ fontSize: 14 }} />
      </IconButton>
      <IconButton
        size="small"
        onClick={() => onFeedback("down")}
        aria-label={t("feedback.notHelpful")}
        title={t("feedback.notHelpful")}
        sx={compact ? { width: 24, height: 24, p: 0 } : undefined}
      >
        <ThumbDownAltOutlined sx={{ fontSize: 14 }} />
      </IconButton>
    </Stack>
  );
}
