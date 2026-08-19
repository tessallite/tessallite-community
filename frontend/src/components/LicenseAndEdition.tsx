import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Divider,
  LinearProgress,
  Stack,
  Typography,
} from "@mui/material";
import OpenInNewIcon from "@mui/icons-material/OpenInNew";
import { useEdition, useLimits } from "../api/hooks";
import { useT } from "../i18n";

/**
 * "License & edition" view: shows the current edition, activation status, and
 * each capped resource as current-vs-max, plus a register / upgrade call to
 * action. Purely a read-only reflection of `/edition` + `/limits`; it holds no
 * enforcement logic (the backend manager owns that).
 */

const UPGRADE_URL_FALLBACK = "https://tessallite.io/register.html";

// Entitlement keys returned by /limits.entitlements, paired with the matching
// current count from /limits.usage. When a cap is null/absent the resource is
// presented as "unlimited" rather than a current/max bar.
type LimitRow = {
  labelKey: string;
  used: number | null | undefined;
  max: number | null | undefined;
  unlimited?: boolean;
};

function toNumber(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function ResourceRow({ row }: { row: LimitRow }) {
  const t = useT();
  const label = t(row.labelKey);
  const used = row.used ?? null;
  const max = toNumber(row.max);

  let valueText: string;
  if (row.unlimited || max == null) {
    valueText =
      used != null
        ? t("license.usedUnlimited", { used })
        : t("license.unlimited");
  } else {
    valueText = `${used ?? "-"} / ${max}`;
  }

  const showBar = !row.unlimited && max != null && max > 0;
  const pct = showBar
    ? Math.min(100, Math.round(((used ?? 0) / (max as number)) * 100))
    : 0;
  const atLimit = showBar && (used ?? 0) >= (max as number);

  return (
    <Box sx={{ py: 0.75 }} data-testid="license-resource-row">
      <Stack direction="row" justifyContent="space-between" alignItems="center">
        <Typography variant="body2">{label}</Typography>
        <Typography
          variant="body2"
          sx={{ fontWeight: 700, color: atLimit ? "error.main" : "text.primary" }}
        >
          {valueText}
        </Typography>
      </Stack>
      {showBar && (
        <LinearProgress
          variant="determinate"
          value={pct}
          color={atLimit ? "error" : "primary"}
          sx={{ mt: 0.5, height: 6, borderRadius: 3 }}
        />
      )}
    </Box>
  );
}

export default function LicenseAndEdition() {
  const t = useT();
  const {
    data: edition,
    isLoading: editionLoading,
    isError: editionError,
    refetch: refetchEdition,
  } = useEdition();
  const {
    data: limits,
    isLoading: limitsLoading,
    isError: limitsError,
    refetch: refetchLimits,
  } = useLimits();

  if (editionLoading || limitsLoading) {
    return (
      <Box sx={{ display: "flex", alignItems: "center", gap: 1, py: 1 }}>
        <CircularProgress size={16} />
        <Typography variant="body2" color="text.secondary">
          {t("license.loading")}
        </Typography>
      </Box>
    );
  }

  // Bug-7470: a failed /edition or /limits read must NOT be silently collapsed
  // into the "unactivated / unlimited" default — that misrepresents a network
  // error as a legitimate licence state. Surface an explicit, retryable error.
  if (editionError || limitsError) {
    return (
      <Box data-testid="license-edition">
        <Alert
          severity="error"
          data-testid="license-load-error"
          action={
            <Button
              color="inherit"
              size="small"
              onClick={() => {
                if (editionError) refetchEdition();
                if (limitsError) refetchLimits();
              }}
            >
              {t("common.retry")}
            </Button>
          }
        >
          {t("license.loadError")}
        </Alert>
      </Box>
    );
  }

  const name = edition?.edition ?? "unactivated";
  // Bug-7680: an installed-but-invalid licence (verifier rejected an expired or
  // untrusted document) reports ``license_state: "invalid"``. It is fail-closed
  // like the unactivated state, but must be shown distinctly so an admin knows a
  // licence WAS installed and needs renewing/replacing — not that none was ever
  // provided.
  // Bug-7851 / G-license-term: a licence that expires after manager load reports
  // ``license_state: "expired"`` — distinct from "invalid" (untrusted signature).
  // Both are non-active states with a distinct error chip and banner.
  const licenseInvalid = edition?.license_state === "invalid";
  const licenseExpired = edition?.license_state === "expired";
  const editionLabel =
    name === "community"
      ? t("edition.community")
      : name === "enterprise"
        ? t("edition.enterprise")
        : name === "unactivated"
          ? t("edition.unactivated")
          : name;

  const ent = (limits?.entitlements ?? {}) as Record<string, unknown>;
  const usage = limits?.usage ?? {};

  const rows: LimitRow[] = [
    {
      labelKey: "license.row.tenants",
      used: usage.tenants,
      max: ent.own_tenants as number | undefined,
    },
    {
      labelKey: "license.row.projects",
      used: usage.projects,
      max: ent.projects_per_own_tenant as number | undefined,
      unlimited: ent.projects_per_own_tenant == null,
    },
    {
      labelKey: "license.row.models",
      used: usage.models,
      max: ent.models as number | undefined,
    },
    {
      labelKey: "license.row.users",
      used: usage.users,
      max: ent.users as number | undefined,
    },
  ];

  // Community / unactivated editions are the ones that benefit from the upgrade
  // CTA; on an activated enterprise license it becomes a "register" link.
  const showUpgrade = name !== "enterprise";

  return (
    <Box data-testid="license-edition">
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 0.5 }}>
        <Typography variant="subtitle2" fontWeight={700}>
          {t("license.heading")}
        </Typography>
        <Chip
          size="small"
          variant="outlined"
          label={editionLabel}
          data-testid="license-edition-chip"
        />
        {edition?.activated && !licenseInvalid && !licenseExpired && (
          <Chip
            size="small"
            color="success"
            variant="outlined"
            label={t("license.activated")}
          />
        )}
        {licenseInvalid && (
          <Chip
            size="small"
            color="error"
            variant="filled"
            label={t("license.invalidChip")}
            data-testid="license-invalid-chip"
          />
        )}
        {licenseExpired && (
          <Chip
            size="small"
            color="error"
            variant="filled"
            label={t("license.expiredChip")}
            data-testid="license-expired-chip"
          />
        )}
      </Stack>

      {/* Bug-7680: installed-but-invalid licence — surface a distinct error
          banner so an admin renews/replaces the licence rather than assuming a
          normal, active state (or that no licence was ever installed). */}
      {licenseInvalid && (
        <Alert severity="error" data-testid="license-invalid-banner" sx={{ mt: 1 }}>
          {t("license.invalidBody")}
        </Alert>
      )}

      {/* Bug-7851 / G-license-term: expired licence — distinct from invalid
          (expired was once valid; invalid was never accepted by the verifier). */}
      {licenseExpired && (
        <Alert severity="warning" data-testid="license-expired-banner" sx={{ mt: 1 }}>
          {t("license.expiredBody")}
        </Alert>
      )}

      {edition?.expires_at && (
        <Typography variant="caption" color="text.secondary">
          {t("license.expires", { date: edition.expires_at })}
        </Typography>
      )}

      <Box sx={{ mt: 1.5 }}>
        <Stack divider={<Divider flexItem />}>
          {rows.map((row) => (
            <ResourceRow key={row.labelKey} row={row} />
          ))}
        </Stack>
      </Box>

      <Box sx={{ mt: 2 }}>
        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1 }}>
          {showUpgrade ? t("license.upgradeBody") : t("license.registerBody")}
        </Typography>
        <Button
          variant="contained"
          size="small"
          href={t("license.upgradeUrl") || UPGRADE_URL_FALLBACK}
          target="_blank"
          rel="noopener noreferrer"
          endIcon={<OpenInNewIcon fontSize="small" />}
        >
          {showUpgrade ? t("license.upgradeCta") : t("license.registerCta")}
        </Button>
      </Box>
    </Box>
  );
}
