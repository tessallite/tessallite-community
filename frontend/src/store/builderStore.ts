import { create } from "zustand";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type ObjectType =
  | "source"
  | "join"
  | "dimension"
  | "measure"
  | "aggregate"
  | "target";

// Single source of truth for the drawer-panel id set. Both the deep-link
// allow-list (ModelBuilder) and the drawer title/help maps (Drawer) derive
// from this so they cannot drift out of sync with the PanelId union
// (F-026-09 / F-026-10). Adding a panel here is the only edit needed for a
// new panel to be deep-linkable and titled.
export const PANEL_IDS = [
  "connections",
  "sources",
  "joins",
  "hierarchies",
  "dimensions",
  "measures",
  "aggregates",
  "pockets",
  "personas",
  "row-security",
  "refresh",
  "lineage",
  "diagnostics",
  "endpoints",
  "query",
  "measure-query",
  "glossary",
  "scheduler",
  "settings",
  "statistics",
  "predictive",
  "lifecycle",
  "parameters",
  "data-quality",
  "data-tags",
  "impact",
  "impact-analysis",
  "schema-changes",
  "named-sets",
  "saved-queries",
  "scratchpad",
  "model-docs",
  "alerts",
] as const;

export type PanelId = (typeof PANEL_IDS)[number];

// "matrix" is the internal name for what the UI now labels
// "Model Health" — kept as-is to avoid churn across every caller.
// The previously-planned "data" value was removed in Phase G2;
// the store is in-memory only, so no persisted state needs
// clamping.
export type MiniTab = "canvas" | "matrix" | "analytics" | "pivot" | "query" | "kpi-scorecard";

export type ValidationSeverity = "error" | "warning" | "info";

export type RelationNotation = "crowsfoot" | "uml" | "diamond";
export type RelationPathing = "orthogonal" | "straight";

export interface ValidationIssue {
  id: string;
  severity: ValidationSeverity;
  message: string;
  affectedObject?: string;
  affectedType?: ObjectType;
  /** Canvas table the issue points at — enables click-to-navigate. */
  tableId?: string;
  /** Source owning `tableId` (needed by the Sources-panel focus affordance). */
  sourceId?: string;
}

export type MessageSeverity = "info" | "success" | "warning" | "error";

export interface GlobalMessage {
  text: string;
  severity: MessageSeverity;
  at: number;
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

interface BuilderState {
  /* Which drawer panel is open (null = closed) */
  activePanel: PanelId | null;

  /* Whether the right-side drawer is expanded to cover the canvas */
  drawerExpanded: boolean;

  /* Currently selected object in sidebar / canvas */
  selectedObjectId: string | null;
  selectedObjectType: ObjectType | null;

  /* Which mini-tab is active */
  miniTab: MiniTab;

  /* Client-side validation issues */
  validationIssues: ValidationIssue[];

  /* Whether the validation tray is expanded (toggled from the status bar) */
  validationExpanded: boolean;

  /* Pending join from canvas drag — pre-populates the join dialog */
  pendingJoin: { leftTableId: string; rightTableId: string } | null;

  /* Canvas → Sources focus: model_table id whose source-list row should be
     expanded, scrolled into view, and briefly highlighted. Cleared by the
     panel after the highlight fires. */
  focusedTableId: string | null;
  focusedSourceId: string | null;

  /* Latest global status message shown in the bottom bar */
  globalMessage: GlobalMessage | null;

  /* Relation layout settings */
  relationNotation: RelationNotation;
  relationPathing: RelationPathing;
  relationTerminalOverrides: Record<string, { source: string; target: string }>;

  /* Table details drawer — opened by clicking a table node on the canvas
     or a table row in the Sources panel. Canvas renders the drawer. */
  tableDrawerTableId: string | null;

  /* Connection drawing mode — when true, canvas handles are interactive and
     the user can drag between tables to create joins. All other canvas
     interactions (open drawer on click) are suppressed until mode is off. */
  isConnectingMode: boolean;

  /* Which sub-tab of the unified Aggregates panel is active */
  aggregateTab: "list" | "refresh" | "smart-builder" | "predictive" | "settings" | null;

  /* Pivot query panel state — persisted across panel navigation so users
     don't lose results when switching to another panel and back. */
  pivotState: {
    /** Primary measure id (back-compat; mirrors measureSelections[0].measureId). */
    measureId: string;
    /** Ordered measure+agg selections driving the pivot columns. */
    measureSelections: { measureId: string; agg: string }[];
    rowDimIds: string[];
    colDimIds: string[];
    executeResult: unknown | null;
    error: string | null;
  };

  /* Active display locale for i18n translations (null = default English) */
  displayLocale: string | null;

  /* SQL injected from SavedQueriesPanel — consumed once by QueryPanel */
  pendingSql: string | null;

