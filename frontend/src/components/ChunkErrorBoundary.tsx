import { Alert, Box, Button, Typography } from "@mui/material";
import { Component, type ReactNode } from "react";
import { I18nContext } from "../i18n";
import en from "../i18n";

type State = { error: Error | null };

function isChunkLoadError(err: Error | null): boolean {
  if (!err) return false;
  const m = err.message ?? "";
  return (
    m.includes("Failed to fetch dynamically imported module") ||
    m.includes("Importing a module script failed") ||
    m.includes("error loading dynamically imported module")
  );
}

export default class ChunkErrorBoundary extends Component<
  { children: ReactNode },
  State
> {
  static contextType = I18nContext;
  declare context: React.ContextType<typeof I18nContext>;

  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  reload = () => {
    window.location.reload();
  };

  render() {
    const msgs = (this.context ?? en) as Record<string, string>;
    const t = (key: string) => msgs[key] ?? (en as Record<string, string>)[key] ?? key;
    const err = this.state.error;
    if (!err) return this.props.children;

    if (isChunkLoadError(err)) {
      return (
        <Box sx={{ p: 3, maxWidth: 560, mx: "auto" }}>
          <Alert
            severity="warning"
            action={
              <Button color="inherit" size="small" onClick={this.reload}>
                {t("common.reload")}
              </Button>
            }
          >
            {t("errors.newerVersionAvailable")}
          </Alert>
        </Box>
      );
    }

    // Non-chunk error. Show a generic fallback with the message so the tree
    // stays mounted — without a fallback, returning children here would
    // re-throw and cause React to unmount the whole subtree (symptom: the
    // page flashes for a second then goes blank).
    return (
      <Box sx={{ p: 3, maxWidth: 720, mx: "auto" }}>
        <Alert
          severity="error"
          action={
            <Button color="inherit" size="small" onClick={this.reload}>
              {t("common.reload")}
            </Button>
          }
        >
          <Typography variant="body2" fontWeight={600} gutterBottom>
            {t("errors.somethingWentWrong")}
          </Typography>
          <Typography variant="caption" component="pre" sx={{ whiteSpace: "pre-wrap", m: 0 }}>
            {err.message || String(err)}
          </Typography>
        </Alert>
      </Box>
    );
  }
}
