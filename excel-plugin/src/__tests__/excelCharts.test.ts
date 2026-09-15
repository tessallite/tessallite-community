import { describe, it, expect, vi } from 'vitest';
import {
  recommendChartType,
  separateColumns,
  enrichAnnotationTimeDimensions,
  buildAnnotationFromCitations,
  buildChartRowsFromRecords,
  createChartOnSheet,
  type ChartTypeRecommendation,
} from '../utils/excelCharts';

describe('enrichAnnotationTimeDimensions (Bug-7416)', () => {
  it('moves a known time dimension from dimensions into timeDimensions', () => {
    const annotation = {
      measures: { revenue: { title: 'Revenue', type: 'sum' } },
      dimensions: {
        business_date_month: { title: 'Month', type: 'integer' },
        region: { title: 'Region', type: 'string' },
      },
      timeDimensions: {},
    };
    const enriched = enrichAnnotationTimeDimensions(annotation, ['business_date_month']);
    expect(enriched?.timeDimensions).toHaveProperty('business_date_month');
    expect(enriched?.dimensions).not.toHaveProperty('business_date_month');
    expect(enriched?.dimensions).toHaveProperty('region');
  });

  it('drives recommendChartType to a line chart once the time dim is reclassified', () => {
    // Before enrichment the backend annotation has an empty timeDimensions map,
    // so the month column reads as a category and the recommender picks a
    // column chart. After enrichment it detects the time axis -> line.
    const headers = ['Month', 'Revenue'];
    const rows: (string | number)[][] = [
      ['2024-01', 100], ['2024-02', 200], ['2024-03', 300], ['2024-04', 400],
    ];
    const backendAnnotation = {
      measures: { revenue: { title: 'Revenue', type: 'sum' } },
      dimensions: { business_date_month: { title: 'Month', type: 'integer' } },
      timeDimensions: {},
    };
    // Sanity: without the time flag the recommender does not see a time axis
    // (the key 'business_date_month' is not among the headers, so it is a plain
    // category) — this asserts the enrichment is what unlocks the line chart.
    const enriched = enrichAnnotationTimeDimensions(backendAnnotation, ['business_date_month']);
    const rec = recommendChartType(headers, rows, enriched);
    expect(rec.chartType).toBe('line');
    expect(rec.confidence).toBe('high');
  });

  it('is a no-op when no dimension matches a time-dimension name', () => {
    const annotation = {
      dimensions: { region: { title: 'Region', type: 'string' } },
      measures: {},
      timeDimensions: {},
    };
    const enriched = enrichAnnotationTimeDimensions(annotation, ['business_date_month']);
    expect(enriched).toBe(annotation); // same reference, nothing moved
  });

  it('is a no-op when the time-dimension name set is empty', () => {
    const annotation = { dimensions: { d: { title: 'D', type: 'x' } }, measures: {}, timeDimensions: {} };
    expect(enrichAnnotationTimeDimensions(annotation, [])).toBe(annotation);
  });

  it('preserves existing timeDimensions entries while adding new ones', () => {
    const annotation = {
      dimensions: { d_month: { title: 'Month', type: 'integer' } },
      measures: {},
      timeDimensions: { d_year: { title: 'Year', type: 'integer' } },
    };
    const enriched = enrichAnnotationTimeDimensions(annotation, ['d_month']);
    expect(enriched?.timeDimensions).toHaveProperty('d_year');
    expect(enriched?.timeDimensions).toHaveProperty('d_month');
  });

  it('returns undefined unchanged for an undefined annotation', () => {
    expect(enrichAnnotationTimeDimensions(undefined, ['d'])).toBeUndefined();
  });
});

