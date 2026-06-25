import { useMemo, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Checkbox,
  CircularProgress,
  Collapse,
  FormControlLabel,
  MenuItem,
  Radio,
  RadioGroup,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  type ProjectBundle,
  type ProjectImportPlan,
  type ProjectImportRequest,
  projectImportExportApi,
} from "../../../api/importExportApi";
import { useConnections, useProjects } from "../../../api/hooks";
import { useConfirm } from "../../Confirm";
import { useT } from "../../../i18n";
import { readFileAsText } from "../helpers";

type Props = {
  onImported?: () => void;
};

export default function ProjectImportPanel({ onImported }: Props) {
  const t = useT();
  const qc = useQueryClient();
  const confirm = useConfirm();
  const [bundle, setBundle] = useState<ProjectBundle | null>(null);
  const [parseError, setParseError] = useState<string | null>(null);
  const [mode, setMode] = useState<"create" | "replace">("create");
  const [passphrase, setPassphrase] = useState("");
  const [projectSlug, setProjectSlug] = useState("");
  const [projectName, setProjectName] = useState("");
  const [modelSlugs, setModelSlugs] = useState<Record<string, string>>({});
  const [connMapping, setConnMapping] = useState<Record<string, string>>({});
  const [overrideConns, setOverrideConns] = useState(false);
  const [showModelSlugs, setShowModelSlugs] = useState(false);
  const [plan, setPlan] = useState<ProjectImportPlan | null>(null);
  const [importResult, setImportResult] = useState<{
    warnings: string[];
    models_requiring_deploy: string[];
    post_import_actions: string[];
  } | null>(null);

  const bundleConns = useMemo(() => bundle?.connections ?? [], [bundle]);
  const { data: allProjects } = useProjects();
  const targetProject = useMemo(() => {
    if (mode !== "replace" || !allProjects || !bundle) return null;
    const slug = projectSlug || bundle.project.slug;
    return allProjects.find((p) => p.slug === slug) ?? null;
  }, [mode, allProjects, bundle, projectSlug]);
  const { data: targetConnections } = useConnections(targetProject?.id ?? "");

  const hasCreds = bundle?.credentials_included ?? false;
  const needsPassphrase = hasCreds && !passphrase;

  async function handleFile(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    if (!f) return;
    try {
      const text = await readFileAsText(f);
      const parsed = JSON.parse(text) as ProjectBundle;
      if (parsed.export_format !== "tessallite-project/v1") {
        throw new Error(t("importDialog.unrecognizedFormat", { format: parsed.export_format }));
      }
      setBundle(parsed);
      setParseError(null);
      setPlan(null);
      setProjectSlug(parsed.project.slug);
      setProjectName(parsed.project.display_name);
      const slugMap: Record<string, string> = {};
      for (const m of parsed.models || []) {
        const slug = (m as Record<string, Record<string, string>>).model?.slug;
        if (slug) slugMap[slug] = slug;
      }
      setModelSlugs(slugMap);
    } catch (err) {
      setBundle(null);
      setParseError(err instanceof Error ? err.message : String(err));
    }
  }

  function buildRequest(dryRun: boolean): ProjectImportRequest {
    if (!bundle) throw new Error("No bundle loaded");
    return {
      bundle,
      // A dry-run never decrypts credentials, so the passphrase is omitted.
      passphrase: hasCreds && !dryRun ? passphrase : null,
      mode,
      dry_run: dryRun,
      project_slug: projectSlug || null,
      project_display_name: projectName || null,
      model_slugs: Object.keys(modelSlugs).length > 0 ? modelSlugs : null,
      connection_mapping:
        Object.keys(connMapping).length > 0 ? connMapping : null,
      override_connections: overrideConns,
    };
  }

  // Drop a stale plan whenever an input that changes the plan is edited, so the
  // user must re-preview before importing and never confirms against an old plan.
  function invalidatePlan() {
    setPlan(null);
  }

  const previewMut = useMutation({
    mutationFn: async () =>
      projectImportExportApi.importProject(buildRequest(true)),
    onSuccess: (resp) => {
      setPlan(resp.plan ?? null);
    },
  });

  const importMut = useMutation({
    mutationFn: async () =>
      projectImportExportApi.importProject(buildRequest(false)),
    onSuccess: (resp) => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      setImportResult({
        warnings: resp.warnings,
        models_requiring_deploy: resp.models_requiring_deploy,
        post_import_actions: resp.post_import_actions,
      });
      onImported?.();
    },
  });

  const canPreview =
    Boolean(bundle) && !previewMut.isPending && !importMut.isPending;
  // The plan must be shown before the actual import can be confirmed.
  const canSubmit =
    Boolean(bundle) &&
    !needsPassphrase &&
    plan !== null &&
    !importMut.isPending &&
    !previewMut.isPending;

  async function handleImport() {
    if (mode === "replace") {
      const ok = await confirm({
        title: t("importDialog.replaceProjectTitle"),
        message: t("importDialog.replaceProjectMessage"),
        confirmLabel: t("importDialog.replaceLabel"),
        destructive: true,
      });
      if (!ok) return;
    }
    importMut.mutate();
  }

  if (importResult) {
    return (
      <Box>
        <Alert severity="success" sx={{ mb: 2 }}>
          {t("importDialog.projectImported")}
        </Alert>
        {importResult.models_requiring_deploy.length > 0 && (
          <Alert severity="info" sx={{ mb: 1 }}>
            {t("importDialog.modelsRequiringDeploy", {
              models: importResult.models_requiring_deploy.join(", "),
            })}
          </Alert>
        )}
        {importResult.post_import_actions.includes("refresh_aggregates") && (
          <Alert severity="info" sx={{ mb: 1 }}>
            {t("importDialog.aggregatesNeedRefresh")}
          </Alert>
        )}
        {importResult.warnings.map((w, i) => (
          <Alert key={i} severity="warning" sx={{ mb: 1 }}>
            {w}
          </Alert>
        ))}
      </Box>
    );
  }

  return (
    <Stack spacing={2}>
      <Typography variant="body2" color="text.secondary">
        {t("importDialog.projectDescription")}
      </Typography>
      <Button variant="outlined" component="label">
        {t("importDialog.chooseProjectFile")}
        <input
          type="file"
          accept="application/json,.json"
          hidden
          onChange={handleFile}
        />
      </Button>
      {parseError && <Alert severity="error">{parseError}</Alert>}

      {bundle && (
        <>
          <Typography variant="subtitle2">{t("importDialog.bundleSummary")}</Typography>
          <Typography variant="body2" color="text.secondary">
            {t("importDialog.bundleDetails", {
              name: bundle.project.display_name,
              slug: bundle.project.slug,
              models: String(bundle.models?.length ?? 0),
              sections: bundle.included_sections?.join(", ") || t("importDialog.none"),
              creds: hasCreds ? t("common.yes") : t("common.no"),
            })}
          </Typography>

          {hasCreds && (
            <TextField
              label={t("importDialog.passphraseLabel")}
              type="password"
              fullWidth
              value={passphrase}
              onChange={(e) => setPassphrase(e.target.value)}
              helperText={t("importDialog.passphraseHelp")}
            />
          )}

          <Typography variant="subtitle2">{t("importDialog.importMode")}</Typography>
          <RadioGroup
            value={mode}
            onChange={(e) => {
              setMode(e.target.value as "create" | "replace");
              invalidatePlan();
            }}
          >
            <FormControlLabel
              value="create"
              control={<Radio />}
              label={t("importDialog.createNewProject")}
            />
            <FormControlLabel
              value="replace"
              control={<Radio />}
              label={t("importDialog.replaceProject")}
            />
          </RadioGroup>

          <TextField
            label={t("importDialog.projectSlugLabel")}
            fullWidth
            value={projectSlug}
            onChange={(e) => {
              setProjectSlug(e.target.value);
              invalidatePlan();
            }}
            helperText={
              mode === "create"
                ? t("importDialog.projectSlugHelp.create")
                : t("importDialog.projectSlugHelp.replace")
            }
          />
          <TextField
            label={t("importDialog.projectDisplayName")}
            fullWidth
            value={projectName}
            onChange={(e) => {
              setProjectName(e.target.value);
              invalidatePlan();
            }}
          />

          <Button
            size="small"
            onClick={() => setShowModelSlugs(!showModelSlugs)}
          >
            {showModelSlugs ? t("importDialog.hideModelSlugs") : t("importDialog.showModelSlugs")}
          </Button>
          <Collapse in={showModelSlugs}>
            <Stack spacing={1}>
              {Object.keys(modelSlugs).map((origSlug) => (
                <TextField
                  key={origSlug}
                  label={t("importDialog.modelLabel", { slug: origSlug })}
                  size="small"
                  value={modelSlugs[origSlug]}
                  onChange={(e) => {
                    setModelSlugs((prev) => ({
                      ...prev,
                      [origSlug]: e.target.value,
                    }));
                    invalidatePlan();
                  }}
                />
              ))}
            </Stack>
          </Collapse>

          {bundleConns.length > 0 && (
            <>
              <Typography variant="subtitle2">{t("importDialog.connectionMapping")}</Typography>
              <Typography variant="body2" color="text.secondary">
                {hasCreds
                  ? t("importDialog.connMappingWithCreds")
                  : t("importDialog.connMappingNoCreds")}
              </Typography>
              {bundleConns.map((ec) => (
                <Box
                  key={ec.id}
                  sx={{
                    display: "grid",
                    gridTemplateColumns: "1fr 1fr",
                    gap: 1,
                  }}
                >
                  <Typography variant="body2" sx={{ alignSelf: "center" }}>
                    {ec.display_name} ({ec.connection_type})
                  </Typography>
                  <TextField
                    select
                    size="small"
                    value={
                      connMapping[ec.id] || (hasCreds ? "__create_new__" : "")
                    }
                    onChange={(e) => {
                      const val = e.target.value;
                      if (val === "__create_new__") {
                        setConnMapping((prev) => {
                          const next = { ...prev };
                          delete next[ec.id];
                          return next;
                        });
                      } else {
                        setConnMapping((prev) => ({ ...prev, [ec.id]: val }));
                      }
                      invalidatePlan();
                    }}
                  >
                    {hasCreds && (
                      <MenuItem value="__create_new__">{t("importDialog.createNew")}</MenuItem>
                    )}
                    {(targetConnections ?? []).length === 0 && (
                      <MenuItem value="" disabled>
                        {mode === "replace"
                          ? t("importDialog.noConnInTarget")
                          : t("importDialog.connAvailableAfter")}
                      </MenuItem>
                    )}
                    {(targetConnections ?? []).map((tc) => (
                      <MenuItem key={tc.id} value={tc.id}>
                        {tc.display_name} ({tc.connection_type})
                      </MenuItem>
                    ))}
                  </TextField>
                </Box>
              ))}

              {hasCreds && (
                <FormControlLabel
                  control={
                    <Checkbox
                      checked={overrideConns}
                      onChange={(_, v) => {
                        setOverrideConns(v);
                        invalidatePlan();
                      }}
                    />
                  }
                  label={t("importDialog.overrideConns")}
                />
              )}
            </>
          )}

          {bundle.test_metadata && (
            <Alert severity="info">
              {t("importDialog.testMetadata", {
                count: String(
                  (bundle.test_metadata as Record<string, unknown[]>)
                    ?.expected_query_results?.length ?? 0
                ),
              })}
            </Alert>
          )}

          {previewMut.isError && (
            <Alert severity="error">
              {(previewMut.error as Error)?.message ||
                t("importDialog.previewError")}
            </Alert>
          )}

          {plan && <ImportPlanPreview plan={plan} t={t} />}

          {importMut.isError && (
            <Alert severity="error">
              {(importMut.error as Error)?.message || t("importDialog.importError")}
            </Alert>
          )}

          <Box display="flex" justifyContent="flex-end" gap={1} pt={1}>
            <Button
              variant="outlined"
              disabled={!canPreview}
              onClick={() => previewMut.mutate()}
            >
              {previewMut.isPending ? (
                <CircularProgress size={18} color="inherit" />
              ) : (
                t("importDialog.previewButton")
              )}
            </Button>
            <Button
              variant="contained"
              disabled={!canSubmit}
              onClick={handleImport}
            >
              {importMut.isPending ? (
                <CircularProgress size={18} color="inherit" />
              ) : (
                t("importDialog.importButton")
              )}
            </Button>
          </Box>
          {bundle && !plan && (
            <Typography variant="caption" color="text.secondary">
              {t("importDialog.previewRequired")}
            </Typography>
          )}
        </>
      )}
    </Stack>
  );
}

