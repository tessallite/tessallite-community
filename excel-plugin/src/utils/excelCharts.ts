/// <reference types="office-js" />

export type ChartTypeRecommendation = 'line' | 'columnClustered' | 'barClustered' | 'pie' | 'doughnut';

export interface ChartRecommendation {
  chartType: ChartTypeRecommendation;
  confidence: 'high' | 'medium' | 'low';
  reason: string;
}

const AGENT_CHART_MAP: Record<string, ChartTypeRecommendation | null> = {
  line: 'line',
  multi_line: 'line',
  multi_line_wide: 'line',
  bar: 'columnClustered',
  grouped_bar: 'columnClustered',
  stacked_bar: 'columnClustered',
  h_bar: 'barClustered',
  pie: 'pie',
  kpi: null,
};

export function mapAgentChartType(agentType: string | null | undefined): ChartTypeRecommendation | null {
  if (!agentType) return null;
  return AGENT_CHART_MAP[agentType] ?? null;
}

export function getChartTypeEnum(type: ChartTypeRecommendation): Excel.ChartType {
  switch (type) {
    case 'line': return Excel.ChartType.line;
    case 'columnClustered': return Excel.ChartType.columnClustered;
    case 'barClustered': return Excel.ChartType.barClustered;
    case 'pie': return Excel.ChartType.pie;
    case 'doughnut': return Excel.ChartType.doughnut;
    default: return Excel.ChartType.columnClustered;
  }
}

export function recommendChartType(
  headers: string[],
  rows: (string | number)[][],
  annotation?: {
    measures?: Record<string, { title: string; type: string }>;
    dimensions?: Record<string, { title: string; type: string }>;
    timeDimensions?: Record<string, { title: string; type: string }>;
  },
): ChartRecommendation {
  const colCount = headers.length;
  const rowCount = rows.length;

  if (rowCount === 0 || colCount < 2) {
    return { chartType: 'columnClustered', confidence: 'low', reason: 'Insufficient data for chart recommendation' };
  }

  const timeColumns = new Set<string>();
  const numericColumns = new Set<string>();
  const stringColumns = new Set<string>();

  if (annotation?.timeDimensions) {
    for (const key of Object.keys(annotation.timeDimensions)) {
      timeColumns.add(key);
    }
  }

  for (let col = 0; col < colCount; col++) {
    const header = headers[col];
    if (timeColumns.has(header)) continue;

    let numericCount = 0;
    let stringCount = 0;
    for (let row = 0; row < Math.min(rowCount, 20); row++) {
      const val = rows[row][col];
      if (typeof val === 'number') {
        numericCount++;
      } else if (typeof val === 'string' && val.length > 0) {
        stringCount++;
      }
    }
    if (numericCount >= stringCount && numericCount > 0) {
      numericColumns.add(header);
    } else {
      stringColumns.add(header);
    }
  }

  const hasTimeAxis = timeColumns.size > 0;
  const numCategories = stringColumns.size + timeColumns.size;
  const numMeasures = numericColumns.size;

  if (hasTimeAxis && numMeasures >= 1) {
    return { chartType: 'line', confidence: 'high', reason: 'Time series detected with date dimension and numeric measures' };
  }

  if (numCategories >= 1 && numMeasures >= 1) {
    if (numCategories === 1 && rowCount <= 8 && numMeasures <= 2) {
      return { chartType: 'pie', confidence: 'medium', reason: 'Few categories with measures suggest part-of-whole view' };
    }
    if (numCategories === 1 && rowCount <= 12 && numMeasures <= 2) {
      return { chartType: 'doughnut', confidence: 'medium', reason: 'Limited categories suitable for ring chart' };
    }
    if (rowCount > 12) {
      return { chartType: 'barClustered', confidence: 'high', reason: 'Many categories work best as horizontal bars' };
    }
    return { chartType: 'columnClustered', confidence: 'high', reason: 'Category + measure data suited for column chart' };
  }

  return { chartType: 'columnClustered', confidence: 'low', reason: 'Default recommendation' };
}

