# Tessallite Excel Plugin

Office Add-in for Excel that integrates with the Tessallite semantic layer platform.

## Requirements

- Node.js 18+
- npm 9+

## Setup

```bash
npm install
```

## Development

```bash
npm run dev        # Start Vite dev server on port 3001
npm run build      # TypeScript check + production build
npm run preview    # Preview production build on HTTPS port 3443
npm test           # Run unit tests (offline, mocked network)
npm run test:watch # Run tests in watch mode
```

### Live harnesses (need a running Tessallite server)

```bash
npm run harness:functions        # the BUILT functions bundle, headless in Node, against a real server
npm run harness:pane             # real pane code over a recording Office.js shim (jsdom)
npm run harness:pane:playwright  # the real pane in headless Chromium, driven by its accessible names
npm run harness:typecheck        # type-check the harness sources
```

### The test-profile build (no login)

```bash
npm run build:test-profile   # VITE_TESSALLITE_TEST_PROFILE=1: a bundle that signs ITSELF in
```

A copy of the add-in with the login removed, used only as a build step for the
headless functions harness and the Playwright pane harness above -- it is never
sideloaded into native Excel. It bakes a preset profile (server URL, tenant,
credentials, project, model) from the same `TESS_HARNESS_*` environment the
harnesses use, and stamps a visible `TEST BUILD` marker in the pane header and
in the bundle.

It CANNOT be produced from a release target: `scripts/testProfileGuard.mjs`
refuses the build when the flag meets a release marker or a non-local
`PLUGIN_BASE_URL`, fails closed on a base URL it cannot parse, and is called by
`vite.config.ts` and CI alike. An ordinary `npm run build` contains no marker
and no credentials.

The native-Excel harness (`tessallite/tests/excel-xmla`) runs in attach mode
against the PRODUCTION add-in instead; see that harness's README.

These are deliberately excluded from `npm test`, which must stay offline and
deterministic. They exist because the unit suite mocks the network and therefore
cannot see a defect that lives in the SHAPE of the real server response — which
is what Bug-9876 was. Setup, environment variables and the full check list are in
`tests-harness/README.md`; the design and the capability inventory are in
`docs/architecture/architecture_excel-plugin-test-harness.md`.

### Sideloading in Excel

The local shared-folder catalogue should point at a folder that contains a valid `manifest.xml`, for example:

```text
\\SERVER\Share\TessalliteExcelAddin
```

That folder contains a local `manifest.xml` for the task pane. It points Excel at:

```text
https://localhost:3443/excel-plugin/index.html
```

1. Start or confirm the local HTTPS add-in host on port `3443`.
2. In Excel, open **File > Options > Trust Center > Trust Center Settings > Trusted Add-in Catalogs**.
3. Paste the shared-folder catalogue path into **Catalog URL** and click **Add catalog**.
4. Tick **Show in Menu** for the catalogue, click **OK**, then restart Excel.
5. Open the blue **Add-ins** ribbon button, usually shown with the tooltip **Insert Add-ins**.
6. Open the **Shared Folder** tab and click **Refresh**.
7. Select the Tessallite tile, add it, then open the Tessallite ribbon button from **Home**, **Insert**, or **Developer**.

### Custom functions on perpetual Office against a localhost server (Bug-6905)

Verified working on Office 2019 Home & Student Build 16.0.20131. Three
machine-level prerequisites apply when the server URL is `https://localhost`;
without them the task pane works but every `TESSALLITE.*` formula returns
`#VALUE!` after a few seconds.

