import { useState } from "react";
import { useT } from "../../../../i18n";
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Divider,
  IconButton,
  Link,
  List,
  ListItem,
  ListItemButton,
  ListItemText,
  Popover,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import FunctionsIcon from "@mui/icons-material/Functions";
import AddIcon from "@mui/icons-material/Add";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { measuresApi, type AvailableVariant } from "../../../../api/client";
import type { Measure, MeasureCreate } from "../../../../api/types";
import {
  TIME_VARIANT_DEFAULT_N,
  isParametricVariant,
} from "../../../../constants/timeVariants";

type Props = {
  projectId: string;
  modelId: string;
  measure: Measure;
  onVariantCreated?: (newMeasureId: string) => void;
};

function buildVariantPayload(
  base: Measure,
  variant: AvailableVariant,
  n: number | null,
): MeasureCreate {
  return {
    name: variant.suggested_name,
    display_name: variant.suggested_display_name,
    description: base.description ?? null,
    display_folder: base.display_folder ?? null,
    source_table_id: base.source_table_id ?? undefined,
    source_column_name: base.source_column_name ?? undefined,
    user_defined_attribute_id: base.user_defined_attribute_id ?? undefined,
    measure_type: base.measure_type,
    expression: base.expression ?? undefined,
    calc_agg_mode: base.calc_agg_mode ?? undefined,
    default_agg: (base.default_agg as MeasureCreate["default_agg"]) || "sum",
    data_type: base.data_type,
    format: base.format,
    is_additive: base.is_additive,
    variant_kind: variant.kind,
    variant_of_measure_id: base.id,
    variant_n: n,
  };
}

const HELP_URL = "/help/modelling/configure-time-variants.html";

