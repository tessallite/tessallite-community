import { useEffect, useState } from "react";
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogContentText,
  DialogTitle,
} from "@mui/material";
import RestartAltIcon from "@mui/icons-material/RestartAlt";
import { useMutation, useQuery } from "@tanstack/react-query";
import { schedulerApiClient } from "../../api/client";
import { useT } from "../../i18n";

/**
 * Tenant-admin control to reseed the demo tenant. Activates the dormant
 * `demo-reseed` scheduler job (POST /scheduler/trigger/demo-reseed) and polls its
 * status. The backend is tenant-admin gated and allowlist-hardened; this is the UI.
 */
export default function DemoReseedButton() {
  const t = useT();
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [polling, setPolling] = useState(false);

  const status = useQuery({
    queryKey: ["demo-reseed-status"],
    queryFn: () => schedulerApiClient.reseedDemoStatus(),
    refetchInterval: polling ? 3000 : false,
    enabled: polling,
  });

  const state = status.data?.state;
  useEffect(() => {
    if (state === "completed" || state === "failed") setPolling(false);
  }, [state]);

  const start = useMutation({
    mutationFn: () => schedulerApiClient.reseedDemo(),
    onSuccess: () => {
      setConfirmOpen(false);
      setPolling(true);
    },
  });

  const running = polling || state === "running";

  return (
    <Box>
      <Button
        variant="outlined"
        color="warning"
        startIcon={running ? <CircularProgress size={16} /> : <RestartAltIcon />}
        disabled={running}
        onClick={() => setConfirmOpen(true)}
      >
        {t("demoReseed.button")}
      </Button>

      {running && (
        <Alert severity="info" sx={{ mt: 1 }}>
          {t("demoReseed.running")}
        </Alert>
      )}
      {!running && state === "completed" && (
        <Alert severity="success" sx={{ mt: 1 }}>
          {t("demoReseed.completed")}
        </Alert>
      )}
      {!running && state === "failed" && (
        <Alert severity="error" sx={{ mt: 1 }}>
          {t("demoReseed.failed")}
        </Alert>
      )}

      <Dialog open={confirmOpen} onClose={() => setConfirmOpen(false)}>
        <DialogTitle>{t("demoReseed.confirmTitle")}</DialogTitle>
        <DialogContent>
          <DialogContentText>{t("demoReseed.confirmBody")}</DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirmOpen(false)}>{t("common.cancel")}</Button>
          <Button
            color="warning"
            variant="contained"
            disabled={start.isPending}
            onClick={() => start.mutate()}
          >
            {t("demoReseed.confirm")}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
