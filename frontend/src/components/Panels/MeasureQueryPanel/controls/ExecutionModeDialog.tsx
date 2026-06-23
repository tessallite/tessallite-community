import {
  Box,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControl,
  FormControlLabel,
  Radio,
  RadioGroup,
  Typography,
} from "@mui/material";
import { useT } from "../../../../i18n";

export type ExecutionMode = "auto" | "force_live";

type Props = {
  open: boolean;
  value: ExecutionMode;
  onChange: (mode: ExecutionMode) => void;
  onClose: () => void;
};

export default function ExecutionModeDialog({
  open,
  value,
  onChange,
  onClose,
}: Props) {
  const t = useT();
  return (
    <Dialog open={open} onClose={onClose} maxWidth="xs" fullWidth>
      <DialogTitle>{t("executionMode.title")}</DialogTitle>
      <DialogContent dividers>
        <FormControl>
          <RadioGroup
            value={value}
            onChange={(_, v) => onChange(v as ExecutionMode)}
          >
            <FormControlLabel
              value="auto"
              control={<Radio />}
              label={
                <Box>
                  <Typography variant="body2">{t("executionMode.autoLabel")}</Typography>
                  <Typography variant="caption" color="text.secondary">
                    {t("executionMode.autoDesc")}
                  </Typography>
                </Box>
              }
            />
            <FormControlLabel
              value="force_live"
              control={<Radio />}
              label={
                <Box>
                  <Typography variant="body2">{t("executionMode.forceLiveLabel")}</Typography>
                  <Typography variant="caption" color="text.secondary">
                    {t("executionMode.forceLiveDesc")}
                  </Typography>
                </Box>
              }
            />
          </RadioGroup>
        </FormControl>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} size="small">
          {t("executionMode.close")}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
