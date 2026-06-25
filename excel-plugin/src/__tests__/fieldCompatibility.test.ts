import { describe, expect, it } from 'vitest';
import type { Dimension, FieldCompatibilityIssue, FieldCompatibilityResponse } from '../types/tessallite';
import {
  dimensionCompatibilityById,
  evaluateZoneFieldCompatibility,
  formatZoneCompatibilityMessages,
  selectedCompatibilityIds,
} from '../utils/fieldCompatibility';
import type { ZoneItem } from '../components/ReportBuilder/ZoneMappingGrid';

const DIMENSIONS = [
  { id: 'dim-school', name: 'school', display_name: 'School', data_type: 'string', source_type: 'dim' },
  { id: 'dim-product', name: 'product', display_name: 'Product', data_type: 'string', source_type: 'dim' },
  { id: 'dim-region', name: 'region', display_name: 'Region', data_type: 'string', source_type: 'dim' },
] as Dimension[];

function issue(
  measureId: string,
  dimensionId: string,
  message: string,
  compatibleNames: string[] = ['School'],
  code: FieldCompatibilityIssue['code'] = 'NO_JOIN_PATH',
  severity: FieldCompatibilityIssue['severity'] = 'error',
): FieldCompatibilityIssue {
  return {
    code,
    severity,
    message,
    measure_id: measureId,
    dimension_id: dimensionId,
    compatible_dimension_ids: ['dim-school'],
    compatible_dimension_names: compatibleNames,
  };
}

function matrix(measures: FieldCompatibilityResponse['measures'], multi_measure: FieldCompatibilityResponse['multi_measure'] = null): FieldCompatibilityResponse {
  return {
    model_id: 'model-1',
    version_id: 'version-1',
    generated_at: '2026-06-14T00:00:00Z',
    status: Object.values(measures).some(m => Object.keys(m.incompatible_dimensions).length > 0) ? 'incompatible' : 'compatible',
    measures,
    multi_measure,
  };
}

