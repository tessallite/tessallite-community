/**
 * Excel formula generation utilities for CUBE functions.
 * Phase 2: Used by the Cube Function Wizard.
 */

export interface CubeFormulaInput {
  connectionName: string;
  measureExpression: string;
  filterExpressions?: string[];
}

/**
 * Canonical Excel workbook connection name for the Tessallite XMLA endpoint.
 * F-025-10: every path that creates, references, or inserts a CUBE/KPI formula
 * MUST use this exact literal. LiveConnectionWizard creates the connection
 * named "Tessallite"; a status/trend formula bound to any other name (e.g. the
 * old "Tessallite <serverUrl>") references a connection that never exists.
 */
export const TESSALLITE_CONNECTION_NAME = 'Tessallite';

/**
 * Build the XMLA Initial Catalog name the gateway publishes for a model.
 * F-025-07: the gateway's MDSCHEMA_CATALOGS lists `<model slug>` and, per
 * persona, `<model slug>_<persona slug>` — never the model UUID. A connection
 * string built from the UUID points at a catalog that is absent from the
 * connect dialog. When a persona is active, the persona-suffixed catalog gives
 * the analyst the persona-scoped shape; otherwise the base business catalog.
 */
export function buildXmlaCatalogName(modelSlug: string, personaSlug?: string | null): string {
  const base = (modelSlug || '').trim();
  const persona = (personaSlug || '').trim();
  return persona ? `${base}_${persona}` : base;
}

export function escapeExcelString(value: string): string {
  return value.replace(/"/g, '""');
}

/**
 * Escape a name for use inside MDX brackets: ']' -> ']]'.
 * Example: escapeMdxBracketContent("Revenue]Total") -> "Revenue]]Total"
 */
export function escapeMdxBracketContent(name: string): string {
  return name.replace(/\]/g, ']]');
}

/** @deprecated Use escapeMdxBracketContent instead */
export const escapeMdxIdentifier = escapeMdxBracketContent;

/**
 * Produce a fully-qualified MDX member reference: [Dimension].[Hierarchy].[Member]
 */
export function qualifyMdxMember(dimension: string, hierarchy: string, member: string): string {
  return `[${escapeMdxBracketContent(dimension)}].[${escapeMdxBracketContent(hierarchy)}].[${escapeMdxBracketContent(member)}]`;
}

/**
 * Build an escaped `[Measures].[<name>]` reference from a technical measure name.
 * F-025-09: a measure name containing ']' (rare but legal) breaks the formula
 * unless the bracket content is escaped. Use this everywhere a measure member
 * is emitted instead of inlining `[Measures].[${name}]`.
 */
export function measureMemberRef(measureName: string): string {
  return `[Measures].[${escapeMdxBracketContent(measureName)}]`;
}

/**
 * Build an escaped dimension member reference in the gateway's published
 * caption grammar: `[<dim>].[<dim>].[<member>]`.
 * F-025-09: each segment is bracket-escaped. The gateway publishes member
 * unique names in exactly this caption form (verified against MDSCHEMA_MEMBERS),
 * so an escaped technical-name reference binds on refresh.
 */
export function dimensionMemberRef(dimensionName: string, memberKey: string): string {
  const dim = escapeMdxBracketContent(dimensionName);
  return `[${dim}].[${dim}].[${escapeMdxBracketContent(memberKey)}]`;
}

/**
 * Generate a CUBEMEMBER formula.
 */
export function generateCubeMember(
  connectionName: string,
  memberExpression: string,
): string {
  return `=CUBEMEMBER("${escapeExcelString(connectionName)}","${escapeExcelString(memberExpression)}")`;
}

/**
 * Generate a CUBEVALUE formula.
 */
export function generateCubeValue(
  connectionName: string,
  measureExpression: string,
  filterExpressions: string[] = [],
): string {
  const parts = [
    `"${escapeExcelString(connectionName)}"`,
    `"${escapeExcelString(measureExpression)}"`,
    ...filterExpressions.map(f => `"${escapeExcelString(f)}"`),
  ];
  return `=CUBEVALUE(${parts.join(',')})`;
}

/**
 * Generate a CUBESET formula.
 */
export function generateCubeSet(
  connectionName: string,
  setExpression: string,
  caption?: string,
): string {
  const captionPart = caption ? `,"${escapeExcelString(caption)}"` : '';
  return `=CUBESET("${escapeExcelString(connectionName)}","${escapeExcelString(setExpression)}"${captionPart})`;
}

/**
 * Generate a CUBERANKEDMEMBER formula.
 * Returns the Nth member from a set defined by a CUBESET formula.
 */
export function buildCubeRankedMemberFormula(
  connectionName: string,
  setCellRef: string,
  rank: number,
): string {
  return `=CUBERANKEDMEMBER("${escapeExcelString(connectionName)}",${setCellRef},${rank})`;
}

export type CubeKpiProperty = 1 | 2 | 3 | 4 | 5;

