/**
 * Resolve the pane harness's fixtures from the LIVE model metadata.
 *
 * The specs need a measure of each class the local-PivotTable gate refuses
 * (non-standard measure_type, time variant, semi-additive, non-additive
 * aggregation) plus one measure that is genuinely safe. Naming those in a
 * constant would let a reseed quietly turn a "variant" fixture into an ordinary
 * sum measure, and the refusal check would then pass for no reason. Classifying
 * them here, from the same metadata the pane reads, makes that impossible: the
 * resolver FAILS if the model no longer contains an example of a class.
 *
 * Display names, not technical names: the pane's libraries show display names,
 * and that is what a spec has to click.
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { login, resolveContext } from '../lib/session.mjs';
import { serverUrl, requireEnv } from './harnessEnv.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const config = JSON.parse(readFileSync(resolve(HERE, '../harness.config.json'), 'utf8'));

/** Must mirror `src/utils/zoneQuery.ts`. */
const ADDITIVE_AGGS = new Set(['sum', 'count', 'count_star']);
const ADDITIVE_SEMI = new Set(['', 'none', 'sum', 'additive']);

const agg = m => (m.default_agg || '').trim().toLowerCase();
const semi = m => (m.semi_additive_behavior || '').trim().toLowerCase();

const CLASSES = {
  // The gate's four refusal conditions, in the order the plan names them.
  calculated: m => m.measure_type !== 'standard',
  variant: m => Boolean(m.variant_of_measure_id),
  semiAdditive: m => !ADDITIVE_SEMI.has(semi(m)),
  nonAdditive: m => !ADDITIVE_AGGS.has(agg(m)),
};

function isSafe(m) {
  return m.measure_type === 'standard'
    && !m.variant_of_measure_id
    && ADDITIVE_AGGS.has(agg(m))
    && ADDITIVE_SEMI.has(semi(m));
}

let cached = null;

export async function paneFixtures() {
  if (cached) return cached;

  const url = serverUrl();
  const token = await login({
    serverUrl: url,
    tenant: requireEnv('TESS_HARNESS_TENANT'),
    email: requireEnv('TESS_HARNESS_EMAIL'),
    password: requireEnv('TESS_HARNESS_PASSWORD'),
  });
  const ctx = await resolveContext({ serverUrl: url, token, config });

  const base = `${url}/api/v1/projects/${ctx.project.id}/models/${ctx.model.id}`;
  const get = async (path) => {
    const res = await fetch(`${base}${path}`, { headers: { Authorization: `Bearer ${token}` } });
    if (!res.ok) throw new Error(`GET ${base}${path} -> ${res.status}`);
    const body = await res.json();
    return Array.isArray(body) ? body : (body.items ?? []);
  };

  const measures = await get('/measures?deployed_only=true');
  const dimensions = await get('/dimensions?deployed_only=true');

  const named = m => m.display_name || m.name;

  const safe = measures.find(m => isSafe(m) && m.name === config.measures.primary)
    ?? measures.find(isSafe);
  if (!safe) throw new Error('no additive standard measure on the fixture model — the pivot checks cannot run');

  const blockedMeasures = {};
  for (const [kind, matches] of Object.entries(CLASSES)) {
    const found = measures.find(m => matches(m) && !isSafe(m));
    if (!found) {
      throw new Error(
        `the fixture model has no ${kind} measure, so the refusal check would pass vacuously. `
        + 'Reseed the demo model or point harness.config.json at one that has all four classes.',
      );
    }
    blockedMeasures[kind] = { name: named(found), technical: found.name };
  }

  const dimension = dimensions.find(d => d.name === config.dimension.column) ?? dimensions[0];
  if (!dimension) throw new Error('the fixture model exposes no dimensions');

  cached = {
    project: ctx.project,
    model: ctx.model,
    kpi: ctx.kpi,
    namedSet: ctx.namedSet,
    measure: { name: named(safe), technical: safe.name },
    dimension: { name: named(dimension), technical: dimension.name },
    blockedMeasures,
  };
  return cached;
}
