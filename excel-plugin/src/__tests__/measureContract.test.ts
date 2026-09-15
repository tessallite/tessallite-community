/**
 * Bug-9882 — the add-in's `Measure` type against what the producer actually
 * sends.
 *
 * The type used to declare `measure_type: 'variant'` and `base_measure_id`.
 * The producer (`GET /api/v1/projects/{p}/models/{m}/measures`) emits neither:
 * it emits `measure_type: 'physical' | 'standard'` and marks a time variant
 * with `variant_of_measure_id`. Nothing on the wire ever matched, so the local
 * PivotTable safety gate classified every time variant as freely
 * re-aggregatable and Excel summed CAGRs and moving averages down a column —
 * a wrong number with no error anywhere.
 *
 * The test escape was that every fixture in the suite was hand-written with
 * the invented values, so the gate was only ever tested against shapes the
 * server does not produce. These tests are built from a response CAPTURED FROM
 * THE LIVE PRODUCER (`fixtures/modely-measures.json`, model `modely` on the
 * local stack), so a field the client believes in but the server does not send
 * fails here.
 */
import { describe, expect, it } from 'vitest';

import liveMeasures from './fixtures/modely-measures.json';
import type { Measure } from '../types/tessallite';
import { MEASURE_TYPE_COLORS } from '../components/ReportBuilder/MeasureCard';
import { isMeasureSafeForLocalPivot, unsafeLocalPivotMeasures } from '../utils/zoneQuery';

const measures = liveMeasures as unknown as Measure[];
const byName = (name: string): Measure => {
  const m = measures.find(x => x.name === name);
  if (!m) throw new Error(`fixture has no measure ${name}`);
  return m;
};

describe('the Measure type matches the producer (Bug-9882)', () => {
  it('reads a fixture captured from the live producer, not a hand-written one', () => {
    expect(measures.length).toBeGreaterThan(0);
    // `model_id` only exists on a real response body; a hand-written fixture
    // would not carry it.
    expect(measures.every(m => 'model_id' in (m as object))).toBe(true);
  });

  it('never sends a \'variant\' measure_type or a base_measure_id', () => {
    const invented = measures.filter(
      m => (m.measure_type as string) === 'variant' || 'base_measure_id' in (m as object),
    );
    expect(invented.map(m => m.name)).toEqual([]);
  });

  it('marks a time variant with variant_of_measure_id and variant_kind', () => {
    const cagr = byName('base_amount_cagr');
    expect(cagr.measure_type).toBe('standard');
    expect(cagr.variant_of_measure_id).toBeTruthy();
    expect(cagr.variant_kind).toBe('cagr');
  });

  it('declares every measure_type value the client type allows, and no other', () => {
    const declared = new Set(['physical', 'standard', 'calculated']);
    const unknown = [...new Set(measures.map(m => m.measure_type))]
      .filter(t => !declared.has(t));
    expect(unknown).toEqual([]);
    // 'physical' is a value the server really sends; a client type or a UI
    // switch that omits it is drift of the same kind as the invented 'variant'.
    expect(measures.some(m => m.measure_type === 'physical')).toBe(true);
  });

  it('gives every measure_type the producer sends its own Measure Library style', () => {
    // The library keyed a colour on the invented 'variant' type and had no
    // entry for 'physical', so a quarter of `modely`'s measures fell through to
    // the 'standard' fallback while a colour sat there for a type that does
    // not exist.
    const unstyled = [...new Set(measures.map(m => m.measure_type))]
      .filter(t => !(t in MEASURE_TYPE_COLORS));
    expect(unstyled).toEqual([]);
    expect('variant' in MEASURE_TYPE_COLORS).toBe(false);
  });

  it('carries the producer\'s own additivity verdict on every measure', () => {
    // `is_additive` is the product's SINGLE definition of effective additivity
    // (shared/schemas/domains/dimensions_measures.py, derive_is_additive): the
    // server has already applied non-additive aggregation, semi-additive
    // behaviour, time variance and calculated-measure precedence before
    // persisting it.
    expect(measures.every(m => typeof m.is_additive === 'boolean')).toBe(true);
  });
});

describe('the local PivotTable gate honours the producer (Bug-9882)', () => {
  it('agrees with the producer\'s verdict on every live measure', () => {
    const disagreements = measures
      .filter(m => isMeasureSafeForLocalPivot(m) !== (m.is_additive === true))
      .map(m => `${m.name}: client=${isMeasureSafeForLocalPivot(m)} server=${m.is_additive}`);
    expect(disagreements).toEqual([]);
  });

  it('lets Excel re-aggregate a plain additive sum measure', () => {
    expect(isMeasureSafeForLocalPivot(byName('base_amount'))).toBe(true);
    expect(isMeasureSafeForLocalPivot(byName('transaction_count'))).toBe(true);
  });

  it('refuses every shape the producer calls non-additive', () => {
    for (const name of [
      'base_amount_cagr',   // time variant: summing a growth rate
      'account_balance',    // semi-additive: summing daily closing balances
      'avg_base_amount',    // avg: summing averages
      'unique_customers',   // count_distinct: summing distinct counts
      'max_transaction',    // max: summing maxima
    ]) {
      expect(isMeasureSafeForLocalPivot(byName(name)), name).toBe(false);
    }
  });

  it('refuses a measure the modeller declared non-additive, whatever its shape', () => {
    // `derive_is_additive` precedence rule 5: "A declared FALSE is never
    // overridden — a modeller marking a plain sum measure non-additive is
    // stating something the shape cannot prove, and that statement is the safe
    // direction." Nothing about the SHAPE of this measure reveals it: standard
    // type, sum aggregation, no variant, no semi-additive behaviour. Only the
    // producer's flag says so, and the add-in must not overrule it.
    const declaredNonAdditive = {
      ...byName('base_amount'),
      id: 'm-rate-stored-as-sum',
      name: 'blended_rate',
      is_additive: false,
    } as Measure;
    expect(isMeasureSafeForLocalPivot(declaredNonAdditive)).toBe(false);
    expect(unsafeLocalPivotMeasures(
      [{ id: 'm-rate-stored-as-sum', name: 'blended_rate', zone: 'values' }],
      [declaredNonAdditive],
    ).map(m => m.name)).toEqual(['blended_rate']);
  });

  it('still fails closed for a response that omits the flag', () => {
    // An older server, or a different endpoint shape, may not carry
    // `is_additive`. The local shape tests stay as the backstop, so the gate
    // never opens just because the field is missing.
    const noFlag = { ...byName('base_amount_cagr'), is_additive: undefined } as Measure;
    expect(isMeasureSafeForLocalPivot(noFlag)).toBe(false);
    const plain = { ...byName('base_amount'), is_additive: undefined } as Measure;
    expect(isMeasureSafeForLocalPivot(plain)).toBe(true);
  });
});
