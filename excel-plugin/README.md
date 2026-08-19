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
npm test           # Run unit tests
npm run test:watch # Run tests in watch mode
```

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

## Architecture

The task pane has three tabs: **Analyse** (Report Builder), **KPIs**, and **Ask** (conversational agent).

The default Excel insert paths are connectionless:

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

The Report Builder header provides two refresh actions:

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

The toggle is visible as a Live/Static chip row in the Report Builder
footer. Existing explicit "Insert as CUBE formulas" actions always insert
formulas regardless of the mode — the insert-mode setting only applies to
the default single-value insert paths.

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

Unit tests cover utility functions (formula generation, storage). Excel integration tests require a running Excel host.
