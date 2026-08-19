import { describe, expect, it } from 'vitest';

import {
  PivotFieldResolutionError,
  resolvePivotFieldMapping,
  type PivotFieldMapping,
} from '../utils/excelPivotTables';

function hierarchy(name: string): Excel.PivotHierarchy {
  return { name } as Excel.PivotHierarchy;
}

describe('resolvePivotFieldMapping', () => {
  const mapping: PivotFieldMapping = {
    rowFields: ['Region'],
    columnFields: ['Year'],
    dataFields: ['Revenue'],
    filterFields: ['Segment'],
  };

  it('resolves every requested PivotTable zone deterministically', () => {
    const region = hierarchy('region');
    const year = hierarchy('Year');
    const revenue = hierarchy('Revenue');
    const segment = hierarchy('Segment');
    const resolved = resolvePivotFieldMapping(
      new Map([
        ['region', region],
        ['Year', year],
        ['Revenue', revenue],
        ['Segment', segment],
      ]),
      mapping,
    );

    expect(resolved.rowFields).toEqual([region]);
    expect(resolved.columnFields).toEqual([year]);
    expect(resolved.dataFields).toEqual([revenue]);
    expect(resolved.filterFields).toEqual([segment]);
  });

  it('Bug-6910: aborts and identifies unresolved row/value fields by zone', () => {
    expect(() => resolvePivotFieldMapping(
      new Map([
        ['Year', hierarchy('Year')],
        ['Segment', hierarchy('Segment')],
      ]),
      mapping,
    )).toThrow(PivotFieldResolutionError);

    try {
      resolvePivotFieldMapping(
        new Map([
          ['Year', hierarchy('Year')],
          ['Segment', hierarchy('Segment')],
        ]),
        mapping,
      );
    } catch (error) {
      expect(error).toBeInstanceOf(PivotFieldResolutionError);
      const resolutionError = error as PivotFieldResolutionError;
      expect(resolutionError.unresolvedByZone.rowFields).toEqual(['Region']);
      expect(resolutionError.unresolvedByZone.dataFields).toEqual(['Revenue']);
      expect(resolutionError.message).toContain('Rows: Region');
      expect(resolutionError.message).toContain('Values: Revenue');
    }
  });
});
