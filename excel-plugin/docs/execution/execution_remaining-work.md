# Excel Plugin — Remaining Work

Date: 2026-05-20
Scope: Gaps between `execution_plan.md` and current codebase
Status: 27 tests pass, tsc clean, core flows (login, ask, report builder, charts, drill) functional

---

## Work Summary

| # | Category | Scope |
|---|---|---|
| A | New components and hook extractions | 14 tasks |
| B | Test coverage | 71 new tests across 12 files |
| C | Hardening (errors, diagnostics, a11y, perf, distribution) | 5 workstreams, partially started |
| D | Open design decisions | 2 remaining |

All work is frontend-only. No backend changes required — all referenced API endpoints exist.

---

## A. New Components and Extractions

### A1. `src/components/common/` — 5 files

**Current state:** Directory exists but is empty. `architecture_specs.md` Section 7.4 lists these components.

#### A1.1 — `src/components/common/SearchBar.tsx`

**Purpose:** Shared search input used by Report Builder and Ask Tessallite glossary search. 300ms debounce, clear button, search icon.

**Input contract:**
```typescript
interface SearchBarProps {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  disabled?: boolean;
}
```

**Implementation:**
- MUI `TextField` variant `"outlined"`, size `"small"`, full-width
- `InputAdornment` with `SearchOutlined` icon on left
- Clear button (`CloseOutlined`) on right when `value` is non-empty
- `useRef` for 300ms debounce timer; call `onChange` only after 300ms since last keystroke
- `InputProps.sx` padding 4px top/bottom, 8px left/right

**Consumed by:** `ReportBuilder.tsx` (replaces inline search field), `GlossaryModal.tsx`

---

#### A1.2 — `src/components/common/StatusBadge.tsx`

**Purpose:** Small coloured chip showing status (connected/disconnected/reconnecting, persona active, agent configured/not-configured).

**Input contract:**
```typescript
interface StatusBadgeProps {
  status: 'connected' | 'disconnected' | 'reconnecting' | 'active' | 'inactive';
  label: string;
  size?: 'small' | 'medium';
}
```

**Implementation:**
- MUI `Chip` with `size={size ?? 'small'}`, `variant="outlined"`
- Colour mapping via `sx`: `connected`/`active` = `success.main`, `disconnected`/`inactive` = `grey.500`, `reconnecting` = `warning.main`
- Leading dot via `avatar={<Box sx={{ width: 8, height: 8, borderRadius: '50%', bgcolor: 'currentColor' }} />}`

**Consumed by:** `App.tsx` footer (connection status), `PersonaDropdown.tsx` (persona active indicator), ChatPanel header (provider/model badge)

---

#### A1.3 — `src/components/common/SectionHeader.tsx`

**Purpose:** Collapsible section header with title, optional badge count, fold/unfold toggle.

**Input contract:**
```typescript
interface SectionHeaderProps {
  title: string;
  count?: number;
  collapsed: boolean;
  onToggle: () => void;
}
```

**Implementation:**
- MUI `Box` with `display: 'flex'`, `alignItems: 'center'`, `justifyContent: 'space-between'`, `p: 0.5`
- Left: `Typography variant="overline"` with title text; if `count` provided, append ` ({count})` 
- Right: `IconButton size="small"` with `ExpandMoreOutlined` rotated 180deg when collapsed vs 0deg when expanded (CSS `transform: rotate(${collapsed ? 0 : 180}deg)`)
- Entire row is clickable to toggle

**Consumed by:** `ReportBuilder.tsx` (Measure/Dimension/Hierarchy library headers), `GlossaryModal.tsx` (type filter sections), `DrillPanel.tsx` (hierarchy path sections)

---

#### A1.4 — `src/components/common/LoadingSkeleton.tsx`

**Purpose:** Skeleton placeholder during loading states.

**Input contract:**
```typescript
interface LoadingSkeletonProps {
  variant: 'card' | 'list' | 'chat' | 'table';
  count?: number;
}
```

**Implementation:**
- `card` (count=1): Single MUI `Skeleton variant="rounded" height={120}` with 60% width last-line
- `list` (count=5): 5x `Skeleton variant="text"` at heights [16, 16, 16, 14, 14] with decreasing widths [90%, 85%, 70%, 80%, 60%]
- `chat` (count=3): 3x alternating left/right `Skeleton variant="rounded"` bubbles (left 70% width, right 60% width)
- `table` (count=5): Header row `Skeleton height=32` + 5x detail rows `Skeleton height=24` each

**Consumed by:** `App.tsx` (projects/models loading), `ReportBuilder.tsx` (measures/dimensions loading), `ChatPanel.tsx` (conversation loading), `GlossaryModal.tsx` (glossary loading)

---

#### A1.5 — `src/components/common/EmptyState.tsx`

**Purpose:** Standardised empty state with icon, message, and optional action button.

**Input contract:**
```typescript
interface EmptyStateProps {
  icon?: React.ReactNode;
  title: string;
  description?: string;
  action?: { label: string; onClick: () => void };
}
```

