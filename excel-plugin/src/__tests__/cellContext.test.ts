import { describe, it, expect, vi, afterEach } from 'vitest';
import { buildDrillRequestContext, resolveCellContext } from '../utils/cellContext';
import * as workbookMetadata from '../utils/workbookMetadata';

describe('cellContext', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    delete (globalThis as Record<string, unknown>).Excel;
  });

  describe('resolveCellContext', () => {
    it('identifies CUBEVALUE formulas', async () => {
      const ctx = await resolveCellContext(
        'Sheet1!A1',
        42,
        '=CUBEVALUE("Tessallite","[Measures].[Revenue]","[Dim].[Country].[US]")',
      );
      expect(ctx.type).toBe('cube-formula');
      expect(ctx.measureName).toBe('Revenue');
    });

    it('preserves supported CUBEVALUE member filters as drill filters', async () => {
      const ctx = await resolveCellContext(
        'Sheet1!A1',
        42,
        '=CUBEVALUE("Tessallite","[Measures].[base_amount]","[country_code].[country_code].[GB]","[fiscal_year].[fiscal_year].[2025]")',
        {
          byName: new Map([['base_amount', 'c50cbb0c-a620-4443-af34-cd92fde1c5c9']]),
          byDisplayName: new Map(),
        },
      );
      expect(ctx.type).toBe('cube-formula');
      expect(ctx.measureId).toBe('c50cbb0c-a620-4443-af34-cd92fde1c5c9');
      expect(ctx.filters).toEqual([
        { column: 'country_code', op: 'eq', value: 'GB' },
        { column: 'fiscal_year', op: 'eq', value: '2025' },
      ]);
      expect(ctx.groupingLevels).toBeUndefined();
      expect(buildDrillRequestContext(ctx, 'fallback-persona')).toEqual({
        grouping_levels: [],
        filters: [
          { column: 'country_code', op: 'eq', value: 'GB' },
          { column: 'fiscal_year', op: 'eq', value: '2025' },
        ],
        persona_id: 'fallback-persona',
      });
    });

    it('marks CUBEVALUE drill unavailable when filter context is unrecoverable', async () => {
      const ctx = await resolveCellContext(
        'Sheet1!A1',
        42,
        '=CUBEVALUE("Tessallite","[Measures].[base_amount]",A1)',
        {
          byName: new Map([['base_amount', 'c50cbb0c-a620-4443-af34-cd92fde1c5c9']]),
          byDisplayName: new Map(),
        },
      );
      expect(ctx.type).toBe('cube-formula');
      expect(ctx.measureId).toBe('c50cbb0c-a620-4443-af34-cd92fde1c5c9');
      expect(ctx.unavailableReason).toBe('cube_filter_context_unrecoverable');
      expect(ctx.drillFilters).toBeUndefined();
      expect(ctx.filters).toBeUndefined();
    });

    it('identifies CUBEMEMBER formulas', async () => {
      const ctx = await resolveCellContext(
        'Sheet1!A1',
        'US',
        '=CUBEMEMBER("Tessallite","[Dim].[Country].[US]")',
      );
      expect(ctx.type).toBe('cube-formula');
    });

    it('returns unknown for regular formulas', async () => {
      const ctx = await resolveCellContext(
        'Sheet1!A1',
        42,
        '=SUM(B1:B10)',
      );
      expect(ctx.type).toBe('unknown');
    });

    it('returns unknown for empty cells', async () => {
      const ctx = await resolveCellContext(
        'Sheet1!A1',
        null,
        '',
      );
      expect(ctx.type).toBe('unknown');
    });
  });

  describe('plugin-table drill coordinates (F-025-06)', () => {
    // Mock Excel so readRowValues can return the selected row.
    function mockExcelRow(rowValues: unknown[]) {
      (globalThis as Record<string, unknown>).Excel = {
        run: async (cb: (ctx: unknown) => Promise<unknown>) => {
          const range = { values: [rowValues], load: () => {} };
          const sheet = { getRangeByIndexes: () => range };
          const context = {
            workbook: {
              worksheets: {
                getItem: () => sheet,
                getActiveWorksheet: () => sheet,
              },
            },
            sync: async () => {},
          };
          return cb(context);
        },
      };
    }

    it('resolves the measure UUID and the row dimension coordinates', async () => {
      // measureColumns maps a TITLE to the measure UUID (the F-025-06 fix);
      // dimensionColumns maps a TITLE to the dimension semantic name.
      vi.spyOn(workbookMetadata, 'getTableMetadata').mockResolvedValue({
        projectId: 'p1',
        modelId: 'm1',
        personaId: 'persona-3',
        columnHeaders: JSON.stringify(['Country', 'Base Amount']),
        measureColumns: JSON.stringify({ 'Base Amount': 'c50cbb0c-a620-4443-af34-cd92fde1c5c9' }),
        dimensionColumns: JSON.stringify({ Country: 'country_code' }),
        // _tableStart is a synthetic key getTableMetadata adds.
        _tableStart: 'A1',
      } as unknown as Partial<workbookMetadata.TableMetadata>);
      // Selected cell B2 (the Base Amount value on the GB row); row values are
      // [Country, Base Amount] = ['GB', 12749].
      mockExcelRow(['GB', 12749]);

      const ctx = await resolveCellContext('Sheet1!B2', 12749, '');
      expect(ctx.type).toBe('plugin-table');
      // measureId is the UUID — not the measure name (the old 422 cause).
      expect(ctx.measureId).toBe('c50cbb0c-a620-4443-af34-cd92fde1c5c9');
      expect(ctx.measureName).toBe('Base Amount');
      // Coordinates captured as {column, op, value} drill filters.
      expect(ctx.drillFilters).toEqual([
        { column: 'country_code', op: 'eq', value: 'GB' },
      ]);
      expect(ctx.groupingLevels).toEqual([
        { column: 'country_code', op: 'eq', value: 'GB' },
      ]);
      expect(ctx.filters).toBeUndefined();
      expect(buildDrillRequestContext(ctx)).toEqual({
        grouping_levels: [
          { column: 'country_code', op: 'eq', value: 'GB' },
        ],
        filters: [],
        persona_id: 'persona-3',
      });
      expect(ctx.personaId).toBe('persona-3');
    });

    it('does not capture the header row as a coordinate', async () => {
      vi.spyOn(workbookMetadata, 'getTableMetadata').mockResolvedValue({
        projectId: 'p1',
        modelId: 'm1',
        columnHeaders: JSON.stringify(['Country', 'Base Amount']),
        measureColumns: JSON.stringify({ 'Base Amount': 'meas-uuid' }),
        dimensionColumns: JSON.stringify({ Country: 'country_code' }),
        _tableStart: 'A1',
      } as unknown as Partial<workbookMetadata.TableMetadata>);
      mockExcelRow(['Country', 'Base Amount']);
      // Selecting the header cell A1 (same row as table start) yields no coords.
      const ctx = await resolveCellContext('Sheet1!A1', 'Country', '');
      expect(ctx.drillFilters).toBeUndefined();
    });
  });

});