export default function VariantPopover({
  projectId,
  modelId,
  measure,
  onVariantCreated,
}: Props) {
  const t = useT();
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const [paramN, setParamN] = useState<Record<string, string>>({});
  const [submittingKind, setSubmittingKind] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const qc = useQueryClient();

  const variantsQuery = useQuery({
    queryKey: ["available-variants", projectId, modelId, measure.id],
    queryFn: () => measuresApi.listAvailableVariants(projectId, modelId, measure.id),
    enabled: Boolean(anchor),
    staleTime: 30_000,
  });

  function closePopover() {
    setAnchor(null);
    setError(null);
  }

  async function handleCreate(v: AvailableVariant) {
    setError(null);
    setSubmittingKind(v.kind);
    try {
      let n: number | null = null;
      if (isParametricVariant(v.kind)) {
        const raw = paramN[v.kind]?.trim();
        const parsed = raw ? Number(raw) : NaN;
        n = Number.isFinite(parsed) && parsed > 0
          ? parsed
          : TIME_VARIANT_DEFAULT_N[v.kind] ?? null;
      }
      const created = await measuresApi.create(
        projectId,
        modelId,
        buildVariantPayload(measure, v, n),
      );
      await qc.invalidateQueries({
        queryKey: ["measures", projectId, modelId],
      });
      await qc.invalidateQueries({
        queryKey: ["available-variants", projectId, modelId, measure.id],
      });
      onVariantCreated?.(created.id);
      closePopover();
    } catch (err: unknown) {
      const raw =
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        (err as { response?: { data?: { detail?: any } } })?.response?.data
          ?.detail;
      let msg: string;
      if (typeof raw === "string") msg = raw;
      else if (raw && typeof raw === "object" && "message" in raw)
        msg = String((raw as { message: unknown }).message);
      else if (err instanceof Error) msg = err.message;
      else msg = t("errors.requestFailed");
      setError(msg);
    } finally {
      setSubmittingKind(null);
    }
  }

  const isVariantRow = measure.variant_kind !== null && measure.variant_kind !== undefined;

  const variants = variantsQuery.data?.variants ?? [];
  const allIneligible =
    !variantsQuery.isLoading &&
    !variantsQuery.isError &&
    variants.length > 0 &&
    variants.every((v) => !v.eligible);
  const firstReason = variants.find((v) => !v.eligible && v.reason)?.reason;

  return (
    <>
      <Tooltip
        title={
          isVariantRow
            ? t("variant.disabledTooltip")
            : t("variant.addTooltip")
        }
      >
        <span>
          <IconButton
            size="small"
            disabled={isVariantRow}
            onClick={(e) => setAnchor(e.currentTarget)}
            aria-label={t("variant.addTooltip")}
          >
            <FunctionsIcon fontSize="small" />
            <AddIcon sx={{ fontSize: 10, ml: -0.5, mt: -0.75 }} />
          </IconButton>
        </span>
      </Tooltip>
      <Popover
        open={Boolean(anchor)}
        anchorEl={anchor}
        onClose={closePopover}
        anchorOrigin={{ vertical: "bottom", horizontal: "left" }}
      >
        <Box sx={{ width: 420, p: 1.5 }}>
          <Typography variant="subtitle2" gutterBottom>
            {t("variant.title", { name: measure.display_name || measure.name })}
          </Typography>
          <Typography variant="caption" color="text.secondary" display="block" sx={{ mb: 1 }}>
            {t("variant.description")}
          </Typography>
          <Divider sx={{ mb: 1 }} />
          {variantsQuery.isLoading && <CircularProgress size={20} />}
          {variantsQuery.isError && (
            <Alert severity="error">{t("variant.failedToLoad")}</Alert>
          )}
          {error && (
            <Alert severity="error" sx={{ mb: 1 }}>
              {error}
            </Alert>
          )}
          {allIneligible && firstReason && (
            <Alert severity="info" sx={{ mb: 1 }}>
              <Typography variant="body2" sx={{ mb: 0.5 }}>
                {t("variant.noEligible")}
              </Typography>
              <Typography variant="caption" display="block">
                {firstReason}
              </Typography>
              <Link
                href={HELP_URL}
                target="_blank"
                rel="noopener"
                variant="caption"
                sx={{ mt: 0.5, display: "inline-block" }}
              >
                {t("variant.seeHelp")}
              </Link>
            </Alert>
          )}
          <List dense disablePadding>
            {variants.map((v) => {
              const exists = Boolean(v.existing_measure_id);
              const disabled = exists || !v.eligible || submittingKind === v.kind;
              const parametric = isParametricVariant(v.kind);
              return (
                <ListItem
                  key={v.kind}
                  disablePadding
                  secondaryAction={
                    parametric && v.eligible && !exists ? (
                      <TextField
                        size="small"
                        placeholder={String(
                          TIME_VARIANT_DEFAULT_N[v.kind] ?? "",
                        )}
                        value={paramN[v.kind] ?? ""}
                        onChange={(e) =>
                          setParamN((prev) => ({
                            ...prev,
                            [v.kind]: e.target.value,
                          }))
                        }
                        sx={{ width: 72 }}
                        inputProps={{ inputMode: "numeric" }}
                      />
                    ) : null
                  }
                >
                  <Tooltip
                    title={v.reason ?? (exists ? t("variant.alreadyCreated") : t("variant.eligibleClick"))}
                    placement="left"
                  >
                    <span style={{ width: "100%" }}>
                      <ListItemButton
                        onClick={() => handleCreate(v)}
                        disabled={disabled}
                      >
                        <ListItemText
                          primary={v.kind}
                          secondary={
                            exists
                              ? t("variant.alreadyAdded")
                              : v.eligible
                                ? v.suggested_name
                                : v.reason ?? t("variant.notEligible")
                          }
                          primaryTypographyProps={{
                            fontFamily: "monospace",
                            fontSize: 13,
                          }}
                          secondaryTypographyProps={{
                            fontSize: 11,
                            fontStyle: exists || !v.eligible ? "italic" : "normal",
                            color: !v.eligible && !exists ? "warning.main" : undefined,
                          }}
                        />
                        {submittingKind === v.kind && (
                          <CircularProgress size={14} sx={{ ml: 1 }} />
                        )}
                      </ListItemButton>
                    </span>
                  </Tooltip>
                </ListItem>
              );
            })}
          </List>
          <Divider sx={{ my: 1 }} />
          <Box sx={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <Link
              href={HELP_URL}
              target="_blank"
              rel="noopener"
              variant="caption"
            >
              {t("variant.helpLink")}
            </Link>
            <Button size="small" onClick={closePopover}>
              {t("variant.close")}
            </Button>
          </Box>
        </Box>
      </Popover>
    </>
  );
}
