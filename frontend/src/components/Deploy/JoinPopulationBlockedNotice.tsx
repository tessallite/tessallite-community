import {
  Alert,
  AlertTitle,
  Box,
  Button,
  List,
  ListItem,
  ListItemText,
  Typography,
} from "@mui/material";
import { useT } from "../../i18n";
import type { JoinPopulationBlockedDetail } from "../../api/versionsApi";

type Props = {
  detail: JoinPopulationBlockedDetail;
  onOpenJoins?: () => void;
  onClose?: () => void;
};

function effectLabel(effect: number | null): string {
  return effect == null ? "—" : `${(effect * 100).toFixed(1)}%`;
}

/** Shared accessible rendering for every deploy caller's typed 409. */
export default function JoinPopulationBlockedNotice({
  detail,
  onOpenJoins,
  onClose,
}: Props) {
  const t = useT();
  return (
    <Alert
      severity="error"
      role="alert"
      aria-live="assertive"
      data-testid="join-population-blocked-notice"
      onClose={onClose}
      sx={{ textAlign: "left" }}
    >
      <AlertTitle>{t("deploy.joinPopulationBlockedTitle")}</AlertTitle>
      <Typography variant="body2">
        {t("deploy.joinPopulationBlockedSummary", {
          threshold: String(detail.threshold),
        })}
      </Typography>
      <List dense disablePadding aria-label={t("deploy.joinPopulationOffenders")}>
        {detail.joins.map((join) => (
          <ListItem key={join.join_id} disableGutters sx={{ py: 0.25 }}>
            <ListItemText
              primary={join.join_label || t("deploy.joinPopulationUnknownJoin", {
                id: join.join_id,
              })}
              secondary={t("deploy.joinPopulationOffenderDetail", {
                effect: effectLabel(join.row_effect_ratio),
                reason: join.reason || t("deploy.joinPopulationMeasuredReason"),
              })}
            />
          </ListItem>
        ))}
      </List>
      <Typography variant="body2" sx={{ mt: 0.5 }}>
        {t("deploy.joinPopulationBlockedAction")}
      </Typography>
      {onOpenJoins && (
        <Box sx={{ mt: 1 }}>
          <Button
            size="small"
            variant="outlined"
            onClick={onOpenJoins}
            aria-label={t("deploy.openJoins")}
          >
            {t("deploy.openJoins")}
          </Button>
        </Box>
      )}
    </Alert>
  );
}
