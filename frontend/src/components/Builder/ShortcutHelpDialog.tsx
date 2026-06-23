import {
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  Button,
  Table,
  TableBody,
  TableCell,
  TableRow,
  Typography,
} from "@mui/material";
import { SHORTCUTS } from "../../hooks/useGlobalShortcuts";
import { useT } from "../../i18n";

interface Props {
  open: boolean;
  onClose: () => void;
}

export default function ShortcutHelpDialog({ open, onClose }: Props) {
  const t = useT();
  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle>{t("shortcuts.title")}</DialogTitle>
      <DialogContent>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
          {t("shortcuts.description")}
        </Typography>
        <Table size="small">
          <TableBody>
            {SHORTCUTS.map((s) => (
              <TableRow key={s.keys}>
                <TableCell sx={{ width: 170, fontFamily: "JetBrains Mono, monospace" }}>
                  {s.keys}
                </TableCell>
                <TableCell>{t(s.action)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>{t("common.close")}</Button>
      </DialogActions>
    </Dialog>
  );
}
