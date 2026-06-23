# Phase 2 -- Review Round 2 Findings

Date: 2026-05-19
Scope: Verification of all fixes from `phase-2-review-fix-report.md` plus full re-scan of Phase 2 codebase.
Build: `tsc` zero errors, Vite zero warnings, 6/6 tests passing.

---

## 1. Fix Verification

| Round 1 Finding | Fix Claim | Verified | Notes |
|---|---|---|---|
| #1 Password in connection string | Removed function and test | YES | `buildMsolapConnectionStringWithAuth` gone from source and tests |
| #2 Dead email/password state | Removed dead state | YES | `LiveConnectionWizard.tsx` clean -- no `email`/`password` state |
| #3 Empty result crash | Added guard | YES | `ReportBuilder.tsx:131-134` -- `!result.data \|\| result.data.length === 0` guard |
| #4 targetCell ignored | Changed signature | PARTIAL | See Finding R2-1 below |
| #5 Invalid filter syntax | Removed filter step | YES | 2-step wizard now, no broken MDX |
| #6 XMLA connection check | Added informational text | YES | `CubeFormulaWizard.tsx:91-93` -- text shown. Runtime check deferred. |
| #7 Search scope | Added folder + synonyms | YES | `ReportBuilder.tsx:87` folder, `ReportBuilder.tsx:89-90` glossary synonyms |
| #8 Template duplicates | Added clearZones() | YES | `ReportBuilder.tsx:155` -- `clearZones()` first |
| #9 Invalid filter query | Removed filters from query | YES | `ReportBuilder.tsx:121-125` -- no `filters` field in SemanticQuery |
| #10 Unused personaId | Removed prop | YES | `ReportBuilderProps` now has 3 fields only |
| #11 Dead checkTemplatePrerequisites import | Removed import | YES | Not in `ReportBuilder.tsx` imports |
| #12 Dead TemplateAssignment | Removed interface | YES | `reportTemplates.ts` no longer has it |
| #13 Expanded state in MeasureCard | Added Phase 3 comment | YES | `MeasureCard.tsx:26` |
| #14 Virtualized lists | Installed react-window | YES | Not wired yet -- acceptable Phase 4 deferral |
| #15 Search debounce | Added 300ms debounce | YES | `ReportBuilder.tsx:42-46` |
| #16 XMLA path in connection string | Added /api/v1/xmla/ | YES | `excelFormulas.ts:54` |
| #17 Dead id prop in DimensionCard | Removed | YES | Not in props interface |
| #18 Glossary button unwired | Created GlossaryModal | YES | `App.tsx:485` onClick wired, `GlossaryModal.tsx` renders entries |
| #19 Missing Slicer button | Added | YES | `DimensionCard.tsx:89-96` |
| #20 Zone type duplication | Unified in tessallite.ts | YES | `tessallite.ts:5` exports `Zone`, both consumers import from there |
| #21 InsertActions phase markers | Updated to Phase 3 | YES | `InsertActions.tsx:8,14` |
| #22 Raw Excel.run without guard | Uses useExcel hook | PARTIAL | See Finding R2-2 below |
| #23 Step labels misleading | Renamed | YES | Start / Manual Setup / Instructions |
| #24 connections.add type cast | Added Excel undefined guard | YES | `LiveConnectionWizard.tsx:26` |
| #25 Metadata incomplete | Deferred | N/A | Accepted Phase 3 deferral |
| #26 Unnecessary React import | Removed | YES | `InsertActions.tsx` no longer imports React |

**Verification result: 25/26 verified. 2 PARTIAL (findings R2-1 and R2-2 below).**

---

## 2. New Findings from Round 2 Re-Scan

### R2-1 (HIGH): `handleInsertFormula` accepts `targetCell` but still ignores it

**File**: `src/components/ReportBuilder/ReportBuilder.tsx:180-186`
**Severity**: High -- Fix Incomplete

The fix report claims Finding #4 is fixed: the `onInsertFormula` signature now takes `(formula: string, targetCell: string)`. However, the receiving function discards `targetCell`:

```ts
const handleInsertFormula = useCallback((formula: string, targetCell: string) => {
    insertFormula(formula).then(() => {  // targetCell is unused
      showToast('Formula inserted', 'success');
    }).catch(() => {
      showToast('Formula insertion failed', 'error');
    });
  }, [insertFormula, showToast]);
```

The `insertFormula` function from `useExcel.ts:84-90` writes to `getSelectedRange()` (the active cell), not to the `targetCell` the user typed.

The user types "B5" in the wizard, sees "Ready to insert at B5", but the formula goes to whatever cell is active.

**Fix**: Use `getRange(targetCell)` instead of `getSelectedRange()`. Either add a `targetCell` parameter to `useExcel.insertFormula`, or resolve the range inside `handleInsertFormula` directly.

---

### R2-2 (MEDIUM): `useExcel.insertFormula` has no `typeof Excel === 'undefined'` guard

**File**: `src/hooks/useExcel.ts:84-90`
**Severity**: Medium -- Compatibility

The fix report claims Finding #22 is fixed because `ReportBuilder` now uses `useExcel.insertFormula` instead of raw `Excel.run`. But the `insertFormula` function itself has no guard:

```ts
const insertFormula = useCallback(async (formula: string): Promise<void> => {
    await Excel.run(async (context) => {  // crashes if Excel global is undefined
```

Same issue applies to `getActiveCellAddress` (line 92-99), `readCellValue` (line 101-116), and `createNewSheet` (line 118-137). None of these guard against `Excel` being undefined.

