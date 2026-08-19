/**
 * Tests for Excel formula generation utilities.
 */
import { describe, it, expect } from 'vitest';
import {
  generateCubeMember,
  generateCubeValue,
  generateCubeSet,
  buildCubeRankedMemberFormula,
  buildCubeKpiFormula,
  buildCubeKpiValueFormula,
  CUBE_KPI_PROPERTIES,
  buildMsolapConnectionString,
  buildXmlaCatalogName,
  TESSALLITE_CONNECTION_NAME,
  measureMemberRef,
  dimensionMemberRef,
  escapeExcelString,
  escapeMdxIdentifier,
  kpiValueCellFormula,
  kpiStatusCellFormula,
} from '../utils/excelFormulas';

describe('excelFormulas', () => {
  describe('generateCubeMember', () => {
    it('generates a CUBEMEMBER formula', () => {
      const result = generateCubeMember('MyConn', '[Dim].[Member]');
      expect(result).toBe('=CUBEMEMBER("MyConn","[Dim].[Member]")');
    });
  });

  describe('generateCubeValue', () => {
    it('generates a CUBEVALUE formula with no filters', () => {
      const result = generateCubeValue('MyConn', '[Measures].[Sales]');
      expect(result).toBe('=CUBEVALUE("MyConn","[Measures].[Sales]")');
    });

    it('generates a CUBEVALUE formula with filters', () => {
      const result = generateCubeValue('MyConn', '[Measures].[Sales]', ['[Dim].[Country].[Country].[US]', '[Dim].[Date].[Year].[2025]']);
      expect(result).toBe('=CUBEVALUE("MyConn","[Measures].[Sales]","[Dim].[Country].[Country].[US]","[Dim].[Date].[Year].[2025]")');
    });
  });

  describe('generateCubeSet', () => {
    it('generates a CUBESET formula without caption', () => {
      const result = generateCubeSet('MyConn', '[Dim].[Children]');
      expect(result).toBe('=CUBESET("MyConn","[Dim].[Children]")');
    });

    it('generates a CUBESET formula with caption', () => {
      const result = generateCubeSet('MyConn', '[Dim].[Children]', 'My Set');
      expect(result).toBe('=CUBESET("MyConn","[Dim].[Children]","My Set")');
    });
  });

  describe('buildCubeRankedMemberFormula', () => {
    it('generates a CUBERANKEDMEMBER formula with cell reference and rank', () => {
      const result = buildCubeRankedMemberFormula('MyConn', 'A1', 1);
      expect(result).toBe('=CUBERANKEDMEMBER("MyConn",A1,1)');
    });

    it('uses absolute cell reference for higher ranks', () => {
      const result = buildCubeRankedMemberFormula('MyConn', '$A$1', 5);
      expect(result).toBe('=CUBERANKEDMEMBER("MyConn",$A$1,5)');
    });
  });

  describe('buildCubeKpiFormula', () => {
    it('generates CUBEKPIMEMBER for value (property 1)', () => {
      const result = buildCubeKpiFormula('MyConn', 'Revenue Growth', CUBE_KPI_PROPERTIES.Value);
      expect(result).toBe('=CUBEKPIMEMBER("MyConn","Revenue Growth",1)');
    });

    it('generates CUBEKPIMEMBER for goal (property 2)', () => {
      const result = buildCubeKpiFormula('MyConn', 'Revenue Growth', CUBE_KPI_PROPERTIES.Goal);
      expect(result).toBe('=CUBEKPIMEMBER("MyConn","Revenue Growth",2)');
    });

    it('generates CUBEKPIMEMBER for status (property 3)', () => {
      const result = buildCubeKpiFormula('MyConn', 'Revenue Growth', CUBE_KPI_PROPERTIES.Status);
      expect(result).toBe('=CUBEKPIMEMBER("MyConn","Revenue Growth",3)');
    });

    it('generates CUBEKPIMEMBER for trend (property 4)', () => {
      const result = buildCubeKpiFormula('MyConn', 'Revenue Growth', CUBE_KPI_PROPERTIES.Trend);
      expect(result).toBe('=CUBEKPIMEMBER("MyConn","Revenue Growth",4)');
    });

    it('generates CUBEKPIMEMBER for weight (property 5)', () => {
      const result = buildCubeKpiFormula('MyConn', 'Revenue Growth', CUBE_KPI_PROPERTIES.Weight);
      expect(result).toBe('=CUBEKPIMEMBER("MyConn","Revenue Growth",5)');
    });
  });

  describe('CUBE_KPI_PROPERTIES', () => {
    it('maps all five KPI properties to correct numeric codes', () => {
      expect(CUBE_KPI_PROPERTIES.Value).toBe(1);
      expect(CUBE_KPI_PROPERTIES.Goal).toBe(2);
      expect(CUBE_KPI_PROPERTIES.Status).toBe(3);
      expect(CUBE_KPI_PROPERTIES.Trend).toBe(4);
      expect(CUBE_KPI_PROPERTIES.Weight).toBe(5);
    });
  });

  describe('buildMsolapConnectionString', () => {
    it('builds a connection string with XMLA endpoint path', () => {
      const result = buildMsolapConnectionString('https://example.com', 'MyCatalog');
      expect(result).toContain('Provider=MSOLAP.8');
      expect(result).toContain('Data Source=https://example.com/api/v1/xmla/');
      expect(result).toContain('Initial Catalog=MyCatalog');
    });
  });

  describe('escapeExcelString', () => {
    it('doubles embedded double quotes', () => {
      expect(escapeExcelString('He said "hello"')).toBe('He said ""hello""');
    });

    it('returns unchanged string without quotes', () => {
      expect(escapeExcelString('simple')).toBe('simple');
    });

    it('handles consecutive quotes', () => {
      expect(escapeExcelString('a""b')).toBe('a""""b');
    });
  });

  describe('escapeMdxIdentifier', () => {
    it('doubles closing brackets', () => {
      expect(escapeMdxIdentifier('Sales]Total')).toBe('Sales]]Total');
    });

    it('returns unchanged string without brackets', () => {
      expect(escapeMdxIdentifier('Revenue')).toBe('Revenue');
    });

    it('handles multiple brackets', () => {
      expect(escapeMdxIdentifier('a]b]c')).toBe('a]]b]]c');
    });
  });

  // F-025-07: the gateway publishes catalogs as `<model slug>` and
  // `<model slug>_<persona slug>` (verified live: modelx, modelx_technical,
  // modely, modely_business, modely_technical). The UUID is never published.
  describe('buildXmlaCatalogName', () => {
    it('returns the model slug when no persona is active', () => {
      expect(buildXmlaCatalogName('modelx')).toBe('modelx');
      expect(buildXmlaCatalogName('modely', null)).toBe('modely');
      expect(buildXmlaCatalogName('modely', '')).toBe('modely');
    });

    it('suffixes the persona slug with an underscore when a persona is active', () => {
      expect(buildXmlaCatalogName('modely', 'technical')).toBe('modely_technical');
      expect(buildXmlaCatalogName('modelx', 'business')).toBe('modelx_business');
    });

    it('never emits a raw UUID — only what was passed as the slug', () => {
      // The caller must pass the slug, not the model id. The helper itself is
      // slug-in/slug-out; this documents the contract the wizard relies on.
      expect(buildXmlaCatalogName('modely', 'technical')).not.toMatch(/[0-9a-f]{8}-[0-9a-f]{4}/);
    });
  });

  describe('TESSALLITE_CONNECTION_NAME', () => {
    it('is the exact literal the LiveConnectionWizard creates', () => {
      // F-025-10: every CUBE/KPI formula path must reference this same name.
      expect(TESSALLITE_CONNECTION_NAME).toBe('Tessallite');
    });
  });

  // F-025-09: member/measure references must be bracket-escaped so a name
  // containing ']' cannot break the formula, and must use the gateway's
  // published caption grammar `[dim].[dim].[member]`.
  describe('measureMemberRef', () => {
    it('wraps a technical measure name in the Measures hierarchy', () => {
      expect(measureMemberRef('fee_amount')).toBe('[Measures].[fee_amount]');
    });
    it('escapes a closing bracket in the measure name', () => {
      expect(measureMemberRef('weird]name')).toBe('[Measures].[weird]]name]');
    });
  });

  describe('dimensionMemberRef', () => {
    it('builds the published caption grammar [dim].[dim].[member]', () => {
      expect(dimensionMemberRef('account_type', 'CREDIT'))
        .toBe('[account_type].[account_type].[CREDIT]');
    });
    it('escapes brackets in both the dimension and the member key', () => {
      expect(dimensionMemberRef('dim]x', 'mem]y'))
        .toBe('[dim]]x].[dim]]x].[mem]]y]');
    });
  });

  // Bug-6729: the CUBEVALUE wrap for KPI property formulas.
  describe('buildCubeKpiValueFormula (Bug-6729)', () => {
    it('wraps CUBEKPIMEMBER Value inside CUBEVALUE', () => {
      const result = buildCubeKpiValueFormula('MyConn', 'Revenue Growth', CUBE_KPI_PROPERTIES.Value);
      expect(result).toBe('=CUBEVALUE("MyConn",CUBEKPIMEMBER("MyConn","Revenue Growth",1))');
    });

    it('wraps Status (property 3) inside CUBEVALUE', () => {
      const result = buildCubeKpiValueFormula('MyConn', 'Revenue Growth', CUBE_KPI_PROPERTIES.Status);
      expect(result).toBe('=CUBEVALUE("MyConn",CUBEKPIMEMBER("MyConn","Revenue Growth",3))');
    });

    it('escapes both connection name and KPI caption', () => {
      const result = buildCubeKpiValueFormula('My "Conn"', 'Revenue "Growth"', CUBE_KPI_PROPERTIES.Value);
      expect(result).toBe('=CUBEVALUE("My ""Conn""",CUBEKPIMEMBER("My ""Conn""","Revenue ""Growth""",1))');
    });

    it('never emits a bare CUBEKPIMEMBER (the caption-rendering defect)', () => {
      const result = buildCubeKpiValueFormula('Tessallite', 'Shipping Cost', CUBE_KPI_PROPERTIES.Value);
      expect(result).toMatch(/^=CUBEVALUE\(/);
      expect(result).toContain('CUBEKPIMEMBER(');
    });
  });

  // Bug-6729: kpiStatusCellFormula and kpiValueCellFormula produce the
  // CUBEVALUE-wrapped formulas for scorecard / full-row / single-cell inserts.
  describe('kpiStatusCellFormula (Bug-6729)', () => {
    it('produces CUBEVALUE(CUBEKPIMEMBER(...,3)) for the Status cell', () => {
      expect(kpiStatusCellFormula('Tessallite', 'Shipping Cost'))
        .toBe('=CUBEVALUE("Tessallite",CUBEKPIMEMBER("Tessallite","Shipping Cost",3))');
    });
  });

  describe('kpiValueCellFormula (Bug-6729)', () => {
    it('measure-backed KPI: CUBEVALUE on the measure (unchanged)', () => {
      expect(kpiValueCellFormula('Tessallite', 'kpi_name', 'net_sales'))
        .toBe('=CUBEVALUE("Tessallite","[Measures].[net_sales]")');
    });

    it('custom KPI: CUBEVALUE(CUBEKPIMEMBER Value) -- never bare CUBEKPIMEMBER', () => {
      const result = kpiValueCellFormula('Tessallite', 'kpi_name', null);
      expect(result).toMatch(/^=CUBEVALUE\(/);
      expect(result).toContain('CUBEKPIMEMBER("Tessallite","kpi_name",1)');
    });
  });

  describe('formula escaping integration', () => {
    it('escapes connection name with double quotes in CUBEMEMBER', () => {
      const result = generateCubeMember('My "Conn"', '[Dim].[Member]');
      expect(result).toBe('=CUBEMEMBER("My ""Conn""","[Dim].[Member]")');
    });

    it('escapes measure expression with quotes in CUBEVALUE', () => {
      const result = generateCubeValue('Conn', '[Measures].[Revenue "Q1"]');
      expect(result).toBe('=CUBEVALUE("Conn","[Measures].[Revenue ""Q1""]")');
    });

    it('emits the bare KPI caption, Excel-escaped (Bug-3659)', () => {
      // Bug-3659: no literal MDX brackets — Excel resolves the bare caption.
      const result = buildCubeKpiFormula('Conn', 'Revenue "Growth"', CUBE_KPI_PROPERTIES.Value);
      expect(result).toBe('=CUBEKPIMEMBER("Conn","Revenue ""Growth""",1)');
    });

    it('escapes caption with quotes in CUBESET', () => {
      const result = generateCubeSet('Conn', '{[A]}', 'Top "Items"');
      expect(result).toBe('=CUBESET("Conn","{[A]}","Top ""Items""")');
    });
  });
});