**Implementation:**
- Default icon: `InboxOutlined` at 48px, colour `grey.400`
- `Typography variant="h6"` for title, `variant="body2"` colour `text.secondary` for description
- Optional MUI `Button variant="outlined" size="small"` for action
- Centred in parent with `display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 1, py: 4`

**Consumed by:** `App.tsx` (no projects / no models / agent not configured states), `ReportBuilder.tsx` (no measures in library), `AskTessallite/ChatPanel.tsx` (empty conversation), `DrillThrough/DrillPanel.tsx` (no cell selected), `Glossary/GlossaryModal.tsx` (no results)

---

### A2. `src/components/ProfileSwitcher/ProfileSwitcher.tsx` — 1 file

**Current state:** Directory exists with `PersonaDropdown.tsx` (persona switcher). Profile menu logic is currently inlined in `App.tsx`. This is an **extraction refactor**, not greenfield.

**Purpose:** Extract the profile menu from `App.tsx` header into a dedicated component. Dropdown shows saved connection profiles and allows switching without re-entering credentials. Per `execution_plan.md` Phase 1 Workstream A task 2 (profile persistence) and SPECS 4.1.

**Input contract:**
```typescript
interface ProfileSwitcherProps {
  profiles: ConnectionProfile[];
  activeProfile: ConnectionProfile | null;
  onSwitch: (profile: ConnectionProfile) => void;
  onRemove: (profileId: string) => void;
}
```

Where `ConnectionProfile` is defined in `src/utils/storage.ts`:
```typescript
interface ConnectionProfile {
  id: string;
  name: string;
  serverUrl: string;
  tenantId: string;
  email: string;
  createdAt: string;
}
```

**Implementation:**
- MUI `Button` with `PersonOutline` icon showing active profile name (truncated to 18 chars)
- Opens MUI `Menu` on click with:
  - Header: "Connection Profiles" in small grey text
  - List of profiles, each showing `name (email)` with a checkmark icon on the active one
  - Clicking a different profile calls `onSwitch(profile)`
  - Each non-active profile has a delete icon button (with confirmation dialog) calling `onRemove(profileId)`
- Below the profile list, a divider + "Sign Out" menu item (calls auth.logout)
- If no profiles exist, show "No saved connections" disabled item

**Consumed by:** `App.tsx` header bar (replace inline profile menu code)

---

### A3. `src/api/gateway.ts` — 1 file

**Purpose:** Client for XMLA gateway endpoints. Per `execution_plan.md` Phase 1 Workstream B.

**Endpoints:**
```typescript
export async function healthCheck(): Promise<{ status: string }> {
  return apiClient.get('/health');
}

// XMLA gateway is never called directly by the plugin (XMLA goes through MSOLAP provider).
// This file exists as a thin placeholder for: health check, future connection-verification calls,
// and any gateway diagnostics the plugin may need in Phase 4 diagnostics.
```

**Implementation:**
- Reuse `apiClient` from `client.ts` (no special auth — XMLA uses Basic Auth separately)
- Export `healthCheck()` and stub `gatewayVersion()` calls
- Document in file header: "The plugin does not send XMLA queries through this client. XMLA traffic flows through Excel's MSOLAP provider directly to the gateway."

**Consumed by:** App.tsx (connection health poll)

---

### A4. `src/hooks/useAgentConversation.ts` + `src/hooks/useSseStream.ts` — 2 files

**Current state:** Conversation and streaming logic is inlined in `App.tsx` (lines 80-340 approximately). These hooks need extraction.

#### A4.1 — `src/hooks/useAgentConversation.ts`

**Purpose:** Manages agent conversation lifecycle: create, load history, send messages, receive responses, feedback.

**Input contract:**
```typescript
interface UseAgentConversation {
  // State
  messages: Message[];
  streaming: boolean;
  conversationId: string | null;
  turnId: string | null;
  error: string | null;

  // Actions
  startConversation: () => Promise<void>;
  sendMessage: (text: string, personaId?: string | null) => Promise<void>;
  sendFeedback: (vote: 'up' | 'down', comment?: string) => Promise<void>;
  loadConversations: () => Promise<ConversationSummary[]>;
  loadConversation: (id: string) => Promise<void>;
  resetConversation: () => void;
}

interface Message {
  role: 'user' | 'assistant';
  content: string;
  streaming?: boolean;
  messageId?: string;
  judgeVerdict?: { confidence: number; rubric_score: number; narrative: string };
  resultPreview?: { headers: string[]; rows: (string | number)[][] };
  resultAnnotation?: {
    measures?: Record<string, { title: string; type: string; format?: string }>;
    dimensions?: Record<string, { title: string; type: string }>;
    timeDimensions?: Record<string, { title: string; type: string }>;
  };
  chartType?: string | null;
}
```

**Implementation:**
- Move the conversation state (`messages`, `streaming`, `conversationId`) from `App.tsx` into this hook
- `startConversation`: calls `createConversation(projectId, modelId)` from `agentService.ts`, stores returned `conversation_id`
- `sendMessage`: calls `sendMessageStream(...)` from `agentService.ts` with SSE parser (extracted from App.tsx lines ~200-300)
- SSE parsing: listen for `narration.delta` (append to current assistant message), `query.rows` (populate `resultPreview`), `turn.completed` (finalise, set `chartType`, `judgeVerdict`)
- `sendFeedback`: calls `sendFeedback(conversationId, turnId, vote, comment)` 
- `loadConversations`: calls `getConversations(projectId)` 
- `loadConversation`: calls `getConversation(projectId, conversationId)`, parses turns into messages
- `resetConversation`: clears messages, sets conversationId null

