import { useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useT } from "../../i18n";
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  MenuItem,
  Stack,
  Switch,
  TextField,
  Typography,
} from "@mui/material";
import AddIcon from "@mui/icons-material/Add";
import DeleteIcon from "@mui/icons-material/Delete";
import EditIcon from "@mui/icons-material/Edit";
import PlayArrowIcon from "@mui/icons-material/PlayArrow";

import { rowSecurityApi } from "../../api/client";
import { useAllModelTables, useDimensions, useRowSecurityRules, useSources } from "../../api/hooks";
import type {
  RowSecurityAttributeSource,
  RowSecurityRule,
  RowSecurityRuleCreate,
  RowSecurityRuleType,
  RowSecurityRuleUpdate,
  RowSecuritySimulateResponse,
} from "../../api/types";
import { canEditModelConfig } from "../../auth/currentUser";
import { useBuilderStore } from "../../store/builderStore";
import { useConfirm } from "../Confirm";

type DialogMode = "create" | "edit";

interface RuleFormState {
  name: string;
  dimension_path: string;
  rule_type: RowSecurityRuleType;
  predicate_expression: string;
  applies_to_roles: string; // comma-separated in the form
  mapping_table_id: string;
  mapping_user_column: string;
  mapping_value_column: string;
  is_enabled: boolean;
  attribute_source: RowSecurityAttributeSource;
  attribute_claim_name: string;
}

const EMPTY_FORM: RuleFormState = {
  name: "",
  dimension_path: "",
  rule_type: "role_predicate",
  predicate_expression: "",
  applies_to_roles: "",
  mapping_table_id: "",
  mapping_user_column: "",
  mapping_value_column: "",
  is_enabled: true,
  attribute_source: "jwt_role",
  attribute_claim_name: "",
};

function fromRule(rule: RowSecurityRule): RuleFormState {
  return {
    name: rule.name,
    dimension_path: rule.dimension_path,
    rule_type: rule.rule_type,
    predicate_expression: rule.predicate_expression ?? "",
    applies_to_roles: (rule.applies_to_roles ?? []).join(", "),
    mapping_table_id: rule.mapping_table_id ?? "",
    mapping_user_column: rule.mapping_user_column ?? "",
    mapping_value_column: rule.mapping_value_column ?? "",
    is_enabled: rule.is_enabled,
    attribute_source: rule.attribute_source ?? "jwt_role",
    attribute_claim_name: rule.attribute_claim_name ?? "",
  };
}

function toCreate(form: RuleFormState): RowSecurityRuleCreate {
  const base = {
    name: form.name.trim(),
    dimension_path: form.dimension_path.trim(),
    rule_type: form.rule_type,
    is_enabled: form.is_enabled,
  };
  if (form.rule_type === "role_predicate") {
    return {
      ...base,
      predicate_expression: form.predicate_expression.trim(),
      applies_to_roles: form.applies_to_roles
        .split(",")
        .map((r) => r.trim())
        .filter(Boolean),
      mapping_table_id: null,
      mapping_user_column: null,
      mapping_value_column: null,
      attribute_source: form.attribute_source,
      attribute_claim_name: form.attribute_claim_name.trim() || null,
    };
  }
  return {
    ...base,
    predicate_expression: null,
    applies_to_roles: null,
    mapping_table_id: form.mapping_table_id.trim() || null,
    mapping_user_column: form.mapping_user_column.trim() || null,
    mapping_value_column: form.mapping_value_column.trim() || null,
  };
}

function toUpdate(form: RuleFormState, original: RowSecurityRule): RowSecurityRuleUpdate {
  const base: RowSecurityRuleUpdate = {
    name: form.name.trim(),
    dimension_path: form.dimension_path.trim(),
    is_enabled: form.is_enabled,
  };
  if (original.rule_type === "role_predicate") {
    base.predicate_expression = form.predicate_expression.trim();
    base.applies_to_roles = form.applies_to_roles
      .split(",")
      .map((r) => r.trim())
      .filter(Boolean);
    base.attribute_source = form.attribute_source;
    base.attribute_claim_name = form.attribute_claim_name.trim() || null;
  } else {
    base.mapping_user_column = form.mapping_user_column.trim() || null;
    base.mapping_value_column = form.mapping_value_column.trim() || null;
  }
  return base;
}

