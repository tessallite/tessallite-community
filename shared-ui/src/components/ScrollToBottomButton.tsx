import { Fab } from "@mui/material";
import { KeyboardArrowDown } from "@mui/icons-material";

interface ScrollToBottomButtonProps {
  onClick: () => void;
}

export function ScrollToBottomButton({ onClick }: ScrollToBottomButtonProps) {
  return (
    <Fab
      size="small"
      color="default"
      onClick={onClick}
      aria-label="Scroll to bottom"
      sx={{
        position: "absolute",
        bottom: 80,
        left: "50%",
        transform: "translateX(-50%)",
        zIndex: 10,
        opacity: 0.8,
        "&:hover": { opacity: 1 },
      }}
    >
      <KeyboardArrowDown />
    </Fab>
  );
}