describe('field compatibility helpers', () => {
  it('compacts selected measure and resolved dimension ids from zone items', () => {
    const items: ZoneItem[] = [
      { id: 'measure-1', name: 'Revenue', zone: 'values' },
      { id: 'measure-1', name: 'Revenue duplicate', zone: 'values' },
      { id: 'hier-1:1', name: 'Region level', zone: 'rows', kind: 'hierarchy_level', bindDimension: 'region' },
      { id: 'dim-school', name: 'School', zone: 'filters' },
    ];

    expect(selectedCompatibilityIds(items, DIMENSIONS)).toEqual({
      measureIds: ['measure-1'],
      dimensionIds: ['dim-region', 'dim-school'],
    });
  });

  it('blocks a verified incompatible measure and dimension pair', () => {
    const result = evaluateZoneFieldCompatibility({
      items: [
        { id: 'measure-1', name: 'Revenue', zone: 'values' },
        { id: 'dim-product', name: 'Product', zone: 'rows' },
      ],
      dimensions: DIMENSIONS,
      matrix: matrix({
        'measure-1': {
          name: 'Revenue',
          compatible_dimension_ids: ['dim-school'],
          incompatible_dimensions: {
            'dim-product': issue('measure-1', 'dim-product', 'Product has no join path to Revenue.'),
          },
        },
      }),
    });

    expect(result.blocking).toBe(true);
    expect(formatZoneCompatibilityMessages(result)).toEqual([
      'Product with Revenue: Product has no join path to Revenue.',
    ]);
    expect(result.compatibleDimensionNames).toEqual(['School']);
  });

  it('does not block or disable fields for not-analyzed compatibility feedback', () => {
    const result = evaluateZoneFieldCompatibility({
      items: [
        { id: 'measure-1', name: 'Revenue', zone: 'values' },
        { id: 'dim-product', name: 'Product', zone: 'rows' },
      ],
      dimensions: DIMENSIONS,
      matrix: matrix({
        'measure-1': {
          name: 'Revenue',
          compatible_dimension_ids: [],
          incompatible_dimensions: {
            'dim-product': {
              ...issue('measure-1', 'dim-product', 'Compatibility has not been analyzed.'),
              code: 'SEMANTIC_COMPATIBILITY_NOT_ANALYZED',
            },
          },
        },
      }),
    });

    expect(result.blocking).toBe(false);
    expect(result.unavailableByDimensionId['dim-product'].disabled).toBe(false);
  });

  it('does not block or disable fields for ambiguous aggregation path warnings', () => {
    const result = evaluateZoneFieldCompatibility({
      items: [
        { id: 'measure-1', name: 'Revenue', zone: 'values' },
        { id: 'dim-product', name: 'Product', zone: 'rows' },
      ],
      dimensions: DIMENSIONS,
      matrix: matrix({
        'measure-1': {
          name: 'Revenue',
          compatible_dimension_ids: ['dim-school'],
          incompatible_dimensions: {
            'dim-product': issue(
              'measure-1',
              'dim-product',
              'Revenue and Product have more than one possible aggregation path.',
              ['School'],
              'AMBIGUOUS_JOIN_PATH',
              'warning',
            ),
          },
        },
      }),
    });

    expect(result.blocking).toBe(false);
    expect(result.issues).toEqual([]);
    expect(result.unavailableByDimensionId['dim-product'].disabled).toBe(false);
  });

  it('marks only common compatible dimensions available for multi-measure layouts when issues are returned', () => {
    const result = evaluateZoneFieldCompatibility({
      items: [
        { id: 'measure-1', name: 'Revenue', zone: 'values' },
        { id: 'measure-2', name: 'Tickets', zone: 'values' },
        { id: 'dim-product', name: 'Product', zone: 'columns' },
      ],
      dimensions: DIMENSIONS,
      matrix: matrix({
        'measure-1': {
          name: 'Revenue',
          compatible_dimension_ids: ['dim-school', 'dim-product'],
          incompatible_dimensions: {},
        },
        'measure-2': {
          name: 'Tickets',
          compatible_dimension_ids: ['dim-school'],
          incompatible_dimensions: {
            'dim-product': issue('measure-2', 'dim-product', 'Product cannot be used with Tickets.'),
          },
        },
      }, {
        selected_measure_ids: ['measure-1', 'measure-2'],
        common_dimension_ids: ['dim-school'],
        common_dimension_names: ['School'],
        conflicts_by_measure: [],
        suggested_actions: ['keep_common_dimensions'],
      }),
    });

    expect(result.blocking).toBe(true);
    expect(result.compatibleDimensionNames).toEqual(['School']);
    expect(result.unavailableByDimensionId['dim-product'].disabled).toBe(true);
  });

  it('filters compatible-dimension suggestions through the persona-visible dimension list', () => {
    const visibleDimensions = [DIMENSIONS[0], DIMENSIONS[1]];
    const availability = dimensionCompatibilityById(
      matrix({
        'measure-1': {
          name: 'Revenue',
          compatible_dimension_ids: ['dim-school'],
          incompatible_dimensions: {
            'dim-product': issue(
              'measure-1',
              'dim-product',
              'Product has no join path to Revenue.',
              ['School', 'Hidden Salary Band'],
            ),
          },
        },
      }),
      ['measure-1'],
      visibleDimensions,
    );

    expect(Object.keys(availability)).toEqual(['dim-school', 'dim-product']);
    expect(availability['dim-product'].compatibleDimensionNames).toEqual(['School']);

    const result = evaluateZoneFieldCompatibility({
      items: [
        { id: 'measure-1', name: 'Revenue', zone: 'values' },
        { id: 'dim-product', name: 'Product', zone: 'rows' },
      ],
      dimensions: visibleDimensions,
      matrix: matrix({
        'measure-1': {
          name: 'Revenue',
          compatible_dimension_ids: ['dim-school'],
          incompatible_dimensions: {
            'dim-product': issue(
              'measure-1',
              'dim-product',
              'Product has no join path to Revenue.',
              ['School', 'Hidden Salary Band'],
            ),
          },
        },
      }),
    });
    expect(result.compatibleDimensionNames).toEqual(['School']);
  });
});