describe('excelCharts', () => {
  describe('recommendChartType', () => {
    it('returns columnClustered for insufficient data', () => {
      const result = recommendChartType([], []);
      expect(result.chartType).toBe('columnClustered');
      expect(result.confidence).toBe('low');
    });

    it('returns columnClustered for single column', () => {
      const result = recommendChartType(['Name'], [['Alice']]);
      expect(result.chartType).toBe('columnClustered');
      expect(result.confidence).toBe('low');
    });

    it('returns line for time series with annotation', () => {
      const headers = ['Date', 'Revenue'];
      const rows: (string | number)[][] = [
        ['2024-01', 100],
        ['2024-02', 200],
        ['2024-03', 300],
      ];
      const annotation = {
        timeDimensions: { 'Date': { title: 'Date', type: 'time' } },
        measures: { 'Revenue': { title: 'Revenue', type: 'number' } },
      };
      const result = recommendChartType(headers, rows, annotation);
      expect(result.chartType).toBe('line');
      expect(result.confidence).toBe('high');
    });

    it('returns columnClustered for category + measure with moderate rows', () => {
      const headers = ['Country', 'Revenue', 'Region'];
      const rows: (string | number)[][] = Array.from({ length: 9 }, (_, i) => [`Country${i}`, i * 100, `Region${i % 3}`]);
      const result = recommendChartType(headers, rows);
      expect(['columnClustered', 'barClustered']).toContain(result.chartType);
      expect(result.confidence).toBe('high');
    });

    it('returns barClustered for many categories', () => {
      const headers = ['Country', 'Revenue'];
      const rows: (string | number)[][] = Array.from({ length: 15 }, (_, i) => [`Country${i}`, i * 100]);
      const result = recommendChartType(headers, rows);
      expect(result.chartType).toBe('barClustered');
    });

    it('uses citation annotation to recognize numeric-string measures', () => {
      const headers = ['base_amount', 'account_type'];
      const rows: (string | number)[][] = [
        ['23332917.80', 'CREDIT'],
        ['23055047.22', 'CURRENT'],
      ];
      const annotation = buildAnnotationFromCitations([
        { kind: 'measure', name: 'base_amount', display_name: 'Base amount' },
        { kind: 'dimension', name: 'account_type', display_name: 'Account type' },
      ]);
      const result = recommendChartType(headers, rows, annotation);
      expect(['pie', 'doughnut']).toContain(result.chartType);
      expect(result.reason).toContain('categories');
    });

    it('treats an uncited column in a partial annotation as a category', () => {
      const headers = ['fiscal_period', 'revenue'];
      const rows: (string | number)[][] = [
        ['FY2024', 100],
        ['FY2025', 200],
      ];
      const annotation = {
        measures: { revenue: { title: 'Revenue', type: 'sum' } },
      };
      const result = recommendChartType(headers, rows, annotation);
      expect(['pie', 'doughnut']).toContain(result.chartType);
      expect(result.confidence).toBe('medium');
    });

    it('returns pie for few categories', () => {
      const headers = ['Category', 'Value'];
      const rows: (string | number)[][] = [
        ['A', 30],
        ['B', 40],
        ['C', 30],
      ];
      const result = recommendChartType(headers, rows);
      expect(['pie', 'doughnut']).toContain(result.chartType);
    });

    it('provides a reason for every recommendation', () => {
      const headers = ['X', 'Y'];
      const rows: (string | number)[][] = [['a', 1], ['b', 2]];
      const result = recommendChartType(headers, rows);
      expect(result.reason).toBeTruthy();
    });
  });

  describe('separateColumns', () => {
    it('puts dimensions first and measures after', () => {
      const headers = ['region', 'revenue', 'cost'];
      const rows: (string | number)[][] = [
        ['North', 1000, 500],
        ['South', 2000, 800],
      ];
      const annotation = {
        dimensions: { region: { title: 'region', type: 'string' } },
        measures: { revenue: { title: 'revenue', type: 'number' }, cost: { title: 'cost', type: 'number' } },
      };
      const { chartHeaders, chartRows } = separateColumns(headers, rows, annotation);
      expect(chartHeaders).toEqual(['Category', 'revenue', 'cost']);
      expect(chartRows[0]).toEqual(['North', 1000, 500]);
      expect(chartRows[1]).toEqual(['South', 2000, 800]);
    });

    it('concatenates multiple dimensions', () => {
      const headers = ['region', 'product', 'revenue'];
      const rows: (string | number)[][] = [
        ['North', 'Widget', 1000],
      ];
      const annotation = {
        dimensions: { region: { title: 'region', type: 'string' }, product: { title: 'product', type: 'string' } },
        measures: { revenue: { title: 'revenue', type: 'number' } },
      };
      const { chartHeaders, chartRows } = separateColumns(headers, rows, annotation);
      expect(chartHeaders).toEqual(['Category', 'revenue']);
      expect(chartRows[0]).toEqual(['North / Widget', 1000]);
    });

    it('uses heuristics without annotation', () => {
      const headers = ['name', 'value'];
      const rows: (string | number)[][] = [['A', 10], ['B', 20]];
      const { chartHeaders, chartRows } = separateColumns(headers, rows);
      expect(chartHeaders[0]).toBe('Category');
      expect(chartRows[0][0]).toBe('A');
      expect(chartRows[0][1]).toBe(10);
    });

    it('Bug-9737: without annotation, canonical numeric strings use the value fallback', () => {
      // Reproduces the Agent API's response shape when citations are missing:
      // measure values arrive as JSON strings, not JS numbers. Decimal display
      // zeros are valid measure text even though Number() normalizes them.
      const headers = ['base_amount', 'account_type'];
      const rows: (string | number)[][] = [
        ['23332917.80', 'CREDIT'],
        ['23055047.22', 'CURRENT'],
      ];
      const { chartHeaders, chartRows } = separateColumns(headers, rows);
      expect(chartHeaders).toEqual(['Category', 'base_amount']);
      expect(chartRows[0]).toEqual(['CREDIT', 23332917.8]);
    });

    it('keeps canonical integer identifiers as categories without annotation', () => {
      const headers = ['year', 'postal_code', 'amount'];
      const rows: (string | number)[][] = [
        ['2024', '90210', '23332917.80'],
        ['2025', '10001', '23055047.22'],
      ];
      const { chartHeaders, chartRows } = separateColumns(headers, rows);
      expect(chartHeaders).toEqual(['Category', 'amount']);
      expect(chartRows).toEqual([
        ['2024 / 90210', 23332917.8],
        ['2025 / 10001', 23055047.22],
      ]);
      const result = recommendChartType(headers, rows);
      expect(result.chartType).toBe('columnClustered');
      expect(result.confidence).toBe('high');
    });

    it('does not reuse the first all-numeric column as a measure', () => {
      const headers = ['left_value', 'right_value'];
      const rows: (string | number)[][] = [[1, 2], [3, 4]];
      const { chartHeaders, chartRows } = separateColumns(headers, rows);
      expect(chartHeaders).toEqual(['Category', 'right_value']);
      expect(chartRows).toEqual([
        ['1', 2],
        ['3', 4],
      ]);
      const result = recommendChartType(headers, rows);
      expect(result.chartType).toBe('pie');
      expect(result.confidence).toBe('medium');
    });

    it('Bug-9737: citation-derived annotation classifies numeric-string measure values', () => {
      const headers = ['base_amount', 'account_type'];
      const rows: (string | number)[][] = [
        ['23332917.80', 'CREDIT'],
        ['23055047.22', 'CURRENT'],
      ];
      const annotation = buildAnnotationFromCitations([
        { kind: 'dimension', name: 'account_type', display_name: 'account type' },
        { kind: 'measure', name: 'base_amount', display_name: 'base amount' },
      ]);
      const { chartHeaders, chartRows } = separateColumns(headers, rows, annotation);
      expect(chartHeaders).toEqual(['Category', 'base_amount']);
      expect(chartRows[0]).toEqual(['CREDIT', 23332917.8]);
      expect(chartRows[1]).toEqual(['CURRENT', 23055047.22]);
    });

    it('treats an uncited text-year column as a category in an annotated answer', () => {
      const headers = ['fiscal_year', 'revenue'];
      const rows: (string | number)[][] = [
        ['FY2024', 100],
        ['FY2025', 200],
      ];
      const annotation = {
        measures: { revenue: { title: 'revenue', type: 'number' } },
      };
      const { chartHeaders, chartRows } = separateColumns(headers, rows, annotation);
      expect(chartHeaders).toEqual(['Category', 'revenue']);
      expect(chartRows).toEqual([
        ['FY2024', 100],
        ['FY2025', 200],
      ]);
    });

    it('retains one annotated scalar measure with a synthetic category', () => {
      const headers = ['Revenue'];
      const rows: (string | number)[][] = [[1000]];
      const annotation = {
        measures: { Revenue: { title: 'Revenue', type: 'number' } },
      };

      expect(separateColumns(headers, rows, annotation)).toEqual({
        chartHeaders: ['Category', 'Revenue'],
        chartRows: [['Result', 1000]],
      });
    });

    it('retains every annotated measure-only series', () => {
      const headers = ['Revenue', 'Cost'];
      const rows: (string | number)[][] = [[1000, 800]];
      const annotation = {
        measures: {
          Revenue: { title: 'Revenue', type: 'number' },
          Cost: { title: 'Cost', type: 'number' },
        },
      };

      expect(separateColumns(headers, rows, annotation)).toEqual({
        chartHeaders: ['Category', 'Revenue', 'Cost'],
        chartRows: [['Result', 1000, 800]],
      });
      const recommendation = recommendChartType(headers, rows, annotation);
      expect(recommendation.chartType).toBe('columnClustered');
      expect(recommendation.confidence).toBe('low');
    });

    it('rejects non-canonical postal-code strings as unannotated measures', () => {
      const headers = ['postal_code', 'amount'];
      const rows: (string | number)[][] = [
        ['00123', '10'],
        ['00456', '20'],
      ];
      const { chartHeaders, chartRows } = separateColumns(headers, rows);
      expect(chartHeaders).toEqual(['Category', 'amount']);
      expect(chartRows).toEqual([
        ['00123', 10],
        ['00456', 20],
      ]);
      const result = recommendChartType(headers, rows);
      expect(['pie', 'doughnut']).toContain(result.chartType);
    });
  });

  describe('buildChartRowsFromRecords (Bug-9737)', () => {
    it('coerces annotated measure strings but preserves dimension strings', () => {
      const annotation = buildAnnotationFromCitations([
        { kind: 'measure', name: 'transaction_count', display_name: 'Transaction count' },
        { kind: 'dimension', name: 'account_code', display_name: 'Account code' },
      ]);
      const rows = buildChartRowsFromRecords(
        ['transaction_count', 'account_code'],
        [{ transaction_count: '1.0E+5', account_code: '0042' }],
        annotation,
      );
      expect(rows).toEqual([[100000, '0042']]);
      expect(typeof rows[0][0]).toBe('number');
      expect(typeof rows[0][1]).toBe('string');
    });

    it('writes a two-column chart range with numeric measures to Office', () => {
      const annotation = buildAnnotationFromCitations([
        { kind: 'measure', name: 'base_amount', display_name: 'Base amount' },
        { kind: 'dimension', name: 'account_type', display_name: 'Account type' },
      ]);
      const headers = ['base_amount', 'account_type'];
      const rows = buildChartRowsFromRecords(headers, [
        { base_amount: '23332917.80', account_type: 'CREDIT' },
        { base_amount: '23055047.22', account_type: 'CURRENT' },
      ], annotation);
      const range = { values: [] as (string | number)[][] };
      const chart = { setPosition: vi.fn(), title: { text: '' } };
      const sheet = {
        getRangeByIndexes: vi.fn(() => range),
        charts: { add: vi.fn(() => chart) },
      };
      vi.stubGlobal('Excel', { ChartSeriesBy: { columns: 'Columns' } });
      try {
        createChartOnSheet(
          'Pie' as Excel.ChartType, sheet as unknown as Excel.Worksheet, headers, rows, annotation,
        );
        expect(sheet.getRangeByIndexes).toHaveBeenCalledWith(5, 0, 3, 2);
        expect(range.values).toEqual([
          ['Category', 'base_amount'],
          ['CREDIT', 23332917.8],
          ['CURRENT', 23055047.22],
        ]);
        expect(sheet.charts.add).toHaveBeenCalledWith('Pie', range, 'Columns');
      } finally {
        vi.unstubAllGlobals();
      }
    });
  });

  describe('buildAnnotationFromCitations (Bug-9737)', () => {
    it('builds measures and dimensions keyed by technical name from citation kind', () => {
      const annotation = buildAnnotationFromCitations([
        { kind: 'measure', name: 'base_amount', display_name: 'base amount' },
        { kind: 'dimension', name: 'account_type', display_name: 'account type' },
      ]);
      expect(annotation).toEqual({
        measures: { base_amount: { title: 'base amount', type: 'measure' } },
        dimensions: { account_type: { title: 'account type', type: 'dimension' } },
      });
    });

    it('returns undefined for null, undefined, or empty citations', () => {
      expect(buildAnnotationFromCitations(null)).toBeUndefined();
      expect(buildAnnotationFromCitations(undefined)).toBeUndefined();
      expect(buildAnnotationFromCitations([])).toBeUndefined();
    });

    it('ignores citation kinds other than measure/dimension', () => {
      const annotation = buildAnnotationFromCitations([
        { kind: 'other', name: 'x', display_name: 'X' },
      ]);
      expect(annotation).toBeUndefined();
    });
  });

  describe('ChartTypeRecommendation type', () => {
    it('all valid chart type strings are accepted', () => {
      const types: ChartTypeRecommendation[] = ['line', 'columnClustered', 'barClustered', 'pie', 'doughnut'];
      expect(types).toHaveLength(5);
    });
  });
});