export default function RowSecurityPanel() {
  const t = useT();
  const { projectId, modelId } = useParams<{ projectId: string; modelId: string }>();
  const qc = useQueryClient();
  const confirm = useConfirm();

  const rules = useRowSecurityRules(projectId!, modelId!);
  const storeReadOnly = useBuilderStore((s) => s.readOnly);
  const canManage = canEditModelConfig() && !storeReadOnly;

  // Model-aware pickers for dimension_path and mapping_table_id (Bug-5208)
  const dimensionsQuery = useDimensions(projectId!, modelId!);
  const sourcesQuery = useSources(projectId!, modelId!);
  const sourceIds = (sourcesQuery.data ?? []).map((s) => s.id);
  const allTablesQuery = useAllModelTables(projectId!, modelId!, sourceIds);

  const [dialogOpen, setDialogOpen] = useState(false);
  const [dialogMode, setDialogMode] = useState<DialogMode>("create");
  const [editing, setEditing] = useState<RowSecurityRule | null>(null);
  const [form, setForm] = useState<RuleFormState>(EMPTY_FORM);
  const [formError, setFormError] = useState<string | null>(null);

  const [simulateOpen, setSimulateOpen] = useState(false);
  const [simUser, setSimUser] = useState("");
  const [simRoles, setSimRoles] = useState("");
  // F-007-06: the rule form supports four attribute sources (jwt_role,
  // idp_group, saml_claim, oidc_scope) but Simulate previously only sent
  // roles, so an idp_group / claim rule could never be exercised here.
  const [simGroups, setSimGroups] = useState("");
  const [simClaims, setSimClaims] = useState("");
  const [simResult, setSimResult] = useState<RowSecuritySimulateResponse | null>(null);
  const [simError, setSimError] = useState<string | null>(null);

  // Surface the group/claim inputs only when at least one rule actually
  // uses a non-jwt_role source — otherwise they are noise.
  const usesNonRoleSource = (rules.data ?? []).some(
    (r) => r.attribute_source && r.attribute_source !== "jwt_role",
  );

  const invalidate = () =>
    qc.invalidateQueries({ queryKey: ["row-security", projectId, modelId] });

  const createMutation = useMutation({
    mutationFn: (body: RowSecurityRuleCreate) =>
      rowSecurityApi.create(projectId!, modelId!, body),
    onSuccess: () => {
      invalidate();
      setDialogOpen(false);
      setFormError(null);
    },
    onError: (err: unknown) => {
      setFormError(extractError(err) || t("errors.requestFailed"));
    },
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, body }: { id: string; body: RowSecurityRuleUpdate }) =>
      rowSecurityApi.update(projectId!, modelId!, id, body),
    onSuccess: () => {
      invalidate();
      setDialogOpen(false);
      setFormError(null);
    },
    onError: (err: unknown) => setFormError(extractError(err) || t("errors.requestFailed")),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => rowSecurityApi.delete(projectId!, modelId!, id),
    onSuccess: () => invalidate(),
  });

  const simulateMutation = useMutation({
    mutationFn: () =>
      rowSecurityApi.simulate(projectId!, modelId!, {
        user_identity: simUser.trim(),
        roles: simRoles
          .split(",")
          .map((r) => r.trim())
          .filter(Boolean),
        groups: simGroups
          .split(",")
          .map((g) => g.trim())
          .filter(Boolean),
        claims: parseClaims(simClaims),
      }),
    onSuccess: (data) => {
      setSimResult(data);
      setSimError(null);
    },
    onError: (err: unknown) => {
      setSimResult(null);
      setSimError(extractError(err) || t("errors.requestFailed"));
    },
  });

  function openCreate() {
    setDialogMode("create");
    setEditing(null);
    setForm(EMPTY_FORM);
    setFormError(null);
    setDialogOpen(true);
  }

  function openEdit(rule: RowSecurityRule) {
    setDialogMode("edit");
    setEditing(rule);
    setForm(fromRule(rule));
    setFormError(null);
    setDialogOpen(true);
  }

  function submitForm() {
    setFormError(null);
    if (dialogMode === "create") {
      createMutation.mutate(toCreate(form));
    } else if (editing) {
      updateMutation.mutate({ id: editing.id, body: toUpdate(form, editing) });
    }
  }

  async function handleDelete(rule: RowSecurityRule) {
    const ok = await confirm({
      title: t("rowSecurity.deleteConfirmTitle"),
      message: (
        <span>
          {t("rowSecurity.deleteConfirmMessage1")} <strong>{rule.name}</strong>? {t("rowSecurity.deleteConfirmMessage2")}
        </span>
      ),
      confirmLabel: t("rowSecurity.deleteRuleLabel"),
    });
    if (ok) deleteMutation.mutate(rule.id);
  }

  const busy = createMutation.isPending || updateMutation.isPending;

  return (
    <Box>
      <Box display="flex" gap={1} mb={1.5}>
        <Box flexGrow={1} />
        <Button
          size="small"
          startIcon={<PlayArrowIcon />}
          onClick={() => {
            setSimulateOpen(true);
            setSimResult(null);
            setSimError(null);
            setSimGroups("");
            setSimClaims("");
          }}
        >
          {t("rowSecurity.simulateAsUser")}
        </Button>
        {canManage && (
          <Button
            size="small"
            variant="contained"
            startIcon={<AddIcon />}
            onClick={openCreate}
          >
            {t("rowSecurity.newRule")}
          </Button>
        )}
      </Box>

      <Alert severity="info" sx={{ mb: 1 }}>
        {t("rowSecurity.infoAlert")}
      </Alert>
      {!canManage && (
        <Alert severity="warning" sx={{ mb: 1 }}>
          {t("rowSecurity.readOnlyAlert")}
        </Alert>
      )}

      {rules.isLoading ? (
        <CircularProgress size={20} />
      ) : (rules.data?.length ?? 0) === 0 ? (
        <Typography variant="body2" color="text.secondary">
          {t("rowSecurity.noRulesDefined")}
        </Typography>
      ) : (
        <Stack spacing={1}>
          {(rules.data ?? []).map((rule) => (
            <Card key={rule.id} variant="outlined">
              <CardContent sx={{ py: 1 }}>
                <Box display="flex" alignItems="center" gap={1}>
                  <Box flexGrow={1}>
                    <Typography variant="body2" fontWeight={600}>
                      {rule.name}
                    </Typography>
                    <Typography variant="caption" color="text.secondary">
                      {rule.dimension_path}
                    </Typography>
                  </Box>
                  <Chip
                    label={rule.rule_type}
                    size="small"
                    variant="outlined"
                  />
                  <Chip
                    label={rule.is_enabled ? t("rowSecurity.enabled") : t("rowSecurity.disabled")}
                    size="small"
                    color={rule.is_enabled ? "success" : "default"}
                  />
                  <Button
                    size="small"
                    startIcon={<EditIcon />}
                    onClick={() => openEdit(rule)}
                    disabled={!canManage}
                  >
                    {t("rowSecurity.edit")}
                  </Button>
                  <Button
                    size="small"
                    startIcon={<DeleteIcon />}
                    onClick={() => void handleDelete(rule)}
                    disabled={!canManage || deleteMutation.isPending}
                  >
                    {t("rowSecurity.delete")}
                  </Button>
                </Box>

                {rule.rule_type === "role_predicate" ? (
                  <Box mt={0.5}>
                    <Typography variant="caption" display="block">
                      {rule.predicate_expression}
                    </Typography>
                    <Box display="flex" flexWrap="wrap" gap={0.5} mt={0.5}>
                      {(rule.applies_to_roles ?? []).map((r) => (
                        <Chip key={r} label={r} size="small" variant="outlined" />
                      ))}
                      {rule.attribute_source && rule.attribute_source !== "jwt_role" && (
                        <Chip
                          label={`via ${rule.attribute_source}${rule.attribute_claim_name ? `:${rule.attribute_claim_name}` : ""}`}
                          size="small"
                          color="info"
                          variant="outlined"
                        />
                      )}
                    </Box>
                  </Box>
                ) : (
                  <Typography variant="caption" display="block" mt={0.5}>
                    {t("rowSecurity.mapLabel")}: {rule.mapping_user_column} → {rule.mapping_value_column}
                  </Typography>
                )}
              </CardContent>
            </Card>
          ))}
        </Stack>
      )}

      {/* Create / edit dialog */}
      <Dialog
        open={dialogOpen}
        onClose={() => setDialogOpen(false)}
        maxWidth="sm"
        fullWidth
      >
        <DialogTitle>
          {dialogMode === "create" ? t("rowSecurity.newRuleTitle") : t("rowSecurity.editRuleTitle")}
        </DialogTitle>
        <DialogContent>
          <Stack spacing={2} mt={0.5}>
            <TextField
              label={t("rowSecurity.nameLabel")}
              size="small"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
            />
            <TextField
              select
              label={t("rowSecurity.dimensionPathLabel")}
              size="small"
              value={form.dimension_path}
              onChange={(e) =>
                setForm({ ...form, dimension_path: e.target.value })
              }
              helperText={t("rowSecurity.dimensionPathHelperText")}
            >
              {(dimensionsQuery.data ?? []).map((d) => (
                <MenuItem key={d.id} value={d.name}>
                  {d.display_name || d.name}
                </MenuItem>
              ))}
            </TextField>
            <TextField
              select
              label={t("rowSecurity.ruleTypeLabel")}
              size="small"
              value={form.rule_type}
              disabled={dialogMode === "edit"}
              onChange={(e) =>
                setForm({
                  ...form,
                  rule_type: e.target.value as RowSecurityRuleType,
                })
              }
              helperText={
                dialogMode === "edit"
                  ? t("rowSecurity.ruleTypeCannotChange")
                  : undefined
              }
            >
              <MenuItem value="role_predicate">{t("rowSecurity.rolePredicate")}</MenuItem>
              <MenuItem value="user_mapping">{t("rowSecurity.userMapping")}</MenuItem>
            </TextField>

            {form.rule_type === "role_predicate" ? (
              <>
                <TextField
                  label={t("rowSecurity.predicateLabel")}
                  size="small"
                  multiline
                  minRows={2}
                  value={form.predicate_expression}
                  placeholder={t("rowSecurity.predicatePlaceholder")}
                  onChange={(e) =>
                    setForm({ ...form, predicate_expression: e.target.value })
                  }
                  helperText={t("rowSecurity.predicateHelperText")}
                />
                <TextField
                  label={t("rowSecurity.appliesToRolesLabel")}
                  size="small"
                  value={form.applies_to_roles}
                  onChange={(e) =>
                    setForm({ ...form, applies_to_roles: e.target.value })
                  }
                />
                <TextField
                  select
                  label={t("rowSecurity.attributeSourceLabel")}
                  size="small"
                  value={form.attribute_source}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      attribute_source: e.target.value as RowSecurityAttributeSource,
                      attribute_claim_name: "",
                    })
                  }
                  helperText={t("rowSecurity.attributeSourceHelperText")}
                >
                  <MenuItem value="jwt_role">{t("rowSecurity.jwtRoleOption")}</MenuItem>
                  <MenuItem value="idp_group">{t("rowSecurity.idpGroupOption")}</MenuItem>
                  <MenuItem value="saml_claim">{t("rowSecurity.samlClaimOption")}</MenuItem>
                  <MenuItem value="oidc_scope">{t("rowSecurity.oidcScopeOption")}</MenuItem>
                </TextField>
                {(form.attribute_source === "saml_claim" ||
                  form.attribute_source === "oidc_scope") && (
                  <TextField
                    label={t("rowSecurity.claimNameLabel")}
                    size="small"
                    value={form.attribute_claim_name}
                    onChange={(e) =>
                      setForm({ ...form, attribute_claim_name: e.target.value })
                    }
                    helperText={t("rowSecurity.claimNameHelperText")}
                  />
                )}
              </>
            ) : (
              <>
                <TextField
                  select
                  label={t("rowSecurity.mappingTableIdLabel")}
                  size="small"
                  value={form.mapping_table_id}
                  disabled={dialogMode === "edit"}
                  onChange={(e) =>
                    setForm({ ...form, mapping_table_id: e.target.value })
                  }
                  helperText={
                    dialogMode === "edit"
                      ? t("rowSecurity.mappingTableCannotChange")
                      : t("rowSecurity.mappingTableIdHelperText")
                  }
                >
                  {(allTablesQuery.data ?? []).map((tbl) => (
                    <MenuItem key={tbl.id} value={tbl.id}>
                      {tbl.alias ?? tbl.display_name} ({tbl.physical_name})
                    </MenuItem>
                  ))}
                </TextField>
                <TextField
                  label={t("rowSecurity.userColumnLabel")}
                  size="small"
                  value={form.mapping_user_column}
                  onChange={(e) =>
                    setForm({ ...form, mapping_user_column: e.target.value })
                  }
                />
                <TextField
                  label={t("rowSecurity.valueColumnLabel")}
                  size="small"
                  value={form.mapping_value_column}
                  onChange={(e) =>
                    setForm({ ...form, mapping_value_column: e.target.value })
                  }
                />
              </>
            )}

            <Box display="flex" alignItems="center" gap={1}>
              <Switch
                size="small"
                checked={form.is_enabled}
                onChange={(_, v) => setForm({ ...form, is_enabled: v })}
              />
              <Typography variant="body2">{t("rowSecurity.enabledLabel")}</Typography>
            </Box>

            {formError && <Alert severity="error">{formError}</Alert>}
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDialogOpen(false)}>{t("rowSecurity.cancel")}</Button>
          <Button variant="contained" onClick={submitForm} disabled={busy}>
            {dialogMode === "create" ? t("rowSecurity.create") : t("rowSecurity.save")}
          </Button>
        </DialogActions>
      </Dialog>

      {/* Simulate-as-user dialog */}
      <Dialog
        open={simulateOpen}
        onClose={() => setSimulateOpen(false)}
        maxWidth="sm"
        fullWidth
      >
        <DialogTitle>{t("rowSecurity.simulateDialogTitle")}</DialogTitle>
        <DialogContent>
          <Stack spacing={2} mt={0.5}>
            <TextField
              label={t("rowSecurity.userIdentityLabel")}
              size="small"
              value={simUser}
              onChange={(e) => setSimUser(e.target.value)}
            />
            <TextField
              label={t("rowSecurity.rolesLabel")}
              size="small"
              value={simRoles}
              onChange={(e) => setSimRoles(e.target.value)}
            />
            {usesNonRoleSource && (
              <>
                <TextField
                  label={t("rowSecurity.simGroupsLabel")}
                  size="small"
                  value={simGroups}
                  onChange={(e) => setSimGroups(e.target.value)}
                  helperText={t("rowSecurity.simGroupsHelperText")}
                />
                <TextField
                  label={t("rowSecurity.simClaimsLabel")}
                  size="small"
                  multiline
                  minRows={2}
                  value={simClaims}
                  placeholder={t("rowSecurity.simClaimsPlaceholder")}
                  onChange={(e) => setSimClaims(e.target.value)}
                  helperText={t("rowSecurity.simClaimsHelperText")}
                />
              </>
            )}
            <Button
              variant="outlined"
              onClick={() => simulateMutation.mutate()}
              disabled={simulateMutation.isPending || !simUser.trim()}
            >
              {t("rowSecurity.preview")}
            </Button>
            {simError && <Alert severity="error">{simError}</Alert>}
            {simResult && (
              <Box>
                <Typography variant="subtitle2">
                  {t("rowSecurity.activeRulesLabel", { count: String(simResult.active_rule_ids.length) })}
                </Typography>
                <Box
                  component="pre"
                  sx={{
                    bgcolor: "grey.100",
                    p: 1,
                    fontSize: 12,
                    overflow: "auto",
                    whiteSpace: "pre-wrap",
                  }}
                >
                  {simResult.compiled_predicate ??
                    t("rowSecurity.noRulesMatchMessage")}
                </Box>
              </Box>
            )}
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setSimulateOpen(false)}>{t("rowSecurity.close")}</Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}

// F-007-06: parse the Simulate claims textarea (one `key=value` per line)
// into the claims map the backend resolves saml_claim / oidc_scope rules
// against. A value containing spaces (e.g. an OAuth scope string
// "openid reports:read") is preserved verbatim; the backend splits scope
// strings itself.
function parseClaims(raw: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of raw.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const eq = trimmed.indexOf("=");
    if (eq <= 0) continue;
    const key = trimmed.slice(0, eq).trim();
    const value = trimmed.slice(eq + 1).trim();
    if (key) out[key] = value;
  }
  return out;
}

function extractError(err: unknown): string {
  const anyErr = err as {
    response?: { data?: { detail?: unknown } };
    message?: string;
  };
  const detail = anyErr?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") return JSON.stringify(detail);
  return anyErr?.message ?? "Request failed";
}
