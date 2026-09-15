#!/usr/bin/env node
/**
 * Headless functions-runtime harness (deliverable (a)).
 *
 * Runs the BUILT `functions.iife.js` in Node against a REAL Tessallite server,
 * with faithful `CustomFunctions` / `OfficeRuntime.storage` stubs, and asserts
 * what a workbook cell would actually receive — including the JavaScript TYPE
 * of every value.
 *
 * Usage and environment: see `tests-harness/README.md`.
 * Design and the full capability inventory: see
 * `docs/architecture/architecture_excel-plugin-test-harness.md`.
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { installOfficeStubs, makeStreamingInvocation, FetchRecorder, ErrorCode } from './lib/officeStubs.mjs';
import { startOriginShim } from './lib/originShim.mjs';
import { login, resolveContext, seedStorage, rawExecute, STORAGE_KEYS } from './lib/session.mjs';
import { loadBundle } from './lib/loadBundle.mjs';
import { runRuntimeGuardChecks } from './checks/runtimeGuards.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
// Every build -- including the test-profile build this harness loads --
// publishes the one production namespace.
const NS = 'TESSALLITE';

// ---------------------------------------------------------------------------
// Tiny assertion runner (no test framework: this harness needs a live server
// and must stay out of `npx vitest run`, which is the offline unit suite).
// ---------------------------------------------------------------------------

const results = [];

async function check(name, fn) {
  const started = Date.now();
  try {
    const note = await fn();
    results.push({ name, ok: true, note, ms: Date.now() - started });
    console.log(`  PASS  ${name}${note ? ` — ${note}` : ''}`);
  } catch (e) {
    results.push({ name, ok: false, error: e, ms: Date.now() - started });
    console.log(`  FAIL  ${name}\n        ${e && e.stack ? e.stack.split('\n').slice(0, 3).join('\n        ') : e}`);
  }
}

/**
 * A check that CANNOT run here. Reported separately and never counted as a
 * pass, so a missing fixture can never be mistaken for coverage.
 */
const skipped = [];
function skip(name, reason) {
  skipped.push({ name, reason });
  console.log(`  SKIP  ${name}\n        ${reason}`);
}

function assert(cond, message) {
  if (!cond) throw new Error(message);
}

function assertType(value, expected, label) {
  assert(
    typeof value === expected,
    `${label}: expected a JS ${expected}, got ${typeof value} (${JSON.stringify(value)?.slice(0, 120)})`,
  );
}

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

function env(name, fallback) {
  const v = process.env[name];
  if (v === undefined || v === '') {
    if (fallback === undefined) {
      throw new Error(`Environment variable ${name} is required. See tests-harness/README.md.`);
    }
    return fallback;
  }
  return v;
}

const config = JSON.parse(readFileSync(resolve(HERE, 'harness.config.json'), 'utf8'));
const bundlePath = resolve(HERE, env('TESS_HARNESS_BUNDLE', '../dist/functions.iife.js'));

// ---------------------------------------------------------------------------

