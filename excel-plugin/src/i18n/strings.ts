import { localizeObject } from "./runtime";

/**
 * Central English fallback string table for the Excel add-in (F-025-26 / Bug-5565).
 *
 * Runtime localization is provided by `runtime.ts`: Office/browser locale
 * selects the active language, German and French currently have a localized
 * task-pane subset, and every missing key falls back to this English table.
 * Keys mirror the platform's `en.json` grouping convention (domain.key).
 */
const englishStrings = {
  // ── Status ──────────────────────────────────────────────────────────
  status: {
    connected: "Connected",
    disconnected: "Disconnected",
    reconnecting: "Reconnecting",
    loading: "Loading...",
  },

  // ── App shell ───────────────────────────────────────────────────────
  app: {
    title: "Tessallite",
    subtitle: "Excel plugin v1.0.0.4",
    askTessallite: "Ask Tessallite",
    loadingProviderInfo: "Loading provider info...",
    selectedValue: "Selected value",
    drillThrough: "Drill through",
    drillThroughCell: "Drill through selected cell",
    sectionsAria: "Tessallite task pane sections",
    projectSelectorAria: "Project selector",
    glossary: "Glossary",
    glossaryAria: "Open glossary",
    settings: "Settings",
    settingsAria: "Open settings",
    diagnosticsMenuItem: "Diagnostics",
    signOut: "Sign Out",
    projectLabel: "Project",
    tabAnalyse: "Analyse",
    tabKpis: "KPIs",
    retry: "Retry",
    viewingAs: "Viewing as",
    resetToDefault: "Reset to default",
    chatMessagesAria: "Chat messages",
    reportBuilderAria: "Report Builder",
    kpiScorecardAria: "KPI Scorecard",
    profileRemoved: "Profile removed",
  },

  // ── Projects ────────────────────────────────────────────────────────
  projects: {
    none: "No projects available. Create one in the Tessallite web app.",
    noModels: "No models available for the selected project.",
    loadFailed: "Failed to load projects",
    noneSelected: "No project selected. Add a project in the Tessallite web app.",
  },

  // ── Auth ────────────────────────────────────────────────────────────
  auth: {
    loginFailed: "Login failed",
  },

  // ── Login Screen ────────────────────────────────────────────────────
  login: {
    formAria: "Connect to Tessallite",
    brandName: "Tessallite",
    tagline: "Governed analytics",
    serverUrl: "Server URL",
    serverUrlPlaceholder: "https://your-tessallite.io",
    tenant: "Tenant",
    tenantPlaceholder: "Tenant slug",
    email: "Email",
    emailPlaceholder: "Email address",
    password: "Password",
    passwordPlaceholder: "Password",
    showPassword: "Show password",
    hidePassword: "Hide password",
    rememberProfile: "Remember this profile",
    connect: "Connect",
    serverUrlMustStart: "Must start with https:// or http://",
    serverUrlInvalid: "Invalid URL format",
  },

  // ── Connection ──────────────────────────────────────────────────────
  connection: {
    restored: "Connection restored",
    lost: "Connection lost. Retrying...",
  },

  // ── Toasts ──────────────────────────────────────────────────────────
  toasts: {
    excelBusy: "An Excel operation is already in progress",
    chartFromAgent: "Chart auto-created from agent suggestion",
    insertTableFailed: "Insert table failed",
    feedbackFailed: "Feedback submission failed",
    feedbackRecorded: "Feedback recorded",
    noDataToInsert: "No data to insert",
    noDataToChart: "No data to chart",
    chartCreated: "Chart created",
    // Bug-6733: the chart was created but a non-critical post-step (axis
    // formatting or metadata tagging) failed. The chart is visible and
    // correct; axis titles may default instead of showing the field names.
    chartCreatedWithPostStepWarning: "Chart created, but axis formatting could not be applied. The chart is visible; axis titles may show defaults.",
    // Bug-7397 R8-3: shown when an insert was blocked (another operation is in
    // progress on the target area, or the target could not be resolved) and
    // nothing was written -- so the user knows to retry rather than seeing a
    // silent no-op.
    insertTableBusy: "Could not insert the table right now — another operation is in progress on that area. Please try again in a moment.",
    chartInsertFailed: "Chart insertion failed. This may not be supported on your Excel version.",
    chartCreationFailed: "Chart creation failed",
    noDataForPivot: "No data for PivotTable",
    pivotInsertFailed: "Local PivotTable insertion failed.",
    // Bug-6909: classified error messages for PivotTable insertion failures.
    pivotInsertFailedCompat: "PivotTable creation is not supported in this version of Excel. This feature requires Excel 2019 or later, or Excel for the Web.",
    pivotInsertFailedMapping: "PivotTable creation failed: the field mapping could not be applied. Check that the selected measures and dimensions are compatible.",
    pivotInsertFailedGeneric: "PivotTable creation failed due to an unexpected error. Check the Diagnostics panel for details.",
    pivotCreated: "Local PivotTable created",
    selectCellForDrill: "Select a cell in an inserted table or CUBE formula to drill through.",
    cellContextFailed: "Could not read cell context. Try selecting a cell first.",
    cubeDrillContextUnavailable: "Drill-through is unavailable because this CUBEVALUE contains slicer context that cannot be recovered.",
    drillMeasureUnavailable: "Drill-through is unavailable because this cell does not contain a resolvable measure id.",
    insertDrillRowsFailed: "Insert drill-through rows failed",
    noProjectForChat: "No project or model available. Please configure a project first.",
    sendMessageFailed: "Failed to send message",
    loadConversationFailed: "Could not load conversation",
    deleteConversationFailed: "Could not delete conversation",
    kpiChartNotSuitable: "KPI results are best viewed as values, not charts.",
    insertFailed: "Insert failed",
    queryNoResults: "Query returned no results",
    queryRowSecurityDenied: "Your row-level security permissions do not grant you access to any rows for this query. Nothing was inserted. This is a permissions restriction, not an empty result — do not read it as zero. Contact your administrator if you believe you should have access.",
    queryExecutionFailed: "Query execution failed",
    formulaInserted: "Formula inserted",
    refreshingValues: "Refreshing all function values...",
    formulaInsertionFailed: "Formula insertion failed",
    kpiInsertionFailed: "KPI insertion failed",
    noKpisAvailable: "No KPIs available",
    scorecardInsertionFailed: "Scorecard insertion failed",
    kpiUndeployedFormula: "This KPI is not yet deployed. CUBE formulas for undeployed KPIs will show #N/A until the KPI is deployed from the model builder.",
    couldNotLoadMembers: "Could not load members",
    namedSetAdvancedExpression: "This named set uses an advanced expression and cannot be dropped into a zone. Use \"Insert as formulas\" instead.",
    namedSetNoMembers: "This named set has no resolvable members to filter by.",
    namedSetResolveFailed: "Could not resolve this named set. Try again.",
    hierarchyResolveFailed: "Could not resolve this hierarchy level to a field.",
    hierarchyDetailFailed: "Could not load hierarchy detail. Try again.",
  },

  // ── KPI Panel ───────────────────────────────────────────────────────
  kpiPanel: {
    evalError: "KPI values could not be evaluated right now. The list below is current; try refreshing.",
    loadError: "Could not load KPIs. Check your connection.",
    // Bug-8710/Bug-8712 — the add-in shows PUBLISHED definitions only. When the
    // published version of the model cannot be read, nothing is shown at all
    // rather than the unpublished draft.
    deployedSnapshotInvalid: "The published version of this model is unavailable, so its KPIs cannot be shown. Ask a modeller to deploy the model again, then refresh.",
    tryAgain: "Try again",
    noKpisTitle: "No KPIs yet",
    noKpisDescription: "Create KPIs in the Tessallite web app, then come back here to view and insert them.",
    refreshKpis: "Refresh KPIs",
    insertAllScorecard: "Insert all KPIs as a scorecard table",
    good: "Good",
    warning: "Warning",
    poor: "Poor",
    filterAll: "All",
    filterCertified: "Certified",
    searchPlaceholder: "Search...",
    noSearchMatch: "No KPIs match your search",
    insertTableTitle: "Insert this KPI as a mini-table in the worksheet",
    insertTable: "Insert Table",
    insertChartTitle: "Insert this KPI as a chart comparing value vs target",
    insertChart: "Insert Chart",
    targetPrefix: "Target:",
    targetLabel: "Target",
    currentLabel: "Current",
    headers: {
      kpi: "KPI",
      value: "Value",
      goal: "Goal",
      status: "Status",
      trend: "Trend",
    },
    metric: "Metric",
  },

  // ── KPI Library (Report Builder) ────────────────────────────────────
  kpiLibrary: {
    insertScorecard: "Insert KPI Scorecard",
    noSearchMatch: "No KPIs match your search",
    noKpisAvailable: "No KPIs available",
  },

  // ── Field-library section names (Bug-6710 chevron aria labels) ──────
  library: {
    measuresSection: "measures",
    kpisSection: "KPIs",
    namedSetsSection: "named lists",
    dimensionsSection: "dimensions",
    hierarchiesSection: "hierarchies",
  },

  // ── Measure Card (Bug-6713 + Bug-6705 i18n sweep) ────────────────────
  measureCard: {
    removeFromValues: "Remove from Values",
    insertAsFunction: "Insert as formula",
    insertAsCubeFormula: "Advanced: Insert as CUBEVALUE formula (requires workbook connection)",
    addToValues: "Add to Values",
    hideDetails: "Hide details",
    showDetails: "Show details",
    detailDescription: "Description",
    detailAggregation: "Aggregation",
    detailFormat: "Format",
    detailFolder: "Folder",
    detailSemiAdditive: "Semi-additive",
    detailDefinition: "Definition",
  },

  // ── Dimension Library (Bug-6713 + Bug-6718 i18n sweep) ───────────────
  dimensionLibrary: {
    closeMemberPreview: "Close",
    closeMemberPreviewAria: "Close member preview",
    noSearchMatch: "No dimensions match your search",
    noItemsAvailable: "No dimensions available",
    membersLabel: "Members",
    noMembersFound: "No members found",
  },

  // ── Measure Library (Bug-6718 i18n sweep) ────────────────────────────
  measureLibrary: {
    noSearchMatch: "No measures match your search",
    noItemsAvailable: "No measures available",
  },

  // ── Named Set Library (Bug-6718 i18n sweep) ──────────────────────────
  namedSetLibrary: {
    noSearchMatch: "No named lists match your search",
    noItemsAvailable: "No named lists available",
    // Bug-8712 — the add-in previews the published list, never an unpublished
    // draft, so both "not in the published version" and "published version
    // unreadable" resolve to the same instruction: deploy the model.
    previewNotPublished: "This list has not been published yet, so its members cannot be shown. Ask a modeller to deploy the model, then refresh.",
    previewFailed: "The list members could not be loaded. Check your connection and try again.",
  },

  // ── Confirm dialogs (Bug-6705 i18n sweep) ────────────────────────────
  confirm: {
    overwriteActiveCell: "The active cell range already contains data. Overwrite existing data?",
    overwriteTargetCell: "The target cell already contains data. Overwrite?",
  },

  // ── Hierarchy Card ───────────────────────────────────────────────────
  // Human labels for the backend hierarchy type enum (explicit /
  // date_embedded / segment). The chip previously rendered the raw enum
  // token ("date_embedded") -- the Bug-6716 defect class.
  hierarchyCard: {
    typeCalendar: "Calendar",
    typeExplicit: "Hierarchy",
    typeSegment: "Segment",
  },

  // ── KPI Card ────────────────────────────────────────────────────────
  kpiCard: {
    chipKpi: "KPI",
    chipCertified: "Certified",
    chipDeprecated: "Deprecated",
    hideDetails: "Hide details",
    showDetails: "Show details",
    insertOptions: "Insert options",
    insertOptionsLabel: "Insert Options",
    insertAsCubeFormulas: "Insert as CUBE formulas",
    addKpiToReport: "Add KPI value to report",
    removeKpiFromReport: "Remove KPI value from report",
    loadingStatus: "Loading KPI status...",
    detailDescription: "Description",
    detailValue: "Value",
    detailGoal: "Goal",
    detailCurrent: "Current",
    detailTarget: "Target",
    detailStatus: "Status",
    detailTrend: "Trend",
    detailGraphic: "Graphic",
    detailWeight: "Weight",
    detailFolder: "Folder",
    detailCertification: "Certification",
    insertFullRow: "Insert Full KPI Row",
    insertFullRowDesc: "Name | Value | Goal | Status",
    insertValueOnly: "Insert Value Only",
    insertValueOnlyDesc: "Single cell with KPI value",
    insertValueGoal: "Insert Value + Goal",
    insertValueGoalDesc: "Two cells: value and goal",
    insertStatusIcon: "Insert Status Icon",
    insertStatusIconDesc: "Traffic light status indicator",
    // Bug-6729: insertTrendIcon/insertTrendIconDesc removed (dead keys).
    insertKpiCard: "Insert KPI Card",
    insertKpiCardDesc: "Formatted card with all details",
    insertFormulaRef: "Insert Formula Reference",
    insertFormulaRefDesc: "CUBEKPIMEMBER formula",
  },

  // ── Drill Through ──────────────────────────────────────────────────
  drill: {
    title: "Drill Through",
    copyTsv: "Copy TSV",
    insertSheet: "Insert Sheet",
    prev: "Prev",
    next: "Next",
    loadMore: "Load more rows...",
    accessDenied: "Access denied. Your persona does not have permission to drill through this measure.",
    rowSecurityDenied: "Your row-level security permissions do not grant you access to any of the underlying rows for this cell. This is a permissions restriction, not an absence of detail. Contact your administrator if you believe you should have access.",
    noPaths: "No drill-through paths available for this measure.",
    moreAvailable: " (more available)",
    drillPaths: "Drill paths",
  },

  // ── Table Refresh ──────────────────────────────────────────────────
  tableRefresh: {
    refreshSheetData: "Refresh sheet data",
    refreshSheetDataAria: "Re-run all Tessallite tables on the active sheet",
    noTables: "No refreshable Tessallite tables on this sheet.",
    refreshing: "Refreshing tables...",
    noModelSelected: "No model selected. Open the Tessallite panel and select a model first.",
    agentSourceSkip: "Inserted from the Ask tab. Re-run the question from the conversational agent.",
    noStoredHeadersSkip: "Table predates refresh support (no stored column layout). Re-insert it to enable refresh.",
    pivotedSkip: "Cross-tab tables cannot be refreshed automatically. Re-run from Report Builder.",
    concurrentModificationSkip: "A concurrent change was detected during refresh; the table was skipped to avoid stale data.",
    metadataModifiedSkip: "The table's metadata was modified during refresh; skipped to avoid stale data.",
    provenanceWriteBackFailed: "The table data was refreshed, but its saved query details could not be updated. A future refresh may not work until you re-insert it.",
    tableResizedSkip: "The table changed size or position since the sheet was scanned (rows may have been added or removed nearby); it was skipped to avoid overwriting cells outside it. Refresh again.",
    // Bug-7397 R12-5: these seven reasons used to be hard-coded English string
    // literals inside tableRefresh.ts. They became user-visible when the
    // skipped/warning details surface was wired up, so they now follow the same
    // convention as every other reason above.
    projectMismatchSkip: "Table belongs to a different project than the active session.",
    modelMismatchSkip: "Table belongs to a different model than the active session.",
    corruptedQuerySkip: "The table's saved query is corrupted and cannot be re-run. Re-insert the table.",
    executionFailedSkip: "The query could not be re-run. Check your connection and try again.",
    noResultsSkip: "The query returned no rows, so the table was left unchanged.",
    corruptedHeadersSkip: "The table's saved column layout is corrupted. Re-insert the table.",
    writeFailedSkip: "The refreshed data could not be written to the table. The table was left unchanged.",
    partialRewriteWarning: "Excel reported an error part-way through rewriting this table, so it may hold a mix of old and new rows. Check it, then refresh again.",
    lockBusySkip: "Another Excel operation was still working on these cells. Nothing was changed - try refreshing again.",
    // Bug-8424: Excel would not hand back the table's saved query details this
    // time. Nothing was changed, and the next refresh reads them again.
    metadataFetchFailedSkip: "Excel could not read this table's saved query details, so it was left unchanged. This is usually temporary - try refreshing again.",
    growthZoneOccupiedSkip: "The query now returns more rows than the table has room for, and the cells directly below it are not empty. The table was left unchanged so your own content is not overwritten. Clear or move those cells, then refresh again.",
    // Bug-7397 R12-5: the details surface for skipped/warned tables.
    detailsShow: "Show details",
    detailsHide: "Hide details",
    detailsTitle: "Refresh details",
    detailsSkippedGroup: "Skipped",
    detailsWarningGroup: "Needs a look",
  },

  // ── Insert Mode ───────────────────────────────────────────────────
  insertMode: {
    label: "Insert mode",
    live: "Live",
    static: "Static",
    liveDescription: "Insert TESSALLITE.* formulas that update on refresh",
    staticDescription: "Insert the current value as a static number",
  },

  // ── Report Builder ──────────────────────────────────────────────────
  reportBuilder: {
    refreshValues: "Refresh",
    modelLabel: "Model",
    modelSelectorAria: "Model selector",
    sortBy: "Sort by",
    sortNone: "None",
    sortAscending: "Ascending",
    sortDescending: "Descending",
    availableFields: "Available fields",
    availableFieldsHint: "Click a field's zone icon to place it above.",
    searchPlaceholder: "Search measures, KPIs, lists, dimensions...",
    certifiedOnly: "Certified only",
    cubeButton: "Advanced: CUBE",
    connectButton: "Connect",
    traceButton: "Trace",
    incompatibleFields: "Selected fields are not compatible",
    removeIncompatibleTable: "Remove incompatible fields before inserting this report.",
    removeIncompatibleChart: "Remove incompatible fields before inserting this chart.",
    removeIncompatiblePivot: "Remove incompatible fields before inserting a local PivotTable.",
    staleDialogTitle: "Stale Entities Detected",
    staleDialogDescription: "The following entities used in this workbook have changed on the server. Formulas referencing these entities may return outdated results.",
    staleKpi: "KPI",
    staleNamedSet: "Named Set",
    staleDeleted: "Deleted from server",
    staleDeprecated: "Deprecated",
    staleDefinitionUpdated: "Definition updated",
    staleCells: "cells:",
    staleLater: "Later",
    staleAcceptCurrent: "Accept Current",
  },

  // ── Provenance footer (Bug-7417) ───────────────────────────────────
  // The attribution row written below an inserted table. Filters and ordering
  // make the inserted data self-describing so a reader can see exactly which
  // slice and sort produced the numbers, not just the model/persona/time.
  provenance: {
    source: "Source: Tessallite",
    model: "Model",
    viewingAs: "Viewing as",
    filters: "Filters",
    sortedBy: "Sorted by",
  },

  // ── Zone Mapping Grid ──────────────────────────────────────────────
  zone: {
    empty: "Empty",
    buildReport: "Build report",
    buildReportHint: "Pick fields, then insert into Excel.",
    clearLayout: "Clear layout",
    clearLayoutAria: "Clear report layout",
    templates: "Templates",
    valuesLabel: "Values",
    valuesHint: "Add measures or KPIs",
    rowsLabel: "Rows",
    rowsHint: "Add dimensions",
    columnsLabel: "Columns",
    columnsHint: "Optional split",
    filtersLabel: "Filters",
    filtersHint: "Optional criteria",
    tableButton: "Table",
    chartButton: "Chart",
    pivotButton: "Pivot",
    addMeasureHint: "Add at least one measure to enable insert actions.",
    filterChipHint: "Select a filter chip to set operator and values.",
    compatibleDimensions: "Compatible dimensions:",
  },

  // ── Filter Dialog ──────────────────────────────────────────────────
  filter: {
    dialogTitlePrefix: "Filter:",
    operatorLabel: "Operator",
    equals: "Equals",
    notEquals: "Not Equals",
    contains: "Contains",
    notContains: "Not Contains",
    greaterThan: "Greater Than",
    lessThan: "Less Than",
    dateRange: "Date Range",
    inSet: "In Set",
    valuesComma: "Values (comma-separated)",
    enterTwoDates: "Enter two dates: start, end",
    enterValuesSeparated: "Enter values separated by commas",
    cancel: "Cancel",
    done: "Done",
    apply: "Apply",
    enterValueToCompare: "Enter a value to compare against.",
  },

  // ── Insert Actions ─────────────────────────────────────────────────
  insertActions: {
    insertTable: "Insert Table",
    chart: "Chart",
    localPivot: "Local Pivot",
    cubeFormulas: "CUBE formulas",
    liveConnection: "Live connection",
    showQuery: "Show Query",
  },

  // ── Cube Formula Wizard ────────────────────────────────────────────
  cubeWizard: {
    title: "Cube Function Wizard",
    measureLabel: "Measure",
    filterOptional: "Filter (optional)",
    none: "None",
    noDimensionsAvailable: "No compatible dimensions are available for this measure.",
    memberLabel: "Member",
    noMembers: "No members found for this dimension.",
    targetCell: "Target cell",
    connectionHint: "CUBE formulas resolve against a workbook connection named \"{connectionName}\". If you have not set one up yet, open Report Builder and use \"Live connection\" to create it. The formula is inserted regardless; it will show #N/A until the connection exists.",
    validating: "Validating...",
    measureNotFound: "Selected measure not found in model",
    dimensionNotFound: "Selected dimension not found in model",
    selectMember: "Select a member for the dimension filter, or remove the filter",
    memberUnavailable: "Selected member is no longer available for this dimension",
    cancel: "Cancel",
    back: "Back",
    next: "Next",
    insertFormula: "Insert Formula",
  },

  // ── Live Connection Wizard ─────────────────────────────────────────
  liveConnection: {
    title: "Live XMLA Connection",
    stepStart: "Start",
    // Bug-6727: renamed from "Manual Setup" — the primary flow now uses the
    // Server-name path, not the raw connection-string paste.
    stepConnect: "Connect",
    stepInstructions: "Instructions",
    description: "A live XMLA connection lets Excel PivotTables query Tessallite directly. You create the connection in Excel using the server address below, then use Excel's native PivotTable dialog.",
    note: "Note: Excel does not let an add-in create connections or PivotTables programmatically, so these steps are done in Excel's own dialogs.",
    xmlaUsernameLabel: "XMLA Username (email)",
    xmlaUsernameRequired: "Username is required.",
    xmlaUsernameInvalid: "Username contains invalid characters.",
    credentialNote: "Excel will prompt for your password when the connection is first used. Credentials are never stored in the workbook file.",
    // Bug-6727: Server-name flow instructions (replaces old raw-string paste).
    serverFlowTitle: "Create the connection in Excel:",
    serverStep1Prefix: "Go to ",
    serverStep1Bold: "Data > Get Data > From Database > From Analysis Services",
    serverStep2: "In the dialog that opens, paste the server address below into the Server name field.",
    serverStep3Prefix: "Server name: ",
    serverStep4Prefix: "Check ",
    serverStep4Bold: "\"Only Create Connection\"",
    serverStep5: "Click OK. Excel will prompt for your username and password.",
    copy: "Copy",
    // Bug-6727: advanced section for the raw MSOLAP connection string.
    advancedToggle: "Advanced: raw connection string",
    advancedNote: "If the server-name approach does not work (e.g. older Excel versions), you can create the connection using a raw MSOLAP connection string instead. Paste it into the connection dialog's connection-string field.",
    copyConnectionString: "Copy connection string",
    connectionCreated: "Connection created",
    pivotInstructions: "To build a live PivotTable:",
    pivotStep1Prefix: "Go to ",
    pivotStep1Bold: "Insert > PivotTable",
    pivotStep2Prefix: "Select ",
    pivotStep2Bold: "\"Use an external data source\"",
    pivotStep3Prefix: "Click ",
    pivotStep3Bold: "\"Choose Connection\"",
    pivotStep4Prefix: "Select the ",
    pivotStep4Bold: "\"Tessallite\"",
    pivotStep4Suffix: " connection",
    pivotStep5: "Choose where to place the PivotTable and click OK",
    close: "Close",
    next: "Next",
    back: "Back",
  },

  // ── Query Trace ────────────────────────────────────────────────────
  trace: {
    title: "Query Trace",
    semanticQuery: "Semantic query",
    description: "The semantic query above is sent to the query-router. The route above is the server's decision — whether the report was answered from an accelerated aggregate/pocket table or the source database — with the rewritten SQL it ran. SQL generation, aggregate routing, and dialect translation are performed server-side.",
    emptyState: "Execute a query in Report Builder to view its trace. Add measures to Values and run Insert Table or Insert Chart.",
    close: "Close",
    routeAggregate: "Aggregate (accelerated)",
    routePocket: "Pocket table (accelerated)",
    routeSource: "Source database",
    sqlRedacted: "The physical SQL is available to model builders and administrators. You can see the route decision above, but not the physical table names or the row-security filters the server applied. Ask your administrator if you need this detail.",
  },

  // ── Glossary ───────────────────────────────────────────────────────
  glossary: {
    title: "Glossary",
    searchPlaceholder: "Search glossary terms...",
    noMatch: "No matching glossary entries",
    noEntries: "No glossary entries available",
    synonyms: "Synonyms:",
  },

  // ── Profile Switcher ───────────────────────────────────────────────
  profileSwitcher: {
    title: "Connection Profiles",
    switchProfileAria: "Switch profile",
    noSaved: "No saved connections",
    signOut: "Sign Out",
    removeTitle: "Remove Profile",
    removeConfirmation: "This will remove the saved connection. You can log in again to restore it. Continue?",
    cancel: "Cancel",
    remove: "Remove",
  },

  // ── Persona Dropdown ───────────────────────────────────────────────
  persona: {
    label: "Persona:",
    default: "Default",
  },

  // ── Diagnostics Panel ──────────────────────────────────────────────
  diagnostics: {
    title: "Diagnostics",
    closeAria: "Close diagnostics",
    environment: "Environment",
    version: "Version",
    excelHost: "Excel Host",
    eventLog: "Event Log",
    colTime: "Time",
    colType: "Type",
    colDetail: "Detail",
    clearLog: "Clear Log",
    runSpike: "Run Spike",
    running: "Running...",
    copyDiagnostics: "Copy Diagnostics",
    copied: "Copied",
    compatibilitySpike: "Compatibility Spike",
    spikeConfirmTitle: "Run compatibility spike?",
    spikeConfirmDescription: "The spike checks whether this Excel host supports tables, charts and CUBE formulas. It writes its test data into a temporary hidden worksheet that is deleted immediately afterwards, and reads (but does not change) your current cell selection. Your workbook content is not modified.",
    cancel: "Cancel",
    spikeRunnerFailed: "Spike runner failed",
    unknownPlatform: "Unknown",
    spikeUnknown: "unknown",
    spikeSkipped: "skipped",
  },

  // ── Chat Shell ─────────────────────────────────────────────────────
  chatShell: {
    newConversation: "New conversation",
    conversationHistory: "Conversation history",
    deleteTitle: "Delete Conversation",
    deleteConfirmation: "This action cannot be undone. Delete this conversation?",
    cancel: "Cancel",
    delete: "Delete",
    unavailableTitle: "Conversational analytics unavailable",
    unavailableDescription: "Contact your Tessallite administrator to configure an LLM provider.",
    composerPlaceholder: "Ask a question about your data...",
  },

  // ── Error Boundary ──────────────────────────────────────────────────
  errorBoundary: {
    title: "Something went wrong",
    description: "The plugin encountered an unexpected error. Please close and reopen the task pane to try again.",
    persistNote: "If the problem persists, contact your Tessallite administrator.",
  },

  // ── Common ──────────────────────────────────────────────────────────
  common: {
    cancel: "Cancel",
    close: "Close",
  },
} as const;

