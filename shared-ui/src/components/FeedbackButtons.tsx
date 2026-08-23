import { IconButton, Stack } from "@mui/material";
import {
  ThumbUpAltOutlined,
  ThumbDownAltOutlined,
} from "@mui/icons-material";

interface FeedbackButtonsProps {
  onFeedback: (vote: "up" | "down") => void;
}

export function FeedbackButtons({ onFeedback }: FeedbackButtonsProps) {
  return (
    <Stack direction="row" spacing={0.25} alignItems="center">
      <IconButton size="small" onClick={() => onFeedback("up")}>
        <ThumbUpAltOutlined sx={{ fontSize: 14 }} />
      </IconButton>
      <IconButton size="small" onClick={() => onFeedback("down")}>
        <ThumbDownAltOutlined sx={{ fontSize: 14 }} />
      </IconButton>
    </Stack>
  );
}