type PlanPreviewProps = {
  plan: ProjectImportPlan;
  t: (key: string, vars?: Record<string, string | number>) => string;
};

/**
 * Bug-5269 / Bug-4263: render the dry-run plan so the user sees what an import
 * will create or replace — and, for a replace, the true cascade row volume
 * (aggregates, pockets, query/route logs) that the replace deletes — before
 * they confirm the actual import.
 */
function ImportPlanPreview({ plan, t }: PlanPreviewProps) {
  const isReplace = plan.will_replace_project;
  const cascade = plan.model_cascade_counts;
  const totals = cascade?.totals ?? {};
  const cascadeTotal = Object.values(totals).reduce(
    (sum, n) => sum + (n ?? 0),
    0,
  );

  const incomingEntries = Object.entries(plan.incoming_counts || {}).filter(
    ([, n]) => n > 0,
  );
  const deleteEntries = Object.entries(plan.delete_counts || {}).filter(
    ([, n]) => n > 0,
  );

  return (
    <Box
      sx={{
        border: 1,
        borderColor: "divider",
        borderRadius: 1,
        p: 2,
      }}
    >
      <Typography variant="subtitle2" gutterBottom>
        {t("importDialog.planTitle")}
      </Typography>

      <Alert severity={isReplace ? "warning" : "info"} sx={{ mb: 2 }}>
        {isReplace
          ? t("importDialog.planReplaceSummary", {
              slug: plan.target_project_slug,
            })
          : t("importDialog.planCreateSummary", {
              slug: plan.target_project_slug,
            })}
      </Alert>

      <Typography variant="body2" sx={{ fontWeight: 600 }}>
        {t("importDialog.planCreatesHeading")}
      </Typography>
      {incomingEntries.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          {t("importDialog.planNothing")}
        </Typography>
      ) : (
        <Stack component="ul" sx={{ pl: 3, my: 0.5 }} spacing={0}>
          {incomingEntries.map(([key, n]) => (
            <Typography component="li" variant="body2" key={key}>
              {t("importDialog.planCountLine", { entity: key, count: n })}
            </Typography>
          ))}
        </Stack>
      )}

      {isReplace && (
        <>
          <Typography variant="body2" sx={{ fontWeight: 600, mt: 1.5 }}>
            {t("importDialog.planDeletesHeading")}
          </Typography>
          {deleteEntries.length === 0 ? (
            <Typography variant="body2" color="text.secondary">
              {t("importDialog.planNothing")}
            </Typography>
          ) : (
            <Stack component="ul" sx={{ pl: 3, my: 0.5 }} spacing={0}>
              {deleteEntries.map(([key, n]) => (
                <Typography component="li" variant="body2" key={key}>
                  {t("importDialog.planCountLine", { entity: key, count: n })}
                </Typography>
              ))}
            </Stack>
          )}

          <Typography variant="body2" sx={{ fontWeight: 600, mt: 1.5 }}>
            {t("importDialog.planCascadeHeading")}
          </Typography>
          {cascadeTotal === 0 ? (
            <Typography variant="body2" color="text.secondary">
              {t("importDialog.planNoCascade")}
            </Typography>
          ) : (
            <>
              <Typography variant="body2" color="text.secondary">
                {t("importDialog.planCascadeTotals", {
                  aggregates: totals.aggregates ?? 0,
                  pockets: totals.pockets ?? 0,
                  query_logs: totals.query_logs ?? 0,
                  query_miss_logs: totals.query_miss_logs ?? 0,
                  route_logs: totals.route_logs ?? 0,
                })}
              </Typography>
              <Stack component="ul" sx={{ pl: 3, my: 0.5 }} spacing={0}>
                {(cascade?.per_model ?? [])
                  .filter(
                    (m) =>
                      m.counts.aggregates +
                        m.counts.pockets +
                        m.counts.query_logs +
                        m.counts.query_miss_logs +
                        m.counts.route_logs >
                      0,
                  )
                  .map((m) => (
                    <Typography component="li" variant="body2" key={m.model_id}>
                      {t("importDialog.planCascadeModelLine", {
                        model: m.display_name || m.slug,
                        aggregates: m.counts.aggregates,
                        pockets: m.counts.pockets,
                        logs:
                          m.counts.query_logs +
                          m.counts.query_miss_logs +
                          m.counts.route_logs,
                      })}
                    </Typography>
                  ))}
              </Stack>
            </>
          )}
        </>
      )}

      {plan.connection_actions.length > 0 && (
        <>
          <Typography variant="body2" sx={{ fontWeight: 600, mt: 1.5 }}>
            {t("importDialog.planConnectionsHeading")}
          </Typography>
          <Stack component="ul" sx={{ pl: 3, my: 0.5 }} spacing={0}>
            {plan.connection_actions.map((ca) => (
              <Typography
                component="li"
                variant="body2"
                key={ca.export_connection_id}
              >
                {t("importDialog.planConnectionLine", {
                  name: ca.display_name || ca.export_connection_id,
                  action: ca.action,
                })}
              </Typography>
            ))}
          </Stack>
        </>
      )}

      {plan.warnings.length > 0 &&
        plan.warnings.map((w, i) => (
          <Alert key={i} severity="warning" sx={{ mt: 1 }}>
            {w}
          </Alert>
        ))}
    </Box>
  );
}