export const strings = localizeObject(englishStrings);

/**
 * Bug-6697 / Bug-6709: the shared post-insert clause for every CUBE-formula
 * toast. Office.js cannot detect or create workbook OLAP connections
 * (F-025-18/21), so connection health is permanently unknowable from the
 * add-in -- every completed CUBE/CUBESET/CUBEKPIMEMBER insert must state the
 * requirement instead of reporting a bare success. Single source so the
 * wording cannot drift between insert paths.
 */
const cubeFormulaConnectionRequirement = (connectionName: string) =>
  `CUBE formulas only resolve once a workbook connection named "${connectionName}" exists — use "Connect" in Report Builder if you have not set one up yet.`;

/**
 * Bug-6716: human phrasing per KPI insert mode. The toast previously
 * interpolated the internal mode enum (`mode.replace('_', ' ')`), leaking
 * tokens like "KPI kpi card inserted" / "KPI formula ref inserted" into user
 * copy. Unknown modes keep the mechanical fallback so a future mode is still
 * announced rather than silently unlabelled.
 */
const kpiModeInsertedLabels: Record<string, string> = {
  full_row: "Full KPI row inserted",
  value_only: "KPI value inserted",
  value_goal: "KPI value and goal inserted",
  status_only: "KPI status icon inserted",
  // Bug-6729: trend_only removed -- the gateway does not serve Trend members.
  kpi_card: "KPI card inserted",
  formula_ref: "KPI formula reference inserted",
};

