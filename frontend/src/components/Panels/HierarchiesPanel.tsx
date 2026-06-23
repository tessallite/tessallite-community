import { useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useT } from "../../i18n";
import { extractApiError } from "../../utils/extractApiError";
import {
  Alert,
  Box,
  Button,
  Checkbox,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  FormControl,
  FormControlLabel,
  IconButton,
  InputLabel,
  List,
  ListItemButton,
  Menu,
  MenuItem,
  Paper,
  Select,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import AccountTreeOutlinedIcon from "@mui/icons-material/AccountTreeOutlined";
import AddIcon from "@mui/icons-material/Add";
import ArrowDropDownIcon from "@mui/icons-material/ArrowDropDown";
import ArrowDownwardIcon from "@mui/icons-material/ArrowDownward";
import ArrowUpwardIcon from "@mui/icons-material/ArrowUpward";
import ChevronRightIcon from "@mui/icons-material/ChevronRight";
import DeleteIcon from "@mui/icons-material/Delete";
import EditIcon from "@mui/icons-material/Edit";
import RefreshIcon from "@mui/icons-material/Refresh";
import { ui } from "../../theme/tokens";
import { hierarchiesApi } from "../../api/client";
import { useConfirm } from "../Confirm";
import {
  useAllModelTables,
  useHierarchies,
  useHierarchy,
  useMeasures,
  useSources,
  useTableAttributes,
} from "../../api/hooks";
import type {
  CalendarType,
  Hierarchy,
  HierarchyCreate,
  HierarchyDimensionKind,
  HierarchyGenerateDateRequest,
  HierarchyGenerateSegmentRequest,
  HierarchyLevel,
  HierarchyLevelCreate,
  HierarchyTimeCalc,
  HierarchyTimeUnit,
  UnassignedDateColumn,
} from "../../api/types";

const HIERARCHY_TYPES: HierarchyCreate["type"][] = ["explicit", "date_embedded", "segment"];
const DATE_TEMPLATES: HierarchyGenerateDateRequest["template"][] = [
  "y_m_d",
  "y_q_m_d",
  "y_h_q_m_d",
  "y_w_d",
  "y_m_w_d",
];

const DIMENSION_KINDS: HierarchyDimensionKind[] = ["time", "geo", "entity"];
const CALENDAR_TYPES: CalendarType[] = [
  "standard",
  "fiscal",
  "iso_week",
  "retail_445",
  "hijri",
  "thai_buddhist",
];

// Translated label for a calendar type. Mirrors CalendarTableDialog so the
// hierarchy picker and the calendar-table picker read identically (F-016-04).
const calendarTypeLabelKey = (v: CalendarType): string =>
  v === "iso_week"
    ? "calendar.isoWeek"
    : v === "retail_445"
      ? "calendar.retail445"
      : v === "thai_buddhist"
        ? "calendar.thaiBuddhist"
        : `calendar.${v}`;
const MONTH_NAMES = [
  "month.january", "month.february", "month.march", "month.april", "month.may", "month.june",
  "month.july", "month.august", "month.september", "month.october", "month.november", "month.december",
];
const TIME_UNITS: HierarchyTimeUnit[] = [
  "year",
  "half",
  "quarter",
  "month",
  "week",
  "day",
  "hour",
  "none",
];
const TIME_CALCS: HierarchyTimeCalc[] = [
  "lag",
  "parallel_period",
  "period_to_date",
  "range",
  "moving_window",
];

function attributeSourceForSelection(isUserDefined: boolean) {
  return isUserDefined ? "user_defined_attribute" : "physical_column";
}

// F-016-17: error messages are translated (the caller passes `t`) instead of
// the previous hardcoded English strings shown directly to users.
function parsePositionalSegments(
  raw: string,
  t: (key: string) => string,
): Array<{ name: string; start: number; length: number }> {
  return raw
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const parts = line.split(":").map((p) => p.trim());
      if (parts.length !== 3) {
        throw new Error(t("hierarchies.positionalSegmentFormat"));
      }
      const [name, startRaw, lengthRaw] = parts;
      const start = Number(startRaw);
      const length = Number(lengthRaw);
      if (!name || !Number.isInteger(start) || !Number.isInteger(length) || start < 1 || length < 1) {
        throw new Error(t("hierarchies.positionalSegmentInvalid"));
      }
      return { name, start, length };
    });
}

