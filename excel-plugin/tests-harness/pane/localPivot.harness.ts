/**
 * Pane harness (b), first flow: Report Builder -> local PivotTable.
 *
 * Drives the REAL pane code path — `api/queryRouter.executeQuery`,
 * `utils/measureValues.normaliseMeasureRows`, `hooks/useExcel.insertLocalPivot`
 * — against the REAL server, with the recording Office.js shim standing in for
 * Excel. It then asserts the resulting WORKBOOK: the hidden backing sheet, the
 * `_tsl_data_*` table, the PivotTable and its field placement, and — the
 * Bug-9876 assertion — that every measure cell written into the backing table
 * is a JS number, not text.
 *
 * A unit test with a mocked response cannot make this assertion meaningfully:
 * the defect lives in the SHAPE of the real response.
 *
 * Runs under `npm run harness:pane`, never under `npx vitest run`.
 */
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { renderHook } from '@testing-library/react';

import { installExcelShim, type InstalledShim } from '../lib/excelShim';
import { installOfficeStubs } from '../lib/officeStubs.mjs';
import { startOriginShim } from '../lib/originShim.mjs';
import { login, resolveContext, seedStorage } from '../lib/session.mjs';
import { configureApiClient } from '../../src/api/client';
import { executeQuery } from '../../src/api/queryRouter';
import { normaliseMeasureRows } from '../../src/utils/measureValues';
import { useExcel } from '../../src/hooks/useExcel';
import config from '../harness.config.json';

function env(name: string, fallback?: string): string {
  const v = process.env[name];
  if (v === undefined || v === '') {
    if (fallback === undefined) throw new Error(`Environment variable ${name} is required (tests-harness/README.md).`);
    return fallback;
  }
  return v;
}

let originShim: { url: string; close(): Promise<void> } | null = null;
let excel: InstalledShim;
let serverUrl: string;
let ctx: Awaited<ReturnType<typeof resolveContext>>;

const MEASURE = config.measures.primary;
const DIM = config.dimension.column;

beforeAll(async () => {
  serverUrl = process.env.TESS_HARNESS_SERVER_URL ?? '';
  if (!serverUrl) {
    originShim = await startOriginShim({
      modelServiceUrl: env('TESS_HARNESS_MODEL_SERVICE_URL', 'http://127.0.0.1:8001'),
      queryRouterUrl: env('TESS_HARNESS_QUERY_ROUTER_URL'),
    });
    serverUrl = originShim.url;
  }

  const tenant = env('TESS_HARNESS_TENANT');
  const email = env('TESS_HARNESS_EMAIL');
  const token = await login({ serverUrl, tenant, email, password: env('TESS_HARNESS_PASSWORD') });
  ctx = await resolveContext({ serverUrl, token, config });

  const { storage } = installOfficeStubs();
  await seedStorage(storage, { serverUrl, token, tenant, email, project: ctx.project, model: ctx.model });
  configureApiClient(serverUrl);

  excel = installExcelShim(['Sheet1']);
}, 120_000);

afterAll(async () => {
  excel?.restore();
  if (originShim) await originShim.close();
});

describe('Report Builder -> local PivotTable', () => {
  it('writes a hidden backing sheet whose measure cells are numbers', async () => {
    // 1. The pane's own query path, against the real server.
    const response = await executeQuery(
      { measures: [MEASURE], dimensions: [DIM] } as never,
      { projectId: ctx.project.id, modelId: ctx.model.id },
    );
    expect(response.data.length).toBeGreaterThan(0);

    // Precondition, not incidental detail: the server really does serialise
    // measures as TEXT. If this ever stops being true the test below stops
    // exercising Bug-9876 and would pass for the wrong reason, so the change
    // must be made here deliberately rather than discovered later.
    const rawMeasure = (response.data[0] as Record<string, unknown>)[MEASURE];
    expect(
      typeof rawMeasure,
      `/plugin/execute no longer serialises "${MEASURE}" as text — re-check that this harness still covers Bug-9876`,
    ).toBe('string');

    // 2. The pane's normalisation, exactly as ReportBuilder applies it.
    const measureKeys = Object.keys(response.annotation?.measures ?? { [MEASURE]: null });
    const rows = normaliseMeasureRows(response.data as Record<string, unknown>[], measureKeys);
    const headers = [DIM, MEASURE];
    const grid = rows.map(r => headers.map(h => r[h] as string | number));

    // 3. The pane's Excel side effect.
    const { result } = renderHook(() => useExcel());
    const address = await result.current.insertLocalPivot(
      headers,
      grid,
      { rowFields: [DIM], columnFields: [], dataFields: [MEASURE], filterFields: [] },
      response.annotation as never,
    );

    // 4. Assert the WORKBOOK, not the call log alone.
    const wb = excel.workbook;

    const dataSheet = wb.sheet('Pivot Data');
    expect(dataSheet, 'the backing data sheet was not created').toBeTruthy();
    expect(dataSheet!.visibility, 'the backing sheet must be hidden').toBe('Hidden');
    expect(address).toContain("'Pivot Data'!");

    const tables = wb.allTables();
    expect(tables).toHaveLength(1);
    expect(tables[0].nameUnchecked).toBe('_tsl_data_Pivot_Data');
    expect(tables[0].headers).toEqual(headers);

    // Bug-9876: EVERY measure cell in the backing table must be a number. A
    // text column makes the PivotTable count instead of sum — a wrong number
    // in front of a user, with no error anywhere.
    const measureCol = headers.indexOf(MEASURE);
    const body = tables[0].bodyRows;
    expect(body.length).toBe(grid.length);
    for (const row of body) {
      const cell = row[measureCol];
      expect(
        typeof cell,
        `measure cell ${JSON.stringify(cell)} reached the workbook as ${typeof cell}`,
      ).toBe('number');
    }

    // 5. The PivotTable itself, and where the fields landed.
    const pivots = wb.allPivotTables();
    expect(pivots).toHaveLength(1);
    expect(pivots[0].name).toBe('TessalliteLocalPivot');
    expect(pivots[0].rowHierarchies.added).toEqual([DIM]);
    expect(pivots[0].dataHierarchies.added).toEqual([MEASURE]);
    expect(wb.sheet('Local Pivot'), 'the pivot sheet was not created').toBeTruthy();

    // 6. Provenance: the insert must leave a named item behind.
    expect(wb.ops('names.add').length).toBeGreaterThan(0);
  }, 180_000);
});
