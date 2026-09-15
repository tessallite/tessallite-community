/**
 * The four functions-runtime GUARD checks (harness (a), phase 2).
 *
 * They are here rather than in `runFunctions.mjs` because each needs something
 * the ordinary checks do not: an injected server state, a wall-clock wait, an
 * inspection of every recorded URL, or a cancelled streaming invocation. The
 * happy-path checks stay a flat, fast list; these four carry their own setup.
 *
 * What is faked, and what is not: only the SERVER STATE is injected (through
 * the origin shim), never the add-in. The bundle under test is the real built
 * `functions.iife.js` issuing real requests.
 */
import { DENY_ALL_RULE_ID } from '../lib/originShim.mjs';
import { STORAGE_KEYS } from '../lib/session.mjs';

/** Wall-clock ceiling the add-in enforces per request (functions.ts). */
const REQUEST_TIMEOUT_MS = 30_000;

// Bug-9881 closed the last exempted route (`/kpis/{id}/evaluate`), so the
// invariant below is now unconditional: EVERY metadata read the functions
// runtime issues must carry `deployed_only=true`. There is no exception list —
// a new route that drops the flag fails the check outright.

/**
 * @param {object} deps
 * @param {(name: string, fn: () => Promise<string|void>) => Promise<void>} deps.check
 * @param {(cond: unknown, message: string) => void} deps.assert
 * @param {(id: string) => Function} deps.fn
 * @param {object} deps.mod loaded bundle namespace
 * @param {object} deps.storage OfficeRuntime.storage stub
 * @param {object} deps.recorder FetchRecorder
 * @param {object|null} deps.shim origin shim handle, or null for a real origin
 * @param {object} deps.ctx resolved fixtures
 * @param {object} deps.config harness.config.json
 * @param {(name: string, reason: string) => void} deps.skip
 */
