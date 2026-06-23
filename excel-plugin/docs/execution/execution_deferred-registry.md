# Excel Plugin -- Deferred, Descoped, and Unimplemented Work Registry

Date: 2026-05-20
Scope: Phases 0, 1, 2, 3
Source: Review reports (14 rounds total) + source code audit

---

## Summary

| Phase | Total Deferred | Implemented Later | Still Open | Partial |
|---|---|---|---|---|
| Phase 0 | 3 | 0 | 3 | 0 |
| Phase 0-1 | 16 | 8 | 7 | 0 |
| Phase 2 | 13 | 0 | 11 | 2 |
| Phase 3 | 10 | 0 | 9 | 1 |
| **Total** | **42** | **8** | **30** | **3** |

---

## Category A: Requires Backend API Change

These items cannot be resolved in the plugin alone. Each requires a new or modified endpoint in a backend service.

| ID | Title | Severity | Blocker | Phase |
|---|---|---|---|---|
| P3-M1 | Full persona measure/dimension filtering | HIGH | `GET /measures?persona_id=` and `GET /dimensions?persona_id=` endpoints needed. Spec written: `Tessallite-API-Change-specs.md` | Phase 3 |
| P3-M2 | Persona-filtered glossary lookup | MEDIUM | Depends on P3-M1 | Phase 3 |
| P3-M3 | Persona-filtered CUBE formula catalog | MEDIUM | Depends on P3-M1 | Phase 3 |
| P2-M5 | Search across alias map | MEDIUM | No alias-map API endpoint exists in model-service | Phase 2 |
| P2-M8 | Formula validation before insert | MEDIUM | `POST /api/v1/validate` exists but returns 404; endpoint not implemented in query-router | Phase 2 |
| P3-M7 | Agent-provided chart type hints | MEDIUM | Requires agent response metadata contract change in agent-service | Phase 3 |
| P3-M8 | DrillThroughSet column configuration | MEDIUM | `GET .../drill-through-set` endpoint not available in query-router | Phase 3 |

---

## Category B: Requires Office.js or Excel Host

These items require either Excel desktop for testing or Office.js APIs not in the standard type definitions.

| ID | Title | Severity | Blocker | Phase |
|---|---|---|---|---|
| P0-T2 | Verify Office host support | HIGH | Requires physical Excel desktop host (Windows/Mac/Web). Compatibility matrix is all `?` placeholders | Phase 0 |
| P0-T3 | Run `runCompatibilitySpike()` | HIGH | Function exists but never executed. Requires Excel desktop host | Phase 0 |
| P0-T4 | Compatibility matrix | HIGH | Template only. Depends on P0-T2/T3 execution | Phase 0 |
| P2-M9 | XMLA connection check before CUBE insert | HIGH | `workbook.connections.load()` not in standard Office.js type definitions | Phase 2 |
| P3-M5 | Context menu integration | MEDIUM | Requires `ExtensionPoint xsi:type="ContextMenu"` in manifest.xml | Phase 3 |
| P1-DEF-9 | Context menu items | LOW | Same as P3-M5. No `<ExtensionPoint xsi:type="ContextMenu">` in manifest | Phase 1 |

---

## Category C: UI Not Built (No External Blocker)

Backend API functions and TypeScript types exist but no UI component consumes them.

| ID | Title | Severity | What Exists | What Is Missing | Phase |
|---|---|---|---|---|---|
| P1-DEF-8 | Query trace modal | LOW | Nothing | Entire modal component | Phase 1 |
| P1-DEF-11 | Follow-up suggestion chips | LOW | Nothing | Chip rendering in ChatPanel after agent response | Phase 1 |
| P1-DEF-12 | Conversation management | LOW | API layer (`getConversations`, `deleteConversation`) | Conversation list, history panel, switch UI | Phase 1 |
| P1-DEF-13 | Number formatting from response metadata | LOW | `format` field in `ExecuteResponse.annotation.measures` | `range.numberFormat` call in `useExcel.ts` | Phase 1 |
| P1-DEF-14 | Loading skeletons | LOW | Nothing (only `CircularProgress` used) | MUI `<Skeleton>` components for all loading states | Phase 1 |
| P1-DEF-15 | Reduced motion support | LOW | Nothing | `prefers-reduced-motion` media query; reduced-motion fallbacks for animations | Phase 1 |
| P1-DEF-16 | Diagnostics module with redaction | MEDIUM | Nothing | `src/utils/diagnostics.ts` with password/JWT/connection-string redaction | Phase 1 |
| P2-M6 | Measure card expanded view | MEDIUM | `MeasureCard.tsx` renders collapsed card only | Expanded state with aggregation, lineage, cross-model badge, semi-additive, glossary definition | Phase 2 |
| P2-M7 | Dimension "Preview members" link | LOW | `discoverMembers` API in `queryRouter.ts` | UI link/button + member browser in `DimensionCard.tsx` | Phase 2 |
| P2-M13 | Measure grouping by `display_folder` | LOW | `display_folder` field on `Measure` type | Grouped rendering with folder headers | Phase 2 |
| P2-#5 | CUBEVALUE member selector | HIGH | `generateCubeValue` accepts `filterExpressions[]` | Member picker UI + `discoverMembers` call in `CubeFormulaWizard` | Phase 2 |
| P2-#9 | Filter zone member selection UI | HIGH | Filter zone chips in `ZoneMappingGrid` | Member value picker, operator editor, `discoverMembers` integration | Phase 2 |

