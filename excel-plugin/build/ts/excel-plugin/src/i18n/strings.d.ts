export declare const strings: {
    readonly status: {
        readonly connected: "Connected";
        readonly disconnected: "Disconnected";
        readonly reconnecting: "Reconnecting";
        readonly loading: "Loading...";
    };
    readonly app: {
        readonly title: "Tessallite";
        readonly subtitle: "Excel plugin v1.0.0.4";
        readonly askTessallite: "Ask Tessallite";
        readonly loadingProviderInfo: "Loading provider info...";
        readonly selectedValue: "Selected value";
        readonly drillThrough: "Drill through";
        readonly drillThroughCell: "Drill through selected cell";
        readonly sectionsAria: "Tessallite task pane sections";
        readonly projectSelectorAria: "Project selector";
        readonly glossary: "Glossary";
        readonly glossaryAria: "Open glossary";
        readonly settings: "Settings";
        readonly settingsAria: "Open settings";
        readonly diagnosticsMenuItem: "Diagnostics";
        readonly signOut: "Sign Out";
        readonly projectLabel: "Project";
        readonly tabAnalyse: "Analyse";
        readonly tabKpis: "KPIs";
        readonly retry: "Retry";
        readonly viewingAs: "Viewing as";
        readonly resetToDefault: "Reset to default";
        readonly chatMessagesAria: "Chat messages";
        readonly reportBuilderAria: "Report Builder";
        readonly kpiScorecardAria: "KPI Scorecard";
        readonly profileRemoved: "Profile removed";
        readonly testBuildMarker: "TEST BUILD";
        readonly testBuildSignInFailed: "TEST BUILD - preset sign-in failed";
    };
    readonly projects: {
        readonly none: "No projects available. Create one in the Tessallite web app.";
        readonly noModels: "No models available for the selected project.";
        readonly loadFailed: "Failed to load projects";
        readonly noneSelected: "No project selected. Add a project in the Tessallite web app.";
    };
    readonly auth: {
        readonly loginFailed: "Login failed";
    };
    readonly login: {
        readonly formAria: "Connect to Tessallite";
        readonly brandName: "Tessallite";
        readonly tagline: "Governed analytics";
        readonly serverUrl: "Server URL";
        readonly serverUrlPlaceholder: "https://your-tessallite.io";
        readonly tenant: "Tenant";
        readonly tenantPlaceholder: "Tenant slug";
        readonly email: "Email";
        readonly emailPlaceholder: "Email address";
        readonly password: "Password";
        readonly passwordPlaceholder: "Password";
        readonly showPassword: "Show password";
        readonly hidePassword: "Hide password";
        readonly rememberProfile: "Remember this profile";
        readonly connect: "Connect";
        readonly serverUrlMustStart: "Must start with https:// or http://";
        readonly serverUrlInvalid: "Invalid URL format";
    };
    readonly connection: {
        readonly restored: "Connection restored";
        readonly lost: "Connection lost. Checking again every 30 seconds.";
    };
    readonly toasts: {
        readonly excelBusy: "An Excel operation is already in progress";
        readonly chartFromAgent: "Chart auto-created from agent suggestion";
        readonly insertTableFailed: "Insert table failed";
        readonly feedbackFailed: "Feedback submission failed";
        readonly feedbackRecorded: "Feedback recorded";
        readonly noDataToInsert: "No data to insert";
        readonly noDataToChart: "No data to chart";
        readonly chartCreated: "Chart created";
        readonly chartCreatedWithPostStepWarning: "Chart created, but axis formatting could not be applied. The chart is visible; axis titles may show defaults.";
        readonly insertTableBusy: "Could not insert the table right now — another operation is in progress on that area. Please try again in a moment.";
        readonly chartInsertFailed: "Chart insertion failed. This may not be supported on your Excel version.";
        readonly chartCreationFailed: "Chart creation failed";
        readonly noDataForPivot: "No data for PivotTable";
        readonly pivotInsertFailed: "Local PivotTable insertion failed.";
        readonly pivotInsertFailedCompat: "PivotTable creation is not supported in this version of Excel. This feature requires Excel 2019 or later, or Excel for the Web.";
        readonly pivotInsertFailedMapping: "PivotTable creation failed: the field mapping could not be applied. Check that the selected measures and dimensions are compatible.";
        readonly pivotInsertFailedGeneric: "PivotTable creation failed due to an unexpected error. Check the Diagnostics panel for details.";
        readonly pivotCreated: "Local PivotTable created";
        readonly selectCellForDrill: "Select a cell in an inserted table or CUBE formula to drill through.";
        readonly cellContextFailed: "Could not read cell context. Try selecting a cell first.";
        readonly cubeDrillContextUnavailable: "Drill-through is unavailable because this CUBEVALUE contains slicer context that cannot be recovered.";
        readonly drillMeasureUnavailable: "Drill-through is unavailable because this cell does not contain a resolvable measure id.";
        readonly insertDrillRowsFailed: "Insert drill-through rows failed";
        readonly noProjectForChat: "No project or model available. Please configure a project first.";
        readonly sendMessageFailed: "Failed to send message";
        readonly loadConversationFailed: "Could not load conversation";
        readonly deleteConversationFailed: "Could not delete conversation";
        readonly kpiChartNotSuitable: "KPI results are best viewed as values, not charts.";
        readonly insertFailed: "Insert failed";
        readonly queryNoResults: "Query returned no results";
        readonly queryRowSecurityDenied: "Your row-level security permissions do not grant you access to any rows for this query. Nothing was inserted. This is a permissions restriction, not an empty result — do not read it as zero. Contact your administrator if you believe you should have access.";
        readonly queryExecutionFailed: "Query execution failed";
        readonly formulaInserted: "Formula inserted";
        readonly refreshingValues: "Refreshing all function values...";
        readonly formulaInsertionFailed: "Formula insertion failed";
        readonly kpiInsertionFailed: "KPI insertion failed";
        readonly noKpisAvailable: "No KPIs available";
        readonly scorecardInsertionFailed: "Scorecard insertion failed";
        readonly kpiUndeployedFormula: "This KPI is not yet deployed. CUBE formulas for undeployed KPIs will show #N/A until the KPI is deployed from the model builder.";
        readonly couldNotLoadMembers: "Could not load members";
        readonly namedSetAdvancedExpression: "This named set uses an advanced expression and cannot be dropped into a zone. Use \"Insert as formulas\" instead.";
        readonly namedSetNoMembers: "This named set has no resolvable members to filter by.";
        readonly namedSetResolveFailed: "Could not resolve this named set. Try again.";
        readonly hierarchyResolveFailed: "Could not resolve this hierarchy level to a field.";
        readonly hierarchyDetailFailed: "Could not load hierarchy detail. Try again.";
    };
    readonly kpiPanel: {
        readonly evalError: "KPI values could not be evaluated right now. The list below is current; try refreshing.";
        readonly loadError: "Could not load KPIs. Check your connection.";
        readonly deployedSnapshotInvalid: "The published version of this model is unavailable, so its KPIs cannot be shown. Ask a modeller to deploy the model again, then refresh.";
        readonly tryAgain: "Try again";
        readonly noKpisTitle: "No KPIs yet";
        readonly noKpisDescription: "Create KPIs in the Tessallite web app, then come back here to view and insert them.";
        readonly refreshKpis: "Refresh KPIs";
        readonly insertAllScorecard: "Insert all KPIs as a scorecard table";
        readonly good: "Good";
        readonly warning: "Warning";
        readonly poor: "Poor";
        readonly filterAll: "All";
        readonly filterCertified: "Certified";
        readonly searchPlaceholder: "Search...";
        readonly noSearchMatch: "No KPIs match your search";
        readonly insertTableTitle: "Insert this KPI as a mini-table in the worksheet";
        readonly insertTable: "Insert Table";
        readonly insertChartTitle: "Insert this KPI as a chart comparing value vs target";
        readonly insertChart: "Insert Chart";
        readonly targetPrefix: "Target:";
        readonly targetLabel: "Target";
        readonly currentLabel: "Current";
        readonly headers: {
            readonly kpi: "KPI";
            readonly value: "Value";
            readonly goal: "Goal";
            readonly status: "Status";
            readonly trend: "Trend";
        };
        readonly metric: "Metric";
    };
    readonly kpiLibrary: {
        readonly insertScorecard: "Insert KPI Scorecard";
        readonly noSearchMatch: "No KPIs match your search";
        readonly noKpisAvailable: "No KPIs available";
    };
    readonly library: {
        readonly measuresSection: "measures";
        readonly kpisSection: "KPIs";
        readonly namedSetsSection: "named lists";
        readonly dimensionsSection: "dimensions";
        readonly hierarchiesSection: "hierarchies";
    };
    readonly measureCard: {
        readonly removeFromValues: "Remove from Values";
        readonly insertAsFunction: "Insert as formula";
        readonly insertAsCubeFormula: "Advanced: Insert as CUBEVALUE formula (requires workbook connection)";
        readonly addToValues: "Add to Values";
        readonly addToFilter: "Add to Filter";
        readonly hideDetails: "Hide details";
        readonly showDetails: "Show details";
        readonly detailDescription: "Description";
        readonly detailAggregation: "Aggregation";
        readonly detailFormat: "Format";
        readonly detailFolder: "Folder";
        readonly detailSemiAdditive: "Semi-additive";
        readonly detailDefinition: "Definition";
    };
    readonly dimensionLibrary: {
        readonly closeMemberPreview: "Close";
        readonly closeMemberPreviewAria: "Close member preview";
        readonly noSearchMatch: "No dimensions match your search";
        readonly noItemsAvailable: "No dimensions available";
        readonly membersLabel: "Members";
        readonly noMembersFound: "No members found";
    };
    readonly measureLibrary: {
        readonly noSearchMatch: "No measures match your search";
        readonly noItemsAvailable: "No measures available";
    };
    readonly namedSetLibrary: {
        readonly noSearchMatch: "No named lists match your search";
        readonly noItemsAvailable: "No named lists available";
        readonly previewNotPublished: "This list has not been published yet, so its members cannot be shown. Ask a modeller to deploy the model, then refresh.";
        readonly previewFailed: "The list members could not be loaded. Check your connection and try again.";
    };
    readonly confirm: {
        readonly overwriteActiveCell: "The active cell range already contains data. Overwrite existing data?";
        readonly overwriteTargetCell: "The target cell already contains data. Overwrite?";
    };
    readonly hierarchyCard: {
        readonly typeCalendar: "Calendar";
        readonly typeExplicit: "Hierarchy";
        readonly typeSegment: "Segment";
    };
    readonly kpiCard: {
        readonly chipKpi: "KPI";
        readonly chipCertified: "Certified";
        readonly chipDeprecated: "Deprecated";
        readonly hideDetails: "Hide details";
        readonly showDetails: "Show details";
        readonly insertOptions: "Insert options";
        readonly insertOptionsLabel: "Insert Options";
        readonly insertAsCubeFormulas: "Insert as CUBE formulas";
        readonly addKpiToReport: "Add KPI value to report";
        readonly removeKpiFromReport: "Remove KPI value from report";
        readonly loadingStatus: "Loading KPI status...";
        readonly detailDescription: "Description";
        readonly detailValue: "Value";
        readonly detailGoal: "Goal";
        readonly detailCurrent: "Current";
        readonly detailTarget: "Target";
        readonly detailStatus: "Status";
        readonly detailTrend: "Trend";
        readonly detailGraphic: "Graphic";
        readonly detailWeight: "Weight";
        readonly detailFolder: "Folder";
        readonly detailCertification: "Certification";
        readonly insertFullRow: "Insert Full KPI Row";
        readonly insertFullRowDesc: "Name | Value | Goal | Status";
        readonly insertValueOnly: "Insert Value Only";
        readonly insertValueOnlyDesc: "Single cell with KPI value";
        readonly insertValueGoal: "Insert Value + Goal";
        readonly insertValueGoalDesc: "Two cells: value and goal";
        readonly insertStatusIcon: "Insert Status Icon";
        readonly insertStatusIconDesc: "Traffic light status indicator";
        readonly insertKpiCard: "Insert KPI Card";
        readonly insertKpiCardDesc: "Formatted card with all details";
        readonly insertFormulaRef: "Insert Formula Reference";
        readonly insertFormulaRefDesc: "CUBEKPIMEMBER formula";
    };
    readonly drill: {
        readonly title: "Drill Through";
        readonly copyTsv: "Copy TSV";
        readonly insertSheet: "Insert Sheet";
        readonly prev: "Prev";
        readonly next: "Next";
        readonly loadMore: "Load more rows...";
        readonly accessDenied: "Access denied. Your persona does not have permission to drill through this measure.";
        readonly rowSecurityDenied: "Your row-level security permissions do not grant you access to any of the underlying rows for this cell. This is a permissions restriction, not an absence of detail. Contact your administrator if you believe you should have access.";
        readonly noPaths: "No drill-through paths available for this measure.";
        readonly moreAvailable: " (more available)";
        readonly drillPaths: "Drill paths";
    };
    readonly tableRefresh: {
        readonly refreshSheetData: "Refresh sheet data";
        readonly refreshSheetDataAria: "Re-run all Tessallite tables on the active sheet";
        readonly noTables: "No refreshable Tessallite tables on this sheet.";
        readonly refreshing: "Refreshing tables...";
        readonly noModelSelected: "No model selected. Open the Tessallite panel and select a model first.";
        readonly agentSourceSkip: "Inserted from the Ask tab. Re-run the question from the conversational agent.";
        readonly noStoredHeadersSkip: "Table predates refresh support (no stored column layout). Re-insert it to enable refresh.";
        readonly pivotedSkip: "Cross-tab tables cannot be refreshed automatically. Re-run from Report Builder.";
        readonly concurrentModificationSkip: "A concurrent change was detected during refresh; the table was skipped to avoid stale data.";
        readonly metadataModifiedSkip: "The table's metadata was modified during refresh; skipped to avoid stale data.";
        readonly provenanceWriteBackFailed: "The table data was refreshed, but its saved query details could not be updated. A future refresh may not work until you re-insert it.";
        readonly tableResizedSkip: "The table changed size or position since the sheet was scanned (rows may have been added or removed nearby); it was skipped to avoid overwriting cells outside it. Refresh again.";
        readonly projectMismatchSkip: "Table belongs to a different project than the active session.";
        readonly modelMismatchSkip: "Table belongs to a different model than the active session.";
        readonly corruptedQuerySkip: "The table's saved query is corrupted and cannot be re-run. Re-insert the table.";
        readonly executionFailedSkip: "The query could not be re-run. Check your connection and try again.";
        readonly noResultsSkip: "The query returned no rows, so the table was left unchanged.";
        readonly corruptedHeadersSkip: "The table's saved column layout is corrupted. Re-insert the table.";
        readonly writeFailedSkip: "The refreshed data could not be written to the table. The table was left unchanged.";
        readonly partialRewriteWarning: "Excel reported an error part-way through rewriting this table, so it may hold a mix of old and new rows. Check it, then refresh again.";
        readonly lockBusySkip: "Another Excel operation was still working on these cells. Nothing was changed - try refreshing again.";
        readonly metadataFetchFailedSkip: "Excel could not read this table's saved query details, so it was left unchanged. This is usually temporary - try refreshing again.";
        readonly growthZoneOccupiedSkip: "The query now returns more rows than the table has room for, and the cells directly below it are not empty. The table was left unchanged so your own content is not overwritten. Clear or move those cells, then refresh again.";
        readonly detailsShow: "Show details";
        readonly detailsHide: "Hide details";
        readonly detailsTitle: "Refresh details";
        readonly detailsSkippedGroup: "Skipped";
        readonly detailsWarningGroup: "Needs a look";
    };
    readonly insertMode: {
        readonly label: "Insert mode";
        readonly live: "Live";
        readonly static: "Static";
        readonly liveDescription: "Insert TESSALLITE.* formulas that update on refresh";
        readonly staticDescription: "Insert the current value as a static number";
    };
    readonly reportBuilder: {
        readonly refreshValues: "Refresh";
        readonly modelLabel: "Model";
        readonly modelSelectorAria: "Model selector";
        readonly sortBy: "Sort by";
        readonly sortNone: "None";
        readonly sortAscending: "Ascending";
        readonly sortDescending: "Descending";
        readonly availableFields: "Available fields";
        readonly availableFieldsHint: "Click a field's zone icon to place it above.";
        readonly searchPlaceholder: "Search measures, KPIs, lists, dimensions...";
        readonly certifiedOnly: "Certified only";
        readonly cubeButton: "Advanced: CUBE";
        readonly connectButton: "Connect";
        readonly traceButton: "Trace";
        readonly incompatibleFields: "Selected fields are not compatible";
        readonly removeIncompatibleTable: "Remove incompatible fields before inserting this report.";
        readonly removeIncompatibleChart: "Remove incompatible fields before inserting this chart.";
        readonly removeIncompatiblePivot: "Remove incompatible fields before inserting a local PivotTable.";
        readonly staleDialogTitle: "Stale Entities Detected";
        readonly staleDialogDescription: "The following entities used in this workbook have changed on the server. Formulas referencing these entities may return outdated results.";
        readonly staleKpi: "KPI";
        readonly staleNamedSet: "Named Set";
        readonly staleDeleted: "Deleted from server";
        readonly staleDeprecated: "Deprecated";
        readonly staleDefinitionUpdated: "Definition updated";
        readonly staleCells: "cells:";
        readonly staleLater: "Later";
        readonly staleAcceptCurrent: "Accept Current";
    };
    readonly provenance: {
        readonly source: "Source: Tessallite";
        readonly model: "Model";
        readonly viewingAs: "Viewing as";
        readonly filters: "Filters";
        readonly sortedBy: "Sorted by";
    };
    readonly zone: {
        readonly empty: "Empty";
        readonly buildReport: "Build report";
        readonly buildReportHint: "Pick fields, then insert into Excel.";
        readonly clearLayout: "Clear layout";
        readonly clearLayoutAria: "Clear report layout";
        readonly templates: "Templates";
        readonly valuesLabel: "Values";
        readonly valuesHint: "Add measures or KPIs";
        readonly rowsLabel: "Rows";
        readonly rowsHint: "Add dimensions";
        readonly columnsLabel: "Columns";
        readonly columnsHint: "Optional split";
        readonly filtersLabel: "Filters";
        readonly filtersHint: "Optional criteria";
        readonly tableButton: "Table";
        readonly chartButton: "Chart";
        readonly pivotButton: "Pivot";
        readonly addMeasureHint: "Add at least one measure to enable insert actions.";
        readonly filterChipHint: "Select a filter chip to set operator and values.";
        readonly compatibleDimensions: "Compatible dimensions:";
    };
    readonly filter: {
        readonly dialogTitlePrefix: "Filter:";
        readonly operatorLabel: "Operator";
        readonly equals: "Equals";
        readonly notEquals: "Not Equals";
        readonly contains: "Contains";
        readonly notContains: "Not Contains";
        readonly greaterThan: "Greater Than";
        readonly lessThan: "Less Than";
        readonly dateRange: "Date Range";
        readonly inSet: "In Set";
        readonly valuesComma: "Values (comma-separated)";
        readonly enterTwoDates: "Enter two dates: start, end";
        readonly enterValuesSeparated: "Enter values separated by commas";
        readonly cancel: "Cancel";
        readonly done: "Done";
        readonly apply: "Apply";
        readonly enterValueToCompare: "Enter a value to compare against.";
    };
    readonly insertActions: {
        readonly insertTable: "Insert Table";
        readonly chart: "Chart";
        readonly localPivot: "Local Pivot";
        readonly cubeFormulas: "CUBE formulas";
        readonly liveConnection: "Live connection";
        readonly showQuery: "Show Query";
    };
    readonly cubeWizard: {
        readonly title: "Cube Function Wizard";
        readonly measureLabel: "Measure";
        readonly filterOptional: "Filter (optional)";
        readonly none: "None";
        readonly noDimensionsAvailable: "No compatible dimensions are available for this measure.";
        readonly memberLabel: "Member";
        readonly noMembers: "No members found for this dimension.";
        readonly targetCell: "Target cell";
        readonly connectionHint: "CUBE formulas resolve against a workbook connection named \"{connectionName}\". If you have not set one up yet, open Report Builder and use \"Live connection\" to create it. The formula is inserted regardless; it will show #N/A until the connection exists.";
        readonly validating: "Validating...";
        readonly measureNotFound: "Selected measure not found in model";
        readonly dimensionNotFound: "Selected dimension not found in model";
        readonly selectMember: "Select a member for the dimension filter, or remove the filter";
        readonly memberUnavailable: "Selected member is no longer available for this dimension";
        readonly cancel: "Cancel";
        readonly back: "Back";
        readonly next: "Next";
        readonly insertFormula: "Insert Formula";
    };
    readonly liveConnection: {
        readonly title: "Live XMLA Connection";
        readonly stepStart: "Start";
        readonly stepConnect: "Connect";
        readonly stepInstructions: "Instructions";
        readonly description: "A live XMLA connection lets Excel PivotTables query Tessallite directly. You create the connection in Excel using the server address below, then use Excel's native PivotTable dialog.";
        readonly note: "Note: Excel does not let an add-in create connections or PivotTables programmatically, so these steps are done in Excel's own dialogs.";
        readonly xmlaUsernameLabel: "XMLA Username (email)";
        readonly xmlaUsernameRequired: "Username is required.";
        readonly xmlaUsernameInvalid: "Username contains invalid characters.";
        readonly credentialNote: "Excel will prompt for your password when the connection is first used. Credentials are never stored in the workbook file.";
        readonly serverFlowTitle: "Create the connection in Excel:";
        readonly serverStep1Prefix: "Go to ";
        readonly serverStep1Bold: "Data > Get Data > From Database > From Analysis Services";
        readonly serverStep2: "In the dialog that opens, paste the server address below into the Server name field.";
        readonly serverStep3Prefix: "Server name: ";
        readonly serverStep4Prefix: "Check ";
        readonly serverStep4Bold: "\"Only Create Connection\"";
        readonly serverStep5: "Click OK. Excel will prompt for your username and password.";
        readonly copy: "Copy";
        readonly advancedToggle: "Advanced: raw connection string";
        readonly advancedNote: "If the server-name approach does not work (e.g. older Excel versions), you can create the connection using a raw MSOLAP connection string instead. Paste it into the connection dialog's connection-string field.";
        readonly copyConnectionString: "Copy connection string";
        readonly connectionCreated: "Connection created";
        readonly pivotInstructions: "To build a live PivotTable:";
        readonly pivotStep1Prefix: "Go to ";
        readonly pivotStep1Bold: "Insert > PivotTable";
        readonly pivotStep2Prefix: "Select ";
        readonly pivotStep2Bold: "\"Use an external data source\"";
        readonly pivotStep3Prefix: "Click ";
        readonly pivotStep3Bold: "\"Choose Connection\"";
        readonly pivotStep4Prefix: "Select the ";
        readonly pivotStep4Bold: "\"Tessallite\"";
        readonly pivotStep4Suffix: " connection";
        readonly pivotStep5: "Choose where to place the PivotTable and click OK";
        readonly close: "Close";
        readonly next: "Next";
        readonly back: "Back";
    };
    readonly trace: {
        readonly title: "Query Trace";
        readonly semanticQuery: "Semantic query";
        readonly description: "The semantic query above is sent to the query-router. The route above is the server's decision — whether the report was answered from an accelerated aggregate/pocket table or the source database — with the rewritten SQL it ran. SQL generation, aggregate routing, and dialect translation are performed server-side.";
        readonly emptyState: "Execute a query in Report Builder to view its trace. Add measures to Values and run Insert Table or Insert Chart.";
        readonly close: "Close";
        readonly routeAggregate: "Aggregate (accelerated)";
        readonly routePocket: "Pocket table (accelerated)";
        readonly routeSource: "Source database";
        readonly sqlRedacted: "The physical SQL is available to model builders and administrators. You can see the route decision above, but not the physical table names or the row-security filters the server applied. Ask your administrator if you need this detail.";
    };
    readonly glossary: {
        readonly title: "Glossary";
        readonly searchPlaceholder: "Search glossary terms...";
        readonly noMatch: "No matching glossary entries";
        readonly noEntries: "No glossary entries available";
        readonly synonyms: "Synonyms:";
    };
    readonly profileSwitcher: {
        readonly title: "Connection Profiles";
        readonly switchProfileAria: "Switch profile";
        readonly noSaved: "No saved connections";
        readonly signOut: "Sign Out";
        readonly removeTitle: "Remove Profile";
        readonly removeConfirmation: "This will remove the saved connection. You can log in again to restore it. Continue?";
        readonly cancel: "Cancel";
        readonly remove: "Remove";
    };
    readonly persona: {
        readonly label: "Persona:";
        readonly default: "Default";
    };
    readonly diagnostics: {
        readonly title: "Diagnostics";
        readonly closeAria: "Close diagnostics";
        readonly environment: "Environment";
        readonly version: "Version";
        readonly excelHost: "Excel Host";
        readonly eventLog: "Event Log";
        readonly colTime: "Time";
        readonly colType: "Type";
        readonly colDetail: "Detail";
        readonly clearLog: "Clear Log";
        readonly runSpike: "Run Spike";
        readonly running: "Running...";
        readonly copyDiagnostics: "Copy Diagnostics";
        readonly copied: "Copied";
        readonly compatibilitySpike: "Compatibility Spike";
        readonly spikeConfirmTitle: "Run compatibility spike?";
        readonly spikeConfirmDescription: "The spike checks whether this Excel host supports tables, charts and CUBE formulas. It writes its test data into a temporary hidden worksheet that is deleted immediately afterwards, and reads (but does not change) your current cell selection. Your workbook content is not modified.";
        readonly cancel: "Cancel";
        readonly spikeRunnerFailed: "Spike runner failed";
        readonly unknownPlatform: "Unknown";
        readonly spikeUnknown: "unknown";
        readonly spikeSkipped: "skipped";
    };
    readonly chatShell: {
        readonly newConversation: "New conversation";
        readonly conversationHistory: "Conversation history";
        readonly deleteTitle: "Delete Conversation";
        readonly deleteConfirmation: "This action cannot be undone. Delete this conversation?";
        readonly cancel: "Cancel";
        readonly delete: "Delete";
        readonly unavailableTitle: "Conversational analytics unavailable";
        readonly unavailableDescription: "Contact your Tessallite administrator to configure an LLM provider.";
        readonly composerPlaceholder: "Ask a question about your data...";
    };
    readonly chartPopout: {
        readonly action: "Pop out";
        readonly tooltip: "Open the chart in a resizable window";
        readonly loading: "Loading chart...";
        readonly empty: "This answer has no chart to show.";
    };
    readonly errorBoundary: {
        readonly title: "Something went wrong";
        readonly description: "The plugin encountered an unexpected error. Please close and reopen the task pane to try again.";
        readonly persistNote: "If the problem persists, contact your Tessallite administrator.";
    };
    readonly common: {
        readonly cancel: "Cancel";
        readonly close: "Close";
        readonly continue: "Continue";
    };
};
/**
 * Template strings that require interpolation. Each function accepts the
 * dynamic parts and returns the assembled English string. A future i18n
 * runtime would replace these with locale-aware formatters.
 */
