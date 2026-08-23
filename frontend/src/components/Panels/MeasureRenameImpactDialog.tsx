import {
  Alert,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  List,
  ListItem,
  ListItemText,
  Typography,
} from "@mui/material";
import { useT } from "../../i18n";
import type { MeasureRenameImpactResponse } from "../../api/types";

export function MeasureRenameImpactDialog({
  impact,
  open,
  onCancel,
  onConfirm,
}: {
  impact: MeasureRenameImpactResponse | null;
  open: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const t = useT();
  if (!impact) return null;

  return (
    <Dialog open={open} onClose={onCancel} maxWidth="sm" fullWidth>
      <DialogTitle>{t("measures.renameImpact.title")}</DialogTitle>
      <DialogContent>
        <Typography variant="body2" sx={{ mb: 1 }}>
          {t("measures.renameImpact.summary", {
            current: impact.current_name,
            next: impact.new_name,
          })}
        </Typography>
        {impact.rewrites.length > 0 && (
          <>
            <Typography variant="subtitle2">
              {t("measures.renameImpact.rewrites", { count: impact.rewrites.length })}
            </Typography>
            <List dense>
              {impact.rewrites.map((item) => (
                <ListItem key={`${item.consumer_type}:${item.consumer_id}:${item.field}`}>
                  <ListItemText
                    primary={item.consumer_name || item.consumer_id}
                    secondary={t("measures.renameImpact.consumerField", {
                      type: item.consumer_type,
                      field: item.field,
                    })}
                  />
                </ListItem>
              ))}
            </List>
          </>
        )}
        {impact.blockers.length > 0 && (
          <Alert severity="error" sx={{ mt: 1 }}>
            <Typography variant="subtitle2">
              {t("measures.renameImpact.blockers", { count: impact.blockers.length })}
            </Typography>
            <List dense disablePadding>
              {impact.blockers.map((item) => (
                <ListItem key={`${item.consumer_type}:${item.consumer_id}:${item.field}`}>
                  <ListItemText
                    primary={item.consumer_name || item.consumer_id}
                    secondary={t("measures.renameImpact.consumerField", {
                      type: item.consumer_type,
                      field: item.field,
                    })}
                  />
                </ListItem>
              ))}
            </List>
          </Alert>
        )}
        {impact.rewrites.length === 0 && impact.blockers.length === 0 && (
          <Alert severity="success">{t("measures.renameImpact.noReferences")}</Alert>
        )}
        {!impact.safe && impact.blockers.length === 0 && (
          <Alert severity="warning" sx={{ mt: 1 }}>
            {t("measures.renameImpact.unsafe")}
          </Alert>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onCancel}>{t("common.cancel")}</Button>
        <Button
          variant="contained"
          onClick={onConfirm}
          disabled={!impact.safe || impact.blockers.length > 0}
        >
          {t("measures.renameImpact.confirm")}
        </Button>
      </DialogActions>
    </Dialog>
  );
}

export default MeasureRenameImpactDialog;
