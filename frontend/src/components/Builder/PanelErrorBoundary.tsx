import { Alert, Box, Button, Typography } from "@mui/material";
import { Component, type ReactNode } from "react";
import { I18nContext } from "../../i18n";
import en from "../../i18n";

type Props = { children: ReactNode; panelName?: string };
type State = { error: Error | null };

export default class PanelErrorBoundary extends Component<Props, State> {
  static contextType = I18nContext;
  declare context: React.ContextType<typeof I18nContext>;

  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  reset = () => this.setState({ error: null });

  render() {
    const msgs = (this.context ?? en) as Record<string, string>;
    const t = (key: string, vars?: Record<string, string>) => {
      let text = msgs[key] ?? (en as Record<string, string>)[key] ?? key;
      if (vars) for (const [k, v] of Object.entries(vars)) text = text.replace(`{{${k}}}`, v);
      return text;
    };
    if (!this.state.error) return this.props.children;
    const label = this.props.panelName ?? t("builder.panelFallback");
    return (
      <Box sx={{ p: 2 }}>
        <Alert
          severity="error"
          action={
            <Button color="inherit" size="small" onClick={this.reset}>
              {t("common.retry")}
            </Button>
          }
        >
          <Typography variant="body2" fontWeight={600} gutterBottom>
            {t("errors.panelFailedToRender", { panel: label })}
          </Typography>
          <Typography variant="caption" component="pre" sx={{ whiteSpace: "pre-wrap", m: 0 }}>
            {this.state.error.message}
          </Typography>
        </Alert>
      </Box>
    );
  }
}
