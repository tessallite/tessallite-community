import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
  FormControl,
  FormControlLabel,
  IconButton,
  InputLabel,
  MenuItem,
  Radio,
  RadioGroup,
  Select,
  Link,
  Stack,
  Tooltip,
  Typography,
} from "@mui/material";
import AddIcon from "@mui/icons-material/Add";
import DeleteIcon from "@mui/icons-material/Delete";
import EditIcon from "@mui/icons-material/Edit";
import { joinPopulationHealthApi, joinsApi, tableAttributesApi } from "../../api/client";
import { useAllModelTables, useJoins, useSources } from "../../api/hooks";
import type {
  JoinCardinality,
  JoinCreate,
  JoinPopulationHealthItem,
  ModelTable,
  PopulationParticipation,
  TableAttribute,
} from "../../api/types";
import { useBuilderStore } from "../../store/builderStore";
import { useConfirm } from "../Confirm";
import { ui } from "../../theme/tokens";

/* ------------------------------------------------------------------ */
/* Join type helpers                                                    */
/* ------------------------------------------------------------------ */
function getJoinTypes(t: (key: string) => string): Array<{ value: JoinCreate["join_type"]; label: string; description: string }> {
  return [
    { value: "inner", label: t("joins.typeInner"), description: t("joins.typeInnerDesc") },
    { value: "left", label: t("joins.typeLeft"), description: t("joins.typeLeftDesc") },
    { value: "right", label: t("joins.typeRight"), description: t("joins.typeRightDesc") },
    { value: "full", label: t("joins.typeFull"), description: t("joins.typeFullDesc") },
  ];
}

const CANONICAL_JOIN_TYPES = new Set<JoinCreate["join_type"]>([
  "inner", "left", "right", "full",
]);

function normaliseJoinTypeForEditor(value: string): JoinCreate["join_type"] {
  const token = value.trim().toLowerCase() as JoinCreate["join_type"];
  return CANONICAL_JOIN_TYPES.has(token) ? token : "left";
}

/**
 * Join CARDINALITY options. Separate from the join type: the join type says
 * which rows survive, the cardinality says how many rows on each side match.
 * Cardinality never changes the generated SQL — it records fan-out so the
 * router can reason about whether joining a table can duplicate rows.
 * "" means undeclared.
 */
function getJoinCardinalities(
  t: (key: string) => string,
): Array<{ value: JoinCardinality | ""; label: string }> {
  return [
    { value: "", label: t("joins.cardinalityUndeclared") },
    { value: "many_to_one", label: t("joins.cardinalityManyToOne") },
    { value: "one_to_many", label: t("joins.cardinalityOneToMany") },
    { value: "one_to_one", label: t("joins.cardinalityOneToOne") },
    { value: "many_to_many", label: t("joins.cardinalityManyToMany") },
  ];
}

/**
 * Join POPULATION PARTICIPATION options (Bug-8615, governance phase G2).
 * Independent of both join type and cardinality: it declares whether the
 * modeller INTENDS this join's row-filtering/row-multiplying effect to be
 * part of what the model means. Unlike cardinality, this field is never
 * "undeclared by omission" on write — the backend always stores one of the
 * four states, defaulting new/untouched joins to "preserve_base_rows" so
 * introducing the field changes no served numbers.
 * docs/architecture/architecture_join-population-governance.md contract 2.
 */
const DEFAULT_POPULATION_PARTICIPATION: PopulationParticipation = "preserve_base_rows";

function getPopulationParticipations(
  t: (key: string) => string,
): Array<{ value: PopulationParticipation; label: string; description: string }> {
  return [
    {
      value: "preserve_base_rows",
      label: t("joins.populationPreserveBaseRows"),
      description: t("joins.populationPreserveBaseRowsDesc"),
    },
    {
      value: "population_defining",
      label: t("joins.populationDefining"),
      description: t("joins.populationDefiningDesc"),
    },
    {
      value: "enrichment_only",
      label: t("joins.populationEnrichmentOnly"),
      description: t("joins.populationEnrichmentOnlyDesc"),
    },
    {
      value: "undeclared",
      label: t("joins.populationUndeclared"),
      description: t("joins.populationUndeclaredDesc"),
    },
  ];
}

/**
 * Determine which join types are allowed and what the default should be
 * when joining a fact table to a dimension table.
 *
 * Rule: outer joins should only allow NULLs on the fact side, because
 * dimension keys should always resolve. So:
 * - If left=fact, right=dim  => allow inner + left (fact side may have NULLs)
 * - If left=dim, right=fact  => allow inner + right (fact side may have NULLs)
 * - Otherwise (fact-fact, dim-dim) => allow all types
 */
function getJoinConstraints(leftTable: ModelTable | undefined, rightTable: ModelTable | undefined, t: (key: string) => string) {
  const leftIsFact = leftTable?.table_type === "fact";
  const rightIsFact = rightTable?.table_type === "fact";
  const leftIsDim = leftTable?.table_type?.startsWith("dim") ?? false;
  const rightIsDim = rightTable?.table_type?.startsWith("dim") ?? false;

  if (leftIsFact && rightIsDim) {
    return {
      allowed: new Set<string>(["inner", "left"]),
      hint: t("joins.hintLeftOuter"),
    };
  }
  if (leftIsDim && rightIsFact) {
    return {
      allowed: new Set<string>(["inner", "right"]),
      hint: t("joins.hintRightOuter"),
    };
  }
  return { allowed: new Set<string>(["inner", "left", "right", "full"]), hint: null };
}

/* ------------------------------------------------------------------ */
/* Column selector — reads synced table attributes from model metadata */
/* ------------------------------------------------------------------ */
function useTableColumns(projectId: string, modelId: string, table: ModelTable | undefined) {
  return useQuery({
    queryKey: ["tableAttributes", projectId, modelId, table?.id],
    queryFn: () => tableAttributesApi.list(projectId, modelId, table!.id),
    enabled: !!projectId && !!modelId && !!table?.id,
    staleTime: 20 * 1000,
  });
}

