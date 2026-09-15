# Excel add-in test harness

Harnesses that test the add-in against a REAL Tessallite server, rather than
against mocks. Design, the full capability inventory and the plan for the
remaining harnesses live in
`docs/architecture/architecture_excel-plugin-test-harness.md`.

Implemented today:

| | Harness | Command |
|---|---|---|
| (a) | headless custom-functions runtime, 19 checks | `npm run harness:functions` |
| (b) | pane over the recording shim, jsdom, 1 check | `npm run harness:pane` |
| (b) | pane in headless Chromium via Playwright, 11 checks | `npm run harness:pane:playwright` |
| (d) | test-profile build (no login), used only as a build step for (a) and (b) above | `npm run build:test-profile` |
| (c) | native Excel over COM: cell types, spill, save/reopen, PivotTable aggregation, against the PRODUCTION add-in | `tessallite/tests/excel-xmla`, scenarios `30-addin-*` |

All three harnesses read the SAME `TESS_HARNESS_*` environment described below,
and so does the test-profile build.

## (a) Headless functions runtime — `runFunctions.mjs`

Runs the BUILT `dist/functions.iife.js` inside Node with faithful
`CustomFunctions` and `OfficeRuntime.storage` stubs, signs in against a live
server, and asserts what a workbook cell would actually receive — including the
JavaScript TYPE of every value.

It is not part of `npx vitest run`: the unit suite must stay offline and
deterministic, and this harness deliberately needs a server.

### Run it

```bash
export TESS_HARNESS_TENANT=<tenant slug>
export TESS_HARNESS_EMAIL=<user>
export TESS_HARNESS_PASSWORD=<password>          # environment only, never committed

# Either a real single origin ...
export TESS_HARNESS_SERVER_URL=https://<host>

# ... or, on a bare local docker stack where model-service and query-router are
# separate hosts, let the harness start a shim that reproduces the deployment's
# single-origin routing:
export TESS_HARNESS_MODEL_SERVICE_URL=http://127.0.0.1:8001
export TESS_HARNESS_QUERY_ROUTER_URL=http://<query-router host>:8000

npm run harness:functions          # builds the bundle, then runs the checks
npm run harness:functions:run      # runs against the existing dist/ bundle
```

`TESS_HARNESS_BUNDLE` overrides which bundle is loaded (default
`../dist/functions.iife.js`).

On a local docker stack the query-router's container IP CHANGES when the stack
restarts. Read it fresh rather than reusing a remembered value:

```bash
docker inspect infra-query-router-1 \
  --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'
```

Fixture names — project, model, measures, dimension member, KPI, named set —
are in `harness.config.json`. They are names, not ids: the harness resolves them
against the live server at start-up, so a reseed does not break it. Credentials
are never read from that file.

### What it checks

| Check | What would break without it |
|---|---|
| every `functions.json` id is registered | a function silently missing from the bundle -> `#NAME?` in every workbook |
| `VALUE` returns a JS number | Bug-9876: measures arrive as numeric strings and Excel stores TEXT |
| second measure also a number | the same defect on a differently-serialised measure (`"1.0E+5"`) |
| `MEMBERVALUE` with a filter | a dropped filter returning the grand total into a member cell |
| `VALUE` with a filter pair == `MEMBERVALUE` | the two entry points disagreeing |
| `KPI` value/goal/status types + status in {1,0,-1} | a KPI property arriving as text, or a status outside its documented domain |
| `KPIVALUE`/`KPIGOAL`/`KPISTATUS` agree with `KPI` by name | the id and name paths diverging |
| `LISTBYID` spills a column of strings | a spilled array of the wrong shape, or an `#ERROR` cell served as data |
| wrong model fails closed with `invalidValue` | a formula naming model A served from model B — wrong numbers |
| wrong model issues no request | the guard running after the query instead of before it |
| `DIAG` shape | the field the support path depends on disappearing |
| two `VALUE` calls -> ONE `/plugin/execute` | the batcher regressing to one request per cell |
| two members -> ONE request, own numbers | fan-out delivering one member's number to another's cell |
| cache-generation bump forces a refetch | stale values surviving a persona/profile switch |
| signed out fails closed, no request | an unauthenticated query leaving the client |
| row-security deny-all raises, and the CELL shows why (Bug-8453 / Bug-9880) | a fabricated `0` in a spreadsheet, or an unexplained `#VALUE!` |
| a stalled request settles at the 30s ceiling (Bug-9749) | a cell stuck at `#GETTING_DATA` forever |
| every metadata read carries `deployed_only=true`, and `persona_id` when a persona is active | the runtime evaluating against LIVE editor state, or returning objects a persona cannot see |
| a cancelled `LISTBYID` delivers no result and caches nothing | Excel writing into a cell the user has moved on from |

