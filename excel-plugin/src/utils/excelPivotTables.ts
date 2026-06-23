/// <reference types="office-js" />

export interface PivotFieldMapping {
  rowFields: string[];
  columnFields: string[];
  dataFields: string[];
  filterFields: string[];
}

function findHierarchy(hierarchyMap: Map<string, Excel.PivotHierarchy>, field: string): Excel.PivotHierarchy | undefined {
  // F-36: Exact match only (case-insensitive, trimmed). Fuzzy includes() removed.
  const exact = hierarchyMap.get(field);
  if (exact) return exact;
  const normalised = field.toLowerCase().trim();
  for (const [name, hier] of hierarchyMap) {
    if (name.toLowerCase().trim() === normalised) return hier;
  }
  return undefined;
}

export async function insertPivotTableWithMapping(
  sourceRangeAddress: string,
  fieldMapping: PivotFieldMapping,
  baseSheetName?: string,
): Promise<Excel.PivotTable> {
  return await Excel.run(async (context) => {
    const sheets = context.workbook.worksheets;
    sheets.load('items/name');
    await context.sync();

    const desiredName = baseSheetName || 'Local Pivot';
    const existingNames = new Set(sheets.items.map(s => s.name));
    let sheetName = desiredName;
    let counter = 1;
    while (existingNames.has(sheetName)) {
      sheetName = `${desiredName} (${counter})`;
      counter++;
    }

    const newSheet = sheets.add(sheetName);
    newSheet.activate();

    const pivotRange = newSheet.getRange('A1');
    const pivotTable = newSheet.pivotTables.add(
      'TessalliteLocalPivot',
      sourceRangeAddress,
      pivotRange,
    );

    pivotTable.load('hierarchies');
    await context.sync();

    const hierarchies = pivotTable.hierarchies;
    hierarchies.load('items/name');
    await context.sync();

    const hierarchyMap = new Map<string, Excel.PivotHierarchy>();
    for (const item of hierarchies.items) {
      item.load('name');
    }
    await context.sync();
    for (const item of hierarchies.items) {
      hierarchyMap.set(item.name, item);
    }

    for (const field of fieldMapping.rowFields) {
      const hier = findHierarchy(hierarchyMap, field);
      if (hier) {
        pivotTable.rowHierarchies.add(hier);
      }
    }

    for (const field of fieldMapping.columnFields) {
      const hier = findHierarchy(hierarchyMap, field);
      if (hier) {
        pivotTable.columnHierarchies.add(hier);
      }
    }

    for (const field of fieldMapping.filterFields) {
      const hier = findHierarchy(hierarchyMap, field);
      if (hier) {
        pivotTable.filterHierarchies.add(hier);
      }
    }

    for (const field of fieldMapping.dataFields) {
      const hier = findHierarchy(hierarchyMap, field);
      if (hier) {
        pivotTable.dataHierarchies.add(hier);
      }
    }

    await context.sync();
    return pivotTable;
  });
}

export async function insertEmptyPivotTable(
  sourceRangeAddress: string,
  baseSheetName?: string,
): Promise<Excel.PivotTable> {
  return insertPivotTableWithMapping(
    sourceRangeAddress,
    { rowFields: [], columnFields: [], dataFields: [], filterFields: [] },
    baseSheetName,
  );
}