/**
 * Template strings that require interpolation. Each function accepts the
 * dynamic parts and returns the assembled English string. A future i18n
 * runtime would replace these with locale-aware formatters.
 */
export const templates = {
  tableRefresh: {
    refreshed: (count: number) => `Refreshed ${count} table${count !== 1 ? 's' : ''}.`,
    refreshedWithSkipped: (refreshed: number, skipped: number) =>
      `Refreshed ${refreshed} table${refreshed !== 1 ? 's' : ''}; ${skipped} skipped (see details).`,
    allSkipped: (skipped: number) => `All ${skipped} table${skipped !== 1 ? 's were' : ' was'} skipped. Check details for reasons.`,
    refreshedWithWarnings: (refreshed: number, warnings: number) =>
      `Refreshed ${refreshed} table${refreshed !== 1 ? 's' : ''}; ${warnings} with warnings (see details).`,
    refreshedWithSkippedAndWarnings: (refreshed: number, skipped: number, warnings: number) =>
      `Refreshed ${refreshed} table${refreshed !== 1 ? 's' : ''}; ${skipped} skipped, ${warnings} with warnings (see details).`,
    skippedDetail: (name: string, reason: string) => `${name}: ${reason}`,
    // Bug-7397 R12 R5: a table can end up ONLY in `warnings` (a rewrite that
    // failed part-way through is neither a clean refresh nor an untouched
    // skip). "Refreshed 0 tables; 1 with warnings" is accurate but reads as a
    // non-event; say plainly that something needs checking.
    warningsOnly: (warnings: number) =>
      `${warnings} table${warnings !== 1 ? 's need' : ' needs'} a look after the refresh (see details).`,
    // Bug-7397 R12-5: schema-drift reasons, moved out of tableRefresh.ts now
    // that skip reasons are shown to the user.
    columnCountChanged: () => 'Column count changed since the table was inserted (schema drift).',
    columnRenamed: (stored: string, returned: string) =>
      `Column "${stored}" changed to "${returned}" (schema drift).`,
  },
  toasts: {
    insertedRows: (count: number) => `Inserted ${count} rows`,
    // Bug-6737: the table data was written but a non-critical post-step
    // (metadata tagging or provenance footer) failed. The table is visible
    // and correct; provenance attribution may be missing.
    tableInsertedWithPostStepWarning: (count: number) => `Table inserted (${count} rows), but metadata tagging could not be applied. The data is correct; provenance attribution may be missing.`,
    insertedDrillRows: (count: number) => `Inserted ${count} drill-through rows`,
    insertTruncatedWarning: (available: number, total: number) =>
      `Cannot insert: only ${available} of ${total} rows available. The full result could not be retrieved.`,
    insertRefetchFailed: (available: number, total: number) =>
      `Cannot insert: failed to fetch the full result (${total} rows). Only the ${available}-row preview is available.`,
    insertRefetchEmpty: (available: number, total: number) =>
      `Cannot insert: the full query returned no rows (expected ${total}). The ${available}-row preview may be stale.`,
    switchedPersona: (name: string) => `Switched to ${name} view`,
    // Bug-6697 / Bug-6709: Office.js exposes no API to detect or create a
    // workbook OLAP connection (F-025-18/21), so NO CUBE-formula insert can
    // ever verify it will resolve -- every completed insert must carry the
    // connection requirement instead of a bare success that leaves the user
    // to discover an unexplained #N/A. All CUBE/CUBESET/CUBEKPIMEMBER insert
    // toasts below share this clause so the wording cannot drift.
    cubeValueFormulaInsertedNeedsConnection: (name: string) =>
      `CUBEVALUE formula inserted. ${cubeFormulaConnectionRequirement(name)}`,
    kpiModeInsertedNeedsConnection: (mode: string, name: string) =>
      `${kpiModeInsertedLabels[mode] ?? `KPI ${mode.replace('_', ' ')} inserted`}. ${cubeFormulaConnectionRequirement(name)}`,
    kpiFormulasInsertedNeedsConnection: (name: string) =>
      `KPI formulas inserted. ${cubeFormulaConnectionRequirement(name)}`,
    scorecardInserted: (count: number) =>
      `KPI scorecard inserted (${count} KPIs).`,
    scorecardInsertedNeedsConnection: (count: number, name: string) =>
      `KPI scorecard inserted (${count} KPIs). ${cubeFormulaConnectionRequirement(name)}`,
    cubeSetFormulasInsertedNeedsConnection: (name: string) =>
      `CUBESET formulas inserted. ${cubeFormulaConnectionRequirement(name)}`,
    // Bug-6701 / Bug-6709: a custom/expression KPI (no value_measure_id)
    // cannot be staged in the pivot "Values" zone -- the zone query only
    // carries measure/dimension names. "Add KPI" reroutes it to the
    // KPI-native formula insert; this toast fires AFTER a completed insert
    // (never before -- the insert can be declined or cancelled) and explains
    // both the reroute and the connection requirement.
    customKpiRoutedToFormulaInserted: (kpiName: string, connectionName: string) =>
      `"${kpiName}" is a custom-expression KPI and cannot be added as a pivot value; a KPI value formula was inserted at the selected cell instead. ${cubeFormulaConnectionRequirement(connectionName)}`,
    // Bug-6721: the measure-backed-but-unresolvable edge (Bug-6719) must
    // state ITS true reason -- telling the user the KPI "is a
    // custom-expression KPI" would send them debugging the KPI type instead
    // of the deleted / persona-hidden value measure.
    kpiUnresolvableMeasureRoutedToFormulaInserted: (kpiName: string, connectionName: string) =>
      `"${kpiName}"'s value measure is not available in this view (it may have been deleted or hidden from your persona), so it cannot be added as a pivot value; a KPI value formula was inserted at the selected cell instead. ${cubeFormulaConnectionRequirement(connectionName)}`,
    resultTruncated: (limit: number) =>
      `Showing the first ${limit} rows — the result was capped. Add filters or a sort to narrow it.`,
    // Bug-6728: composite-expression KPIs cannot be served through CUBE
    // formulas -- their values are plugin-evaluated. Insert the evaluated
    // literal instead.
    compositeKpiFormulaInserted: (kpiName: string) =>
      `"${kpiName}" is a composite-expression KPI. A static evaluated value was inserted; it will not refresh automatically with the connection.`,
    compositeKpiScorecardNote: (names: string) =>
      `Composite-expression KPIs (${names}) were inserted as literal values — their values are calculated by the plugin and cannot be served through CUBE formulas.`,
    undeployedKpiFormulaWarning: (kpiName: string, connectionName: string) =>
      `"${kpiName}" is not yet deployed. Its CUBE formula will show #N/A until the KPI is deployed from the model builder. ${cubeFormulaConnectionRequirement(connectionName)}`,
    undeployedKpiScorecardWarning: (names: string) =>
      `The following KPIs are not yet deployed and their formulas will show #N/A until deployed: ${names}.`,
    // Bug-6738: some levels of a whole-hierarchy add could not be resolved.
    hierarchyPartialLevels: (hierName: string, unresolvedLevels: string[]) =>
      `Added hierarchy "${hierName}" but ${unresolvedLevels.length === 1 ? `level "${unresolvedLevels[0]}" could` : `levels ${unresolvedLevels.map(l => `"${l}"`).join(', ')} could`} not be resolved to a field.`,
    localPivotUnsafeMeasures: (measureNames: string[]) =>
      `Local PivotTable cannot be inserted because Excel would re-aggregate non-additive measure${measureNames.length === 1 ? '' : 's'}: ${measureNames.join(', ')}. Use Insert Table or CUBE formulas for these measures.`,
  },
  app: {
    askAgent: (name: string) => `Ask ${name}`,
    viewingAsPersona: (name: string) => `Viewing as "${name}".`,
  },
  drill: {
    detailRowsLoaded: (count: number) => `${count} detail rows loaded`,
    page: (current: number, total: number) => `Page ${current}/${total}`,
  },
  insertActions: {
    dimensions: (rows: number, cols: number) => `${rows} rows x ${cols} columns`,
  },
  cubeWizard: {
    stepOf: (step: number, total: number) => `Step ${step} of ${total}`,
    readyToInsert: (cell: string) => `Ready to insert at ${cell}`,
    connectionHintWithName: (name: string) => `CUBE formulas resolve against a workbook connection named "${name}". If you have not set one up yet, open Report Builder and use "Live connection" to create it. The formula is inserted regardless; it will show #N/A until the connection exists.`,
  },
  kpiPanel: {
    kpiCount: (count: number) => `KPIs (${count})`,
    statusCount: (count: number, label: string) => `${count} ${label}`,
  },
  kpiLibrary: {
    kpiCount: (count: number) => `KPIs (${count})`,
  },
  reportBuilder: {
    staleStatusChanged: (status: string) => `Status changed to ${status}`,
  },
  filter: {
    valueLabel: (kind: string) => `Value (${kind})`,
    enterSingle: (kind: string) => `Enter a single ${kind}`,
    notANumber: (value: string) => `"${value}" is not a number. Greater Than / Less Than need a numeric value here.`,
    notADate: (value: string) => `"${value}" is not a date. Greater Than / Less Than need a date here (e.g. 2025-06-01).`,
    scalarTruncated: (value: string) => `Greater Than / Less Than use a single value — using "${value}".`,
    datesReordered: (low: string, high: string) => `Dates were entered high-to-low — reordered to ${low} … ${high}.`,
  },
  diagnostics: {
    spikeHeading: (host: string, platform: string) => `Compatibility Spike — ${host} / ${platform}`,
    spikeFailed: (message: string) => `failed: ${message}`,
  },
  trace: {
    modelPersona: (modelId: string, personaId?: string | null) => personaId ? `Model: ${modelId} | Persona: ${personaId}` : `Model: ${modelId}`,
  },
  chatShell: {
    deleteConversationAria: (title: string) => `Delete conversation ${title}`,
  },
  kpiCard: {
    detailsAria: (action: string, name: string) => `${action} details for ${name}`,
    valueVsGoal: (value: string, goal: string) => `${value} vs ${goal}`,
    // Bug-6708: accessible names for the per-KPI icon controls, so keyboard
    // and screen-reader users can identify WHICH KPI each button acts on.
    insertOptionsAria: (name: string) => `Insert options for ${name}`,
    insertAsCubeFormulasAria: (name: string) => `Insert ${name} as CUBE formulas`,
    addKpiToReportAria: (name: string) => `Add KPI ${name} value to report`,
    // Bug-6710: a staged (checked) KPI hid its icon cluster, so removing it
    // was row-click-only -- unreachable by keyboard.
    removeKpiFromReportAria: (name: string) => `Remove KPI ${name} value from report`,
  },
  // Bug-6710: accessible name for each field-library section's expand/collapse
  // chevron. The section header row stays a mouse-clickable convenience (like
  // the KPI/measure rows); the chevron IconButton is the keyboard path.
  // Bug-6712: the visible header labels live here too, so the section copy is
  // centralized instead of hardcoded per component.
  library: {
    toggleSectionAria: (expanded: boolean, section: string) =>
      `${expanded ? 'Collapse' : 'Expand'} the ${section} section`,
    measuresHeader: (count: number) => `Measures (${count})`,
    dimensionsHeader: (count: number) => `Dimensions (${count})`,
    namedSetsHeader: (count: number) => `Named Lists (${count})`,
    hierarchiesHeader: (count: number) => `Hierarchies (${count})`,
  },
  // Bug-6713: accessible name for the staged-measure remove control.
  measureCard: {
    removeFromValuesAria: (name: string) => `Remove ${name} from Values`,
    insertAsFunction: (name: string) => `Insert ${name} as formula`,
    insertAsCubeFormula: (name: string) => `Insert ${name} as CUBEVALUE formula`,
  },
  // Bug-6705: accessible name templates for MeasureCard detail toggle and add action.
  measureCardDetail: {
    detailsAria: (action: string, name: string) => `${action} for ${name}`,
    addToValuesAria: (name: string) => `Add ${name} to Values`,
  },
  // Accessible names for the hierarchy "Rows" assign actions (Bug-6708 class).
  hierarchyCard: {
    addToRowsAria: (name: string) => `Add ${name} to Rows`,
    addLevelToRowsAria: (hierarchyName: string, levelName: string) =>
      `Add ${hierarchyName} level ${levelName} to Rows`,
  },
  confirm: {
    overwriteCells: (count: number) => `This will overwrite ${count} cells. Continue?`,
    overwriteRowsCols: (rows: number, cols: number) => `This will overwrite ${rows} rows x ${cols} columns. Continue?`,
    overwriteRowByColsLabel: (rows: number, cols: number) =>
      `This will overwrite ${rows} row${rows > 1 ? 's' : ''} x ${cols} columns. Continue?`,
    largeResult: (rowCount: string, threshold: string) =>
      `This result contains ${rowCount} rows, which exceeds ${threshold}. Inserting may cause Excel to become unresponsive. Continue?`,
  },
  liveConnection: {
    // Bug-6707: every generated CUBE formula hard-requires a workbook
    // connection literally named TESSALLITE_CONNECTION_NAME (F-025-10).
    // Excel's Data Connection Wizard defaults the friendly name from the
    // server/catalog, so without this explicit step a user who follows the
    // manual setup verbatim still ends at #N/A formulas.
    connectionNameInstruction: (name: string) =>
      `Important: on the last page of Excel's connection wizard, set the connection's Friendly Name to exactly "${name}" (no quotes). CUBE formulas inserted by this add-in only resolve against a connection with that exact name. For an existing connection, rename it via Data > Queries & Connections > Connections > Properties.`,
  },
  namedSet: {
    dynamicTruncated: (name: string, shown: number, total: number) => `"${name}" is a dynamic set with more members than can be captured in a zone (showing ${shown} of ${total}). A zone drop would freeze a partial, point-in-time list. Use "Insert as formulas" for the full, live set.`,
    truncated: (name: string, total: number, shown: number) => `"${name}" has more members (${total}) than can be captured in a zone (only ${shown} would bind), so the result would under-count. Use "Insert as formulas" for the full set.`,
    dynamic: (name: string) => `"${name}" is a dynamic set; a zone drop freezes its membership at this point in time. Use "Insert as formulas" to keep it live.`,
  },
} as const;