**Files to extract from:** `App.tsx`

---

#### A4.2 — `src/hooks/useSseStream.ts`

**Purpose:** Generic SSE stream parser. Extracted from inline App.tsx streaming logic.

**Input contract:**
```typescript
interface UseSseStream {
  connect: (url: string, token: string, body: unknown) => void;
  disconnect: () => void;
  onEvent: (eventType: string, data: unknown) => void;
  onError: (error: Error) => void;
  onComplete: () => void;
  isStreaming: boolean;
}
```

**Implementation:**
- Uses `EventSource` polyfill (since Office.js webview may not support native SSE): `fetch` with `ReadableStream` reader
- Parse SSE protocol: split by `\n\n`, extract `event:` and `data:` lines, emit typed events
- Reconnect logic: 1s, 2s, 4s, max 30s exponential backoff
- Abort controller for `disconnect()`
- `isStreaming` ref for guard against double-connect

**Files to extract from:** App.tsx streaming section

---

### ~~A5. `src/utils/excelTables.ts`~~ — ALREADY EXISTS

> **This item is invalid.** Table creation, auto-fit, number formatting, and metadata tagging are already implemented across:
> - `src/utils/officeSpike.ts` — `insertResultTable()` handles sheet creation, header/row writing, table creation, auto-fit, and number format application
> - `src/hooks/useExcel.ts` — `doInsertAndTag()` orchestrates `insertResultTable()` + `setTableMetadata()`, with large-row confirmation guard and format token passthrough
> - `src/utils/workbookMetadata.ts` — `setTableMetadata()` / `getTableMetadata()` for result refresh metadata
>
> No new file needed. Remove from implementation sequence.

---

### A6. `src/components/ReportBuilder/MeasureLibrary.tsx` + `src/components/ReportBuilder/DimensionLibrary.tsx` — 2 files

**Current state:** `ReportBuilder.tsx` renders measures and dimensions inline. Per `execution_plan.md` Phase 2 Workstream B and `execution_deferred-action-plan.md` Workstream I2.

#### A6.1 — `src/components/ReportBuilder/MeasureLibrary.tsx`

**Purpose:** Standalone measure list with search filtering, grouping by folder, variant indentation, and multi-select checkboxes.

**Input contract:**
```typescript
interface MeasureLibraryProps {
  measures: Measure[];
  searchQuery: string;
  selectedMeasureIds: string[];
  onToggleMeasure: (measureId: string) => void;
  onAddToValues: (measureId: string) => void;
  onMeasureDetail: (measureId: string) => void;
  personaId?: string | null;
  loading?: boolean;
}
```

**Implementation:**
- Accept full `measures` array; filter by `searchQuery` across `display_name`, `name`, `description`, `effective_description`, `display_folder`, glossary synonyms (if available)
- Group by `display_folder` with collapsible `SectionHeader`; measures without a folder go in "Other" group at bottom
- Sort groups alphabetically; within each group, sort measures alphabetically by `display_name`
- Variant measures (where `variant_of` field is non-null) indented under base measure with 20px left margin. Collapse/expand toggle `[+ N variants]`
- Each measure rendered via `<MeasureCard>` with checkbox for multi-select and `[+ Values]` quick-action button
- When `loading` is true, render `<LoadingSkeleton variant="list" count={8} />`
- When filtered list is empty, render `<EmptyState title="No measures match your search" />`

**Files to extract from:** `ReportBuilder.tsx` — all `measures.filter(...).map(...)` blocks

---

#### A6.2 — `src/components/ReportBuilder/DimensionLibrary.tsx`

**Purpose:** Standalone dimension list with search filtering and quick-action buttons.

**Input contract:**
```typescript
interface DimensionLibraryProps {
  dimensions: Dimension[];
  searchQuery: string;
  onAddToRows: (dimensionId: string) => void;
  onAddToColumns: (dimensionId: string) => void;
  onAddToFilter: (dimensionId: string) => void;
  onPreviewMembers: (dimensionId: string) => void;
  personaId?: string | null;
  loading?: boolean;
}
```

**Implementation:**
- Filter dimensions by `searchQuery` across `display_name`, `name`, `description`, `effective_description`
- Each dimension rendered via `<DimensionCard>` with quick-action buttons: `[-> Rows]`, `[-> Columns]`, `[-> Filter]`
- `[Preview members]` link on each card calls `onPreviewMembers(dimensionId)` → loads via `POST /api/v1/discover/members`, shows inline panel with first 20 values
- When `loading`, render `<LoadingSkeleton variant="list" count={5} />`
- When empty, render `<EmptyState title="No dimensions match your search" />`

**Files to extract from:** `ReportBuilder.tsx` — all `dimensions.filter(...).map(...)` blocks

