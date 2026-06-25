import { describe, expect, it } from 'vitest';
import { buildTableDrillMetadata } from '../utils/drillMetadata';
import type { Measure } from '../types/tessallite';

const measures: Measure[] = [
  {
    id: 'c50cbb0c-a620-4443-af34-cd92fde1c5c9',
    name: 'base_amount',
    display_name: 'Base Amount',
    default_agg: 'SUM',
    measure_type: 'standard',
  },
];

describe('buildTableDrillMetadata', () => {
  it('normalizes agent annotation measure keys to measure UUIDs', () => {
    const metadata = buildTableDrillMetadata(
      {
        measures: {
          base_amount: { title: 'Base Amount', type: 'number', format: '$#,##0' },
        },
        dimensions: {
          country_code: { title: 'Country', type: 'string' },
        },
      },
      measures,
    );

    expect(metadata.measureColumns).toEqual({
      'Base Amount': 'c50cbb0c-a620-4443-af34-cd92fde1c5c9',
    });
    expect(metadata.dimensionColumns).toEqual({
      Country: 'country_code',
    });
    expect(metadata.formatTokens).toEqual({
      base_amount: '$#,##0',
      'Base Amount': '$#,##0',
    });
  });

  it('does not write stale measure names when a UUID cannot be resolved', () => {
    const metadata = buildTableDrillMetadata(
      {
        measures: {
          unknown_amount: { title: 'Unknown Amount', type: 'number' },
        },
      },
      measures,
    );

    expect(metadata.measureColumns).toEqual({});
  });

  it('treats agent time dimensions as row-coordinate drill dimensions', () => {
    const metadata = buildTableDrillMetadata(
      {
        timeDimensions: {
          fiscal_year: { title: 'Fiscal Year', type: 'number' },
        },
      },
      measures,
    );

    expect(metadata.dimensionColumns).toEqual({
      'Fiscal Year': 'fiscal_year',
    });
  });
});