export async function runRuntimeGuardChecks(deps) {
  const { check, assert, fn, mod, storage, recorder, shim, ctx, config, skip } = deps;
  const modelRef = ctx.model.slug ?? ctx.model.name;
  const M1 = config.measures.primary;

  /** Drop every TTL cache so the next call really goes to the network. */
  let generation = 2;
  const invalidateCaches = async () => {
    generation += 1;
    await storage.setItem(STORAGE_KEYS.CACHE_GENERATION, String(generation));
    mod._resetLastSeenGeneration();
  };

  // -- row-security deny-all (Bug-8453) -------------------------------------
  if (!shim) {
    skip(
      'row-security deny-all raises instead of writing 0 (Bug-8453)',
      'needs the origin shim to inject the deny-all sentinel; TESS_HARNESS_SERVER_URL points at a real origin.',
    );
  } else {
    await check('row-security deny-all raises instead of writing 0 (Bug-8453)', async () => {
      await invalidateCaches();
      shim.injectFault('/api/v1/plugin/execute', 'deny-all');
      let thrown = null;
      let value;
      try {
        value = await fn('VALUE')(modelRef, M1);
      } catch (e) {
        thrown = e;
      } finally {
        shim.clearFault();
      }
      assert(
        thrown,
        `a deny-all response produced the cell value ${JSON.stringify(value)} instead of an error — `
        + 'a COUNT-shaped measure would have written a fabricated 0 into a spreadsheet',
      );
      assert(
        /Row-level security/i.test(thrown.message),
        `expected the row-security message, got: ${thrown.message}`,
      );
      assert(
        !/^0$/.test(String(value ?? '')),
        'the runtime returned a value as well as raising',
      );
      await invalidateCaches();
      return `#VALUE! "${thrown.message.slice(0, 60)}..." (sentinel "${DENY_ALL_RULE_ID}" injected by the shim)`;
    });
  }

  // -- request ceiling (Bug-9749) -------------------------------------------
  if (!shim) {
    skip(
      'a stalled request settles at the 30s ceiling (Bug-9749)',
      'needs the origin shim to stall a request; TESS_HARNESS_SERVER_URL points at a real origin.',
    );
  } else {
    await check('a stalled request settles at the 30s ceiling (Bug-9749)', async () => {
      await invalidateCaches();
      shim.injectFault('/api/v1/plugin/execute', 'stall');
      const started = Date.now();
      let thrown = null;
      let value;
      try {
        value = await fn('VALUE')(modelRef, M1);
      } catch (e) {
        thrown = e;
      } finally {
        shim.clearFault();
      }
      const elapsed = Date.now() - started;
      assert(
        thrown,
        `a stalled request returned ${JSON.stringify(value)} instead of failing — `
        + 'without the ceiling the cell stays at #GETTING_DATA forever',
      );
      assert(
        /Request timed out/.test(thrown.message),
        `expected the request-timeout message, got: ${thrown.message}`,
      );
      assert(
        elapsed < REQUEST_TIMEOUT_MS * 1.5,
        `the cell took ${elapsed}ms to settle; the ceiling is ${REQUEST_TIMEOUT_MS}ms`,
      );
      assert(
        elapsed > REQUEST_TIMEOUT_MS * 0.5,
        `the cell settled after only ${elapsed}ms — the stall was not reaching the runtime, so the ceiling was not what settled it`,
      );
      await invalidateCaches();
      return `settled in ${(elapsed / 1000).toFixed(1)}s with "Request timed out"`;
    });
  }

  // -- deployed_only + persona scoping on every metadata read ---------------
  await check('every metadata read carries deployed_only=true, and persona_id when a persona is active', async () => {
    await invalidateCaches();
    const noPersonaMark = recorder.mark();
    await fn('KPIVALUE')(ctx.kpi.id);
    await new Promise((resolve) => { fn('LISTBYID')(ctx.namedSet.id, { onCanceled: null, setResult: resolve }); });
    const modelReads = url => url.includes('/models/') && !url.includes('/api/v1/plugin/');
    const withoutPersona = recorder.since(noPersonaMark).filter(c => modelReads(c.url));
    assert(withoutPersona.length > 0, 'no metadata read was recorded — the check would pass vacuously');
    for (const call of withoutPersona) {
      assert(
        call.url.includes('deployed_only=true'),
        `a metadata read reached the server without deployed_only=true: ${call.url}\n`
        + '        The runtime would then evaluate against LIVE editor state instead of the '
        + 'deployed snapshot. Route it through consumptionQuery().',
      );
      assert(
        !call.url.includes('persona_id='),
        `no persona is active, yet the read carried one: ${call.url}`,
      );
    }
    const persona = ctx.persona;
    if (!persona) {
      return `${withoutPersona.length} read(s) checked; no persona fixture on this model, so persona_id scoping was not exercised`;
    }

    await storage.setItem(STORAGE_KEYS.ACTIVE_PERSONA_ID, persona.id);
    await invalidateCaches();
    const personaMark = recorder.mark();
    try {
      await fn('KPIVALUE')(ctx.kpi.id);
      await new Promise((resolve) => { fn('LISTBYID')(ctx.namedSet.id, { onCanceled: null, setResult: resolve }); });
    } catch {
      // A persona may legitimately deny the KPI; the URL is what is asserted.
    }
    const withPersona = recorder.since(personaMark).filter(c => modelReads(c.url));
    assert(withPersona.length > 0, 'the persona-scoped call issued no metadata read');
    for (const call of withPersona) {
      assert(
        call.url.includes('deployed_only=true'),
        `a persona-scoped read dropped deployed_only=true: ${call.url}`,
      );
      assert(
        call.url.includes(`persona_id=${encodeURIComponent(persona.id)}`),
        `a persona is active but the read is unscoped — it would return objects the persona cannot see: ${call.url}`,
      );
    }
    await storage.removeItem(STORAGE_KEYS.ACTIVE_PERSONA_ID);
    await invalidateCaches();
    return `${withoutPersona.length} unscoped + ${withPersona.length} persona-scoped read(s) checked ("${persona.name}")`;
  });

  // -- LISTBYID cancellation -------------------------------------------------
  await check('a cancelled LISTBYID delivers no result and caches nothing', async () => {
    await invalidateCaches();
    let delivered = null;
    let deliveries = 0;
    const invocation = {
      onCanceled: null,
      setResult(value) { deliveries += 1; delivered = value; },
    };

    fn('LISTBYID')(ctx.namedSet.id, invocation);
    assert(
      typeof invocation.onCanceled === 'function',
      'LISTBYID did not register an onCanceled handler, so Excel could never cancel it',
    );
    // Cancel while the request is still in flight — the function has only just
    // been invoked and its first await (context resolution) has not returned.
    invocation.onCanceled();

    // Give the in-flight request every chance to come back and deliver.
    await new Promise(resolve => setTimeout(resolve, 5_000));
    assert(
      deliveries === 0,
      `a cancelled invocation still delivered ${deliveries} result(s) (${JSON.stringify(delivered)?.slice(0, 80)}) — Excel would write into a cell the user has moved on from`,
    );

    // A cancelled call must not populate the TTL cache either: the next real
    // invocation has to go to the network rather than serve a half-finished one.
    const mark = recorder.mark();
    const fresh = await new Promise((resolve) => {
      fn('LISTBYID')(ctx.namedSet.id, { onCanceled: null, setResult: resolve });
    });
    const requests = recorder.since(mark).filter(c => c.url.includes('/named-sets/')).length;
    assert(Array.isArray(fresh), `the follow-up call returned ${typeof fresh}`);
    assert(
      requests >= 1,
      'the follow-up call was served from cache — the cancelled invocation had written to it',
    );
    return `no result after cancel; the next call re-fetched (${fresh.length} members)`;
  });
}