---

### A7. `src/components/ReportBuilder/TemplateCard.tsx` — 1 file

**Purpose:** Extract the single template card rendering from `TemplatePicker.tsx` into a dedicated component. Per `execution_plan.md` Phase 2 Workstream C. This is a **refactor extraction**, not greenfield — `TemplatePicker.tsx` already exists and renders template cards inline.

**Input contract:**
```typescript
interface TemplateCardProps {
  template: ReportTemplate;
  isApplicable: boolean;
  missingFields: string[];
  onSelect: (template: ReportTemplate) => void;
}

interface ReportTemplate {
  id: string;
  name: string;
  description: string;
  icon: string;         // MUI icon name, e.g. "BarChart", "Timeline"
  minMeasures: number;
  preferredMeasureTypes?: string[];
  requiredDimensions: string[];  // dimension type hints, e.g. ["time", "geography"]
}
```

**Implementation:**
- MUI `Card` with `variant="outlined"`, width 100%, cursor pointer
- `CardContent`: icon (via dynamic MUI icon import), `name` as `Typography variant="subtitle2"`, `description` as `variant="caption"` colour `text.secondary`
- If `!isApplicable`: card is greyed (`sx={{ opacity: 0.5, pointerEvents: 'none' }}`), show `missingFields` list in red caption text
- On click: `onSelect(template)`

**Template data:** Uses `REPORT_TEMPLATES` from `src/utils/reportTemplates.ts` (already exists, tested with 8 tests)

**Consumed by:** `TemplatePicker.tsx` (already exists)

---

### A8. `src/utils/connectionStrings.ts` + `src/hooks/useExcelConnections.ts` — 2 files

**Current state:** Neither file exists. `CubeFormulaWizard.tsx` and `LiveConnectionWizard.tsx` need these. Per `execution_plan.md` Phase 2 Workstreams D+E.

#### A8.1 — `src/utils/connectionStrings.ts`

**Purpose:** Build MSOLAP/OLEDB connection strings and detect existing workbook connections.

**Input contract:**
```typescript
export function buildMsolapConnectionString(params: {
  serverUrl: string;
  catalogName: string;
  email: string;
  password: string;
}): string;

export function buildOledbConnectionString(params: {
  serverUrl: string;
  catalogName: string;
  email: string;
  password: string;
}): string;

export function parseExistingConnection(connectionString: string): {
  serverUrl?: string;
  catalogName?: string;
  email?: string;
} | null;
```

**Implementation:**
- `buildMsolapConnectionString`: `Provider=MSOLAP.8;Data Source={serverUrl}/api/v1/xmla/;Initial Catalog={catalogName};User ID={email};Password={password};Persist Security Info=False`
- `buildOledbConnectionString`: `Provider=MSOLAP;Data Source={serverUrl}/api/v1/xmla/;Catalog={catalogName};User ID={email};Password={password}`
- `parseExistingConnection`: regex extract Data Source, Catalog, User ID from connection string

**Consumed by:** `LiveConnectionWizard.tsx`, `CubeFormulaWizard.tsx`

---

#### A8.2 — `src/hooks/useExcelConnections.ts`

**Purpose:** Detect and manage workbook XMLA connections via Office.js. Per `execution_plan.md` Phase 2 Workstream E.

**Input contract:**
```typescript
interface UseExcelConnections {
  connections: ExcelConnectionInfo[];
  hasTessalliteConnection: boolean;
  refreshConnections: () => Promise<void>;
  createConnection: (params: ConnectionParams) => Promise<boolean>;
  removeConnection: (connectionId: string) => Promise<void>;
}

interface ExcelConnectionInfo {
  id: string;
  name: string;
  type: string;
  description: string;
}

interface ConnectionParams {
  serverUrl: string;
  catalogName: string;
  email: string;
  password: string;
}
```

**Implementation:**
- `refreshConnections`: calls `context.workbook.connections.load('items')`, maps to `ExcelConnectionInfo[]`
- `hasTessalliteConnection`: computed — any connection where `name` or `description` contains "Tessallite"
- `createConnection`: 
  1. Attempt `context.workbook.connections.add(name, description, connectionString, null)` via Office.js — this may fail depending on host support
  2. If fails with "not supported", return `false` (caller should show manual instructions)
  3. If succeeds, return `true`
- `removeConnection`: `context.workbook.connections.getItem(connectionId).delete()`
- Password is consumed and discarded — never stored in component state longer than the connection creation call

**Consumed by:** `LiveConnectionWizard.tsx`, `CubeFormulaWizard.tsx`

---

### A9. `src/components/Settings/DiagnosticsPanel.tsx` — 1 file

**Purpose:** Settings panel showing client-side diagnostics log with "Copy Diagnostics" button. Per `execution_plan.md` Phase 4 Workstream B.

**Input contract:**
```typescript
interface DiagnosticsPanelProps {
  open: boolean;
  onClose: () => void;
}
```