/**
 * Bug-8504 / R2 review: a refusal is not a failure.
 *
 * The read-only guards below throw so the request is never issued, but the
 * mutation's error surface must be able to tell "the session declined this"
 * apart from "the server rejected this", or a permission boundary renders as
 * "Failed to delete join." and the user believes the product is broken.
 */
class ReadOnlyRefusedError extends Error {}

/* ------------------------------------------------------------------ */
/* Read-only cardinality marker                                         */
/* ------------------------------------------------------------------ */
/**
 * Bug-8504: in read-only mode the relationship cardinality must still be
 * READABLE, but it must not be an actionable control — the editable variant
 * writes a persisted terminal override. This renders the same glyph as an
 * inert element so no information is lost when the button is withdrawn.
 */
function TerminalMarker({ marker, label }: { marker: string; label: string }) {
  return (
    <Typography
      component="span"
      variant="caption"
      // ARIA 1.2 forbids naming a `generic` element, so the label needs a role
      // that accepts an accessible name — otherwise several AT/browser pairs
      // drop it and announce only the bare "many"/"one" glyph with no side.
      role="img"
      aria-label={label}
      sx={{
        px: 0.75,
        fontSize: 11,
        fontWeight: 600,
        lineHeight: "22px",
        border: "1px solid",
        borderColor: "divider",
        borderRadius: 1,
        color: "text.primary",
        flexShrink: 0,
      }}
    >
      {marker}
    </Typography>
  );
}

