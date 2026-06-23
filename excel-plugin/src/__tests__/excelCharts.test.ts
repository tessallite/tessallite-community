import { describe, it, expect } from 'vitest';
import {
  recommendChartType,
  separateColumns,
  type ChartTypeRecommendation,
} from '../utils/excelCharts';

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