**Implementation:**
- MUI `Dialog` maxWidth="sm", fullWidth
- `DialogTitle`: "Diagnostics" with close button
- `DialogContent`:
  - Summary stats: plugin version, Excel host (if detectable via `Office.context.platform`), connected server URL, active tenant
  - Table of last 100 events: timestamp, event type (API call, Excel operation, error), status, duration
  - Events sourced from `src/utils/diagnostics.ts` (already exists and is complete — ring buffer, redaction, export all implemented)
- `DialogActions`:
  - "Copy Diagnostics" button: serializes events to JSON, copies to clipboard via `navigator.clipboard.writeText()`
  - "Clear Log" button: resets diagnostics buffer
  - Redaction is applied in `diagnostics.ts` before copy (passwords, JWTs, connection strings)

**Consumed by:** `App.tsx` settings menu (gear icon)

---

## B. Missing Test Files

### B1. Unit Tests (5 additional categories needed)

Current: 4 test files (27 tests). Need 5 more:

#### B1.1 — `src/__tests__/apiClient.test.ts` (API client error normalisation)
- **Tests (8):**
  1. 401 response clears JWT and redirects to login
  2. 403 response shows permission denied message
  3. 422 response extracts validation errors
  4. 500 response shows generic server error
  5. Network timeout shows timeout message
  6. JSON parse failure on non-JSON response
  7. Retry on 5xx (GET only) — 3 attempts with exponential backoff
  8. No retry on mutations (POST/PUT/PATCH/DELETE)

#### B1.2 — `src/__tests__/storage.test.ts` (Storage helpers — no password persistence)
- **Tests (5):**
  1. `saveProfile` stores name, serverUrl, tenantId, email — no password field
  2. `loadProfiles` returns parsed profiles
  3. `removeProfile` deletes by index
  4. `setActiveProfile` / `getActiveProfile` round-trips
  5. Storage mock: verify `OfficeRuntime.storage.setItem` never called with a key containing "password"

#### B1.3 — `src/__tests__/formatMapping.test.ts` (Format token to Excel number format mapping)
- **Tests (9):**
  1. `currency` → `$#,##0.00`
  2. `currency_0dp` → `$#,##0`
  3. `percent` → `0.00%`
  4. `percent_0dp` → `0%`
  5. `decimal_2dp` → `#,##0.00`
  6. `decimal_0dp` → `#,##0`
  7. Unknown token → `General`
  8. Null/undefined → `General`
  9. Mapping applies via `table.columns.getItem(...).getDataBodyRange().numberFormat`

#### B1.4 — `src/__tests__/queryBuilder.test.ts` (Report layout to semantic query conversion)
- **Tests (6):**
  1. Measures only → `{ measures: ["m1", "m2"], dimensions: [] }`
  2. Measures + dimensions → correct structure
  3. Filters included in query
  4. Time dimension mapped correctly
  5. PersonaId threaded through
  6. Limit applied

#### B1.5 — `src/__tests__/search.test.ts` (Search matching logic)
- **Tests (7):**
  1. Exact display_name match
  2. Partial display_name match
  3. Match on technical name
  4. Match on description text
  5. Match on glossary synonym (alias map)
  6. Case-insensitive match
  7. No match returns empty

---

### B2. Component Tests — 0 currently exist. Need:

#### B2.1 — `src/__tests__/LoginScreen.test.tsx`
- **Tests (5):**
  1. Renders email, password, tenant, server URL fields
  2. Submit calls login with form values
  3. Shows error message on auth failure
  4. Shows loading spinner during login
  5. Password field has type="password"

#### B2.2 — `src/__tests__/ChatPanel.test.tsx`
- **Tests (6):**
  1. Renders empty state with suggested prompts
  2. Renders user message bubble
  3. Renders streaming assistant message
  4. Shows agent-not-configured state with disabled input
  5. Send button disabled when input is empty
  6. Feedback buttons call sendFeedback

#### B2.3 — `src/__tests__/ReportBuilder.test.tsx`
- **Tests (6):**
  1. Renders measure, dimension, hierarchy libraries
  2. Search bar filters all libraries
  3. Adding measure to Values shows chip in zone grid
  4. Adding dimension to Rows shows chip in zone grid
  5. Clear button resets all zones
  6. Insert button disabled when no items assigned

#### B2.4 — `src/__tests__/TemplatePicker.test.tsx`
- **Tests (4):**
  1. Renders all 6 templates
  2. Templates with missing fields show warning
  3. Selecting template populates zones
  4. Template card shows description and field requirements

---

### B3. Component Tests — additional categories

#### B3.1 — `src/__tests__/PersonaDropdown.test.tsx`
- **Tests (4):**
  1. Renders persona list with names and descriptions
  2. Switching persona calls onSelect
  3. Active persona shows checkmark
  4. Info bar shown when non-default persona active

#### B3.2 — `src/__tests__/GlossaryModal.test.tsx`
- **Tests (5):**
  1. Searchable input filters glossary terms
  2. Clicking term shows definition, synonyms, source badge
  3. Empty results show empty state
  4. Filter by type (All/Measures/Dimensions)
  5. Filter by source (All/User/LLM)

---

### B4. Security Tests — 0 currently exist. Need:

#### B4.1 — `src/__tests__/security.test.ts`
- **Tests (6):**
  1. `diagnostics.ts` redacts passwords from event log before export
  2. `diagnostics.ts` redacts JWT tokens from event log
  3. `diagnostics.ts` redacts connection strings containing passwords
  4. `storage.ts` never writes password to OfficeRuntime.storage
  5. Login form clears password from component state after submit
  6. Profile switch clears React Query cache (via testing queryClient)

---

### B5. Test Plan Summary

| Test File | Category | Tests | Priority |
|---|---|---|---|
| `apiClient.test.ts` | Unit | 8 | HIGH |
| `storage.test.ts` | Unit | 5 | HIGH |
| `formatMapping.test.ts` | Unit | 9 | MEDIUM |
| `queryBuilder.test.ts` | Unit | 6 | MEDIUM |
| `search.test.ts` | Unit | 7 | MEDIUM |
| `LoginScreen.test.tsx` | Component | 5 | HIGH |
| `ChatPanel.test.tsx` | Component | 6 | HIGH |
| `ReportBuilder.test.tsx` | Component | 6 | HIGH |
| `TemplatePicker.test.tsx` | Component | 4 | MEDIUM |
| `PersonaDropdown.test.tsx` | Component | 4 | MEDIUM |
| `GlossaryModal.test.tsx` | Component | 5 | MEDIUM |
| `security.test.ts` | Security | 6 | HIGH |
| **Total** | | **71** | |

Notes:
- Excel integration tests (Section 9.3) and UAT scenarios (Section 9.5) require a live Excel instance and cannot be run from vitest. They are documented requirements for a live-test pass, not blocked code tasks.
- Office.js mocking strategy for component tests: `vitest.mock('office-js')` providing stub implementations of `Excel.run`, `context.workbook`, etc.

---

## C. Hardening — 5 Workstreams (Partially Started)

### C1. Error Handling and Resilience (Workstream A)

All changes to existing code — no new files. **Partial implementation already exists.**

**Already done in `src/api/client.ts`:**
- `ApiError` class (lines 16-28) with `status` and `body` fields, extracts `detail` from response body
- 401 handling: removes JWT, calls `onUnauthorized`, throws typed `ApiError`
- GET retry: `MAX_SAFE_RETRIES = 3`, exponential backoff (`BASE_DELAY_MS = 1000`, 1s/2s/4s)
- Mutations (POST/PUT/DELETE): no retry (correct)

**Remaining tasks:**

1. **Add typed error mapping to `src/api/client.ts`:**
   - Map HTTP status codes to semantic types: 403→`PermissionDenied`, 404→`ModelNotDeployed` (for model-specific calls), 422→`ValidationError`, 500→`ServerError`
   - Export `formatApiError(err: ApiError): string` — user-readable error message per type

2. **SSE reconnect in `useSseStream.ts`** (once extracted per A4.2):
   - Exponential backoff 1s, 2s, 4s, max 30s

3. **Offline banner in `App.tsx`:**
   - Health check poll every 30s (already exists in App.tsx `useEffect` with `healthCheck()`)
   - After 3 consecutive failures: show banner at top of pane: "Connection lost. [Retry]" with countdown
   - Retry button resets counter and forces immediate health check

---

### C2. Diagnostics (Workstream B)

**`src/utils/diagnostics.ts` is complete and tracked.** It implements:
- Ring buffer of last 100 events (`MAX_EVENTS = 100`)
- 4 typed event loggers: `logApiEvent()`, `logExcelEvent()`, `logError()`, `logInfo()`
- `redactUrl()` strips path segments; `redactString()` handles Bearer tokens and `Password=` fields
- `getDiagnosticsReport()` serializes buffer to text format
- `copyDiagnostics()` copies report to clipboard

**Remaining tasks:**

1. **Build `Settings/DiagnosticsPanel.tsx`** (covered in A9) — the UI wrapper that displays these events
2. **Wire into `App.tsx`:** call `logApiEvent(...)` from fetch wrapper on every API call, `logExcelEvent(...)` on every Excel operation
3. **Additional redaction:** Replace JWT tokens in URL params with `[REDACTED]` (not yet covered by `redactUrl()`)

---

### C3. Accessibility (Workstream C)

1. **Keyboard navigation:**
   - Mode switcher: arrow keys between "Ask Tessallite" / "Report Builder", Enter to select (already `role="tablist"` in plan — verify)
   - Search bar: Escape clears search, focus auto-captured
   - Chat input: Enter sends, Shift+Enter newline
   - Insert buttons: Tab-accessible, Enter to activate
   - Modals: Focus trap (tab cycles within modal), Escape closes
   - Dropdowns: Arrow keys to navigate, Enter to select, Escape to close

2. **ARIA attributes:**
   - Mode switcher: `role="tablist"`, each button `role="tab"` with `aria-selected`
   - Chat messages: `role="log"`, `aria-live="polite"` on streaming content
   - Toasts: `aria-live="assertive"`
   - Insert buttons: `aria-label` describing action (e.g., "Insert revenue by country as Excel table")

3. **Reduced motion:**
   - In `theme.ts`: check `window.matchMedia('(prefers-reduced-motion: reduce)')`
   - If true: set MUI transitions duration to `0ms`, disable streaming text animation