  /* Whether the model builder is in read-only share-link mode. Panels
     should check this to disable editing controls (Bug-5301). */
  readOnly: boolean;

  /* Immediate (non-debounced) canvas layout flush. Registered by Canvas on
     mount, cleared on unmount. Returns a Promise that resolves when the
     server PATCH completes. Used by the Save dialog's "layout only" mode. */
  flushCanvasLayoutNow: (() => Promise<void>) | null;

  // ---- actions ----
  openPanel: (panel: PanelId) => void;
  closePanel: () => void;
  setDrawerExpanded: (expanded: boolean) => void;
  toggleDrawerExpanded: () => void;
  selectObject: (id: string | null, type: ObjectType | null) => void;
  setMiniTab: (tab: MiniTab) => void;
  setValidationIssues: (issues: ValidationIssue[]) => void;
  addValidationIssue: (issue: ValidationIssue) => void;
  clearValidation: () => void;
  setValidationExpanded: (expanded: boolean) => void;
  toggleValidationExpanded: () => void;
  setPendingJoin: (join: { leftTableId: string; rightTableId: string } | null) => void;
  focusTable: (tableId: string, sourceId: string) => void;
  clearFocusedTable: () => void;
  setGlobalMessage: (text: string, severity?: MessageSeverity) => void;
  clearGlobalMessage: () => void;
  setRelationNotation: (notation: RelationNotation) => void;
  setRelationPathing: (pathing: RelationPathing) => void;
  setRelationTerminalOverride: (joinId: string, override: { source: string; target: string }) => void;
  openTableDrawer: (tableId: string) => void;
  closeTableDrawer: () => void;
  setConnectingMode: (active: boolean) => void;
  setAggregateTab: (tab: "list" | "refresh" | "smart-builder" | "predictive" | "settings") => void;
  setPivotState: (state: Partial<BuilderState["pivotState"]>) => void;
  setDisplayLocale: (locale: string | null) => void;
  setPendingSql: (sql: string | null) => void;
  setReadOnly: (readOnly: boolean) => void;
  setFlushCanvasLayoutNow: (fn: (() => Promise<void>) | null) => void;
  reset: () => void;
}

// ---------------------------------------------------------------------------
// Relation-setting persistence (F-026-18)
//
// Notation and pathing are display preferences (set once in Settings, applied
// to every edge) and the per-join terminal-cardinality overrides set in the
// Joins panel are user choices the modeller expects to survive reopening a
// model. They previously lived only in store memory and were wiped by reset()
// on every model mount, so they silently reverted — inconsistent with the
// toolbelt expansion, which persists. We persist all three to localStorage so
// they survive reload and model switches. Terminal overrides are keyed by the
// globally-unique join id, so a single bucket is safe across models.
// ---------------------------------------------------------------------------

const LS_NOTATION = "tsl.relation.notation";
const LS_PATHING = "tsl.relation.pathing";
const LS_TERMINAL_OVERRIDES = "tsl.relation.terminalOverrides";

type TerminalOverrideMap = Record<string, { source: string; target: string }>;

function loadRelationNotation(): RelationNotation {
  if (typeof window === "undefined") return "crowsfoot";
  const v = localStorage.getItem(LS_NOTATION);
  return v === "uml" || v === "diamond" || v === "crowsfoot" ? v : "crowsfoot";
}

function loadRelationPathing(): RelationPathing {
  if (typeof window === "undefined") return "orthogonal";
  const v = localStorage.getItem(LS_PATHING);
  return v === "straight" || v === "orthogonal" ? v : "orthogonal";
}

function loadTerminalOverrides(): TerminalOverrideMap {
  if (typeof window === "undefined") return {};
  try {
    const raw = localStorage.getItem(LS_TERMINAL_OVERRIDES);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? (parsed as TerminalOverrideMap) : {};
  } catch {
    return {};
  }
}

// The active display locale is a user preference persisted to localStorage by
// setDisplayLocale. Like the relation prefs above it must be re-read (not reset
// to English) whenever the store is reset on a model mount — otherwise opening
// any model silently reverts the whole UI to English for non-English users
// (Bug-6374 / F-026-02).
function loadDisplayLocale(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("display_locale") || null;
}

function persist(key: string, value: string): void {
  if (typeof window === "undefined") return;
  try {
    localStorage.setItem(key, value);
  } catch {
    // localStorage may be unavailable (private mode / quota) — preferences are
    // non-critical, so swallow rather than break the editor.
  }
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

const initialState = {
  activePanel: null as PanelId | null,
  drawerExpanded: false,
  selectedObjectId: null as string | null,
  selectedObjectType: null as ObjectType | null,
  miniTab: "canvas" as MiniTab,
  validationIssues: [] as ValidationIssue[],
  validationExpanded: false,
  pendingJoin: null as { leftTableId: string; rightTableId: string } | null,
  focusedTableId: null as string | null,
  focusedSourceId: null as string | null,
  globalMessage: null as GlobalMessage | null,
  relationNotation: loadRelationNotation(),
  relationPathing: loadRelationPathing(),
  relationTerminalOverrides: loadTerminalOverrides(),
  tableDrawerTableId: null as string | null,
  isConnectingMode: false,
  aggregateTab: null as "list" | "refresh" | "smart-builder" | "predictive" | "settings" | null,
  pivotState: {
    measureId: "",
    measureSelections: [] as { measureId: string; agg: string }[],
    rowDimIds: [] as string[],
    colDimIds: [] as string[],
    executeResult: null as unknown | null,
    error: null as string | null,
  },
  displayLocale: loadDisplayLocale() as string | null,
  pendingSql: null as string | null,
  readOnly: false,
  flushCanvasLayoutNow: null as (() => Promise<void>) | null,
};

export const useBuilderStore = create<BuilderState>()((set, get) => ({
  ...initialState,

  openPanel: (panel) => set({ activePanel: panel }),
  closePanel: () => set({ activePanel: null, drawerExpanded: false, isConnectingMode: false }),
  setDrawerExpanded: (expanded) => set({ drawerExpanded: expanded }),
  toggleDrawerExpanded: () => set((s) => ({ drawerExpanded: !s.drawerExpanded })),

  selectObject: (id, type) =>
    set({ selectedObjectId: id, selectedObjectType: type }),

  setMiniTab: (tab) => set({ miniTab: tab }),
  setValidationIssues: (issues) => set({ validationIssues: issues }),
  addValidationIssue: (issue) =>
    set((s) => ({ validationIssues: [...s.validationIssues, issue] })),
  clearValidation: () => set({ validationIssues: [] }),
  setValidationExpanded: (expanded) => set({ validationExpanded: expanded }),
  toggleValidationExpanded: () =>
    set((s) => ({ validationExpanded: !s.validationExpanded })),

  setPendingJoin: (join) => set({ pendingJoin: join }),

  focusTable: (tableId, sourceId) =>
    set({
      activePanel: "sources",
      focusedTableId: tableId,
      focusedSourceId: sourceId,
    }),
  clearFocusedTable: () =>
    set({ focusedTableId: null, focusedSourceId: null }),

  setGlobalMessage: (text, severity = "info") =>
    set({ globalMessage: { text, severity, at: Date.now() } }),
  clearGlobalMessage: () => set({ globalMessage: null }),

  setRelationNotation: (notation) => {
    persist(LS_NOTATION, notation);
    set({ relationNotation: notation });
  },
  setRelationPathing: (pathing) => {
    persist(LS_PATHING, pathing);
    set({ relationPathing: pathing });
  },
  setRelationTerminalOverride: (joinId, override) =>
    set((s) => {
      const next = { ...s.relationTerminalOverrides, [joinId]: override };
      persist(LS_TERMINAL_OVERRIDES, JSON.stringify(next));
      return { relationTerminalOverrides: next };
    }),

  openTableDrawer: (tableId) => set({ tableDrawerTableId: tableId }),
  closeTableDrawer: () => set({ tableDrawerTableId: null }),
  setConnectingMode: (active) => set({ isConnectingMode: active }),
  setAggregateTab: (tab) => set({ aggregateTab: tab }),
  setPivotState: (partial) =>
    set((s) => ({ pivotState: { ...s.pivotState, ...partial } })),
  setDisplayLocale: (locale) => {
    if (typeof window !== "undefined") {
      if (locale === null) {
        localStorage.removeItem("display_locale");
      } else {
        localStorage.setItem("display_locale", locale);
      }
    }
    set({ displayLocale: locale });
  },
  setPendingSql: (sql) => set({ pendingSql: sql }),
  setReadOnly: (readOnly) => set({ readOnly }),
  setFlushCanvasLayoutNow: (fn) => set({ flushCanvasLayoutNow: fn }),

  // reset() runs on every model mount. Relation display preferences, the
  // per-join terminal overrides (F-026-18) and the active display locale
  // (Bug-6374) are persisted to localStorage and must survive the reset —
  // re-read them so a model switch keeps the user's notation/pathing/cardinality
  // choices and their chosen UI language instead of silently reverting to
  // defaults / English.
  reset: () =>
    set({
      ...initialState,
      relationNotation: loadRelationNotation(),
      relationPathing: loadRelationPathing(),
      relationTerminalOverrides: loadTerminalOverrides(),
      displayLocale: loadDisplayLocale(),
      readOnly: get().readOnly,
    }),
}));