During browser dev (outside Excel), calling any of these will throw `ReferenceError: Excel is not defined`.

**Fix**: Add a guard at the top of each function:
```ts
if (typeof Excel === 'undefined') {
  throw new Error('Excel API not available. Open this add-in inside Excel.');
}
```

Or wrap the `Excel.run` call in a shared utility.

---

### R2-3 (LOW): Dead import -- `ClearIcon` in `ZoneMappingGrid.tsx`

**File**: `src/components/ReportBuilder/ZoneMappingGrid.tsx:2`
**Severity**: Low -- Dead Import

```ts
import { Clear as ClearIcon } from '@mui/icons-material';
```

`ClearIcon` is imported but never used in the component. The "Clear" text is rendered via MUI `Button`, not an icon.

---

### R2-4 (LOW): Dead import -- `IconButton` in `MeasureCard.tsx`

**File**: `src/components/ReportBuilder/MeasureCard.tsx:1`
**Severity**: Low -- Dead Import

```ts
import { Box, Typography, Chip, IconButton } from '@mui/material';
```

`IconButton` is imported but never used in the component.

---

### R2-5 (LOW): Dead import -- `IconButton` in `ZoneMappingGrid.tsx`

**File**: `src/components/ReportBuilder/ZoneMappingGrid.tsx:1`
**Severity**: Low -- Dead Import

```ts
import { Box, Typography, Chip, Button, IconButton } from '@mui/material';
```

`IconButton` is imported but never used.

---

### R2-6 (LOW): Dead imports -- `ExpandMore`, `ExpandLess` in `MeasureCard.tsx`

**File**: `src/components/ReportBuilder/MeasureCard.tsx:2`
**Severity**: Low -- Dead Import

```ts
import { Add as AddIcon, ExpandMore, ExpandLess } from '@mui/icons-material';
```

`ExpandMore` and `ExpandLess` are imported but never used. The `expanded` state exists but no expand/collapse UI is rendered (deferred to Phase 3).

---

### R2-7 (LOW): Dead import -- `Chip` in `InsertActions.tsx`

**File**: `src/components/AskTessallite/InsertActions.tsx:1`
**Severity**: Low -- Dead Import

```ts
import { Box, Button, Chip, Typography } from '@mui/material';
```

`Chip` is imported but never used.

---

### R2-8 (MEDIUM): `CubeFormulaWizard` `dimensions` prop removed but `dimensions` import type remains

**File**: `src/components/CubeFunctions/CubeFormulaWizard.tsx:10`
**Severity**: Low -- Minor inconsistency

The wizard no longer uses dimensions (the filter step was removed). The `Dimension` type import from `tessallite` was correctly removed. No issue here -- verified clean.

---

### R2-9 (MEDIUM): `GlossaryModal` renders outside `ThemeProvider` scope

**File**: `src/App.tsx:636-640`
**Severity**: Medium -- Theming Bug

```tsx
      </Box>

      <GlossaryModal
        open={glossaryOpen}
        onClose={() => setGlossaryOpen(false)}
        entries={glossaryEntries || []}
      />
    </ThemeProvider>
```

The `<GlossaryModal>` is a sibling of the main `<Box>` inside `<ThemeProvider>`, so it IS within the theme scope. However, it is rendered AFTER the closing `</Box>` of the root layout but still inside `<ThemeProvider>`. This is fine structurally. But the MUI `Dialog` component renders via a React portal into `document.body`, which is OUTSIDE the `<ThemeProvider>` tree. This means MUI Dialog components inside `GlossaryModal` will not inherit the custom theme (colors, typography, etc.).

**Fix**: Either:
1. Wrap `GlossaryModal`'s content in its own `<ThemeProvider theme={theme}>`, or
2. Move `GlossaryModal` inside the root `<Box>`, or
3. Use MUI's `styled` approach that doesn't rely on context inheritance for portals.

This affects ALL dialogs in the plugin: `TemplatePicker`, `CubeFormulaWizard`, `LiveConnectionWizard` all use `Dialog` which portals to `document.body`.

---

### R2-10 (LOW): `ZoneMappingGrid` unnecessary cast on line 108

**File**: `src/components/ReportBuilder/ZoneMappingGrid.tsx:108`
**Severity**: Low -- Unnecessary Code

```ts
<ChipList items={rows as ZoneItem[]} onRemove={onRemove} />
```

`rows` is already `ZoneItem[]` (it's `items.filter(...)` where items is `ZoneItem[]`). The `as ZoneItem[]` cast is redundant. All other `ChipList` usages (lines 84, 93, 99) don't have this cast.

---

## 3. Summary

| Category | Count |
|---|---|
| High (fix incomplete) | 1 |
| Medium | 2 |
| Low (dead imports / minor) | 6 |
| **Total new findings** | **9** |

### Actionable items for next fix round:

1. **R2-1** (HIGH): Wire `targetCell` through to the actual Excel range write. The signature change was cosmetic.
2. **R2-2** (MEDIUM): Add `typeof Excel` guards to all 4 `useExcel` functions that call `Excel.run`.
3. **R2-9** (MEDIUM): All `Dialog` components portal outside `ThemeProvider`. Add a theme wrapper inside each dialog or at the portal level.
4. **R2-3 through R2-7** (LOW): Remove 5 dead imports across `ZoneMappingGrid.tsx`, `MeasureCard.tsx`, and `InsertActions.tsx`.
5. **R2-10** (LOW): Remove unnecessary `as ZoneItem[]` cast.

---

*End of Phase 2 Round 2 review. 25/26 original fixes verified. 9 new findings (1 high, 2 medium, 6 low).*