---

### C4. Performance (Workstream D)

1. **Virtualised lists:** `VirtualList.tsx` exists but does **NOT** use `react-window`. It is a simple truncation (`items.slice(0, maxVisibleItems)`) with a "Showing X of Y" message — not true virtualisation. Needs to be **replaced** with a proper `react-window` `FixedSizeList` implementation. `react-window` is in `package.json` deps but never imported.

2. **Debounced search:** Search bar in `ReportBuilder.tsx` (or new `SearchBar.tsx` common component). 300ms debounce implemented in `SearchBar.tsx` (see A1.1).

3. **Avoid chat re-render on streaming:** Current `App.tsx` updates `messages` state on every SSE token. Refactor to use `useRef` for accumulating streaming text, only update state every 100ms (batch updates).

4. ~~**Metadata cache by deployed_version_id:**~~ **ALREADY DONE.**
   - `useModel.ts` already includes `model.deployed_version_id` (via `versionKey`) in TanStack Query keys for `useMeasures`, `useDimensions`, `useHierarchies`
   - Version change → automatic refetch already works
   - Stale time: 5 minutes (`staleTime: 5 * 60 * 1000`) — no change needed

5. **Cap task pane previews:**
   - Result preview in ChatPanel: max 20 rows shown
   - "Show all N rows" link expands to full scrollable list (still capped at 200)

---

### C5. Distribution (Workstream E)

**`manifest.xml` is already production-ready.** Current state:
- `Id`: real UUID `e3ae536b-1d2f-44fd-bdc4-e01b5a7597d2` (not a placeholder)
- `Version`: `1.0.0.0`
- `ProviderName`: "Tessallite"
- `DefaultLocale`: "en-US"
- `Host`: `<Host Name="Workbook"/>` (correct for Excel; NOT Mailbox which is Outlook)
- Ribbon: 3 buttons (Connect, Ask, Report Builder) with icons, supertips, and deep-link URLs
- Icon sizes: 16, 32, 80 registered in Resources

**Remaining tasks:**

1. **Verify icon assets exist** — confirm `public/assets/icon-16.png`, `icon-32.png`, `icon-80.png` are present and correct dimensions
2. **Update URLs for production** — replace `https://localhost:3000` with deployment URL
3. **Sideloading docs:** Already in SPECS Section 7.1 — verify steps are accurate against current manifest
4. **Enterprise deployment docs:** Already in SPECS Section 7.2 — verify

---

## D. Open Design Decisions — 2 Remaining

6 decisions from `execution_plan.md` Section 11. 4 are already resolved/implemented; 2 remain.

| # | Decision | Recommendation | Status | Evidence |
|---|---|---|---|---|
| D1 | Login token exposure | Cookie flow with `include_token` | **RESOLVED** | `client.ts` uses Bearer token from `OfficeRuntime.storage` (JWT stored locally, sent per-request). Cookie flow was rejected in favour of explicit Bearer auth. Implementation is complete. |
| D2 | Local PivotTable support | Gate by runtime capability | **Open** | `excelPivotTables.ts` exists with `insertPivotTableWithMapping()` and `useExcel.ts` imports it. Runtime capability check not yet implemented — need `Office.context.requirements.isSetSupported('ExcelApi', '1.7')` gate. |
| D3 | XMLA connection creation | Attempt `connections.add2()` + manual fallback | **Open** | Not yet implemented. Covered by A8.2 (`useExcelConnections.ts`). |
| D4 | Result refresh metadata | Custom properties preferred | **RESOLVED** | `workbookMetadata.ts` implements `setTableMetadata()` / `getTableMetadata()` using named ranges with comment-based key/value pairs. Fully functional. |
| D5 | Chart type selection | Agent metadata + heuristic fallback | **RESOLVED** | `App.tsx` imports `mapAgentChartType` from `excelCharts.ts`. Agent `chart_type` → `recommendChartType()` heuristic fallback. Backend Bug-626 (SSE `chart_type` not emitted) fixed 2026-05-20. |
| D6 | Large result limit | Preview cap + confirmation | **RESOLVED** | `useExcel.ts` has `LARGE_ROW_THRESHOLD = 10000` with `ConfirmGuard` callback pattern. Caller provides the confirmation dialog. |

**Remaining actions:**
- D2: Add runtime check in `useExcel.ts`: `Office.context.requirements.isSetSupported('ExcelApi', '1.7')` before enabling Local PivotTable button
- D3: Already covered by A8.2 implementation

---

## Implementation Sequence

Execute in this order. Each group is independent of later groups.

### Tier 1 — Foundation (2-3 days)

| Step | Category | Items | Files Created/Changed |
|---|---|---|---|
| 1.1 | A1 | Common components (5 files) | `common/SearchBar.tsx`, `common/StatusBadge.tsx`, `common/SectionHeader.tsx`, `common/LoadingSkeleton.tsx`, `common/EmptyState.tsx` |
| 1.2 | A2 | Profile switcher | `ProfileSwitcher/ProfileSwitcher.tsx` |
| 1.3 | A3 | Gateway API client | `api/gateway.ts` |
| 1.4 | B1.1–B1.2 | Storage + API client unit tests | `__tests__/apiClient.test.ts`, `__tests__/storage.test.ts` |
| 1.5 | B4.1 | Security tests (minimum: password/JWT redaction) | `__tests__/security.test.ts` |

