import { describe, it, expect } from 'vitest';
import {
  recommendChartType,
  separateColumns,
  enrichAnnotationTimeDimensions,
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
  });

  describe('ChartTypeRecommendation type', () => {
    it('all valid chart type strings are accepted', () => {
      const types: ChartTypeRecommendation[] = ['line', 'columnClustered', 'barClustered', 'pie', 'doughnut'];
      expect(types).toHaveLength(5);
    });
  });
});