/* ------------------------------------------------------------------ */
/* Main JoinsPanel                                                     */
/* ------------------------------------------------------------------ */
export default function JoinsPanel() {
  const t = useT();
  const { projectId, modelId } = useParams<{
    projectId: string;
    modelId: string;
  }>();
  const qc = useQueryClient();

  const [dialogOpen, setDialogOpen] = useState(false);
  const [editingJoinId, setEditingJoinId] = useState<string | null>(null);
  const [leftTableId, setLeftTableId] = useState("");
  const [rightTableId, setRightTableId] = useState("");
  const [joinType, setJoinType] = useState<JoinCreate["join_type"]>("inner");
  const [cardinality, setCardinality] = useState<JoinCardinality | "">("");
  const [populationParticipation, setPopulationParticipation] = useState<PopulationParticipation>(
    DEFAULT_POPULATION_PARTICIPATION,
  );
  // Captured when the edit dialog opens so the diff preview can show
  // "changing from X to Y" against the value the join actually had, not
  // against whatever the form field currently holds.
  const [originalPopulationParticipation, setOriginalPopulationParticipation] =
    useState<PopulationParticipation>(DEFAULT_POPULATION_PARTICIPATION);
  const [leftCol, setLeftCol] = useState("");
  const [rightCol, setRightCol] = useState("");

  const pendingJoin        = useBuilderStore((s) => s.pendingJoin);
  const setPendingJoin     = useBuilderStore((s) => s.setPendingJoin);
  const selectedObjectId   = useBuilderStore((s) => s.selectedObjectId);
  const selectedObjectType = useBuilderStore((s) => s.selectedObjectType);
  const selectObject       = useBuilderStore((s) => s.selectObject);
  // Bug-8504: the authoring gate is the builder-session flag, which ModelBuilder
  // derives from the model detail's `caller_can_author` (resolved server-side
  // from the caller's per-project access binding) OR the `?readonly=1` share
  // link. That is the SAME value Canvas gates connection drawing, node dragging
  // and every layout write on, so the panel and the canvas can never disagree
  // about whether this model is editable.
  //
  // Deliberately NOT `useCanAuthorModel()`, despite eight sibling panels using
  // it: that hook ANDs in `canEditModelConfig()`, which tests the COARSE local
  // role string from `/users/me`. `ALLOWED_LOCAL_USER_ROLES` is
  // (member | tenant_admin | model_technical) — `modeler` is a per-project
  // BINDING, not a local role — so the canonical locally-provisioned modeller
  // (local role `member`, project binding `modeler`) reads as a non-editor
  // there. Adopting it here would have hidden every join control from a user
  // the backend authorises, while the canvas beside it stayed fully editable
  // and silently discarded the joins they drew. Tracked separately; see the
  // parity regression test in JoinsPanel.readonly.test.tsx.
  const isReadOnly         = useBuilderStore((s) => s.readOnly);
  const setGlobalMessage   = useBuilderStore((s) => s.setGlobalMessage);

  // The join that was clicked on the canvas (if any)
  const focusedJoinId =
    selectedObjectType === "join" ? selectedObjectId : null;

  // Scroll the focused join card into view
  const joinRefs = useRef<Record<string, HTMLDivElement | null>>({});
  useEffect(() => {
    if (!focusedJoinId) return;
    const el = joinRefs.current[focusedJoinId];
    el?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [focusedJoinId]);

  const sources = useSources(projectId!, modelId!);
  const sourceIds = (sources.data ?? []).map((s) => s.id);
  const allTables = useAllModelTables(projectId!, modelId!, sourceIds);
  const joins = useJoins(projectId!, modelId!);
  type JoinRow = NonNullable<typeof joins.data>[number];

  // Model-level population governance rollup (Bug-8615 G2). Computed at
  // deploy time, never on the query path — this is a read-only health
  // surface, not a serving authority. Refetched after any join create/
  // update/delete so a declaration edit's "needs recheck" state shows
  // immediately, even though the verdict itself only refreshes on redeploy.
  const joinPopulationHealth = useQuery({
    queryKey: ["join-population-health", projectId, modelId],
    queryFn: () => joinPopulationHealthApi.get(projectId!, modelId!),
    enabled: !!projectId && !!modelId,
  });
  const populationHealthByJoinId = new Map<string, JoinPopulationHealthItem>(
    (joinPopulationHealth.data?.items ?? []).map((item) => [item.join_id, item]),
  );

  // Resolve selected tables
  const leftTable = allTables.data?.find((t) => t.id === leftTableId);
  const rightTable = allTables.data?.find((t) => t.id === rightTableId);

  // Fetch columns for selected tables from synced table attributes.
  // Using table attributes (not discoverColumns) so calendar tables and
  // aggregate-schema tables always resolve — their attributes are synced
  // at creation time and don't require a live source connection query.
  const leftColumns  = useTableColumns(projectId!, modelId!, leftTable);
  const rightColumns = useTableColumns(projectId!, modelId!, rightTable);

  // Join type constraints based on fact/dim relationship
  const constraints = getJoinConstraints(leftTable, rightTable, t);
  const JOIN_TYPES = getJoinTypes(t);
  const JOIN_CARDINALITIES = getJoinCardinalities(t);
  const POPULATION_PARTICIPATIONS = getPopulationParticipations(t);

  // When constraints change, reset join type if current is no longer allowed
  useEffect(() => {
    if (!constraints.allowed.has(joinType)) {
      setJoinType("inner");
    }
  }, [leftTableId, rightTableId]);

  // Handle pending join from canvas drag.
  //
  // Bug-8504: the create dialog has a SECOND entry point besides the Add button
  // — anything that writes `pendingJoin` into the builder store opens it. Hiding
  // the Add button therefore does not close the create path on its own, so the
  // read-only check has to sit on the effect that opens the dialog, not only on
  // the control that is the usual way in.
  //
  // R1 review: refusing it must not be SILENT. The user made a deliberate
  // gesture on the canvas; swallowing it with no dialog and no message reads as
  // a broken product rather than as a permission boundary.
  useEffect(() => {
    if (pendingJoin && isReadOnly) {
      setPendingJoin(null);
      setGlobalMessage(t("joins.readOnlyRefused"), "info");
      return;
    }
    if (pendingJoin) {
      setLeftTableId(pendingJoin.leftTableId);
      setRightTableId(pendingJoin.rightTableId);
      setJoinType("inner");
      setLeftCol("");
      setRightCol("");
      setCardinality("");
      setPopulationParticipation(DEFAULT_POPULATION_PARTICIPATION);
      setOriginalPopulationParticipation(DEFAULT_POPULATION_PARTICIPATION);
      setDialogOpen(true);
      setPendingJoin(null);
    }
  }, [pendingJoin, setPendingJoin, isReadOnly, setGlobalMessage, t]);

  // R1 review: the create/edit dialog can outlive the editable session — the
  // model detail can resolve to `caller_can_author: false` while the form is
  // open. Leaving it open with an enabled Save turns a permission boundary into
  // a generic "failed to save" alert, so withdraw the dialog the moment the
  // session stops being editable and say why.
  useEffect(() => {
    if (!isReadOnly || !dialogOpen) return;
    setDialogOpen(false);
    setEditingJoinId(null);
    setGlobalMessage(t("joins.readOnlyRefused"), "info");
  }, [isReadOnly, dialogOpen, setGlobalMessage, t]);

  /**
   * Bug-8504: fail-closed guard at the point of persistence.
   *
   * Hiding a control is a presentation decision; it is not an enforcement
   * boundary. Every join write in this panel goes through one of the three
   * mutations below, so the check lives on them as well as on the controls —
   * a future entry point (a keyboard shortcut, a deep link, a canvas gesture,
   * a re-used dialog) then inherits it instead of having to remember it. The
   * server gate stays authoritative; this stops the request being made at all.
   */
  function assertCanAuthor(action: string): void {
    if (isReadOnly) {
      throw new ReadOnlyRefusedError(
        `Read-only model session: join ${action} is not permitted.`,
      );
    }
  }

  /**
   * R2 review: report a refusal as a refusal.
   *
   * A guard that surfaces as "Failed to delete join." tells the user the
   * product broke when in fact it declined, which is the same silent/misleading
   * outcome the pending-join path was fixed for. Every mutation's onError
   * branches on this type so a permission boundary always reads as one.
   */
  function reportMutationError(error: unknown, fallbackKey: string): void {
    setGlobalMessage(
      error instanceof ReadOnlyRefusedError
        ? t("joins.readOnlyRefused")
        : t(fallbackKey),
      error instanceof ReadOnlyRefusedError ? "info" : "error",
    );
  }

  const createJoin = useMutation({
    mutationFn: () => {
      assertCanAuthor("create");
      return joinsApi.create(projectId!, modelId!, {
        left_table_id: leftTableId,
        right_table_id: rightTableId,
        join_type: joinType,
        cardinality: cardinality || null,
        population_participation: populationParticipation,
        left_column_name: leftCol,
        right_column_name: rightCol,
      });
    },
    onSuccess: (created) => {
      window.dispatchEvent(
        new CustomEvent("canvas-history-action", {
          detail: {
            action: {
              type: "addLink",
              joinId: created.id,
              createData: {
                left_table_id: created.left_table_id,
                right_table_id: created.right_table_id,
                join_type: normaliseJoinTypeForEditor(created.join_type),
                cardinality: (created.cardinality ?? null) as JoinCardinality | null,
                population_participation: (created.population_participation ||
                  DEFAULT_POPULATION_PARTICIPATION) as PopulationParticipation,
                left_column_name: created.left_column_name ?? leftCol,
                right_column_name: created.right_column_name ?? rightCol,
              },
            },
          },
        }),
      );
      qc.invalidateQueries({ queryKey: ["joins", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["tableAttributes", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["dimensions", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["join-population-health", projectId, modelId] });
      setDialogOpen(false);
    },
    onError: (error: unknown) => {
      // The in-dialog alert cannot show a refusal — the dialog is withdrawn the
      // moment the session turns read-only — so state it at panel level (R2).
      if (error instanceof ReadOnlyRefusedError) {
        reportMutationError(error, "joins.failedToCreate");
      }
    },
  });

  const updateJoin = useMutation({
    mutationFn: () => {
      assertCanAuthor("update");
      return joinsApi.update(projectId!, modelId!, editingJoinId!, {
        join_type: joinType,
        cardinality: cardinality || null,
        population_participation: populationParticipation,
        left_column_name: leftCol,
        right_column_name: rightCol,
      });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["joins", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["tableAttributes", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["dimensions", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["join-population-health", projectId, modelId] });
      setDialogOpen(false);
      setEditingJoinId(null);
    },
    onError: (error: unknown) => {
      if (error instanceof ReadOnlyRefusedError) {
        reportMutationError(error, "joins.failedToUpdate");
      }
    },
  });

  const deleteJoin = useMutation({
    // Carry the full join through the mutation so the undo entry can be built
    // in onSuccess from the confirmed-deleted row (Bug-6376).
    mutationFn: (join: JoinRow) => {
      assertCanAuthor("delete");
      return joinsApi.delete(projectId!, modelId!, join.id).then(() => join);
    },
    onSuccess: (join) => {
      // Push the undo entry ONLY after the delete actually succeeds. Recording
      // it before the request (the old behaviour) left a `deleteLink` action on
      // the history stack even when the delete failed, so an undo re-created a
      // duplicate join (Bug-6376).
      window.dispatchEvent(
        new CustomEvent("canvas-history-action", {
          detail: {
            action: {
              type: "deleteLink",
              joinId: join.id,
              createData: {
                left_table_id: join.left_table_id,
                right_table_id: join.right_table_id,
                join_type: normaliseJoinTypeForEditor(join.join_type),
                cardinality: (join.cardinality ?? null) as JoinCardinality | null,
                population_participation: (join.population_participation ||
                  DEFAULT_POPULATION_PARTICIPATION) as PopulationParticipation,
                left_column_name: join.left_column_name ?? "",
                right_column_name: join.right_column_name ?? "",
              },
            },
          },
        }),
      );
      qc.invalidateQueries({ queryKey: ["joins", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["tableAttributes", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["dimensions", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["join-population-health", projectId, modelId] });
    },
    onError: (error: unknown) => {
      // Surface the failure instead of failing silently (Bug-6376), and keep a
      // read-only refusal distinguishable from a real delete failure (R2).
      reportMutationError(error, "joins.failedToDelete");
    },
  });

  const relationTerminalOverrides = useBuilderStore((s) => s.relationTerminalOverrides);
  const setRelationTerminalOverride = useBuilderStore((s) => s.setRelationTerminalOverride);

  function handleToggleTerminal(j: any, side: "source" | "target") {
    // Bug-8504: cardinality overrides are a persisted authoring choice
    // (localStorage-backed, applied to the shared canvas rendering). A
    // read-only session must not be able to change them from any entry point.
    if (isReadOnly) return;
    const leftIsFact = allTables.data?.find((t) => t.id === j.left_table_id)?.table_type === "fact";
    const rightIsFact = allTables.data?.find((t) => t.id === j.right_table_id)?.table_type === "fact";
    const currentOverride = relationTerminalOverrides[j.id] || {
      source: leftIsFact && !rightIsFact ? "many" : !leftIsFact && rightIsFact ? "one" : "none",
      target: leftIsFact && !rightIsFact ? "one" : !leftIsFact && rightIsFact ? "many" : "none",
    };
    const current = currentOverride[side];
    const next = current === "many" ? "one" : current === "one" ? "optional" : "many";
    setRelationTerminalOverride(j.id, { ...currentOverride, [side]: next });
  }

  const confirm = useConfirm();
  async function handleDeleteJoin(joinId: string) {
    const ok = await confirm({
      title: t("joins.deleteConfirm"),
      message: t("joins.deleteMessage"),
      confirmLabel: t("joins.delete"),
    });
    if (ok) {
      const j = joins.data?.find((j) => j.id === joinId);
      if (j) {
        // The undo entry is recorded in deleteJoin.onSuccess, only once the
        // delete is confirmed (Bug-6376).
        deleteJoin.mutate(j);
        selectObject(null, null);
      }
    }
  }

  function tableLabel(id: string) {
    const tbl = allTables.data?.find((tbl) => tbl.id === id);
    return tbl?.alias ?? tbl?.display_name ?? id.slice(0, 8);
  }

  function openDialog() {
    // R3 review: a previous attempt's error state is sticky — react-query
    // keeps isError until the mutation is reset — so reopening the dialog
    // showed a failure the user had not yet caused. Worse since the read-only
    // guard landed: a REFUSAL would reopen as "Failed to create join.",
    // attributing a permission decision to a product fault.
    createJoin.reset();
    updateJoin.reset();
    setEditingJoinId(null);
    setLeftTableId("");
    setRightTableId("");
    setJoinType("inner");
    setCardinality("");
    setPopulationParticipation(DEFAULT_POPULATION_PARTICIPATION);
    setOriginalPopulationParticipation(DEFAULT_POPULATION_PARTICIPATION);
    setLeftCol("");
    setRightCol("");
    setDialogOpen(true);
  }

  function openEditDialog(joinId: string) {
    const j = joins.data?.find((j) => j.id === joinId);
    if (!j) return;
    createJoin.reset();
    updateJoin.reset();
    setEditingJoinId(joinId);
    setLeftTableId(j.left_table_id);
    setRightTableId(j.right_table_id);
    setJoinType(normaliseJoinTypeForEditor(j.join_type));
    setCardinality((j.cardinality ?? "") as JoinCardinality | "");
    const currentParticipation = (j.population_participation ||
      DEFAULT_POPULATION_PARTICIPATION) as PopulationParticipation;
    setPopulationParticipation(currentParticipation);
    setOriginalPopulationParticipation(currentParticipation);
    setLeftCol(j.left_column_name ?? "");
    setRightCol(j.right_column_name ?? "");
    setDialogOpen(true);
  }

  const isConnectingMode  = useBuilderStore((s) => s.isConnectingMode);
  const setConnectingMode = useBuilderStore((s) => s.setConnectingMode);
  const closePanel        = useBuilderStore((s) => s.closePanel);

  return (
    <Box>
      {/* Connection drawing mode banner */}
      {isConnectingMode ? (
        <Alert
          severity="info"
          sx={{ mb: 1.5, alignItems: "flex-start" }}
          action={
            <Button
              size="small"
              color="inherit"
              onClick={() => { setConnectingMode(false); closePanel(); }}
              sx={{ whiteSpace: "nowrap", fontWeight: 600 }}
            >
              {t("joins.endConnectionDrawing")}
            </Button>
          }
        >
          <Typography variant="body2" fontWeight={600} mb={0.25}>
            {t("joins.connectionModeActive")}
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {t("joins.connectionModeHelp")}
          </Typography>
        </Alert>
      ) : (
        <Alert severity="info" sx={{ mb: 1.5 }}>
          <Typography variant="caption" color="text.secondary">
            {t("joins.canvasHelp")}
          </Typography>
        </Alert>
      )}

      <Box display="flex" alignItems="center" mb={1.5}>
        <Typography variant="body2" color="text.secondary" sx={{ flex: 1 }}>
          {t("joins.description")}{" "}
          <Link
            href="/help/modelling/dimension-aliases.html"
            target="_blank"
            rel="noopener"
          >
            {t("common.learnMore")}
          </Link>
          .
        </Typography>
        {!isReadOnly && (
          <Button
            size="small"
            variant="contained"
            startIcon={<AddIcon />}
            onClick={openDialog}
            sx={{ ml: 1, whiteSpace: "nowrap" }}
          >
            {t("joins.add")}
          </Button>
        )}
      </Box>

      {/* Population governance rollup (Bug-8615 G5, contract invariant 6):
          a model's OK/WARNING/BLOCKED status is a first-class surfaced fact.
          Hidden while loading/absent so an empty model (or one on a build
          predating this feature) shows no banner at all. */}
      {joinPopulationHealth.data && joinPopulationHealth.data.join_count > 0 && (
        <Alert
          severity={
            joinPopulationHealth.data.status === "BLOCKED"
              ? "error"
              : joinPopulationHealth.data.status === "WARNING"
              ? "warning"
              : "success"
          }
          sx={{ mb: 1.5 }}
        >
          <Typography variant="body2" fontWeight={600}>
            {t("joins.populationHealthTitle")}
          </Typography>
          <Typography variant="caption" display="block">
            {joinPopulationHealth.data.status === "BLOCKED"
              ? t("joins.populationHealthBlocked", {
                  count: String(joinPopulationHealth.data.blocked_count),
                })
              : joinPopulationHealth.data.status === "WARNING"
              ? t("joins.populationHealthWarning", {
                  count: String(joinPopulationHealth.data.warning_count),
                })
              : t("joins.populationHealthOk")}
          </Typography>
          {!joinPopulationHealth.data.evaluated && (
            <Typography variant="caption" display="block" color="text.secondary">
              {t("joins.populationHealthUnevaluated")}
            </Typography>
          )}
          {!joinPopulationHealth.data.warn_only &&
            joinPopulationHealth.data.status === "BLOCKED" && (
              <Typography variant="caption" display="block" color="text.secondary">
                {t("joins.populationHealthEnforced")}
              </Typography>
            )}
        </Alert>
      )}

      {joins.isLoading ? (
        <CircularProgress size={20} />
      ) : joins.data?.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          {t("joins.none")}
        </Typography>
      ) : (
        <Stack spacing={1}>
          {joins.data?.map((j) => {
            const isFocused = j.id === focusedJoinId;
            const leftIsFact = allTables.data?.find((t) => t.id === j.left_table_id)?.table_type === "fact";
            const rightIsFact = allTables.data?.find((t) => t.id === j.right_table_id)?.table_type === "fact";
            const override = relationTerminalOverrides[j.id];
            const srcMarker = override?.source || (leftIsFact && !rightIsFact ? "many" : !leftIsFact && rightIsFact ? "one" : "none");
            const tgtMarker = override?.target || (leftIsFact && !rightIsFact ? "one" : !leftIsFact && rightIsFact ? "many" : "none");

            return (
              <Card
                key={j.id}
                variant="outlined"
                ref={(el) => { joinRefs.current[j.id] = el; }}
                onClick={() => selectObject(j.id, "join")}
                sx={{
                  cursor: "pointer",
                  borderColor: isFocused ? "primary.main" : "divider",
                  borderWidth: isFocused ? 2 : 1,
                  transition: "border-color 0.15s",
                  "&:hover": { borderColor: "primary.main" },
                }}
              >
                <CardContent sx={{ py: 1, px: 1.5, "&:last-child": { pb: 1 } }}>
                  {/* Row 1 — table names + join type */}
                  <Box display="flex" alignItems="center" gap={0.5} mb={0.5} minWidth={0}>
                    {isReadOnly ? (
                      <TerminalMarker
                        marker={srcMarker}
                        label={t("joins.sourceCardinality", { value: srcMarker })}
                      />
                    ) : (
                    <Tooltip title={t("joins.cycleCardinality")} arrow placement="top">
                      <Button
                        size="small"
                        variant="outlined"
                        data-testid={`join-cardinality-source-${j.id}`}
                        onClick={(e) => { e.stopPropagation(); handleToggleTerminal(j, "source"); }}
                        sx={{
                          minWidth: 0,
                          px: 0.75,
                          py: 0,
                          fontSize: 11,
                          fontWeight: 600,
                          lineHeight: "22px",
                          textTransform: "none",
                          borderColor: "divider",
                          color: "text.primary",
                          "&:hover": { borderColor: "primary.main", color: "primary.main" },
                        }}
                      >
                        {srcMarker}
                      </Button>
                    </Tooltip>
                    )}
                    <Typography variant="body2" fontWeight={600} noWrap sx={{ flex: "1 1 0", minWidth: 0 }}>
                      {tableLabel(j.left_table_id)}
                    </Typography>
                    <Typography
                      variant="caption"
                      sx={{ flexShrink: 0, bgcolor: ui.greenBg, color: ui.green, fontWeight: 600, px: 1, py: 0.25, borderRadius: 1 }}
                    >
                      {normaliseJoinTypeForEditor(j.join_type).toUpperCase()}
                    </Typography>
                    {j.cardinality ? (
                      <Typography
                        variant="caption"
                        color="text.secondary"
                        sx={{ flexShrink: 0 }}
                        title={t("joins.cardinalityHelp")}
                      >
                        {j.cardinality.replace(/_/g, "-")}
                      </Typography>
                    ) : null}
                    <Typography variant="body2" fontWeight={600} noWrap sx={{ flex: "1 1 0", minWidth: 0, textAlign: "right" }}>
                      {tableLabel(j.right_table_id)}
                    </Typography>
                    {isReadOnly ? (
                      <TerminalMarker
                        marker={tgtMarker}
                        label={t("joins.targetCardinality", { value: tgtMarker })}
                      />
                    ) : (
                    <Tooltip title={t("joins.cycleCardinality")} arrow placement="top">
                      <Button
                        size="small"
                        variant="outlined"
                        data-testid={`join-cardinality-target-${j.id}`}
                        onClick={(e) => { e.stopPropagation(); handleToggleTerminal(j, "target"); }}
                        sx={{
                          minWidth: 0,
                          px: 0.75,
                          py: 0,
                          fontSize: 11,
                          fontWeight: 600,
                          lineHeight: "22px",
                          textTransform: "none",
                          borderColor: "divider",
                          color: "text.primary",
                          "&:hover": { borderColor: "primary.main", color: "primary.main" },
                        }}
                      >
                        {tgtMarker}
                      </Button>
                    </Tooltip>
                    )}
                  </Box>
                  {/* Row 2 — column keys + action buttons */}
                  <Box display="flex" alignItems="center" gap={0.5}>
                    <Typography variant="caption" color="text.secondary" noWrap sx={{ flex: 1, minWidth: 0 }}>
                      {j.left_column_name ?? t("joins.unknownColumn")} = {j.right_column_name ?? t("joins.unknownColumn")}
                    </Typography>
                    {/* Bug-8504: both edge-path controls write the persisted
                        canvas layout through Canvas.flushLayout, so they are
                        authoring controls and must not be offered read-only. */}
                    {!isReadOnly && (
                      <Tooltip title={t("joins.resetPath")}>
                        <IconButton size="small" aria-label={t("joins.resetPath")} data-testid={`join-reset-path-${j.id}`} onClick={(e) => {
                          e.stopPropagation();
                          window.dispatchEvent(new CustomEvent('reset-edge-path', { detail: j.id }));
                        }}>
                          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/></svg>
                        </IconButton>
                      </Tooltip>
                    )}
                    {!isReadOnly && (
                      <Tooltip title={t("joins.togglePathing")}>
                        <IconButton size="small" aria-label={t("joins.togglePathing")} data-testid={`join-toggle-pathing-${j.id}`} onClick={(e) => {
                          e.stopPropagation();
                          window.dispatchEvent(new CustomEvent('toggle-edge-pathing-auto', { detail: j.id }));
                        }}>
                          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"></polyline></svg>
                        </IconButton>
                      </Tooltip>
                    )}
                    {!isReadOnly && (
                      <Tooltip title={t("joins.edit")}>
                        <IconButton
                          size="small"
                          aria-label={t("joins.edit")}
                          data-testid={`join-edit-${j.id}`}
                          onClick={(e) => { e.stopPropagation(); openEditDialog(j.id); }}
                        >
                          <EditIcon sx={{ fontSize: 15 }} />
                        </IconButton>
                      </Tooltip>
                    )}
                    {!isReadOnly && (
                      <Tooltip title={t("joins.delete")}>
                        <IconButton
                          size="small"
                          aria-label={t("joins.delete")}
                          data-testid={`join-delete-${j.id}`}
                          onClick={(e) => { e.stopPropagation(); handleDeleteJoin(j.id); }}
                        >
                          <DeleteIcon sx={{ fontSize: 15 }} />
                        </IconButton>
                      </Tooltip>
                    )}
                  </Box>
                  {/* Row 3 — population participation + per-join health, when
                      the join carries a non-default declaration or a stale
                      verdict. Hidden for the common preserve_base_rows/no-
                      verdict case to avoid cluttering every card. */}
                  {(() => {
                    const participation = j.population_participation || DEFAULT_POPULATION_PARTICIPATION;
                    const health = populationHealthByJoinId.get(j.id);
                    const participationLabel = POPULATION_PARTICIPATIONS.find(
                      (p) => p.value === participation,
                    )?.label;
                    const showParticipation = participation !== DEFAULT_POPULATION_PARTICIPATION;
                    // Bug-8667 (see join_population_health.py): a plain PATCH —
                    // exactly what this dialog's Save does — does NOT bump the
                    // model's deploy epoch, so `stale` alone stays false right
                    // after a modeller re-declares a join here.
                    // `declaration_changed_since_check` is the field that
                    // actually flips in that case; both must be checked or the
                    // one edit flow this panel offers would never show "needs
                    // recheck" against its own change.
                    const showStale = !!health && (health.stale || health.declaration_changed_since_check);
                    const showStatus = health?.status === "WARNING" || health?.status === "BLOCKED";
                    if (!showParticipation && !showStale && !showStatus) return null;
                    return (
                      <Box display="flex" alignItems="center" gap={0.5} mt={0.5} flexWrap="wrap">
                        {showParticipation && (
                          <Typography
                            variant="caption"
                            sx={{
                              bgcolor: ui.purpleBg,
                              color: ui.purple,
                              fontWeight: 600,
                              px: 1,
                              py: 0.25,
                              borderRadius: 1,
                            }}
                            title={t("joins.populationParticipationHelp")}
                          >
                            {participationLabel}
                          </Typography>
                        )}
                        {showStatus && (
                          <Tooltip title={health?.reason ?? ""}>
                            <Typography
                              variant="caption"
                              sx={{
                                bgcolor: health?.status === "BLOCKED" ? ui.redBg : ui.goldBg,
                                color: health?.status === "BLOCKED" ? ui.red : ui.goldDark,
                                fontWeight: 600,
                                px: 1,
                                py: 0.25,
                                borderRadius: 1,
                              }}
                            >
                              {health?.status === "BLOCKED"
                                ? t("joins.populationStatusBlocked")
                                : t("joins.populationStatusWarning")}
                            </Typography>
                          </Tooltip>
                        )}
                        {showStale && (
                          // Deliberately an OUTLINED gold treatment, not the
                          // filled gold the WARNING status chip above uses —
                          // "needs recheck" (this verdict is out of date) and
                          // a WARNING verdict (this is the verdict) are
                          // different facts and can appear side by side; the
                          // fill/outline distinction keeps them visually
                          // separable without a new design token.
                          <Tooltip title={t("joins.populationHealthNeedsRecheck")}>
                            <Typography
                              variant="caption"
                              sx={{
                                border: "1px solid",
                                borderColor: ui.goldDark,
                                color: ui.goldDark,
                                fontWeight: 600,
                                px: 1,
                                py: 0.25,
                                borderRadius: 1,
                              }}
                            >
                              {t("joins.populationHealthNeedsRecheck")}
                            </Typography>
                          </Tooltip>
                        )}
                      </Box>
                    );
                  })()}
                  {(j.warnings ?? []).length > 0 ? (
                    <Alert
                      severity="warning"
                      role="status"
                      aria-label={t("joins.validationWarnings")}
                      onClick={(event) => event.stopPropagation()}
                      sx={{ mt: 0.75, py: 0.25, px: 0.75 }}
                    >
                      <Typography variant="caption" fontWeight={700} display="block">
                        {t("joins.validationWarnings")}
                      </Typography>
                      <Box component="ul" sx={{ m: 0, pl: 2 }}>
                        {(j.warnings ?? []).map((warning, index) => (
                          <Typography component="li" variant="caption" key={`${j.id}-warning-${index}`}>
                            {warning}
                          </Typography>
                        ))}
                      </Box>
                    </Alert>
                  ) : null}
                </CardContent>
              </Card>
            );
          })}
        </Stack>
      )}

      {/* Join creation dialog */}
      <Dialog
        open={dialogOpen}
        onClose={() => setDialogOpen(false)}
        maxWidth="sm"
        fullWidth
      >
        <DialogTitle>{editingJoinId ? t("joins.editTitle") : t("joins.addTitle")}</DialogTitle>
        <DialogContent>
          {/* Left table */}
          <FormControl fullWidth margin="normal">
            <InputLabel id="join-left-table-label">{t("joins.leftTable")}</InputLabel>
            <Select
              labelId="join-left-table-label"
              value={leftTableId}
              label={t("joins.leftTable")}
              disabled={!!editingJoinId}
              onChange={(e) => {
                setLeftTableId(e.target.value);
                setLeftCol("");
              }}
            >
              {allTables.data?.map((tbl) => (
                <MenuItem key={tbl.id} value={tbl.id}>
                  <Box display="flex" alignItems="center" gap={1}>
                    <span>{tbl.alias ?? tbl.display_name}</span>
                    <Chip
                      label={tbl.table_type === "fact" ? t("joins.tableFact") : tbl.table_type === "dim_detail" ? t("joins.tableDimDetail") : t("joins.tableDimAgg")}
                      size="small"
                      sx={{
                        height: 18, fontSize: 10, fontWeight: 500,
                        bgcolor: tbl.table_type === "fact" ? ui.greenBg : tbl.table_type === "dim_detail" ? ui.goldBg : ui.purpleBg,
                        color: tbl.table_type === "fact" ? ui.green : tbl.table_type === "dim_detail" ? ui.goldDark : ui.purple,
                      }}
                    />
                  </Box>
                </MenuItem>
              ))}
            </Select>
          </FormControl>

          {/* Left column */}
          <FormControl fullWidth margin="dense">
            <InputLabel id="join-left-column-label">{t("joins.leftColumn")}</InputLabel>
            <Select
              labelId="join-left-column-label"
              value={leftColumns.data?.some((c: TableAttribute) => c.name === leftCol) ? leftCol : ""}
              label={t("joins.leftColumn")}
              disabled={!leftTableId || leftColumns.isLoading}
              onChange={(e) => setLeftCol(e.target.value)}
            >
              {leftColumns.isLoading && (
                <MenuItem disabled>
                  <CircularProgress size={14} sx={{ mr: 1 }} /> {t("joins.loadingColumns")}
                </MenuItem>
              )}
              {leftColumns.data?.map((c: TableAttribute) => (
                <MenuItem key={c.name} value={c.name}>
                  <Box display="flex" alignItems="center" gap={1}>
                    <span>{c.name}</span>
                    <Typography variant="caption" color="text.secondary">
                      {c.data_type}
                    </Typography>
                  </Box>
                </MenuItem>
              ))}
            </Select>
          </FormControl>

          {/* Join type radio group */}
          <Box sx={{ mt: 2, mb: 1, p: 1.5, bgcolor: ui.mutedBg, borderRadius: 1 }}>
            <Typography variant="subtitle2" fontWeight={700} mb={0.5}>
              {t("joins.joinType")}
            </Typography>
            {constraints.hint && (
              <Alert severity="info" sx={{ mb: 1, py: 0 }}>
                <Typography variant="caption">{constraints.hint}</Typography>
              </Alert>
            )}
            <RadioGroup
              value={joinType}
              onChange={(e) => setJoinType(e.target.value as JoinCreate["join_type"])}
            >
              {JOIN_TYPES.map((jt) => {
                const disabled = !constraints.allowed.has(jt.value);
                return (
                  <FormControlLabel
                    key={jt.value}
                    value={jt.value}
                    disabled={disabled}
                    control={<Radio size="small" />}
                    label={
                      <Box>
                        <Typography variant="body2" fontWeight={joinType === jt.value ? 700 : 400}>
                          {jt.label}
                        </Typography>
                        <Typography variant="caption" color="text.secondary">
                          {jt.description}
                        </Typography>
                      </Box>
                    }
                    sx={{ mb: 0.5 }}
                  />
                );
              })}
            </RadioGroup>

            <FormControl fullWidth size="small" sx={{ mt: 1.5 }}>
              <InputLabel id="join-cardinality-label">
                {t("joins.cardinality")}
              </InputLabel>
              <Select
                labelId="join-cardinality-label"
                value={cardinality}
                label={t("joins.cardinality")}
                onChange={(e) =>
                  setCardinality(e.target.value as JoinCardinality | "")
                }
              >
                {JOIN_CARDINALITIES.map((c) => (
                  <MenuItem key={c.value || "undeclared"} value={c.value}>
                    {c.label}
                  </MenuItem>
                ))}
              </Select>
              <Typography variant="caption" color="text.secondary" sx={{ mt: 0.5 }}>
                {t("joins.cardinalityHelp")}
              </Typography>
            </FormControl>

            <FormControl fullWidth size="small" sx={{ mt: 1.5 }}>
              <InputLabel id="join-population-participation-label">
                {t("joins.populationParticipation")}
              </InputLabel>
              <Select
                labelId="join-population-participation-label"
                value={populationParticipation}
                label={t("joins.populationParticipation")}
                onChange={(e) =>
                  setPopulationParticipation(e.target.value as PopulationParticipation)
                }
              >
                {POPULATION_PARTICIPATIONS.map((p) => (
                  <MenuItem key={p.value} value={p.value}>
                    <Box>
                      <Typography variant="body2">{p.label}</Typography>
                      <Typography variant="caption" color="text.secondary">
                        {p.description}
                      </Typography>
                    </Box>
                  </MenuItem>
                ))}
              </Select>
              <Typography variant="caption" color="text.secondary" sx={{ mt: 0.5 }}>
                {t("joins.populationParticipationHelp")}
              </Typography>
              {/* Diff preview (Bug-8615 G2): only shown once there is a prior
                  declaration to diff against, i.e. while editing an existing
                  join, and only when the form value actually differs from it. */}
              {editingJoinId && populationParticipation !== originalPopulationParticipation && (
                <Alert severity="info" sx={{ mt: 1, py: 0.25 }}>
                  <Typography variant="caption">
                    {t("joins.populationParticipationChanging", {
                      from:
                        POPULATION_PARTICIPATIONS.find(
                          (p) => p.value === originalPopulationParticipation,
                        )?.label ?? originalPopulationParticipation,
                      to:
                        POPULATION_PARTICIPATIONS.find(
                          (p) => p.value === populationParticipation,
                        )?.label ?? populationParticipation,
                    })}
                  </Typography>
                </Alert>
              )}
            </FormControl>
          </Box>

          {/* Right table */}
          <FormControl fullWidth margin="normal">
            <InputLabel id="join-right-table-label">{t("joins.rightTable")}</InputLabel>
            <Select
              labelId="join-right-table-label"
              value={rightTableId}
              label={t("joins.rightTable")}
              disabled={!!editingJoinId}
              onChange={(e) => {
                setRightTableId(e.target.value);
                setRightCol("");
              }}
            >
              {allTables.data?.map((tbl) => (
                <MenuItem key={tbl.id} value={tbl.id}>
                  <Box display="flex" alignItems="center" gap={1}>
                    <span>{tbl.alias ?? tbl.display_name}</span>
                    <Chip
                      label={tbl.table_type === "fact" ? t("joins.tableFact") : tbl.table_type === "dim_detail" ? t("joins.tableDimDetail") : t("joins.tableDimAgg")}
                      size="small"
                      sx={{
                        height: 18, fontSize: 10, fontWeight: 500,
                        bgcolor: tbl.table_type === "fact" ? ui.greenBg : tbl.table_type === "dim_detail" ? ui.goldBg : ui.purpleBg,
                        color: tbl.table_type === "fact" ? ui.green : tbl.table_type === "dim_detail" ? ui.goldDark : ui.purple,
                      }}
                    />
                  </Box>
                </MenuItem>
              ))}
            </Select>
          </FormControl>

          {/* Right column */}
          <FormControl fullWidth margin="dense">
            <InputLabel id="join-right-column-label">{t("joins.rightColumn")}</InputLabel>
            <Select
              labelId="join-right-column-label"
              value={rightColumns.data?.some((c: TableAttribute) => c.name === rightCol) ? rightCol : ""}
              label={t("joins.rightColumn")}
              disabled={!rightTableId || rightColumns.isLoading}
              onChange={(e) => setRightCol(e.target.value)}
            >
              {rightColumns.isLoading && (
                <MenuItem disabled>
                  <CircularProgress size={14} sx={{ mr: 1 }} /> {t("joins.loadingColumns")}
                </MenuItem>
              )}
              {rightColumns.data?.map((c: TableAttribute) => (
                <MenuItem key={c.name} value={c.name}>
                  <Box display="flex" alignItems="center" gap={1}>
                    <span>{c.name}</span>
                    <Typography variant="caption" color="text.secondary">
                      {c.data_type}
                    </Typography>
                  </Box>
                </MenuItem>
              ))}
            </Select>
          </FormControl>

          {(() => {
            const mutationError = createJoin.error ?? updateJoin.error;
            if (!createJoin.isError && !updateJoin.isError) return null;
            // R3 review: a refusal is not a failure — word it as one or the
            // other, never the generic failure text for both.
            const refused = mutationError instanceof ReadOnlyRefusedError;
            return (
              <Alert severity={refused ? "info" : "error"} sx={{ mt: 1 }}>
                {refused
                  ? t("joins.readOnlyRefused")
                  : editingJoinId
                    ? t("joins.failedToUpdate")
                    : t("joins.failedToCreate")}
              </Alert>
            );
          })()}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDialogOpen(false)}>{t("common.cancel")}</Button>
          <Button
            variant="contained"
            onClick={() => editingJoinId ? updateJoin.mutate() : createJoin.mutate()}
            disabled={
              isReadOnly ||
              !leftTableId ||
              !rightTableId ||
              !leftCol ||
              !rightCol ||
              createJoin.isPending ||
              updateJoin.isPending
            }
          >
            {(createJoin.isPending || updateJoin.isPending)
              ? <CircularProgress size={18} />
              : editingJoinId ? t("common.save") : t("joins.add")}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