export declare const templates: {
    readonly tableRefresh: {
        readonly refreshed: (count: number) => string;
        readonly refreshedWithSkipped: (refreshed: number, skipped: number) => string;
        readonly allSkipped: (skipped: number) => string;
        readonly refreshedWithWarnings: (refreshed: number, warnings: number) => string;
        readonly refreshedWithSkippedAndWarnings: (refreshed: number, skipped: number, warnings: number) => string;
        readonly skippedDetail: (name: string, reason: string) => string;
        readonly warningsOnly: (warnings: number) => string;
        readonly columnCountChanged: () => string;
        readonly columnRenamed: (stored: string, returned: string) => string;
    };
    readonly toasts: {
        readonly insertedRows: (count: number) => string;
        readonly tableInsertedWithPostStepWarning: (count: number) => string;
        readonly insertedDrillRows: (count: number) => string;
        readonly insertTruncatedWarning: (available: number, total: number) => string;
        readonly insertRefetchFailed: (available: number, total: number) => string;
        readonly insertRefetchEmpty: (available: number, total: number) => string;
        readonly switchedPersona: (name: string) => string;
        readonly cubeValueFormulaInsertedNeedsConnection: (name: string) => string;
        readonly kpiModeInsertedNeedsConnection: (mode: string, name: string) => string;
        readonly kpiFormulasInsertedNeedsConnection: (name: string) => string;
        readonly scorecardInserted: (count: number) => string;
        readonly scorecardInsertedNeedsConnection: (count: number, name: string) => string;
        readonly cubeSetFormulasInsertedNeedsConnection: (name: string) => string;
        readonly customKpiRoutedToFormulaInserted: (kpiName: string, connectionName: string) => string;
        readonly kpiUnresolvableMeasureRoutedToFormulaInserted: (kpiName: string, connectionName: string) => string;
        readonly resultTruncated: (limit: number) => string;
        readonly compositeKpiFormulaInserted: (kpiName: string) => string;
        readonly compositeKpiScorecardNote: (names: string) => string;
        readonly undeployedKpiFormulaWarning: (kpiName: string, connectionName: string) => string;
        readonly undeployedKpiScorecardWarning: (names: string) => string;
        readonly hierarchyPartialLevels: (hierName: string, unresolvedLevels: string[]) => string;
        readonly localPivotUnsafeMeasures: (measureNames: string[]) => string;
    };
    readonly app: {
        readonly askAgent: (name: string) => string;
        readonly viewingAsPersona: (name: string) => string;
    };
    readonly drill: {
        readonly detailRowsLoaded: (count: number) => string;
        readonly page: (current: number, total: number) => string;
    };
    readonly insertActions: {
        readonly dimensions: (rows: number, cols: number) => string;
    };
    readonly cubeWizard: {
        readonly stepOf: (step: number, total: number) => string;
        readonly readyToInsert: (cell: string) => string;
        readonly connectionHintWithName: (name: string) => string;
    };
    readonly kpiPanel: {
        readonly kpiCount: (count: number) => string;
        readonly statusCount: (count: number, label: string) => string;
    };
    readonly kpiLibrary: {
        readonly kpiCount: (count: number) => string;
    };
    readonly reportBuilder: {
        readonly staleStatusChanged: (status: string) => string;
    };
    readonly filter: {
        readonly valueLabel: (kind: string) => string;
        readonly enterSingle: (kind: string) => string;
        readonly notANumber: (value: string) => string;
        readonly notADate: (value: string) => string;
        readonly scalarTruncated: (value: string) => string;
        readonly datesReordered: (low: string, high: string) => string;
    };
    readonly diagnostics: {
        readonly spikeHeading: (host: string, platform: string) => string;
        readonly spikeFailed: (message: string) => string;
    };
    readonly trace: {
        readonly modelPersona: (modelName: string | null, personaName?: string | null) => string;
    };
    readonly chatShell: {
        readonly deleteConversationAria: (title: string) => string;
    };
    readonly kpiCard: {
        readonly detailsAria: (action: string, name: string) => string;
        readonly valueVsGoal: (value: string, goal: string) => string;
        readonly insertOptionsAria: (name: string) => string;
        readonly insertAsCubeFormulasAria: (name: string) => string;
        readonly addKpiToReportAria: (name: string) => string;
        readonly removeKpiFromReportAria: (name: string) => string;
    };
    readonly library: {
        readonly toggleSectionAria: (expanded: boolean, section: string) => string;
        readonly measuresHeader: (count: number) => string;
        readonly dimensionsHeader: (count: number) => string;
        readonly namedSetsHeader: (count: number) => string;
        readonly hierarchiesHeader: (count: number) => string;
    };
    readonly measureCard: {
        readonly removeFromValuesAria: (name: string) => string;
        readonly insertAsFunction: (name: string) => string;
        readonly insertAsCubeFormula: (name: string) => string;
    };
    readonly measureCardDetail: {
        readonly detailsAria: (action: string, name: string) => string;
        readonly addToValuesAria: (name: string) => string;
        readonly addToFilterAria: (name: string) => string;
    };
    readonly hierarchyCard: {
        readonly addToRowsAria: (name: string) => string;
        readonly addLevelToRowsAria: (hierarchyName: string, levelName: string) => string;
    };
    readonly confirm: {
        readonly overwriteCells: (count: number) => string;
        readonly overwriteRowsCols: (rows: number, cols: number) => string;
        readonly overwriteRowByColsLabel: (rows: number, cols: number) => string;
        readonly largeResult: (rowCount: string, threshold: string) => string;
    };
    readonly liveConnection: {
        readonly connectionNameInstruction: (name: string) => string;
    };
    readonly namedSet: {
        readonly dynamicTruncated: (name: string, shown: number, total: number) => string;
        readonly truncated: (name: string, total: number, shown: number) => string;
        readonly dynamic: (name: string) => string;
    };
};