---

## Category D: Partially Implemented

| ID | Title | Severity | What Is Done | What Is Missing | Phase |
|---|---|---|---|---|---|
| P2-M14 | Variant measure indentation | LOW | Variant chip badge shows `variant of {base_measure_id}` | No visual indentation under base measure; no sorting to group variants with their base | Phase 2 |
| P2-#25 | Table metadata incomplete | MEDIUM | `TableMetadata` interface has `projectId`, `modelId`, `semanticQuery` fields | `doInsertAndTag` only passes `pluginVersion` and `timestamp`; other fields never populated | Phase 2 |
| P3-M4 | Breadcrumb up-level navigation | MEDIUM | Breadcrumb renders with clickable segments | Up-level drill navigation needs drill-options per level from backend | Phase 3 |

---

## Category E: Structural / Performance / Cosmetic

| ID | Title | Severity | Notes | Phase |
|---|---|---|---|---|
| P2-M10 | Virtualized lists (`react-window`) | MEDIUM | Package installed but never imported. All lists use plain `.map()`. Deferred to production hardening phase | Phase 2 |
| P2-M11 | Metadata caching by `deployed_version_id` | LOW | Cache keys do not include version. TanStack Query handles staleness but no version-based invalidation | Phase 2 |
| P2-#27 | `HierarchyLibrary` as separate component file | LOW | Hierarchies rendered inline in `ReportBuilder.tsx` | Phase 2 |
| P2-#28 | `MeasureLibrary`/`DimensionLibrary` as separate files | LOW | All rendering in `ReportBuilder.tsx` (currently 414 lines) | Phase 2 |
| P3-M6 | Ribbon integration for Phase 3 features | LOW | Manifest-only change. No ribbon buttons for Chart, Pivot, Drill | Phase 3 |
| P3-16 | Multi-measure CUBE drill-through | LOW | Only first `[Measures]` match extracted. Known limitation, no fix planned | Phase 3 |
| P1-DEF-10 | Manifest GUID placeholder | LOW | `a1b2c3d4-...` is intentional for dev. Must replace before AppSource/enterprise deployment | Phase 1 |

---

## What Was Successfully Recovered

These Phase 0-1 items were deferred and then fully implemented in Phases 2-3:

| ID | Title | Implemented In |
|---|---|---|
| P1-DEF-1 | Report Builder (zone mapping, libraries) | Phase 2 |
| P1-DEF-2 | Report templates | Phase 2 |
| P1-DEF-3 | Cube Function Wizard | Phase 2 |
| P1-DEF-4 | Live connection helper | Phase 2 |
| P1-DEF-5 | Glossary search modal | Phase 2 |
| P1-DEF-6 | Persona switcher | Phase 3 |
| P1-DEF-7 | Drill-through panel | Phase 3 |

---

## Recommended Prioritization

### Must Fix Before Production (blocks users)

1. **P0-T2/T3/T4** -- Compatibility matrix must be populated. Requires manual testing on Excel desktop.
2. **P1-DEF-10** -- Manifest GUID must be replaced with a real GUID before any distribution.
3. **P3-M1** -- Persona filtering. API spec already written.
4. **P1-DEF-16** -- Diagnostics module. Needed for production support.

### High Value, Low Effort

5. **P2-#25** -- Populate table metadata (just wire existing fields through `doInsertAndTag`).
6. **P1-DEF-13** -- Apply number formatting from annotation (single `range.numberFormat` call).
7. **P1-DEF-14** -- Replace `CircularProgress` with `Skeleton` components.
8. **P2-M14** -- Variant indentation (sorting + CSS left margin).

### High Value, Medium Effort

9. **P2-#5** -- CUBEVALUE member selector (UI for `discoverMembers`).
10. **P2-#9** -- Filter zone member selection UI.
11. **P1-DEF-12** -- Conversation management UI (API already wired).
12. **P2-M6** -- Measure card expanded view.

### Can Defer Further

13. P1-DEF-8 (query trace), P1-DEF-11 (suggestion chips), P1-DEF-15 (reduced motion)
14. P2-M5 (alias map), P2-M10 (virtualization), P2-M11 (version caching), P2-M13 (folder grouping)
15. P3-M5/M6 (context menu/ribbon), P2-#27/28 (structural refactor)

---

*End of deferred work registry. 30 items still open, 3 partial, 8 recovered. 7 items require backend changes, 6 require Excel host or Office.js, 12 have no external blocker.*