export default function HierarchiesPanel() {
  const t = useT();
  const { projectId, modelId } = useParams<{ projectId: string; modelId: string }>();
  const qc = useQueryClient();

  const hierarchies = useHierarchies(projectId!, modelId!);
  const [selectedHierarchyId, setSelectedHierarchyId] = useState("");
  const selectedHierarchy = useHierarchy(projectId!, modelId!, selectedHierarchyId);

  const sources = useSources(projectId!, modelId!);
  const sourceIds = (sources.data ?? []).map((s) => s.id);
  const allTables = useAllModelTables(projectId!, modelId!, sourceIds);
  const measures = useMeasures(projectId!, modelId!);
  const factTables = useMemo(
    () => (allTables.data ?? []).filter((t) => t.table_type === "fact"),
    [allTables.data],
  );

  const isTimeHierarchy = useMemo(
    () =>
      selectedHierarchy.data?.dimension_kind === "time" ||
      (selectedHierarchy.data?.levels ?? []).some(
        (l) => !!l.time_unit && l.time_unit !== "none",
      ),
    [selectedHierarchy.data],
  );

  const [newMenuAnchor, setNewMenuAnchor] = useState<null | HTMLElement>(null);

  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [editingHierarchyId, setEditingHierarchyId] = useState<string | null>(null);
  const [newHierarchyName, setNewHierarchyName] = useState("");
  const [newHierarchyDescription, setNewHierarchyDescription] = useState("");
  const [newHierarchyType, setNewHierarchyType] = useState<HierarchyCreate["type"]>("explicit");
  const [newHierarchyDimensionKind, setNewHierarchyDimensionKind] = useState<HierarchyDimensionKind | "">("");
  const [newCalendarType, setNewCalendarType] = useState<CalendarType | "">("");
  const [newFiscalStartMonth, setNewFiscalStartMonth] = useState<number | "">(1);

  const [dateDialogOpen, setDateDialogOpen] = useState(false);
  const [dateHierarchyName, setDateHierarchyName] = useState("");
  const [dateHierarchyDescription, setDateHierarchyDescription] = useState("");
  const [dateSourceTableId, setDateSourceTableId] = useState("");
  const [dateSourceAttributeId, setDateSourceAttributeId] = useState("");
  const [dateTemplate, setDateTemplate] = useState<HierarchyGenerateDateRequest["template"]>("y_m_d");
  const [dateCalendarType, setDateCalendarType] = useState<CalendarType | "">("");
  const [dateFiscalStartMonth, setDateFiscalStartMonth] = useState<number | "">(1);
  const dateTableAttributes = useTableAttributes(projectId!, modelId!, dateSourceTableId);
  const selectedDateAttribute = (dateTableAttributes.data ?? []).find(
    (a) => a.id === dateSourceAttributeId,
  );

  const [segmentDialogOpen, setSegmentDialogOpen] = useState(false);
  const [segmentHierarchyName, setSegmentHierarchyName] = useState("");
  const [segmentHierarchyDescription, setSegmentHierarchyDescription] = useState("");
  const [segmentSourceTableId, setSegmentSourceTableId] = useState("");
  const [segmentSourceAttributeId, setSegmentSourceAttributeId] = useState("");
  const [segmentMode, setSegmentMode] = useState<HierarchyGenerateSegmentRequest["mode"]>("delimiter");
  const [segmentDelimiter, setSegmentDelimiter] = useState("-");
  const [segmentLevelNames, setSegmentLevelNames] = useState("Group, Category, Detail");
  const [segmentPositions, setSegmentPositions] = useState("Group:1:2\nCategory:3:2\nDetail:5:2");
  const segmentTableAttributes = useTableAttributes(projectId!, modelId!, segmentSourceTableId);
  const selectedSegmentAttribute = (segmentTableAttributes.data ?? []).find(
    (a) => a.id === segmentSourceAttributeId,
  );

  const healthQuery = useQuery({
    queryKey: ["hierarchy-health", projectId, modelId],
    queryFn: () => hierarchiesApi.health(projectId!, modelId!),
    enabled: !!projectId && !!modelId,
  });
  const healthMap = useMemo(() => {
    const m = new Map<string, string>();
    for (const h of healthQuery.data ?? []) m.set(h.hierarchy_id, h.status);
    return m;
  }, [healthQuery.data]);

  const calendarModelTables = useMemo(
    () => (allTables.data ?? []).filter((t) => t.table_type === "calendar"),
    [allTables.data],
  );

  const [dateIntelOpen, setDateIntelOpen] = useState(false);
  const [dateIntelSelected, setDateIntelSelected] = useState<Set<string>>(new Set());
  const [dateIntelGrain, setDateIntelGrain] = useState("y_m_d");
  const [dateIntelCalendarId, setDateIntelCalendarId] = useState("");

  const dateIntelCols = useQuery<UnassignedDateColumn[]>({
    queryKey: ["unassigned-dates-intel", projectId, modelId],
    queryFn: () => hierarchiesApi.listUnassignedDates(projectId!, modelId!),
    enabled: dateIntelOpen,
  });

  const [levelDialogOpen, setLevelDialogOpen] = useState(false);
  const [editingLevelId, setEditingLevelId] = useState<string | null>(null);
  const [levelName, setLevelName] = useState("");
  const [levelTableId, setLevelTableId] = useState("");
  const [levelKeyAttributeId, setLevelKeyAttributeId] = useState("");
  const [levelDisplayAttributeId, setLevelDisplayAttributeId] = useState("");
  const [levelTimeUnit, setLevelTimeUnit] = useState<HierarchyTimeUnit | "">("");
  const [levelAllowedCalcs, setLevelAllowedCalcs] = useState<HierarchyTimeCalc[]>([]);
  const levelTableAttributes = useTableAttributes(projectId!, modelId!, levelTableId);
  const selectedLevelKeyAttr = (levelTableAttributes.data ?? []).find((a) => a.id === levelKeyAttributeId);
  const selectedLevelDisplayAttr = (levelTableAttributes.data ?? []).find(
    (a) => a.id === levelDisplayAttributeId,
  );

  const [previewSampleSize, setPreviewSampleSize] = useState(10);
  const [previewLevelOrdinal, setPreviewLevelOrdinal] = useState(0);
  const [previewParentKey, setPreviewParentKey] = useState("");
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [previewData, setPreviewData] = useState<{
    levels_summary: Array<{ ordinal: number; name: string; estimated_members?: number | null }>;
    warnings: Array<{ level_name: string; type: string; message: string }>;
    members: Array<{ key_value: string; level_name: string; level_ordinal: number }>;
  } | null>(null);

  const refreshHierarchyQueries = () => {
    qc.invalidateQueries({ queryKey: ["hierarchies", projectId, modelId] });
    if (selectedHierarchyId) {
      qc.invalidateQueries({
        queryKey: ["hierarchy", projectId, modelId, selectedHierarchyId],
      });
    }
    qc.invalidateQueries({ queryKey: ["tableAttributes", projectId, modelId] });
    qc.invalidateQueries({ queryKey: ["dimensions", projectId, modelId] });
    qc.invalidateQueries({ queryKey: ["sources", projectId, modelId] });
    qc.invalidateQueries({ queryKey: ["allModelTables", projectId, modelId] });
    // Date/time-intelligence generation provisions calendar aliases AND joins
    // (alias -> fact) on the backend. The ERD canvas draws edges from this
    // query, so it must be refreshed or the new links stay hidden until the
    // model is closed and reopened.
    qc.invalidateQueries({ queryKey: ["joins", projectId, modelId] });
    for (const sourceId of sourceIds) {
      qc.invalidateQueries({ queryKey: ["modelTables", projectId, modelId, sourceId] });
    }
  };

  const createHierarchy = useMutation({
    mutationFn: () =>
      hierarchiesApi.create(projectId!, modelId!, {
        name: newHierarchyName,
        type: newHierarchyType,
        dimension_kind: newHierarchyDimensionKind || null,
        description: newHierarchyDescription || undefined,
        calendar_type: newCalendarType || null,
        fiscal_year_start_month:
          newCalendarType === "fiscal" && newFiscalStartMonth
            ? Number(newFiscalStartMonth)
            : null,
      }),
    onSuccess: (created) => {
      refreshHierarchyQueries();
      setSelectedHierarchyId(created.id);
      closeHierarchyDialog();
    },
  });

  const updateHierarchy = useMutation({
    mutationFn: () => {
      if (!editingHierarchyId) {
        throw new Error("No hierarchy selected for edit");
      }
      return hierarchiesApi.update(projectId!, modelId!, editingHierarchyId, {
        name: newHierarchyName,
        type: newHierarchyType,
        dimension_kind: newHierarchyDimensionKind || null,
        description: newHierarchyDescription || null,
        calendar_type: newCalendarType || null,
        fiscal_year_start_month:
          newCalendarType === "fiscal" && newFiscalStartMonth
            ? Number(newFiscalStartMonth)
            : null,
      });
    },
    onSuccess: () => {
      refreshHierarchyQueries();
      closeHierarchyDialog();
    },
  });

  const deleteHierarchy = useMutation({
    mutationFn: (hierarchyId: string) =>
      hierarchiesApi.delete(projectId!, modelId!, hierarchyId),
    onSuccess: (_, hierarchyId) => {
      refreshHierarchyQueries();
      if (selectedHierarchyId === hierarchyId) {
        setSelectedHierarchyId("");
        setPreviewData(null);
      }
    },
  });

  const createLevel = useMutation({
    mutationFn: () => {
      if (!selectedHierarchyId || !selectedLevelKeyAttr) {
        throw new Error("Hierarchy and key attribute are required");
      }
      const existingLevels = selectedHierarchy.data?.levels ?? [];
      const payload: HierarchyLevelCreate = {
        name: levelName,
        ordinal: existingLevels.length,
        key_attribute_id: levelKeyAttributeId,
        key_attribute_source: attributeSourceForSelection(selectedLevelKeyAttr.is_user_defined),
        time_unit: levelTimeUnit || null,
        allowed_time_calcs: levelAllowedCalcs,
        attributes:
          selectedLevelDisplayAttr == null
            ? []
            : [
                {
                  attribute_id: selectedLevelDisplayAttr.id,
                  attribute_source: attributeSourceForSelection(
                    selectedLevelDisplayAttr.is_user_defined,
                  ),
                  role: "display",
                },
              ],
      };
      return hierarchiesApi.createLevel(projectId!, modelId!, selectedHierarchyId, payload);
    },
    onSuccess: () => {
      refreshHierarchyQueries();
      setLevelDialogOpen(false);
    },
  });

  const updateLevel = useMutation({
    mutationFn: () => {
      if (!selectedHierarchyId || !editingLevelId || !selectedLevelKeyAttr) {
        throw new Error("Hierarchy, level, and key attribute are required");
      }
      return hierarchiesApi.updateLevel(
        projectId!,
        modelId!,
        selectedHierarchyId,
        editingLevelId,
        {
          name: levelName,
          key_attribute_id: levelKeyAttributeId,
          key_attribute_source: attributeSourceForSelection(
            selectedLevelKeyAttr.is_user_defined,
          ),
          time_unit: levelTimeUnit || null,
          allowed_time_calcs: levelAllowedCalcs,
          attributes:
            selectedLevelDisplayAttr == null
              ? []
              : [
                  {
                    attribute_id: selectedLevelDisplayAttr.id,
                    attribute_source: attributeSourceForSelection(
                      selectedLevelDisplayAttr.is_user_defined,
                    ),
                    role: "display",
                  },
                ],
        },
      );
    },
    onSuccess: () => {
      refreshHierarchyQueries();
      setLevelDialogOpen(false);
      setEditingLevelId(null);
    },
  });

  const deleteLevel = useMutation({
    mutationFn: (levelId: string) =>
      hierarchiesApi.deleteLevel(projectId!, modelId!, selectedHierarchyId, levelId),
    onSuccess: () => {
      refreshHierarchyQueries();
    },
  });

  const reorderLevels = useMutation({
    mutationFn: (orderedLevelIds: string[]) =>
      hierarchiesApi.reorderLevels(projectId!, modelId!, selectedHierarchyId, {
        level_ids_in_order: orderedLevelIds,
      }),
    onSuccess: () => {
      refreshHierarchyQueries();
    },
  });

  const generateDateHierarchy = useMutation({
    mutationFn: () => {
      if (!selectedDateAttribute) {
        throw new Error("Date source attribute is required");
      }
      return hierarchiesApi.generateDate(projectId!, modelId!, {
        name: dateHierarchyName,
        description: dateHierarchyDescription || undefined,
        template: dateTemplate,
        source_attribute_id: selectedDateAttribute.id,
        source_attribute_source: attributeSourceForSelection(selectedDateAttribute.is_user_defined),
        calendar_type: dateCalendarType || null,
        fiscal_year_start_month:
          dateCalendarType === "fiscal" && dateFiscalStartMonth
            ? dateFiscalStartMonth
            : null,
      });
    },
    onSuccess: (res) => {
      refreshHierarchyQueries();
      setSelectedHierarchyId(res.hierarchy.id);
      setDateDialogOpen(false);
    },
  });

  const generateSegmentHierarchy = useMutation({
    mutationFn: () => {
      if (!selectedSegmentAttribute) {
        throw new Error("Segment source attribute is required");
      }
      const payload: HierarchyGenerateSegmentRequest = {
        name: segmentHierarchyName,
        description: segmentHierarchyDescription || undefined,
        source_attribute_id: selectedSegmentAttribute.id,
        source_attribute_source: attributeSourceForSelection(selectedSegmentAttribute.is_user_defined),
        mode: segmentMode,
      };
      if (segmentMode === "delimiter") {
        const levels = segmentLevelNames
          .split(",")
          .map((v) => v.trim())
          .filter(Boolean)
          .map((name) => ({ name }));
        if (levels.length < 2) {
          throw new Error(t("hierarchies.delimiterTwoLevels"));
        }
        payload.delimiter = segmentDelimiter;
        payload.levels = levels;
      } else {
        const segments = parsePositionalSegments(segmentPositions, t);
        if (segments.length < 2) {
          throw new Error(t("hierarchies.positionalTwoSegments"));
        }
        payload.segments = segments;
      }
      return hierarchiesApi.generateSegment(projectId!, modelId!, payload);
    },
    onSuccess: (res) => {
      refreshHierarchyQueries();
      setSelectedHierarchyId(res.hierarchy.id);
      setSegmentDialogOpen(false);
    },
  });

  const dateIntelMut = useMutation({
    mutationFn: () =>
      hierarchiesApi.batchCreateDate(projectId!, modelId!, {
        grain: dateIntelGrain,
        calendar_table_id: dateIntelCalendarId,
        column_ids: Array.from(dateIntelSelected),
      }),
    onSuccess: () => {
      refreshHierarchyQueries();
      setDateIntelOpen(false);
    },
  });

  const confirm = useConfirm();
  async function handleDeleteHierarchy(id: string, name: string) {
    const ok = await confirm({
      title: t("hierarchies.deleteConfirm"),
      message: (
        <span>
          {t("hierarchies.deleteMessage", { name })}
        </span>
      ),
      confirmLabel: t("hierarchies.delete"),
    });
    if (ok) deleteHierarchy.mutate(id);
  }
  async function handleDeleteLevel(id: string, lvlName: string) {
    const ok = await confirm({
      title: t("hierarchies.deleteLevelConfirm"),
      message: (
        <span>
          {t("hierarchies.deleteLevelMessage", { name: lvlName })}
        </span>
      ),
      confirmLabel: t("hierarchies.deleteLevel"),
    });
    if (ok) deleteLevel.mutate(id);
  }
  const runPreview = useMutation({
    mutationFn: () => {
      if (!selectedHierarchyId) {
        throw new Error("Select a hierarchy first");
      }
      return hierarchiesApi.preview(projectId!, modelId!, selectedHierarchyId, {
        sample_size: previewSampleSize,
        expand_level: previewLevelOrdinal,
        parent_key: previewParentKey || undefined,
      });
    },
    onSuccess: (data) => {
      setPreviewError(null);
      setPreviewData({
        levels_summary: data.levels_summary,
        warnings: data.warnings,
        members: data.members.map((m) => ({
          key_value: m.key_value,
          level_name: m.level_name,
          level_ordinal: m.level_ordinal,
        })),
      });
    },
    onError: (err) => {
      setPreviewData(null);
      setPreviewError(err instanceof Error ? err.message : "Preview failed");
    },
  });

  function openCreateDialog() {
    setEditingHierarchyId(null);
    setNewHierarchyName("");
    setNewHierarchyDescription("");
    setNewHierarchyType("explicit");
    setNewHierarchyDimensionKind("");
    setNewCalendarType("");
    setNewFiscalStartMonth(1);
    setCreateDialogOpen(true);
  }

  function openEditHierarchyDialog(h: Hierarchy) {
    setEditingHierarchyId(h.id);
    setNewHierarchyName(h.name);
    setNewHierarchyDescription(h.description ?? "");
    setNewHierarchyType(h.type);
    setNewHierarchyDimensionKind((h.dimension_kind ?? "") as HierarchyDimensionKind | "");
    setNewCalendarType((h.calendar_type ?? "") as CalendarType | "");
    setNewFiscalStartMonth(h.fiscal_year_start_month ?? 1);
    setCreateDialogOpen(true);
  }

  function closeHierarchyDialog() {
    setCreateDialogOpen(false);
    setEditingHierarchyId(null);
  }

  function openGenerateDateDialog() {
    setDateHierarchyName("");
    setDateHierarchyDescription("");
    setDateSourceTableId("");
    setDateSourceAttributeId("");
    setDateTemplate("y_m_d");
    setDateCalendarType("");
    setDateFiscalStartMonth(1);
    setDateDialogOpen(true);
  }

  function openGenerateSegmentDialog() {
    setSegmentHierarchyName("");
    setSegmentHierarchyDescription("");
    setSegmentSourceTableId("");
    setSegmentSourceAttributeId("");
    setSegmentMode("delimiter");
    setSegmentDelimiter("-");
    setSegmentLevelNames("Group, Category, Detail");
    setSegmentPositions("Group:1:2\nCategory:3:2\nDetail:5:2");
    setSegmentDialogOpen(true);
  }

  function openAddLevelDialog() {
    setEditingLevelId(null);
    setLevelName("");
    setLevelTableId("");
    setLevelKeyAttributeId("");
    setLevelDisplayAttributeId("");
    setLevelTimeUnit("");
    setLevelAllowedCalcs([]);
    setLevelDialogOpen(true);
  }

  function openEditLevelDialog(level: HierarchyLevel) {
    setEditingLevelId(level.id);
    setLevelName(level.name);
    setLevelTableId(level.key_attribute.table_id);
    setLevelKeyAttributeId(level.key_attribute.id);
    const displayAttr = level.attributes.find((a) => a.role === "display");
    setLevelDisplayAttributeId(displayAttr?.attribute.id ?? "");
    setLevelTimeUnit(level.time_unit ?? "");
    setLevelAllowedCalcs(level.allowed_time_calcs ?? []);
    setLevelDialogOpen(true);
  }

  function moveLevel(levelId: string, direction: -1 | 1) {
    if (!selectedHierarchy.data) return;
    const ordered = selectedHierarchy.data.levels.slice().sort((a, b) => a.ordinal - b.ordinal);
    const idx = ordered.findIndex((l) => l.id === levelId);
    if (idx < 0) return;
    const target = idx + direction;
    if (target < 0 || target >= ordered.length) return;
    const swapped = ordered.slice();
    const tmp = swapped[idx];
    swapped[idx] = swapped[target];
    swapped[target] = tmp;
    reorderLevels.mutate(swapped.map((l) => l.id));
  }

  const sortedLevels = useMemo(
    () => (selectedHierarchy.data?.levels ?? []).slice().sort((a, b) => a.ordinal - b.ordinal),
    [selectedHierarchy.data],
  );

  return (
    <Box sx={{ display: "flex", gap: 0, alignItems: "flex-start" }}>
      {/* ── Left pane: list ── */}
      <Box
        sx={{
          width: "40%",
          maxWidth: 360,
          flexShrink: 0,
          borderRight: 1,
          borderColor: "divider",
          pr: 2,
        }}
      >
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          {t("hierarchies.description")}
        </Typography>

        {/* Single "New" button opens a menu */}
        <Box sx={{ mb: 1.5 }}>
          <Button
            size="small"
            variant="contained"
            startIcon={<AddIcon />}
            endIcon={<ArrowDropDownIcon />}
            onClick={(e) => setNewMenuAnchor(e.currentTarget)}
          >
            {t("hierarchies.new")}
          </Button>
          <Menu
            anchorEl={newMenuAnchor}
            open={Boolean(newMenuAnchor)}
            onClose={() => setNewMenuAnchor(null)}
          >
            <MenuItem
              onClick={() => {
                setNewMenuAnchor(null);
                openCreateDialog();
              }}
            >
              {t("hierarchies.customHierarchy")}
            </MenuItem>
            <MenuItem
              onClick={() => {
                setNewMenuAnchor(null);
                openGenerateDateDialog();
              }}
            >
              {t("hierarchies.fromDateColumn")}
            </MenuItem>
            <MenuItem
              onClick={() => {
                setNewMenuAnchor(null);
                setDateIntelSelected(new Set());
                setDateIntelGrain("y_m_d");
                setDateIntelCalendarId(calendarModelTables[0]?.id ?? "");
                dateIntelMut.reset();
                setDateIntelOpen(true);
              }}
            >
              {t("hierarchies.dateIntelligence")}
            </MenuItem>
            <MenuItem
              onClick={() => {
                setNewMenuAnchor(null);
                openGenerateSegmentDialog();
              }}
            >
              {t("hierarchies.fromSegmentColumn")}
            </MenuItem>
          </Menu>
        </Box>

        {/* Hierarchy list */}
        {hierarchies.isLoading ? (
          <CircularProgress size={20} />
        ) : (hierarchies.data ?? []).length === 0 ? (
          <Typography variant="body2" color="text.secondary" sx={{ py: 1.5 }}>
            {t("hierarchies.none")}
          </Typography>
        ) : (
          <List dense disablePadding>
            {(hierarchies.data ?? []).map((h) => (
              <ListItemButton
                key={h.id}
                selected={selectedHierarchyId === h.id}
                onClick={() => setSelectedHierarchyId(h.id)}
                sx={{
                  borderRadius: 1,
                  mb: 0.25,
                  pr: 0.5,
                  alignItems: "flex-start",
                  "& .row-actions": { display: "none" },
                  "&:hover .row-actions": { display: "flex" },
                  "&.Mui-selected .row-actions": { display: "flex" },
                }}
              >
                <Box sx={{ flex: 1, minWidth: 0 }}>
                  <Box sx={{ display: "flex", alignItems: "center", gap: 0.75 }}>
                    <Box
                      component="span"
                      sx={{
                        width: 7,
                        height: 7,
                        borderRadius: "50%",
                        flexShrink: 0,
                        mt: "2px",
                        bgcolor:
                          healthMap.get(h.id) === "error"
                            ? "error.main"
                            : healthMap.get(h.id) === "warning"
                              ? "warning.main"
                              : "success.main",
                      }}
                    />
                    <Typography variant="body2" fontWeight={500} noWrap sx={{ flex: 1 }}>
                      {h.name}
                    </Typography>
                    <Typography
                      variant="caption"
                      sx={{
                        px: 0.5,
                        py: 0.125,
                        borderRadius: 0.5,
                        bgcolor: ui.mutedBg,
                        color: ui.muted,
                        fontSize: 10,
                        flexShrink: 0,
                      }}
                    >
                      {h.type}
                    </Typography>
                  </Box>
                  {h.level_names && h.level_names.length > 0 ? (
                    <Typography
                      variant="caption"
                      color="text.secondary"
                      noWrap
                      sx={{ display: "block", pl: 1.75 }}
                    >
                      {h.level_names.join(" → ")}
                    </Typography>
                  ) : null}
                </Box>
                <Box
                  className="row-actions"
                  sx={{ display: "none", alignItems: "center", flexShrink: 0, ml: 0.5 }}
                >
                  <Tooltip title={t("common.edit")}>
                    <IconButton
                      size="small"
                      onClick={(e) => {
                        e.stopPropagation();
                        openEditHierarchyDialog(h);
                      }}
                    >
                      <EditIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                  <Tooltip title={t("common.delete")}>
                    <IconButton
                      size="small"
                      onClick={(e) => {
                        e.stopPropagation();
                        handleDeleteHierarchy(h.id, h.name);
                      }}
                    >
                      <DeleteIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                </Box>
              </ListItemButton>
            ))}
          </List>
        )}
      </Box>

      {/* ── Right pane: detail ── */}
      <Box sx={{ flex: 1, pl: 2.5, minWidth: 0 }}>
        {!selectedHierarchyId ? (
          <Box
            sx={{
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              justifyContent: "center",
              minHeight: 300,
              textAlign: "center",
            }}
          >
            <AccountTreeOutlinedIcon sx={{ fontSize: 48, mb: 1.5, color: "text.disabled" }} />
            <Typography variant="body1" fontWeight={500} color="text.secondary">
              {t("hierarchies.noSelection")}
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
              {t("hierarchies.noSelectionHelp")}
            </Typography>
          </Box>
        ) : selectedHierarchy.isLoading ? (
          <Box sx={{ display: "flex", justifyContent: "center", pt: 4 }}>
            <CircularProgress size={24} />
          </Box>
        ) : selectedHierarchy.data ? (
          <Box>
            {/* Hierarchy header */}
            <Box sx={{ mb: 2 }}>
              <Typography variant="h6" fontWeight={700}>
                {selectedHierarchy.data.name}
              </Typography>
              {selectedHierarchy.data.description ? (
                <Typography variant="body2" color="text.secondary" sx={{ mt: 0.25 }}>
                  {selectedHierarchy.data.description}
                </Typography>
              ) : null}
              <Box sx={{ display: "flex", gap: 0.75, mt: 0.75, flexWrap: "wrap" }}>
                <Typography
                  variant="caption"
                  sx={{
                    px: 0.75,
                    py: 0.25,
                    borderRadius: 0.5,
                    bgcolor: ui.mutedBg,
                    color: ui.muted,
                    fontSize: 11,
                  }}
                >
                  {selectedHierarchy.data.type}
                </Typography>
                {selectedHierarchy.data.dimension_kind ? (
                  <Typography
                    variant="caption"
                    sx={{
                      px: 0.75,
                      py: 0.25,
                      borderRadius: 0.5,
                      fontSize: 11,
                      bgcolor:
                        selectedHierarchy.data.dimension_kind === "time"
                          ? ui.greenBg
                          : ui.mutedBg,
                      color:
                        selectedHierarchy.data.dimension_kind === "time"
                          ? ui.green
                          : ui.muted,
                    }}
                  >
                    {selectedHierarchy.data.dimension_kind} {t("hierarchies.dimensionSuffix")}
                  </Typography>
                ) : null}
                {selectedHierarchy.data.calendar_type ? (
                  <Typography
                    variant="caption"
                    sx={{
                      px: 0.75,
                      py: 0.25,
                      borderRadius: 0.5,
                      fontSize: 11,
                      bgcolor: ui.mutedBg,
                      color: ui.muted,
                    }}
                  >
                    {t(calendarTypeLabelKey(selectedHierarchy.data.calendar_type as CalendarType))}
                    {selectedHierarchy.data.calendar_type === "fiscal" &&
                      selectedHierarchy.data.fiscal_year_start_month
                      ? ` (${t(MONTH_NAMES[selectedHierarchy.data.fiscal_year_start_month - 1])})`
                      : ""}
                  </Typography>
                ) : null}
              </Box>
            </Box>

            {/* Calendar type info for time hierarchies */}
            {isTimeHierarchy && selectedHierarchy.data.calendar_type ? (
              <Alert severity="info" sx={{ mb: 2 }}>
                {t("hierarchies.calendarTypeAlertPrefix")}{" "}
                <strong>{t(calendarTypeLabelKey(selectedHierarchy.data.calendar_type as CalendarType))}</strong>
                {selectedHierarchy.data.calendar_type === "fiscal" &&
                  selectedHierarchy.data.fiscal_year_start_month
                  ? t("hierarchies.fiscalStartInfo", { month: t(MONTH_NAMES[selectedHierarchy.data.fiscal_year_start_month - 1]) })
                  : ""}
                {t("hierarchies.calendarTypeAlertSuffix")}
              </Alert>
            ) : isTimeHierarchy ? (
              <Alert severity="info" sx={{ mb: 2 }}>
                {t("hierarchies.noCalendarType")}
              </Alert>
            ) : null}

            <Divider sx={{ mb: 2.5 }} />

            {/* ── Section 1: Drill path ── */}
            <Box sx={{ mb: 3 }}>
              <Box sx={{ display: "flex", alignItems: "flex-start", mb: 1 }}>
                <Box sx={{ flex: 1 }}>
                  <Typography variant="subtitle2" fontWeight={700}>
                    {t("hierarchies.drillPath")}
                  </Typography>
                  <Typography variant="caption" color="text.secondary">
                    {t("hierarchies.drillPathHelp")}
                  </Typography>
                </Box>
                <Button size="small" startIcon={<AddIcon />} onClick={openAddLevelDialog} sx={{ ml: 1, flexShrink: 0 }}>
                  {t("hierarchies.addLevel")}
                </Button>
              </Box>

              {sortedLevels.length > 0 ? (
                <>
                  <Box
                    display="flex"
                    alignItems="center"
                    flexWrap="wrap"
                    gap={0.5}
                    mb={1}
                    px={1}
                    py={0.75}
                    sx={{ bgcolor: ui.tableHeaderBg, borderRadius: 1 }}
                  >
                    {sortedLevels.map((lvl, idx, arr) => (
                      <Box key={lvl.id} display="flex" alignItems="center">
                        <Chip
                          label={lvl.time_unit ? `${lvl.name} (${lvl.time_unit})` : lvl.name}
                          size="small"
                          sx={{
                            bgcolor: lvl.time_unit ? ui.greenBg : ui.mutedBg,
                            color: lvl.time_unit ? ui.green : ui.muted,
                            fontWeight: 500,
                          }}
                        />
                        {idx < arr.length - 1 ? (
                          <ChevronRightIcon
                            fontSize="small"
                            sx={{ color: "text.secondary", mx: 0.25 }}
                          />
                        ) : null}
                      </Box>
                    ))}
                  </Box>

                  <TableContainer component={Paper} variant="outlined">
                    <Table size="small">
                      <TableHead>
                        <TableRow sx={{ bgcolor: ui.tableHeaderBg }}>
                          <TableCell><strong>{t("hierarchies.colRank")}</strong></TableCell>
                          <TableCell><strong>{t("hierarchies.colName")}</strong></TableCell>
                          <TableCell><strong>{t("hierarchies.colKeyAttribute")}</strong></TableCell>
                          <TableCell><strong>{t("hierarchies.colTimeUnit")}</strong></TableCell>
                          <TableCell><strong>{t("hierarchies.colAllowedCalcs")}</strong></TableCell>
                          <TableCell />
                        </TableRow>
                      </TableHead>
                      <TableBody>
                        {sortedLevels.map((lvl, idx, arr) => (
                          <TableRow key={lvl.id}>
                            <TableCell>{lvl.ordinal}</TableCell>
                            <TableCell>{lvl.name}</TableCell>
                            <TableCell>
                              <Typography
                                variant="caption"
                                sx={{
                                  fontFamily: "monospace",
                                  fontSize: 11,
                                  color:
                                    lvl.key_attribute.source === "user_defined_attribute"
                                      ? ui.purple
                                      : ui.muted,
                                }}
                              >
                                {lvl.key_attribute.table_name}.{lvl.key_attribute.name}
                              </Typography>
                            </TableCell>
                            <TableCell>
                              {lvl.time_unit ? (
                                <Typography
                                  variant="caption"
                                  sx={{
                                    px: 0.5,
                                    py: 0.125,
                                    borderRadius: 0.5,
                                    bgcolor: ui.greenBg,
                                    color: ui.green,
                                    fontWeight: 500,
                                    fontSize: 11,
                                  }}
                                >
                                  {lvl.time_unit}
                                </Typography>
                              ) : (
                                <Typography variant="caption" color="text.secondary">—</Typography>
                              )}
                            </TableCell>
                            <TableCell>
                              {lvl.allowed_time_calcs && lvl.allowed_time_calcs.length > 0 ? (
                                <Box display="flex" flexWrap="wrap" gap={0.25}>
                                  {lvl.allowed_time_calcs.map((c) => (
                                    <Typography
                                      key={c}
                                      variant="caption"
                                      sx={{
                                        px: 0.5,
                                        py: 0.125,
                                        borderRadius: 0.5,
                                        bgcolor: ui.mutedBg,
                                        color: ui.muted,
                                        fontSize: 10,
                                      }}
                                    >
                                      {c}
                                    </Typography>
                                  ))}
                                </Box>
                              ) : (
                                <Typography variant="caption" color="text.secondary">—</Typography>
                              )}
                            </TableCell>
                            <TableCell align="right">
                              <Tooltip title={t("hierarchies.moveUp")}>
                                <span>
                                  <IconButton
                                    size="small"
                                    onClick={() => moveLevel(lvl.id, -1)}
                                    disabled={idx === 0 || reorderLevels.isPending}
                                  >
                                    <ArrowUpwardIcon fontSize="small" />
                                  </IconButton>
                                </span>
                              </Tooltip>
                              <Tooltip title={t("hierarchies.moveDown")}>
                                <span>
                                  <IconButton
                                    size="small"
                                    onClick={() => moveLevel(lvl.id, 1)}
                                    disabled={idx === arr.length - 1 || reorderLevels.isPending}
                                  >
                                    <ArrowDownwardIcon fontSize="small" />
                                  </IconButton>
                                </span>
                              </Tooltip>
                              <Tooltip title={t("hierarchies.editLevel")}>
                                <IconButton size="small" onClick={() => openEditLevelDialog(lvl)}>
                                  <EditIcon fontSize="small" />
                                </IconButton>
                              </Tooltip>
                              <Tooltip title={t("hierarchies.deleteLevel")}>
                                <IconButton
                                  size="small"
                                  onClick={() => handleDeleteLevel(lvl.id, lvl.name)}
                                >
                                  <DeleteIcon fontSize="small" />
                                </IconButton>
                              </Tooltip>
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </TableContainer>
                </>
              ) : (
                <Typography variant="body2" color="text.secondary" sx={{ py: 1 }}>
                  {t("hierarchies.noLevels")}
                </Typography>
              )}
            </Box>

            <Divider sx={{ mb: 2.5 }} />

            {/* ── Section 2: Data preview ── */}
            <Box>
              <Typography variant="subtitle2" fontWeight={700} gutterBottom>
                {t("hierarchies.dataPreview", { name: selectedHierarchy.data.name })}
              </Typography>
              <Box display="flex" gap={1} flexWrap="wrap" mb={1}>
                <TextField
                  size="small"
                  type="number"
                  label={t("hierarchies.sampleSizeLabel")}
                  value={previewSampleSize}
                  onChange={(e) => setPreviewSampleSize(Number(e.target.value || "10"))}
                  sx={{ width: 130 }}
                />
                <FormControl size="small" sx={{ width: 170 }}>
                  <InputLabel>{t("hierarchies.levelLabel")}</InputLabel>
                  <Select
                    value={String(previewLevelOrdinal)}
                    label={t("hierarchies.levelLabel")}
                    onChange={(e) => {
                      const ord = Number(e.target.value);
                      setPreviewLevelOrdinal(ord);
                      if (ord === 0) setPreviewParentKey("");
                    }}
                  >
                    {sortedLevels.map((lvl) => (
                      <MenuItem key={lvl.id} value={String(lvl.ordinal)}>
                        {lvl.ordinal} – {lvl.name}
                      </MenuItem>
                    ))}
                  </Select>
                </FormControl>
                <TextField
                  size="small"
                  label={t("hierarchies.filterByParentLabel")}
                  placeholder={previewLevelOrdinal === 0 ? t("hierarchies.naRootLevel") : t("hierarchies.leaveEmptyForAll")}
                  disabled={previewLevelOrdinal === 0}
                  value={previewParentKey}
                  onChange={(e) => setPreviewParentKey(e.target.value)}
                  sx={{ minWidth: 170 }}
                />
                <Button
                  size="small"
                  variant="outlined"
                  startIcon={<RefreshIcon />}
                  onClick={() => runPreview.mutate()}
                  disabled={runPreview.isPending}
                >
                  {t("hierarchies.runPreview")}
                </Button>
              </Box>

              {previewError ? (
                <Alert severity="error" sx={{ mb: 1 }}>
                  {previewError}
                </Alert>
              ) : null}
              {previewData ? (
                <Box>
                  {previewData.warnings.length > 0 ? (
                    <Box display="flex" flexDirection="column" gap={0.5} mb={1}>
                      {previewData.warnings.map((w, i) => (
                        <Alert key={i} severity="warning" sx={{ fontSize: 12 }}>
                          {w.message}
                        </Alert>
                      ))}
                    </Box>
                  ) : null}
                  <Typography variant="caption" color="text.secondary">
                    Members: {previewData.members.length}
                  </Typography>
                  <TableContainer component={Paper} variant="outlined" sx={{ mt: 0.5 }}>
                    <Table size="small">
                      <TableHead>
                        <TableRow sx={{ bgcolor: ui.tableHeaderBg }}>
                          <TableCell><strong>{t("hierarchies.levelLabel")}</strong></TableCell>
                          <TableCell><strong>{t("hierarchies.colKey")}</strong></TableCell>
                        </TableRow>
                      </TableHead>
                      <TableBody>
                        {previewData.members.map((m, idx) => (
                          <TableRow key={`${m.key_value}-${idx}`}>
                            <TableCell>{m.level_name}</TableCell>
                            <TableCell>{m.key_value}</TableCell>
                          </TableRow>
                        ))}
                        {previewData.members.length === 0 ? (
                          <TableRow>
                            <TableCell colSpan={2}>
                              <Typography variant="body2" color="text.secondary">
                                {t("hierarchies.noMembers")}
                              </Typography>
                            </TableCell>
                          </TableRow>
                        ) : null}
                      </TableBody>
                    </Table>
                  </TableContainer>
                </Box>
              ) : null}
            </Box>
          </Box>
        ) : (
          <Alert severity="warning">{t("hierarchies.loadError")}</Alert>
        )}
      </Box>

      {/* ── Dialogs (all unchanged) ── */}

      <Dialog open={dateDialogOpen} onClose={() => setDateDialogOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle>{t("hierarchies.generateDateTitle")}</DialogTitle>
        <DialogContent>
          <TextField
            label={t("hierarchies.nameLabel")}
            fullWidth
            margin="normal"
            value={dateHierarchyName}
            onChange={(e) => setDateHierarchyName(e.target.value)}
            autoFocus
          />
          <FormControl fullWidth margin="dense">
            <InputLabel>{t("hierarchies.templateLabel")}</InputLabel>
            <Select
              value={dateTemplate}
              label={t("hierarchies.templateLabel")}
              onChange={(e) => setDateTemplate(e.target.value as HierarchyGenerateDateRequest["template"])}
            >
              {DATE_TEMPLATES.map((tpl) => (
                <MenuItem key={tpl} value={tpl}>
                  {tpl}
                </MenuItem>
              ))}
            </Select>
          </FormControl>
          <FormControl fullWidth margin="dense">
            <InputLabel>{t("hierarchies.sourceTableLabel")}</InputLabel>
            <Select
              value={dateSourceTableId}
              label={t("hierarchies.sourceTableLabel")}
              onChange={(e) => {
                setDateSourceTableId(e.target.value);
                setDateSourceAttributeId("");
              }}
            >
              {(allTables.data ?? []).map((t) => (
                <MenuItem key={t.id} value={t.id}>
                  {t.alias || t.display_name} ({t.physical_name})
                </MenuItem>
              ))}
            </Select>
          </FormControl>
          <FormControl fullWidth margin="dense" disabled={!dateSourceTableId}>
            <InputLabel>{t("hierarchies.sourceAttributeLabel")}</InputLabel>
            <Select
              value={dateSourceAttributeId}
              label={t("hierarchies.sourceAttributeLabel")}
              onChange={(e) => setDateSourceAttributeId(e.target.value)}
            >
              {(dateTableAttributes.data ?? []).map((a) => (
                <MenuItem key={a.id} value={a.id}>
                  {a.is_user_defined ? `${t("hierarchies.computedPrefix")}${a.name}` : a.name} ({a.data_type})
                </MenuItem>
              ))}
            </Select>
          </FormControl>
          <FormControl fullWidth margin="dense">
            <InputLabel>{t("hierarchies.calendarTypeLabel")}</InputLabel>
            <Select
              value={dateCalendarType}
              label={t("hierarchies.calendarTypeLabel")}
              onChange={(e) =>
                setDateCalendarType(e.target.value as CalendarType | "")
              }
            >
              <MenuItem value="">{t("common.unspecified")}</MenuItem>
              {CALENDAR_TYPES.map((v) => (
                <MenuItem key={v} value={v}>
                  {t(calendarTypeLabelKey(v))}
                </MenuItem>
              ))}
            </Select>
          </FormControl>
          {dateCalendarType === "fiscal" && (
            <FormControl fullWidth margin="dense">
              <InputLabel>{t("hierarchies.fiscalYearStartLabel")}</InputLabel>
              <Select
                value={dateFiscalStartMonth}
                label={t("hierarchies.fiscalYearStartLabel")}
                onChange={(e) =>
                  setDateFiscalStartMonth(e.target.value as number)
                }
              >
                {MONTH_NAMES.map((name, idx) => (
                  <MenuItem key={idx + 1} value={idx + 1}>
                    {t(name)}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
          )}
          <TextField
            label={t("hierarchies.descriptionLabel")}
            fullWidth
            margin="normal"
            value={dateHierarchyDescription}
            onChange={(e) => setDateHierarchyDescription(e.target.value)}
          />
          {generateDateHierarchy.isError ? (
            <Alert severity="error" sx={{ mt: 1 }}>
              {extractApiError(generateDateHierarchy.error, t("hierarchies.generateDateFailed"))}
            </Alert>
          ) : null}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDateDialogOpen(false)}>{t("common.cancel")}</Button>
          <Button
            variant="contained"
            onClick={() => generateDateHierarchy.mutate()}
            disabled={!dateHierarchyName.trim() || !dateSourceAttributeId || generateDateHierarchy.isPending}
          >
            {generateDateHierarchy.isPending ? <CircularProgress size={18} /> : t("hierarchies.generate")}
          </Button>
        </DialogActions>
      </Dialog>

      <Dialog open={segmentDialogOpen} onClose={() => setSegmentDialogOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle>{t("hierarchies.generateSegmentTitle")}</DialogTitle>
        <DialogContent>
          <TextField
            label={t("hierarchies.nameLabel")}
            fullWidth
            margin="normal"
            value={segmentHierarchyName}
            onChange={(e) => setSegmentHierarchyName(e.target.value)}
            autoFocus
          />
          <FormControl fullWidth margin="dense">
            <InputLabel>{t("hierarchies.sourceTableLabel")}</InputLabel>
            <Select
              value={segmentSourceTableId}
              label={t("hierarchies.sourceTableLabel")}
              onChange={(e) => {
                setSegmentSourceTableId(e.target.value);
                setSegmentSourceAttributeId("");
              }}
            >
              {(allTables.data ?? []).map((t) => (
                <MenuItem key={t.id} value={t.id}>
                  {t.alias || t.display_name} ({t.physical_name})
                </MenuItem>
              ))}
            </Select>
          </FormControl>
          <FormControl fullWidth margin="dense" disabled={!segmentSourceTableId}>
            <InputLabel>{t("hierarchies.sourceAttributeLabel")}</InputLabel>
            <Select
              value={segmentSourceAttributeId}
              label={t("hierarchies.sourceAttributeLabel")}
              onChange={(e) => setSegmentSourceAttributeId(e.target.value)}
            >
              {(segmentTableAttributes.data ?? []).map((a) => (
                <MenuItem key={a.id} value={a.id}>
                  {a.is_user_defined ? `${t("hierarchies.computedPrefix")}${a.name}` : a.name}
                </MenuItem>
              ))}
            </Select>
          </FormControl>
          <FormControl fullWidth margin="dense">
            <InputLabel>{t("hierarchies.modeLabel")}</InputLabel>
            <Select
              value={segmentMode}
              label={t("hierarchies.modeLabel")}
              onChange={(e) => setSegmentMode(e.target.value as HierarchyGenerateSegmentRequest["mode"])}
            >
              <MenuItem value="delimiter">{t("hierarchies.modeDelimiter")}</MenuItem>
              <MenuItem value="positional">{t("hierarchies.modePositional")}</MenuItem>
            </Select>
          </FormControl>
          {segmentMode === "delimiter" ? (
            <>
              <TextField
                label={t("hierarchies.delimiterLabel")}
                fullWidth
                margin="normal"
                value={segmentDelimiter}
                onChange={(e) => setSegmentDelimiter(e.target.value)}
              />
              <TextField
                label={t("hierarchies.levelNamesLabel")}
                fullWidth
                margin="normal"
                value={segmentLevelNames}
                onChange={(e) => setSegmentLevelNames(e.target.value)}
                helperText={t("hierarchies.levelNamesExample")}
              />
            </>
          ) : (
            <TextField
              label={t("hierarchies.segmentsLabel")}
              fullWidth
              margin="normal"
              value={segmentPositions}
              onChange={(e) => setSegmentPositions(e.target.value)}
              multiline
              minRows={4}
              helperText={t("hierarchies.segmentsExample")}
            />
          )}
          <TextField
            label={t("hierarchies.descriptionLabel")}
            fullWidth
            margin="normal"
            value={segmentHierarchyDescription}
            onChange={(e) => setSegmentHierarchyDescription(e.target.value)}
          />
          {generateSegmentHierarchy.isError ? (
            <Alert severity="error" sx={{ mt: 1 }}>
              {extractApiError(generateSegmentHierarchy.error, t("hierarchies.generateSegmentFailed"))}
            </Alert>
          ) : null}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setSegmentDialogOpen(false)}>{t("common.cancel")}</Button>
          <Button
            variant="contained"
            onClick={() => generateSegmentHierarchy.mutate()}
            disabled={!segmentHierarchyName.trim() || !segmentSourceAttributeId || generateSegmentHierarchy.isPending}
          >
            {generateSegmentHierarchy.isPending ? <CircularProgress size={18} /> : t("hierarchies.generate")}
          </Button>
        </DialogActions>
      </Dialog>

      <Dialog open={createDialogOpen} onClose={closeHierarchyDialog} maxWidth="sm" fullWidth>
        <DialogTitle>{editingHierarchyId ? t("hierarchies.editTitle") : t("hierarchies.addTitle")}</DialogTitle>
        <DialogContent>
          <TextField
            label={t("hierarchies.nameLabel")}
            fullWidth
            margin="normal"
            value={newHierarchyName}
            onChange={(e) => setNewHierarchyName(e.target.value)}
            autoFocus
          />
          <FormControl fullWidth margin="dense">
            <InputLabel>{t("hierarchies.typeLabel")}</InputLabel>
            <Select
              value={newHierarchyType}
              label={t("hierarchies.typeLabel")}
              onChange={(e) => setNewHierarchyType(e.target.value as HierarchyCreate["type"])}
            >
              {HIERARCHY_TYPES.map((v) => (
                <MenuItem key={v} value={v}>
                  {v}
                </MenuItem>
              ))}
            </Select>
          </FormControl>
          <FormControl fullWidth margin="dense">
            <InputLabel>{t("hierarchies.dimensionKindLabel")}</InputLabel>
            <Select
              value={newHierarchyDimensionKind}
              label={t("hierarchies.dimensionKindLabel")}
              onChange={(e) =>
                setNewHierarchyDimensionKind(e.target.value as HierarchyDimensionKind | "")
              }
            >
              <MenuItem value="">{t("common.unspecified")}</MenuItem>
              {DIMENSION_KINDS.map((v) => (
                <MenuItem key={v} value={v}>
                  {v}
                </MenuItem>
              ))}
            </Select>
          </FormControl>
          {(newHierarchyDimensionKind === "time" || newHierarchyType === "date_embedded") && (
            <FormControl fullWidth margin="dense">
              <InputLabel>{t("hierarchies.calendarTypeLabel")}</InputLabel>
              <Select
                value={newCalendarType}
                label={t("hierarchies.calendarTypeLabel")}
                onChange={(e) =>
                  setNewCalendarType(e.target.value as CalendarType | "")
                }
              >
                <MenuItem value="">{t("common.unspecified")}</MenuItem>
                {CALENDAR_TYPES.map((v) => (
                  <MenuItem key={v} value={v}>
                    {t(calendarTypeLabelKey(v))}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
          )}
          {newCalendarType === "fiscal" && (
            <FormControl fullWidth margin="dense">
              <InputLabel>{t("hierarchies.fiscalYearStartLabel")}</InputLabel>
              <Select
                value={newFiscalStartMonth}
                label={t("hierarchies.fiscalYearStartLabel")}
                onChange={(e) =>
                  setNewFiscalStartMonth(e.target.value as number)
                }
              >
                {MONTH_NAMES.map((name, idx) => (
                  <MenuItem key={idx + 1} value={idx + 1}>
                    {t(name)}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
          )}
          <TextField
            label={t("hierarchies.descriptionLabel")}
            fullWidth
            margin="normal"
            value={newHierarchyDescription}
            onChange={(e) => setNewHierarchyDescription(e.target.value)}
          />
          {createHierarchy.isError ? (
            <Alert severity="error" sx={{ mt: 1 }}>
              {extractApiError(createHierarchy.error, t("hierarchies.createFailed"))}
            </Alert>
          ) : null}
          {updateHierarchy.isError ? (
            <Alert severity="error" sx={{ mt: 1 }}>
              {extractApiError(updateHierarchy.error, t("hierarchies.updateFailed"))}
            </Alert>
          ) : null}
        </DialogContent>
        <DialogActions>
          <Button onClick={closeHierarchyDialog}>{t("common.cancel")}</Button>
          <Button
            variant="contained"
            onClick={() => (editingHierarchyId ? updateHierarchy.mutate() : createHierarchy.mutate())}
            disabled={
              !newHierarchyName.trim() ||
              createHierarchy.isPending ||
              updateHierarchy.isPending
            }
          >
            {createHierarchy.isPending || updateHierarchy.isPending ? (
              <CircularProgress size={18} />
            ) : editingHierarchyId ? (
              t("common.save")
            ) : (
              t("common.add")
            )}
          </Button>
        </DialogActions>
      </Dialog>

      <Dialog open={levelDialogOpen} onClose={() => setLevelDialogOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle>{editingLevelId ? t("hierarchies.editLevelTitle") : t("hierarchies.addLevelTitle")}</DialogTitle>
        <DialogContent>
          <TextField
            label={t("hierarchies.levelNameLabel")}
            fullWidth
            margin="normal"
            value={levelName}
            onChange={(e) => setLevelName(e.target.value)}
            autoFocus
          />
          <FormControl fullWidth margin="dense">
            <InputLabel>{t("hierarchies.tableLabel")}</InputLabel>
            <Select
              value={levelTableId}
              label={t("hierarchies.tableLabel")}
              onChange={(e) => {
                setLevelTableId(e.target.value);
                setLevelKeyAttributeId("");
                setLevelDisplayAttributeId("");
              }}
            >
              {(allTables.data ?? []).map((t) => (
                <MenuItem key={t.id} value={t.id}>
                  {t.alias || t.display_name} ({t.physical_name})
                </MenuItem>
              ))}
            </Select>
          </FormControl>
          <FormControl fullWidth margin="dense" disabled={!levelTableId}>
            <InputLabel>{t("hierarchies.keyAttributeLabel")}</InputLabel>
            <Select
              value={levelKeyAttributeId}
              label={t("hierarchies.keyAttributeLabel")}
              onChange={(e) => setLevelKeyAttributeId(e.target.value)}
            >
              {(levelTableAttributes.data ?? []).map((a) => (
                <MenuItem key={a.id} value={a.id}>
                  {a.is_user_defined ? `${t("hierarchies.computedPrefix")}${a.name}` : a.name}
                </MenuItem>
              ))}
            </Select>
          </FormControl>
          <FormControl fullWidth margin="dense" disabled={!levelTableId}>
            <InputLabel>{t("hierarchies.displayAttributeLabel")}</InputLabel>
            <Select
              value={levelDisplayAttributeId}
              label={t("hierarchies.displayAttributeLabel")}
              onChange={(e) => setLevelDisplayAttributeId(e.target.value)}
            >
              <MenuItem value="">{t("common.none")}</MenuItem>
              {(levelTableAttributes.data ?? []).map((a) => (
                <MenuItem key={a.id} value={a.id}>
                  {a.is_user_defined ? `${t("hierarchies.computedPrefix")}${a.name}` : a.name}
                </MenuItem>
              ))}
            </Select>
          </FormControl>
          <FormControl fullWidth margin="dense">
            <InputLabel>{t("hierarchies.timeUnitLabel")}</InputLabel>
            <Select
              value={levelTimeUnit}
              label={t("hierarchies.timeUnitLabel")}
              onChange={(e) => setLevelTimeUnit(e.target.value as HierarchyTimeUnit | "")}
            >
              <MenuItem value="">{t("common.none")}</MenuItem>
              {TIME_UNITS.map((v) => (
                <MenuItem key={v} value={v}>
                  {v}
                </MenuItem>
              ))}
            </Select>
          </FormControl>
          <FormControl fullWidth margin="dense">
            <InputLabel>{t("hierarchies.allowedCalcsLabel")}</InputLabel>
            <Select
              multiple
              value={levelAllowedCalcs}
              label={t("hierarchies.allowedCalcsLabel")}
              onChange={(e) => {
                const v = e.target.value;
                setLevelAllowedCalcs(
                  (typeof v === "string" ? v.split(",") : v) as HierarchyTimeCalc[],
                );
              }}
              renderValue={(selected) => (
                <Box sx={{ display: "flex", flexWrap: "wrap", gap: 0.5 }}>
                  {(selected as HierarchyTimeCalc[]).map((value) => (
                    <Chip
                      key={value}
                      label={value}
                      size="small"
                      sx={{ bgcolor: ui.mutedBg, color: ui.muted }}
                    />
                  ))}
                </Box>
              )}
            >
              {TIME_CALCS.map((v) => (
                <MenuItem key={v} value={v}>
                  {v}
                </MenuItem>
              ))}
            </Select>
          </FormControl>
          {createLevel.isError ? (
            <Alert severity="error" sx={{ mt: 1 }}>
              {extractApiError(createLevel.error, t("hierarchies.createLevelFailed"))}
            </Alert>
          ) : null}
          {updateLevel.isError ? (
            <Alert severity="error" sx={{ mt: 1 }}>
              {extractApiError(updateLevel.error, t("hierarchies.updateLevelFailed"))}
            </Alert>
          ) : null}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setLevelDialogOpen(false)}>{t("common.cancel")}</Button>
          <Button
            variant="contained"
            onClick={() => (editingLevelId ? updateLevel.mutate() : createLevel.mutate())}
            disabled={
              !levelName.trim() ||
              !levelKeyAttributeId ||
              createLevel.isPending ||
              updateLevel.isPending
            }
          >
            {createLevel.isPending || updateLevel.isPending ? (
              <CircularProgress size={18} />
            ) : editingLevelId ? (
              t("common.save")
            ) : (
              t("common.add")
            )}
          </Button>
        </DialogActions>
      </Dialog>

      {/* Date Intelligence Dialog */}
      <Dialog
        open={dateIntelOpen}
        onClose={() => setDateIntelOpen(false)}
        maxWidth="sm"
        fullWidth
      >
        <DialogTitle>{t("hierarchies.dateIntelligence")}</DialogTitle>
        <DialogContent>
          <Typography variant="body2" color="text.secondary" mb={2}>
            {t("hierarchies.dateIntelligenceDesc")}
          </Typography>

          {dateIntelCols.isLoading && <CircularProgress size={20} />}

          {(dateIntelCols.data ?? []).length > 0 && (
            <Box display="flex" gap={1} mb={1}>
              <Button
                size="small"
                onClick={() =>
                  setDateIntelSelected(
                    new Set((dateIntelCols.data ?? []).map((c) => c.column_id)),
                  )
                }
              >
                {t("hierarchies.selectAll")}
              </Button>
              <Button size="small" onClick={() => setDateIntelSelected(new Set())}>
                {t("hierarchies.deselectAll")}
              </Button>
            </Box>
          )}

          {(dateIntelCols.data ?? []).length === 0 && !dateIntelCols.isLoading && (
            <Alert severity="info">
              {t("hierarchies.allDateColumnsCovered")}
            </Alert>
          )}

          {(dateIntelCols.data ?? []).map((col) => (
            <FormControlLabel
              key={col.column_id}
              control={
                <Checkbox
                  checked={dateIntelSelected.has(col.column_id)}
                  onChange={(_, checked) => {
                    setDateIntelSelected((prev) => {
                      const next = new Set(prev);
                      if (checked) next.add(col.column_id);
                      else next.delete(col.column_id);
                      return next;
                    });
                  }}
                />
              }
              label={
                <Box>
                  <Typography
                    variant="body2"
                    fontWeight={500}
                    sx={col.is_uda ? { fontStyle: "italic", color: "secondary.main" } : undefined}
                  >
                    {col.is_uda ? `${t("hierarchies.computedPrefix")}` : ""}{col.column_name}
                  </Typography>
                  <Typography variant="caption" color="text.secondary">
                    {col.table_alias} ({col.data_type}){col.is_uda ? ` ${t("hierarchies.computedSuffix")}` : ""}
                  </Typography>
                </Box>
              }
              sx={{ display: "flex", mb: 0.5 }}
            />
          ))}

          {(dateIntelCols.data ?? []).length > 0 && (
            <>
              <FormControl fullWidth margin="normal" size="small">
                <InputLabel>{t("hierarchies.calendarTableLabel")}</InputLabel>
                <Select
                  label={t("hierarchies.calendarTableLabel")}
                  value={dateIntelCalendarId}
                  onChange={(e) => setDateIntelCalendarId(e.target.value)}
                >
                  {calendarModelTables.length === 0 && (
                    <MenuItem value="" disabled>
                      {t("hierarchies.noCalendarTables")}
                    </MenuItem>
                  )}
                  {calendarModelTables.map((t) => (
                    <MenuItem key={t.id} value={t.id}>
                      {t.display_name} ({t.alias})
                    </MenuItem>
                  ))}
                </Select>
              </FormControl>
              <FormControl fullWidth margin="normal" size="small">
                <InputLabel>{t("hierarchies.grainLabel")}</InputLabel>
                <Select
                  label={t("hierarchies.grainLabel")}
                  value={dateIntelGrain}
                  onChange={(e) => setDateIntelGrain(e.target.value)}
                >
                  <MenuItem value="y_m_d">{t("hierarchies.grainYearMonthDay")}</MenuItem>
                  <MenuItem value="y_q_m_d">{t("hierarchies.grainYearQuarterMonthDay")}</MenuItem>
                  <MenuItem value="y_h_q_m_d">{t("hierarchies.grainYearHalfQuarterMonthDay")}</MenuItem>
                  <MenuItem value="y_w_d">{t("hierarchies.grainYearWeekDay")}</MenuItem>
                  <MenuItem value="y_m_w_d">{t("hierarchies.grainYearMonthWeekDay")}</MenuItem>
                </Select>
              </FormControl>
            </>
          )}
          {dateIntelMut.isError && (
            <Alert severity="error" sx={{ mt: 1 }}>
              {(() => {
                const err = dateIntelMut.error as
                  | { response?: { data?: { detail?: string } }; message?: string }
                  | undefined;
                return err?.response?.data?.detail ?? err?.message ?? t("hierarchies.createDateFailed");
              })()}
            </Alert>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDateIntelOpen(false)}>{t("common.cancel")}</Button>
          <Button
            variant="contained"
            disabled={
              dateIntelSelected.size === 0 ||
              !dateIntelCalendarId ||
              dateIntelMut.isPending
            }
            onClick={() => dateIntelMut.mutate()}
          >
            {dateIntelMut.isPending ? <CircularProgress size={16} /> : t("hierarchies.setUpSelected")}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