The custom functions runtime executes in a WWAHost AppContainer sandbox
(separate from the task pane's WebView2), which blocks loopback networking and
only trusts the machine certificate store. One-time setup, elevated prompt:

```text
CheckNetIsolation LoopbackExempt -a -n="microsoft.win32webviewhost_cw5n1h2txyewy"
CheckNetIsolation LoopbackExempt -a -p=<SID>
certutil -addstore Root %USERPROFILE%\.office-addin-dev-certs\ca.crt
```

`<SID>` is the per-origin AppContainer created for the add-in, found under
`HKCU\Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppContainer\Mappings`
with DisplayName `1_https___localhost_3443`. It only exists after the add-in
has run at least once. Remote (non-localhost) servers need only a trusted
certificate; the loopback exemptions do not apply.

Troubleshooting on perpetual Office:

- `#NAME?` for all functions: the custom functions runtime did not register
  this session. Re-insert the add-in via **Insert > My Add-ins > Shared
  Folder**; a plain Excel restart does not always start it.
- Add-in part-loads (functions work but no ribbon icon, or vice versa) after
  repeated add/remove cycles: close Excel, delete the contents of
  `%LOCALAPPDATA%\Microsoft\Office\16.0\Wef`, reopen, re-insert. This also
  clears `OfficeRuntime.storage`, so sign in again through the pane.
- `=TESSALLITE.DIAG()` reports the runtime state (storage, model context,
  server URL, connectivity) directly in a cell for support diagnosis.

The manifest must keep `VersionOverridesV1_0` with the custom functions
Script URL pointing at the classic-script `functions.iife.js` bundle:
perpetual Office ignores a V1_1 CustomFunctions extension point for
sideloaded add-ins, and an ES module crashes the JS-only runtime.
`manifestCustomFunctions.test.ts` pins this structure.
That guard recursively scans every Vitest/Vite JavaScript and TypeScript source
extension, including `.js`, `.jsx`, `.mts`, and `.cts`, when checking raw imports.

## Architecture

The task pane has three tabs: **Analyse** (Report Builder), **KPIs**, and **Ask** (conversational agent).

The compact pane keeps its header, tabs and connection/persona footer visible. Click the project/model title to select the project, model or persona. The brand mark opens plugin information; the gear menu contains profile switching, Diagnostics and sign-out. Analyse uses compact zone rows and an icon toolbar; hover or focus an icon for its action label. Search, Certified and sorting controls sit above the field sections. KPI status pills filter the list. In Ask, the conversation selector and composer stay fixed while the message log scrolls.

The default Excel insert paths are connectionless:

- Ask chart inserts classify columns using the turn's citations and convert numeric measure strings before insertion. Numeric-looking dimension identifiers retain their text. Without citations, chart classification checks the actual values, including numeric strings.
- When citations are present, returned columns not cited as measures are categories. Without citations, decimal numeric strings remain measure series even with display zeros; identifier-headed or fixed-width integer text such as year and postal-code values remains categories, and an all-numeric result uses only its first column as the category axis.
- Single-value measure and KPI inserts use `TESSALLITE.VALUE`, `TESSALLITE.KPI`, and `TESSALLITE.MEMBERVALUE` custom functions that run through the add-in session.
- Local PivotTable inserts query Tessallite through the plugin API, write a flat grouped result to a hidden backing worksheet, create a namespaced `_tsl_data_*` Excel Table, and build a native range-backed PivotTable over it using the Report Builder Rows, Columns, Filters, and Values zones.
- Local PivotTable inserts fail closed when Excel cannot resolve any requested row, column, value, or filter field. The add-in aborts the insert, names the unresolved fields by zone, and removes the created backing sheet/table instead of reporting success for an incomplete layout.
- Local PivotTable inserts are limited to additive standard measures. Calculated, variant, semi-additive, and non-additive measures should be inserted as a table or connection-backed CUBE formula so Excel does not freely re-aggregate them.
- CUBE formulas and the workbook-level `Tessallite` OLAP connection remain available as the advanced Analysis Services path.

`TESSALLITE.*` formulas fail closed when their model argument does not match the active model stored by the task pane, preventing a workbook from recalculating against the wrong model after a model switch.

- `src/App.tsx` — Root application shell (3-tab switcher, header, footer)
- `src/api/` — API clients (auth, agent service, model service, query router)
- `src/components/ReportBuilder/` — Report Builder panel (measures, dimensions, query execution)
- `src/components/KpiPanel/` — KPI scorecard panel (batch evaluation, insert table/chart)
- `src/components/AskTessallite/` — Chat panel, message bubbles, insert actions
- `src/components/LoginScreen/` — Login form
- `src/components/Toast/` — Toast notification system
- `src/hooks/` — React hooks (useAuth, useExcel, useModel)
- `src/utils/` — Storage abstraction, Excel formulas, Office.js spike, metadata persistence
- `src/types/` — TypeScript type definitions

## Refresh and Cache Invalidation

The Report Builder toolbar's Refresh menu provides two refresh actions:

- **Refresh** — clears the custom functions runtime caches (KPI eval, KPI list,
  named-set preview) and triggers a workbook full recalculation. This ensures
  `TESSALLITE.VALUE`, `TESSALLITE.KPI`, and `TESSALLITE.MEMBERVALUE` formulas
  fetch fresh values immediately instead of serving up-to-60 s stale data from
  the TTL caches. Profile switch, persona switch, and logout also trigger this
  invalidation automatically (Bug-6912).
- **Refresh sheet data** — re-executes every Tessallite-inserted table on the
  active sheet using its stored semantic query, under the current session and
  persona. Tables whose stored project/model do not match the active session
  are skipped (fail-closed: never silently re-pointed at another model).
  Tables whose returned column set differs from the original insert are also
  skipped (schema drift protection). After rewriting, any local PivotTables
  backed by those tables are refreshed. Every skip or warning carries a
  per-table reason, surfaced in the UI behind **Show details** under the
  buttons.

## Cell-write concurrency (Bug-7397)

Two Tessallite operations must never mutate the same worksheet cells at the
same time -- that is how a table ends up showing one query's numbers under
another query's provenance. Exclusion is spatial and holds by construction:

- The sheet grid is partitioned into fixed 64x64 cell **blocks**
  (`workbookMetadata.cellBlockKeys`). A lock key names one block, so two
  operations whose rectangles overlap NECESSARILY share a key. There is no
  signal to publish and re-read, and therefore no sampling-instant race.
- **Every** first-party cell write participates. The table insert and the table
  refresh acquire the blocks covering the cells they touch; every other writer
  (formula, literal, named set, the KPI writers, the scorecard) goes through
  `utils/lockedCellWrite.withPinnedCellWrite`, which resolves the target from
  ONE host sample, derives the blocks from the rectangle it is about to write,
  and runs the write against those pinned coordinates. A writer that opened its
  own `Excel.run` would be outside the contract, which is exactly the gap this
  helper exists to close -- add new cell writers through it.
  Chart and local-pivot inserts are the deliberate exception: they write into a
  worksheet they create in the same operation, which nothing else can be
  touching.
- A refresh's footprint includes the table's **provenance footer row** at both
  its current and post-resize position, so growing or shrinking a table moves
  the footer atomically instead of eating or orphaning it.
- A refresh that must GROW probes the rows it would newly claim first. If they
  hold anything other than our own footer, the table is left completely
  untouched and skipped with a reason (Bug-8340) -- a refresh never destroys
  user content to make room for itself.
- Participation is machine-checked, not merely documented.
  `src/__tests__/cellWriteContract.test.ts` builds a real TypeScript Program and
  works on the TYPED AST. An Office mutation is an assignment to
  `.values`/`.formulas`/`.hyperlink`/... (property OR static bracket access, any
  assignment operator), a call to a range-mutating method, or a `Reflect.set` /
  `Object.defineProperty` / `Object.assign` onto a range -- identified by the
  RECEIVER'S TYPE, unwrapping casts so `(range as unknown as X)` cannot erase
  it, while a `Set.add` or `Map.clear` is excluded by type. On a cell-owning
  type (`Range`/`Table`/`Worksheet`/row+column collections) the default is
  fail-closed: a method in neither the mutating nor the read-only set is
  reported, so an office-js upgrade adding a new mutating API fails the test
  instead of shipping silently.
  Lock scope is the BODY of the callback passed to
  `withPinnedCellWrite`/`withTableLocksKeys`, with the helper resolved by SYMBOL
  (a local shadow does not open a span) and a closure that escapes the callback
  not counted as locked. Anything outside every lock body must be on an
  allowlist keyed by file + ENCLOSING FUNCTION + statement, with occurrences
  counted, so one site's justification cannot cover another. `Excel.run` is
  restricted to an approved module list through any spelling of the namespace
  (`const host = Excel`, `globalThis.Excel`, `(Excel as any)`), and the scanned
  file-set is asserted TOTAL against an independent sweep so no directory or
  file extension can quietly fall outside it.
  What it CANNOT do is prove a lock is HELD at runtime -- that is what the
  per-writer deferral tests in `lockedCellWrite.test.tsx` do. The detector is
  pinned by negative fixtures reproducing every write shape that escaped a
  previous review round (five rounds' worth), with the type-sensitive ones run
  against a REAL typed program rather than the no-lib fixture harness.
- Waiting for a block is bounded (`DEFAULT_LOCK_ACQUIRE_TIMEOUT_MS`). The
  deadline bounds only the WAIT, never the critical section: interrupting a
  holder would admit a second writer into cells the first may still be
  mutating. On expiry the waiter abandons its work entirely and the user is told
  the cells are busy -- an insert reports `blocked`, a refresh reports an honest
  skip with a retry hint.

Cross-runtime invalidation: when the custom functions runtime runs in a
separate JS context (perpetual Office WWAHost AppContainer), pane actions
cannot directly clear the runtime's in-memory Maps. A cache generation token
in `OfficeRuntime.storage` bridges the gap: the pane bumps the token on
Refresh/switch/logout; the functions runtime reads it on every evaluation
via `requireFullContext()` and clears its caches when the token changes.

## Insert Mode

A global insert-mode setting (persisted in `OfficeRuntime.storage`) controls
whether single-value inserts write a live `TESSALLITE.*` formula or a static
number:

- **Live** (default) — inserts `=TESSALLITE.VALUE(...)` or `=TESSALLITE.KPI(...)`
  formulas that refresh on recalculation.
- **Static** — fetches the current value once through the governed
  plugin-execute endpoint and writes it as a literal cell value. The value
  does not update automatically. When the server returns null (no data for
  that measure/KPI), the cell receives `#N/A` — matching the live formula's
  `#N/A No data for measure "..."` behaviour so the user sees the same
  signal regardless of mode.

The **Live** checkbox is in the Report Builder toolbar: checked means live formulas;
unchecked means static values. Existing explicit "Insert as CUBE formulas" actions always insert
formulas regardless of the mode — the insert-mode setting only applies to
the default single-value insert paths.

## Answer pop-out

Any answer with rows shows a **Pop out** action, which opens it in an Office
dialog window sized to the screen. The shared chat UI's own maximise control can
only ever fill the task pane — Office exposes no API to widen a task pane — so
the dialog is the only way to get a result big enough to present from. In the
task pane the Visual panel's maximise is routed to the same window; the web app
keeps the in-pane overlay, where a browser window is available to grow into.

The payload carries `kind: "chart" | "table"`: a turn that renders a chart pops
out as a chart, anything else as its table. `buildPopoutPayload()` in
`utils/chartPopout.ts` assembles it, so both entry points always agree.

A dialog cannot read add-in storage or call the API; Office allows it only
`messageParent`/`addHandlerAsync`. So `chart-dialog.html` announces itself once it
is listening and the task pane posts the payload back over that channel. Nothing
is persisted and no data goes in the URL.

This needs DialogApi 1.2 (for `messageChild`), which is not declared in the
manifest on purpose — a manifest requirement would stop the whole add-in loading
on a host that lacks it. `isChartPopoutSupported()` feature-detects instead, and
the action is hidden when the host cannot deliver the payload.

### Save, Copy and Close

The pop-out window carries a thin toolbar. **Save** is a menu whose contents
follow the kind — table: Excel, CSV, TSV; chart: SVG, PNG — and every table
export carries its column headers. **Copy** writes a table to the clipboard as
both `text/html` (a `<table>`, so Excel pastes a grid with headers) and
`text/plain` TSV, and a chart as `image/png`. **Close** asks the task pane to
close the dialog, which is how Office closes one.

These live in the pop-out and not in the pane because the pop-out is a real
browser window: `<a download>` and image clipboard writes are unreliable inside
the task-pane iframe. Every clipboard and download call reports its outcome in
the window, so a refusal is visible rather than a click that did nothing.

**Save as Excel adds no dependency.** `utils/xlsxWriter.ts` builds a
single-sheet .xlsx in the browser — a STORE-only ZIP (hand-computed CRC-32, no
DEFLATE) of the five required OOXML parts, with text cells as inline strings so
there is no shared-strings part and numbers written bare so Excel stores them as
numbers. The dialog posts the base64 to the task pane, which calls
`Excel.createWorkbook` (ExcelApi 1.8): the result opens as a new workbook in the
user's own Excel with no download and no file dialog, and they keep it with
Excel's own Save. Hosts below ExcelApi 1.8, and results past a conservative
message size, download the same bytes instead. The pane posts the outcome back
so a failed open is visible in the window.

**PNG is rasterised, not exported.** Charts use the ECharts SVG renderer, whose
toolbox cannot emit PNG. `utils/popoutExport.ts` serialises the live `<svg>`,
loads it as an image and draws it onto a canvas at `devicePixelRatio`; the same
blob serves Copy and Save as PNG.

## Storage

All auth data uses `OfficeRuntime.storage` for secure, sandboxed persistence. Passwords are never stored.

## Non-Excel BI Clients

Looker Studio/Data Studio direct does not run through the Office.js add-in or
Excel/XMLA surface. It uses the Tessallite PostgreSQL wire gateway without
LookML. A customer-supplied Looker instance can optionally consume a generated
LookML adapter. See
`docs/architecture/looker-and-looker-cloud-core-client-specs.md` and the
integration help pages under `../help/integrations/`.

## Design Tokens

Tokens in `src/theme.ts` mirror the main Tessallite frontend (`frontend/src/theme/tokens.ts`). Keep them in sync.

## Testing

Three tiers, with one home per behaviour:

- `npm test` — the offline unit suite. Mocks the network, so it cannot see a
  defect that lives in the SHAPE of a real server response.
- the harnesses above — real client, real server, nothing mocked between the
  add-in's own code and the network. They assert the JS TYPE of every value that
  reaches a cell.
- native Excel on a Windows host — the only place real recalculation, real
  dynamic-array spill and real PivotTable aggregation can be observed. Designed,
  not built; see the architecture document.

CI runs the unit suite, the type checks and the build on every push, and the
harnesses whenever a Tessallite server is configured through the
`TESS_HARNESS_*` secrets.