async function main() {
  console.log(`Tessallite Excel custom-functions harness`);
  console.log(`  bundle: ${bundlePath}`);

  // 1. Resolve the single origin the add-in will use.
  let shim = null;
  let serverUrl = process.env.TESS_HARNESS_SERVER_URL;
  if (!serverUrl) {
    shim = await startOriginShim({
      modelServiceUrl: env('TESS_HARNESS_MODEL_SERVICE_URL', 'http://127.0.0.1:8001'),
      queryRouterUrl: env('TESS_HARNESS_QUERY_ROUTER_URL'),
    });
    serverUrl = shim.url;
    console.log(`  origin: ${serverUrl} (shim over model-service + query-router)`);
  } else {
    console.log(`  origin: ${serverUrl}`);
  }

  const tenant = env('TESS_HARNESS_TENANT');
  const email = env('TESS_HARNESS_EMAIL');
  const password = env('TESS_HARNESS_PASSWORD');

  // Scoped to the add-in's own origin: the shim's upstream calls must not be
  // counted, or every request would appear twice and the batching assertions
  // would pass or fail for the wrong reason.
  const recorder = new FetchRecorder(serverUrl).install();

  try {
    // 2. Sign in and resolve fixtures BEFORE the bundle is loaded, so the
    //    runtime starts from exactly the state a signed-in pane leaves behind.
    const token = await login({ serverUrl, tenant, email, password });
    const ctx = await resolveContext({ serverUrl, token, config });
    console.log(`  model:  ${ctx.model.slug ?? ctx.model.name} (${ctx.model.id})`);

    const { registry, storage } = installOfficeStubs();
    await seedStorage(storage, { serverUrl, token, tenant, email, project: ctx.project, model: ctx.model });

    // 3. Load the built bundle. Registration happens on load.
    const mod = loadBundle(bundlePath);

    const expectedIds = ['VALUE', 'KPI', 'MEMBERVALUE', 'LISTBYID', 'KPIVALUE', 'KPIGOAL', 'KPISTATUS', 'DIAG'];
    const fn = id => {
      const f = registry.get(id);
      if (!f) throw new Error(`Function ${id} is not registered by the bundle.`);
      return f;
    };

    const modelRef = ctx.model.slug ?? ctx.model.name;
    const wrongModelRef = ctx.wrongModel.slug ?? ctx.wrongModel.name;
    const M1 = config.measures.primary;
    const M2 = config.measures.secondary;
    const DIM = config.dimension.column;
    const MEMBER = config.dimension.member;

    console.log('\nChecks:');

    // -- registration -----------------------------------------------------
    await check('every functions.json id is registered by the bundle', async () => {
      const manifest = JSON.parse(readFileSync(resolve(HERE, '../public/functions.json'), 'utf8'));
      const declared = manifest.functions.map(f => f.id).sort();
      const registered = [...registry.keys()].sort();
      assert(
        JSON.stringify(declared) === JSON.stringify(registered),
        `declared ${declared.join(',')} vs registered ${registered.join(',')}`,
      );
      assert(
        JSON.stringify(declared) === JSON.stringify([...expectedIds].sort()),
        `functions.json changed: ${declared.join(',')}`,
      );
      return `${registered.length} functions`;
    });

    // -- Bug-9876: VALUE must hand Excel a NUMBER ---------------------------
    await check('VALUE returns a JS number (Bug-9876)', async () => {
      const raw = await rawExecute({
        serverUrl, token, projectId: ctx.project.id, modelId: ctx.model.id, measures: [M1],
      });
      const serverValue = raw.data[0][M1];
      const cell = await fn('VALUE')(modelRef, M1);
      assertType(cell, 'number', `${NS}.VALUE("${modelRef}","${M1}")`);
      assert(Number.isFinite(cell), `value is not finite: ${cell}`);
      assert(
        Math.abs(cell - Number(serverValue)) < 1e-6,
        `cell ${cell} does not equal server value ${serverValue}`,
      );
      return `server sent ${typeof serverValue} ${JSON.stringify(serverValue)}, cell got number ${cell}`;
    });

    await check('a second measure in the same model also arrives as a number', async () => {
      const cell = await fn('VALUE')(modelRef, M2);
      assertType(cell, 'number', `${NS}.VALUE("${modelRef}","${M2}")`);
      return `${M2} = ${cell}`;
    });

    // -- MEMBERVALUE with a filter -----------------------------------------
    await check('MEMBERVALUE with a filter returns that member\'s number', async () => {
      const raw = await rawExecute({
        serverUrl, token, projectId: ctx.project.id, modelId: ctx.model.id,
        measures: [M1], dimensions: [DIM],
      });
      const row = raw.data.find(r => String(r[DIM]) === MEMBER);
      assert(row, `member "${MEMBER}" not present in ${DIM} rows`);
      const cell = await fn('MEMBERVALUE')(modelRef, M1, DIM, MEMBER);
      assertType(cell, 'number', `${NS}.MEMBERVALUE`);
      assert(
        Math.abs(cell - Number(row[M1])) < 1e-6,
        `cell ${cell} != member row ${row[M1]}`,
      );
      // A wrong-number guard: the member figure must NOT be the grand total.
      const total = (await rawExecute({
        serverUrl, token, projectId: ctx.project.id, modelId: ctx.model.id, measures: [M1],
      })).data[0][M1];
      assert(Math.abs(cell - Number(total)) > 1e-6, 'member value equals the unfiltered total — the filter was dropped');
      return `${DIM}="${MEMBER}" -> ${cell}`;
    });

    await check('VALUE with an explicit filter pair matches MEMBERVALUE', async () => {
      const viaFilter = await fn('VALUE')(modelRef, M1, DIM, MEMBER);
      const viaMember = await fn('MEMBERVALUE')(modelRef, M1, DIM, MEMBER);
      assertType(viaFilter, 'number', `${NS}.VALUE with filter`);
      assert(viaFilter === viaMember, `${viaFilter} != ${viaMember}`);
      return String(viaFilter);
    });

    // -- KPI value / goal / status -----------------------------------------
    await check('KPI value/goal/status return numbers of the documented shape', async () => {
      const value = await fn('KPI')(modelRef, config.kpi, 'value');
      const goal = await fn('KPI')(modelRef, config.kpi, 'goal');
      const status = await fn('KPI')(modelRef, config.kpi, 'status');
      assertType(value, 'number', 'KPI value');
      assertType(goal, 'number', 'KPI goal');
      assertType(status, 'number', 'KPI status');
      assert([1, 0, -1].includes(status), `status must be 1/0/-1 per functions.json, got ${status}`);
      return `value=${value} goal=${goal} status=${status}`;
    });

    await check('KPIVALUE / KPIGOAL / KPISTATUS by id agree with KPI by name', async () => {
      const [v, g, s] = await Promise.all([
        fn('KPIVALUE')(ctx.kpi.id),
        fn('KPIGOAL')(ctx.kpi.id),
        fn('KPISTATUS')(ctx.kpi.id),
      ]);
      assertType(v, 'number', 'KPIVALUE');
      assertType(g, 'number', 'KPIGOAL');
      assertType(s, 'number', 'KPISTATUS');
      const byName = await fn('KPI')(modelRef, config.kpi, 'value');
      assert(v === byName, `KPIVALUE ${v} != KPI(...,"value") ${byName}`);
      return `value=${v} goal=${g} status=${s}`;
    });

    // -- LISTBYID (streaming, spilled array) --------------------------------
    await check('LISTBYID spills a column of strings', async () => {
      const { invocation, result } = makeStreamingInvocation();
      fn('LISTBYID')(ctx.namedSet.id, invocation);
      const grid = await result;
      assert(Array.isArray(grid), `expected a matrix, got ${typeof grid}`);
      assert(grid.length > 0, 'named-set fixture spilled zero rows — pick a previewable set in harness.config.json');
      for (const row of grid) {
        assert(Array.isArray(row) && row.length === 1, `each row must be a single cell, got ${JSON.stringify(row)}`);
        assertType(row[0], 'string', 'LISTBYID member caption');
        assert(!row[0].startsWith('#ERROR'), `LISTBYID returned an error cell: ${row[0]}`);
      }
      return `${grid.length} members, first "${grid[0][0]}"`;
    });

    // -- fail-closed on model mismatch --------------------------------------
    await check('VALUE on the wrong model fails closed with invalidValue', async () => {
      let thrown = null;
      try {
        await fn('VALUE')(wrongModelRef, M1);
      } catch (e) {
        thrown = e;
      }
      assert(thrown, 'a wrong-model formula returned a value instead of failing');
      assert(
        thrown.code === ErrorCode.invalidValue,
        `expected CustomFunctions.ErrorCode.invalidValue, got ${thrown.code}`,
      );
      assert(
        thrown.message.startsWith('Formula model does not match the selected model'),
        `unexpected message: ${thrown.message}`,
      );
      return `#VALUE! "${thrown.message.slice(0, 48)}..."`;
    });

    await check('the wrong-model formula never reaches /plugin/execute', async () => {
      const mark = recorder.mark();
      try { await fn('VALUE')(wrongModelRef, M1); } catch { /* expected */ }
      const posts = recorder.countSince(mark, 'POST', '/api/v1/plugin/execute');
      assert(posts === 0, `guard leaked ${posts} execute request(s) for a non-selected model`);
      return 'no request issued';
    });

    // -- DIAG ---------------------------------------------------------------
    await check('DIAG reports the runtime state', async () => {
      const out = await fn('DIAG')();
      assertType(out, 'string', `${NS}.DIAG`);
      const parts = out.split(' | ');
      const has = p => parts.some(x => x.startsWith(p));
      for (const key of ['STOR=', 'SLUG=', 'MNAME=', 'URL=', 'FETCH=']) {
        assert(has(key), `DIAG output is missing ${key}: ${out}`);
      }
      assert(out.includes('STOR=OK'), `storage not visible to the runtime: ${out}`);
      assert(out.includes('FETCH=200'), `health probe did not return 200: ${out}`);
      return out.slice(0, 90);
    });

    // -- batching -----------------------------------------------------------
    await check('two VALUE calls coalesce into ONE /plugin/execute request', async () => {
      const mark = recorder.mark();
      const [a, b] = await Promise.all([
        fn('VALUE')(modelRef, M1),
        fn('VALUE')(modelRef, M2),
      ]);
      const posts = recorder.countSince(mark, 'POST', '/api/v1/plugin/execute');
      assertType(a, 'number', `batched ${M1}`);
      assertType(b, 'number', `batched ${M2}`);
      assert(posts === 1, `expected 1 execute request, got ${posts}`);
      return `1 request served ${M1}=${a} and ${M2}=${b}`;
    });

    await check('two members of one dimension coalesce and keep their own numbers', async () => {
      const raw = await rawExecute({
        serverUrl, token, projectId: ctx.project.id, modelId: ctx.model.id,
        measures: [M1], dimensions: [DIM],
      });
      const members = raw.data
        .filter(r => r[DIM] !== null && r[DIM] !== undefined)
        .slice(0, 2)
        .map(r => [String(r[DIM]), Number(r[M1])]);
      assert(members.length === 2, `need two members of ${DIM} for this check`);
      const mark = recorder.mark();
      const cells = await Promise.all(
        members.map(([m]) => fn('MEMBERVALUE')(modelRef, M1, DIM, m)),
      );
      const posts = recorder.countSince(mark, 'POST', '/api/v1/plugin/execute');
      assert(posts === 1, `expected 1 grouped execute request, got ${posts}`);
      cells.forEach((cell, i) => {
        assertType(cell, 'number', `member ${members[i][0]}`);
        assert(
          Math.abs(cell - members[i][1]) < 1e-6,
          `member "${members[i][0]}" got ${cell}, server row says ${members[i][1]} — fan-out delivered the wrong member's number`,
        );
      });
      return `1 request, ${members.map(([m], i) => `${m}=${cells[i]}`).join(', ')}`;
    });

    // -- cache generation ---------------------------------------------------
    await check('a cache-generation bump makes the runtime refetch', async () => {
      await fn('KPIVALUE')(ctx.kpi.id);          // populates the TTL cache
      const cachedMark = recorder.mark();
      await fn('KPIVALUE')(ctx.kpi.id);          // must be served from cache
      assert(
        recorder.countSince(cachedMark, 'POST', '/evaluate') === 0,
        'the KPI TTL cache did not serve the second call',
      );
      await storage.setItem(STORAGE_KEYS.CACHE_GENERATION, '2');
      const bumpedMark = recorder.mark();
      const after = await fn('KPIVALUE')(ctx.kpi.id);
      assertType(after, 'number', 'KPIVALUE after bump');
      assert(
        recorder.countSince(bumpedMark, 'POST', '/evaluate') === 1,
        'the runtime kept serving a stale cache after the generation was bumped',
      );
      return 'cache hit, then refetch after bump';
    });

    // -- fail-closed when signed out ----------------------------------------
    await check('signed out, VALUE fails closed and issues no query', async () => {
      mod._resetLastSeenGeneration();
      await storage.removeItem(STORAGE_KEYS.JWT);
      const mark = recorder.mark();
      let thrown = null;
      try { await fn('VALUE')(modelRef, M1); } catch (e) { thrown = e; }
      // Restore before asserting so a failure cannot poison later checks.
      await storage.setItem(STORAGE_KEYS.JWT, token);
      assert(thrown, 'a signed-out formula returned a value');
      assert(
        thrown.code === ErrorCode.notAvailable,
        `expected notAvailable (#CONNECT!), got ${thrown.code}`,
      );
      assert(
        recorder.countSince(mark, 'POST', '/api/v1/plugin/execute') === 0,
        'an unauthenticated execute request was issued',
      );
      return `#CONNECT! "${thrown.message}"`;
    });

    // -- guard checks (injected server states, cancellation, URL scoping) ----
    // Restore the signed-in state the guards need; the check above removed it.
    mod._resetLastSeenGeneration();
    await storage.setItem(STORAGE_KEYS.JWT, token);
    await runRuntimeGuardChecks({
      check, assert, skip, fn, mod, storage, recorder, shim, ctx, config,
    });

  } finally {
    recorder.restore();
    if (shim) await shim.close();
  }

  const failed = results.filter(r => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} checks passed.`);
  if (skipped.length) {
    console.log(`${skipped.length} check(s) skipped — NOT covered here:`);
    for (const s of skipped) console.log(`  - ${s.name}: ${s.reason}`);
  }
  if (failed.length) {
    console.log('Failed:');
    for (const f of failed) console.log(`  - ${f.name}: ${f.error?.message ?? f.error}`);
    process.exitCode = 1;
  }
}

main().catch((e) => {
  console.error(`\nHarness could not run: ${e?.stack ?? e}`);
  process.exitCode = 2;
});
