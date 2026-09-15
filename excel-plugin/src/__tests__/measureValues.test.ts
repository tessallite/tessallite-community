/** Bug-9876: measure values from /plugin/execute reach Excel as numbers. */
import { describe, expect, it } from 'vitest';

import { normaliseMeasureRows, parseMeasureValue } from '../utils/measureValues';
import { pivotZoneResult } from '../utils/zoneQuery';

describe('parseMeasureValue', () => {
  it('turns the router\'s numeric strings into numbers', () => {
    expect(parseMeasureValue('180720566.17')).toBe(180720566.17);
    expect(parseMeasureValue('-12')).toBe(-12);
    expect(parseMeasureValue('1e3')).toBe(1000);
    expect(parseMeasureValue(' 42 ')).toBe(42);
  });
  it('parses the scientific-notation form a count Decimal takes (Bug-9910)', () => {
    // `str(Decimal('1.0E+5'))` is `"1.0E+5"`, which is what `transaction_count`
    // carried on `modely` while `base_amount` carried plain decimal text.
    expect(parseMeasureValue('1.0E+5')).toBe(100000);
    expect(parseMeasureValue('1E+5')).toBe(100000);
    expect(parseMeasureValue('2.5e-3')).toBe(0.0025);
  });
  it('keeps numbers, empties and genuine text', () => {
    expect(parseMeasureValue(7)).toBe(7);
    expect(parseMeasureValue(null)).toBeNull();
    expect(parseMeasureValue(undefined)).toBeNull();
    expect(parseMeasureValue('')).toBeNull();
    expect(parseMeasureValue('On Track')).toBe('On Track');
    expect(parseMeasureValue('12abc')).toBe('12abc');
  });
  it('never renumbers text whose leading zeros carry meaning', () => {
    // A cost centre, product code or account number. `Number('0042')` is 42,
    // and the producer now types its own measure columns, so a string here
    // means the value genuinely is text. A real Decimal never serialises with
    // a leading zero before another digit.
    expect(parseMeasureValue('0042')).toBe('0042');
    expect(parseMeasureValue('-007')).toBe('-007');
    // A leading zero before the decimal point is an ordinary number.
    expect(parseMeasureValue('0.5')).toBe(0.5);
    expect(parseMeasureValue('0')).toBe(0);
  });
});

describe('normaliseMeasureRows', () => {
  const rows = [
    { country_code: 'GB', base_amount: '67969062.99', fee_amount: '1' },
    { country_code: '007', base_amount: null, fee_amount: '2.5' },
  ];
  it('parses only the measure columns', () => {
    const out = normaliseMeasureRows(rows, ['base_amount', 'fee_amount']);
    expect(out[0]).toEqual({ country_code: 'GB', base_amount: 67969062.99, fee_amount: 1 });
    expect(out[1]).toEqual({ country_code: '007', base_amount: null, fee_amount: 2.5 });
    // dimension text that looks numeric is untouched
    expect(typeof out[1].country_code).toBe('string');
  });
  it('feeds numbers into the local PivotTable backing rows', () => {
    const { rows: cells } = pivotZoneResult(
      normaliseMeasureRows(rows, ['base_amount']), ['country_code'], [], ['base_amount'], {},
    );
    expect(cells[0][1]).toBe(67969062.99);
    expect(typeof cells[0][1]).toBe('number');
  });
});
