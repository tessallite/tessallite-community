import {
  Alert,
  Box,
  Chip,
  CircularProgress,
  Divider,
  Link,
  Stack,
  Typography,
} from "@mui/material";
import { useAdvisories } from "../api/hooks";
import type { Advisory } from "../api/client";
import { useT } from "../i18n";

/**
 * Security advisory + update-notice feed, sourced from the public issuer
 * service (`/advisories`). Read-only and display-only — it carries no
 * enforcement logic. Every load state (loading / empty / unreachable) renders a
 * calm, non-blocking message; a failed fetch never crashes the surrounding page.
 */

type Severity = "info" | "low" | "medium" | "high" | "critical";

const SEVERITY_COLOR: Record<
  Severity,
  "default" | "info" | "warning" | "error"
> = {
  info: "info",
  low: "info",
  medium: "warning",
  high: "error",
  critical: "error",
};

function severityKey(raw?: string): Severity {
  const s = (raw ?? "").toLowerCase();
  if (s === "critical" || s === "high" || s === "medium" || s === "low") {
    return s as Severity;
  }
  return "info";
}

function AdvisoryRow({ advisory }: { advisory: Advisory }) {
  const t = useT();
  const sev = severityKey(advisory.severity);
  return (
    <Box sx={{ py: 1.25 }} data-testid="advisory-row">
      <Stack
        direction="row"
        alignItems="center"
        spacing={1}
        sx={{ flexWrap: "wrap" }}
      >
        <Chip
          size="small"
          color={SEVERITY_COLOR[sev]}
          variant="outlined"
          label={t(`advisories.severity.${sev}`)}
        />
        <Typography variant="body2" sx={{ fontWeight: 700 }}>
          {advisory.title}
        </Typography>
      </Stack>
      <Stack
        direction="row"
        spacing={2}
        sx={{ mt: 0.5, flexWrap: "wrap", color: "text.secondary" }}
      >
        <Typography variant="caption">{advisory.id}</Typography>
        {advisory.published && (
          <Typography variant="caption">{advisory.published}</Typography>
        )}
        {advisory.fixed_in && (
          <Typography variant="caption">
            {t("advisories.fixedIn", { version: advisory.fixed_in })}
          </Typography>
        )}
      </Stack>
      {advisory.summary && (
        <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
          {advisory.summary}
        </Typography>
      )}
      {advisory.link && (
        <Link
          href={advisory.link}
          target="_blank"
          rel="noopener noreferrer"
          variant="body2"
          sx={{ mt: 0.5, display: "inline-block" }}
        >
          {t("advisories.readMore")}
        </Link>
      )}
    </Box>
  );
}

export default function AdvisoryPanel() {
  const t = useT();
  const { data, isLoading, isError } = useAdvisories();

  return (
    <Box data-testid="advisory-panel">
      <Typography variant="subtitle2" fontWeight={700} sx={{ mb: 0.5 }}>
        {t("advisories.heading")}
      </Typography>
      <Typography variant="caption" color="text.secondary">
        {t("advisories.subheading")}
      </Typography>

      <Box sx={{ mt: 1.5 }}>
        {isLoading && (
          <Box sx={{ display: "flex", alignItems: "center", gap: 1, py: 1 }}>
            <CircularProgress size={16} />
            <Typography variant="body2" color="text.secondary">
              {t("advisories.loading")}
            </Typography>
          </Box>
        )}

        {!isLoading && isError && (
          <Alert severity="info" variant="outlined">
            {t("advisories.unreachable")}
          </Alert>
        )}

        {!isLoading && !isError && (data?.length ?? 0) === 0 && (
          <Typography variant="body2" color="text.secondary" sx={{ py: 1 }}>
            {t("advisories.empty")}
          </Typography>
        )}

        {!isLoading && !isError && (data?.length ?? 0) > 0 && (
          <Stack divider={<Divider flexItem />}>
            {data!.map((a) => (
              <AdvisoryRow key={a.id} advisory={a} />
            ))}
          </Stack>
        )}
      </Box>
    </Box>
  );
}
