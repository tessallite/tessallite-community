/**
 * Central English string table for the Excel add-in (F-025-26).
 *
 * The add-in is English-only by design (the project's translation effort is
 * parked — see docs). Like the conversational client's `strings.ts`, this
 * module ships NO runtime translation layer and adds NO i18n dependency: it is
 * simply the single place user-visible copy lives, so strings no longer
 * scatter as hardcoded literals across components and a future translation
 * effort has one file to target. Keys mirror the platform's `en.json`
 * grouping convention (domain.key).
 *
 * Scope note: the platform i18n policy is en-only with translations parked, so
 * this is the string-extraction half of the work only — no multi-locale
 * runtime is built here. Manifest strings and full per-component coverage are
 * tracked as follow-on under the parked-translation policy.
 */
export const strings = {
  status: {
    connected: "Connected",
    disconnected: "Disconnected",
    reconnecting: "Reconnecting",
    loading: "Loading...",
  },
  app: {
    askTessallite: "Ask Tessallite",
    loadingProviderInfo: "Loading provider info...",
    selectedValue: "Selected value",
    drillThrough: "Drill through",
    drillThroughCell: "Drill through selected cell",
    sectionsAria: "Tessallite task pane sections",
    projectSelectorAria: "Project selector",
  },
  projects: {
    none: "No projects available. Create one in the Tessallite web app.",
    noModels: "No models available for the selected project.",
    loadFailed: "Failed to load projects",
    noneSelected: "No project selected. Add a project in the Tessallite web app.",
  },
  auth: {
    loginFailed: "Login failed",
  },
  connection: {
    restored: "Connection restored",
    lost: "Connection lost. Retrying...",
  },
  toasts: {
    excelBusy: "An Excel operation is already in progress",
    chartFromAgent: "Chart auto-created from agent suggestion",
    insertTableFailed: "Insert table failed",
    feedbackFailed: "Feedback submission failed",
    noDataToChart: "No data to chart",
    chartCreated: "Chart created",
    chartInsertFailed: "Chart insertion failed. This may not be supported on your Excel version.",
    noDataForPivot: "No data for PivotTable",
    pivotInsertFailed: "Local PivotTable insertion failed. This requires Excel 2019+ or Web.",
    selectCellForDrill: "Select a cell in an inserted table or CUBE formula to drill through.",
    cellContextFailed: "Could not read cell context. Try selecting a cell first.",
    cubeDrillContextUnavailable: "Drill-through is unavailable because this CUBEVALUE contains slicer context that cannot be recovered.",
    drillMeasureUnavailable: "Drill-through is unavailable because this cell does not contain a resolvable measure id.",
    insertDrillRowsFailed: "Insert drill-through rows failed",
    noProjectForChat: "No project or model available. Please configure a project first.",
    sendMessageFailed: "Failed to send message",
    loadConversationFailed: "Could not load conversation",
    deleteConversationFailed: "Could not delete conversation",
  },
} as const;