export const CUBE_KPI_PROPERTIES: Record<string, CubeKpiProperty> = {
  Value: 1,
  Goal: 2,
  Status: 3,
  Trend: 4,
  Weight: 5,
};

/**
 * Generate a CUBEKPIMEMBER formula for a KPI property.
 * property: 1=Value, 2=Goal, 3=Status, 4=Trend, 5=Weight
 *
 * NOTE: a bare CUBEKPIMEMBER returns the KPI MEMBER OBJECT, which Excel
 * renders as the member's CAPTION text (e.g. "Shipping Cost"), NOT the
 * numeric value. To obtain the actual number, wrap CUBEKPIMEMBER inside
 * CUBEVALUE -- see `buildCubeKpiValueFormula`. Use the bare form only
 * when the member reference itself is needed (e.g. as a filter argument
 * to another CUBE function).
 */
export function buildCubeKpiFormula(
  connectionName: string,
  kpiName: string,
  property: CubeKpiProperty,
): string {
  // Bug-3659: CUBEKPIMEMBER takes the BARE KPI caption — Excel builds the KPI
  // member reference internally. Wrapping the name in literal brackets ("[aa]")
  // made Excel resolve a KPI literally named "[aa]". Emit the bare caption,
  // Excel-string-escaped only (no MDX bracket escaping).
  return `=CUBEKPIMEMBER("${escapeExcelString(connectionName)}","${escapeExcelString(kpiName)}",${property})`;
}

/**
 * Bug-6729: wrap a CUBEKPIMEMBER inside CUBEVALUE so Excel resolves the KPI
 * property's NUMERIC VALUE instead of rendering the member's caption text.
 *
 * A bare =CUBEKPIMEMBER("conn","kpi",1) displays the caption "Shipping Cost";
 * =CUBEVALUE("conn",CUBEKPIMEMBER("conn","kpi",1)) displays the number.
 * Same pattern for Status (property 3): bare CUBEKPIMEMBER shows
 * "Shipping Cost Status"; the wrapped form shows the normalised -1/0/1 value.
 *
 * This is the canonical KPI-property formula for every Value / Status cell
 * emitted by the scorecard, full row, and single-cell insert paths.
 */
export function buildCubeKpiValueFormula(
  connectionName: string,
  kpiName: string,
  property: CubeKpiProperty,
): string {
  const conn = escapeExcelString(connectionName);
  const name = escapeExcelString(kpiName);
  return `=CUBEVALUE("${conn}",CUBEKPIMEMBER("${conn}","${name}",${property}))`;
}

/**
 * Bug-6714 / Bug-6729: the formula for a KPI's Value cell, for ANY KPI shape.
 * A measure-backed KPI resolves through CUBEVALUE on its value measure; a
 * custom/expression KPI (value_measure_id null -- every KPI in the acme-demo
 * seed) has no measure to reference, so the Value comes from the KPI's own
 * Value property via CUBEVALUE(CUBEKPIMEMBER(...)) -- the CUBEVALUE wrap is
 * essential: a bare CUBEKPIMEMBER shows the member CAPTION text, not the
 * number (Bug-6729). Previously every multi-cell KPI insert (full row,
 * value+goal, scorecard) guarded the Value cell behind
 * `if (valueMeasureName)` and silently left it EMPTY for custom KPIs.
 * Single source so all Value cells stay consistent.
 */
export function kpiValueCellFormula(
  connectionName: string,
  kpiTechnicalName: string,
  valueMeasureName: string | null,
): string {
  return valueMeasureName
    ? generateCubeValue(connectionName, measureMemberRef(valueMeasureName))
    : buildCubeKpiValueFormula(connectionName, kpiTechnicalName, CUBE_KPI_PROPERTIES.Value);
}

/**
 * Bug-6729: the formula for a KPI's Status cell. Wraps CUBEKPIMEMBER Status
 * inside CUBEVALUE so Excel shows the normalised -1/0/1 numeric value the
 * icon-set conditional format needs, not the caption text
 * "Shipping Cost Status".
 */
export function kpiStatusCellFormula(
  connectionName: string,
  kpiTechnicalName: string,
): string {
  return buildCubeKpiValueFormula(connectionName, kpiTechnicalName, CUBE_KPI_PROPERTIES.Status);
}

/**
 * Build an MSOLAP connection string pointing to the Tessallite XMLA endpoint.
 * SECURITY: Password is NEVER included — Excel persists connection strings in .xlsx files.
 * Excel will prompt for credentials at connection time via Persist Security Info=False.
 */
export function buildMsolapConnectionString(
  serverUrl: string,
  catalog: string,
  username?: string,
): string {
  const xmlaUrl = serverUrl.replace(/\/$/, '') + '/api/v1/xmla/';
  let cs = `Provider=MSOLAP.8;Data Source=${xmlaUrl};Initial Catalog=${catalog}`;
  if (username) cs += `;User ID=${username}`;
  cs += ';Persist Security Info=False';
  return cs;
}