export function separateColumns(
  headers: string[],
  rows: (string | number)[][],
  annotation?: {
    measures?: Record<string, { title: string; type: string }>;
    dimensions?: Record<string, { title: string; type: string }>;
    timeDimensions?: Record<string, { title: string; type: string }>;
  },
): { chartHeaders: string[]; chartRows: (string | number)[][] } {
  const measureKeys = new Set<string>();
  const dimKeys = new Set<string>();

  if (annotation?.measures) {
    for (const [key, m] of Object.entries(annotation.measures)) {
      measureKeys.add(key);
      measureKeys.add(m.title);
    }
  }
  if (annotation?.dimensions) {
    for (const [key, d] of Object.entries(annotation.dimensions)) {
      dimKeys.add(key);
      dimKeys.add(d.title);
    }
  }
  if (annotation?.timeDimensions) {
    for (const [key, d] of Object.entries(annotation.timeDimensions)) {
      dimKeys.add(key);
      dimKeys.add(d.title);
    }
  }

  const dimIndices: number[] = [];
  const measureIndices: number[] = [];

  for (let i = 0; i < headers.length; i++) {
    const h = headers[i];
    if (measureKeys.has(h)) {
      measureIndices.push(i);
    } else if (dimKeys.has(h)) {
      dimIndices.push(i);
    } else {
      const sample = rows.slice(0, 20);
      const numCount = sample.filter(r => typeof r[i] === 'number').length;
      if (numCount > sample.length / 2) {
        measureIndices.push(i);
      } else {
        dimIndices.push(i);
      }
    }
  }

  if (dimIndices.length === 0) dimIndices.push(0);
  if (measureIndices.length === 0) {
    for (let i = 0; i < headers.length; i++) {
      if (!dimIndices.includes(i)) measureIndices.push(i);
    }
  }

  const chartHeaders = ['Category', ...measureIndices.map(i => headers[i])];
  const chartRows = rows.map(row => {
    const label = dimIndices.map(i => String(row[i] ?? '')).join(' / ');
    const values = measureIndices.map(i => row[i]);
    return [label, ...values] as (string | number)[];
  });

  return { chartHeaders, chartRows };
}

export async function insertChartFromRange(
  chartType: Excel.ChartType,
  sheet: Excel.Worksheet,
  headers: string[],
  rows: (string | number)[][],
  annotation?: {
    measures?: Record<string, { title: string; type: string }>;
    dimensions?: Record<string, { title: string; type: string }>;
    timeDimensions?: Record<string, { title: string; type: string }>;
  },
  title?: string,
): Promise<void> {
  const { chartHeaders, chartRows } = separateColumns(headers, rows, annotation);

  const dataRowCount = chartRows.length + 1;
  const dataColCount = chartHeaders.length;
  const startRow = rows.length + 3;
  const chartDataRange = sheet.getRangeByIndexes(startRow, 0, dataRowCount, dataColCount);
  chartDataRange.values = [chartHeaders, ...chartRows];

  const chart = sheet.charts.add(chartType, chartDataRange, Excel.ChartSeriesBy.columns);
  const chartTop = startRow + dataRowCount + 1;
  chart.setPosition(`A${chartTop + 1}`, `K${chartTop + 21}`);
  chart.title.text = title || 'Tessallite Result';

  const dimNames: string[] = [];
  if (annotation?.dimensions) {
    for (const d of Object.values(annotation.dimensions)) dimNames.push(d.title);
  }
  if (annotation?.timeDimensions) {
    for (const d of Object.values(annotation.timeDimensions)) dimNames.push(d.title);
  }
  const measureNames = annotation?.measures
    ? Object.values(annotation.measures).map(m => m.title)
    : chartHeaders.slice(1);

  const categoryAxis = chart.axes.getItem(Excel.ChartAxisType.category);
  categoryAxis.title.text = dimNames.join(' / ') || 'Category';
  categoryAxis.title.visible = true;

  const valueAxis = chart.axes.getItem(Excel.ChartAxisType.value);
  valueAxis.title.text = measureNames.join(' / ') || 'Value';
  valueAxis.title.visible = true;
}