The deny-all and request-ceiling checks INJECT A SERVER STATE through the origin
shim (`stall`, `deny-all`), because neither can be produced from the demo seed —
there is no deny-all persona on it. Only the server state is injected; the
add-in under test is the real built bundle issuing real requests.

The `deployed_only` check carries ONE named exception,
`DEPLOYED_ONLY_KNOWN_GAPS` in `checks/runtimeGuards.mjs`, pointing at Bug-9881
(the KPI evaluate routes omit the flag in every client). Any OTHER route that
drops it still fails the check, and closing Bug-9881 means deleting that entry.

### Proving the harness has teeth

Point it at the last build BEFORE the Bug-9876 fix:

```bash
./tests-harness/build-reference-bundle.sh d6e81e759^ /tmp/pre-9876
TESS_HARNESS_BUNDLE=/tmp/pre-9876/tessallite/excel-plugin/dist/functions.iife.js \
  npm run harness:functions:run
```

Six checks fail, each reporting the exact text a cell would have held
(`"249318056.79"`, `"1.0E+5"`). The current bundle passes all nineteen.


## (b) Pane harnesses

Both drive the REAL pane code against the REAL server, with
`lib/excelShim.ts` — a workbook MUTATION MODEL — standing in for Excel, and
assert the workbook that resulted, including the JS TYPE of every written value.

### jsdom — `npm run harness:pane`

`pane/*.harness.ts` under Vitest. Calls the pane's modules directly
(`api/queryRouter`, `utils/measureValues`, `hooks/useExcel`). Fast, and the
right home for a flow whose UI is not the interesting part.

### Playwright — `npm run harness:pane:playwright`

`pane-e2e/*.spec.ts` in headless Chromium. Serves the BUILT test-profile bundle,
injects the shim before the app loads, and clicks the real MUI controls by their
ACCESSIBLE NAMES. Eleven checks:

| Check | What would break without it |
|---|---|
| loads signed in under the test profile | the whole no-login premise of harness (c) |
| the PRESET model is selected, not the project's first | a harness silently testing the wrong model |
| local PivotTable: hidden sheet, `_tsl_data_*` table, number-typed measure cells, text-typed member cells, field axes, provenance name | Bug-9876, one level up from the functions runtime |
| local PivotTable refuses a non-standard measure, with a visible reason | Excel re-aggregating a value it must not |
| ... refuses a time-variant measure | Bug-9882: Excel summing a CAGR down a column |
| ... refuses a semi-additive measure | a balance summed across time |
| ... refuses a non-additive-aggregation measure | an average of averages |
| every refusal writes NOTHING to the workbook | a half-inserted pivot the user has no reason to distrust |
| single measure, LIVE mode: a `TESSALLITE.VALUE` formula by model SLUG and TECHNICAL name, and no value | a display name in a formula renders `#VALUE!` |
| single measure, STATIC mode: a NUMBER through the values channel, nothing through the formula channel | Bug-7393 formula injection; Bug-9876 text-typed numbers |
| named set: a `CUBESET`/`CUBERANKEDMEMBER` block, every cell a formula | a member frozen as a literal stops following the set |
| KPI scorecard: `TESSALLITE.KPI` formulas, no CUBE formula, a text name column, a three-icon format WITH criteria | Bug-6903 (`#NAME?` without a connection); an icon set with no criteria is a no-op |
| Report Builder table: numeric measure column plus a styled provenance footer | wrong numbers, and data that cannot say where it came from |

