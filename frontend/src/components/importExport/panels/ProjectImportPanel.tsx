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

  const importMut = useMutation({
    mutationFn: async () => {
      if (!bundle) throw new Error("No bundle loaded");
      const body: ProjectImportRequest = {
        bundle,
        passphrase: hasCreds ? passphrase : null,
        mode,
        project_slug: projectSlug || null,
        project_display_name: projectName || null,
        model_slugs: Object.keys(modelSlugs).length > 0 ? modelSlugs : null,
        connection_mapping:
          Object.keys(connMapping).length > 0 ? connMapping : null,
        override_connections: overrideConns,
      };
      return projectImportExportApi.importProject(body);
    },
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

  const canSubmit = Boolean(bundle) && !needsPassphrase && !importMut.isPending;

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
            onChange={(e) => setMode(e.target.value as "create" | "replace")}
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
            onChange={(e) => setProjectSlug(e.target.value)}
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
            onChange={(e) => setProjectName(e.target.value)}
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
                  onChange={(e) =>
                    setModelSlugs((prev) => ({
                      ...prev,
                      [origSlug]: e.target.value,
                    }))
                  }
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
                      onChange={(_, v) => setOverrideConns(v)}
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

          {importMut.isError && (
            <Alert severity="error">
              {(importMut.error as Error)?.message || t("importDialog.importError")}
            </Alert>
          )}

          <Box display="flex" justifyContent="flex-end" pt={1}>
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
        </>
      )}
    </Stack>
  );
}