### Tier 2 — Refactoring (2 days)

| Step | Category | Items | Files Created/Changed |
|---|---|---|---|
| 2.1 | A4.1 | Extract useAgentConversation hook | `hooks/useAgentConversation.ts`, modify `App.tsx` |
| 2.2 | A4.2 | Extract useSseStream hook | `hooks/useSseStream.ts` |
| 2.3 | B1.3 | Format mapping unit tests | `__tests__/formatMapping.test.ts` |

### Tier 3 — Report Builder Completion (2-3 days)

| Step | Category | Items | Files Created/Changed |
|---|---|---|---|
| 3.1 | A6.1 | MeasureLibrary component | `ReportBuilder/MeasureLibrary.tsx`, modify `ReportBuilder.tsx` |
| 3.2 | A6.2 | DimensionLibrary component | `ReportBuilder/DimensionLibrary.tsx`, modify `ReportBuilder.tsx` |
| 3.3 | A7 | TemplateCard component | `ReportBuilder/TemplateCard.tsx` |
| 3.4 | B1.4–B1.5 | Query builder + search unit tests | `__tests__/queryBuilder.test.ts`, `__tests__/search.test.ts` |
| 3.5 | B2.3–B2.4 | ReportBuilder + TemplatePicker component tests | `__tests__/ReportBuilder.test.tsx`, `__tests__/TemplatePicker.test.tsx` |

### Tier 4 — Connection & CUBE (1-2 days)

| Step | Category | Items | Files Created/Changed |
|---|---|---|---|
| 4.1 | A8.1 | Connection string builder | `utils/connectionStrings.ts` |
| 4.2 | A8.2 | Excel connections hook | `hooks/useExcelConnections.ts` |
| 4.3 | D3 | Connection creation decision (D3) | Modify `hooks/useExcelConnections.ts`. D4 (metadata) already resolved in `workbookMetadata.ts`. |

### Tier 5 — Hardening (2-3 days)

| Step | Category | Items | Files Created/Changed |
|---|---|---|---|
| 5.1 | A9 | Diagnostics panel | `Settings/DiagnosticsPanel.tsx` (verify `utils/diagnostics.ts` is complete) |
| 5.2 | C1 | Error handling + retry | Modify `api/client.ts`, `hooks/useSseStream.ts`, `App.tsx` |
| 5.3 | C3 | Accessibility pass | Modify `App.tsx`, `components/AskTessallite/ChatPanel.tsx`, `components/ReportBuilder/ReportBuilder.tsx`, `theme.ts` |
| 5.4 | C4 | Performance (virtualise, debounce, batch streaming) | **Replace** `VirtualList.tsx` with `react-window`, add `SearchBar.tsx`, modify `App.tsx` streaming. Cache keys already done. |
| 5.5 | C5/D2-D3 | Distribution + remaining open decisions | Verify icon assets, update manifest URLs for prod, add PivotTable capability gate (D2). Connection creation (D3) covered by A8.2. |

### Tier 6 — Remaining Tests (2 days)

| Step | Category | Items |
|---|---|---|
| 6.1 | B2.1–B2.2 | LoginScreen + ChatPanel component tests |
| 6.2 | B3.1–B3.2 | PersonaDropdown + GlossaryModal component tests |
| 6.3 | B4.1 (remaining) | Security tests (state clearing, cache clearing) |

---

## Definition of Done per Tier

### Tier 1
- All 7 new files exist and compile (`tsc --noEmit` clean)
- 18+ new unit tests pass (`npm test` green)
- Common components render correctly in isolation (manual spot-check in dev server)

### Tier 2
- `App.tsx` reduces from ~813 lines to under 500 lines (delegates conversation/SSE to extracted hooks)
- All 27 existing tests still pass (no regressions)
- Agent conversation flow still works end-to-end (manual smoke test)

### Tier 3
- Report Builder renders measures/dimensions via dedicated library components
- Template picker shows cards with field requirements
- 21+ new unit + component tests pass

### Tier 4
- Connection wizard can detect existing connections
- Connection string builder produces valid OLEDB and MSOLAP strings
- D3 (connection creation) attempt-or-fallback logic implemented

### Tier 5
- Diagnostics panel shows event log with redacted secrets
- Error messages are user-readable (not raw HTTP status codes)
- Keyboard navigation works for mode switcher, search, chat input, insert buttons
- Reduced-motion mode disables animations
- Streaming doesn't re-render entire chat on every token
- VirtualList replaced with react-window FixedSizeList
- Icon assets verified, manifest URLs updated for production deployment

### Tier 6
- Total tests: 71+ unit + 30 component + 6 security = 107+ tests (up from 27)
- `tsc --noEmit` zero errors
- `npm test` all green
- `npm run build` produces production bundle with no warnings

---

*End of missing features plan.*