The four blocked measure classes are resolved from the LIVE model metadata by
`pane-e2e/fixtures.mjs`, which FAILS if the model no longer contains an example
of a class. A reseed cannot quietly turn a "variant" fixture into an ordinary
sum measure and leave the refusal check passing for nothing.

Ports are fixed (`TESS_HARNESS_ORIGIN_PORT`, `TESS_HARNESS_PANE_PORT`) because
the origin is BAKED INTO the test-profile bundle at build time, so the build
step and the servers the run starts have to agree without passing anything
between processes.

**Growing the shim.** Grow it strictly. Every addition made so far fixed a check
that was failing for a REAL reason — `load()` not accepting an array threw
inside `Excel.run` and the pane swallowed it as a silent no-op; a `B2:B2`
address and a needlessly quoted sheet name both broke the pane's own
table-metadata lookup. A lenient shim stops catching the defect class it exists
for.

## (d) The test-profile build

A copy of the add-in with the login removed. `VITE_TESSALLITE_TEST_PROFILE=1`
bakes a preset profile — server URL, tenant, credentials, project, model, all
from the `TESS_HARNESS_*` variables above — signs the pane in before React
mounts, and seeds the storage keys the custom-functions runtime shares. A
workbook therefore calculates with no pane interaction at all.

```bash
export TESS_HARNESS_SERVER_URL=https://<host>       # or the local shim's URL
export TESS_HARNESS_TENANT=<tenant>
export TESS_HARNESS_EMAIL=<user>
export TESS_HARNESS_PASSWORD=<password>             # environment only

npm run build:test-profile                          # -> dist-test-profile/
```

`VITE_OUT_DIR` sends the bundle somewhere other than `dist/`; the Playwright
harness uses `dist-test-profile/` so a bundle carrying credentials can never
end up in the `dist/` a deploy picks up. BOTH Vite configs honour it — the pane
and the custom-functions IIFE — so the served origin is self-contained. Until
Bug-9889 the functions config ignored it and left `functions.iife.js` in
`dist/`, which meant the manifest's `<Script>` URL 404'd and every TESSALLITE
function on the host showed `#NAME?`;
`src/__tests__/buildOutputDirectory.test.ts` now pins both configs.

This build is a build STEP only, for the headless functions harness (a) and the
Playwright pane harness (b) above. It is never sideloaded into native Excel:
`30-addin-*` (harness (c)) runs against the PRODUCTION add-in instead, in attach
mode against an Excel signed in normally — see
`tessallite/tests/excel-xmla/README.md`, "Attach mode".

### It cannot be built from a release target

`scripts/testProfileGuard.mjs` throws when the flag meets a release marker
(`TESSALLITE_RELEASE`, `TESSALLITE_RELEASE_CHANNEL`, `TESSALLITE_RELEASE_VERSION`,
`TESSALLITE_EDITION`) or a non-local `PLUGIN_BASE_URL`, and FAILS CLOSED on a
base URL it cannot parse. `vite.config.ts` and the CI job both call that one
rule, and `src/__tests__/testProfileGuard.test.ts` pins it.

The build also stamps a marker: `TESSALLITE TEST BUILD - NOT FOR RELEASE` in the
bundle and a `TEST BUILD` chip in the pane header. An ordinary `npm run build`
contains neither (the branch is a constant `false` and the bundler removes it).

Every build — including this one — publishes the single production
custom-function namespace, `TESSALLITE`. A separate native "TEST BUILD" of the
add-in with its own manifest, its own origin and its own `TESSALLITETEST`
namespace existed briefly (Bug-9879) and was retired: it was five ways
different from the product (own origin, baked credentials, a same-origin `/api`
proxy, its own namespace, a Developer sideload), so a pass on it proved nothing
about the product. `30-addin-*` now runs against the real add-in instead.
