# Excel Plugin -- Deferred Work: Ready vs Blocked Analysis

Date: 2026-05-20
Trigger: Backend persona-scoped API (`?persona_id=`) confirmed implemented for measures, dimensions, and hierarchies.

---

## Backend Persona API -- Verification

| Endpoint | Status | Evidence |
|---|---|---|
| `GET /measures?persona_id=` | Implemented | `measures.py:582,589-597` |
| `GET /dimensions?persona_id=` | Implemented | `dimensions.py:236,243-251` |
| `GET /hierarchies?persona_id=` | Implemented | `hierarchies.py:827,834-845` |
| `resolve_effective_persona` helper | Implemented | `_persona_scope.py:28-78` |
| `parse_allowed_ids` helper | Implemented | `_persona_scope.py:81-99` |
| Tests (17 test methods) | Implemented | `tests/test_persona_scope.py` (542 lines) |

---

## Ready to Implement Now (33 items)

### Previously Blocked by Backend -- Now Unblocked (3 items)

| ID | Title | Severity | What to Do | Phase |
|---|---|---|---|---|
| P3-M1 | Full persona measure/dimension/hierarchy filtering | HIGH | Wire `persona_id` into `modelService.ts`, `useModel.ts`, `usePersona.ts`, `types/tessallite.ts`, `ReportBuilder.tsx`. Server now returns scoped lists. | Phase 3 |
| P3-M2 | Persona-filtered glossary lookup | MEDIUM | Pass `persona_id` to glossary endpoint (if backend supports it) or filter locally using persona allow-lists from `PersonaResponse`. Depends on P3-M1. | Phase 3 |
| P3-M3 | Persona-filtered CUBE formula catalog | MEDIUM | Pass `persona_id` to CUBE metadata lookups. Depends on P3-M1. | Phase 3 |

### No External Blocker -- Ready (12 items)

| ID | Title | Severity | Phase |
|---|---|---|---|
| P1-DEF-8 | Query trace modal | LOW | Phase 1 |
| P1-DEF-11 | Follow-up suggestion chips | LOW | Phase 1 |
| P1-DEF-12 | Conversation management UI | LOW | Phase 1 |
| P1-DEF-13 | Number formatting from response metadata | LOW | Phase 1 |
| P1-DEF-14 | Loading skeletons (MUI `<Skeleton>`) | LOW | Phase 1 |
| P1-DEF-15 | Reduced motion support | LOW | Phase 1 |
| P1-DEF-16 | Diagnostics module with redaction | MEDIUM | Phase 1 |
| P2-M6 | Measure card expanded view | MEDIUM | Phase 2 |
| P2-M7 | Dimension "Preview members" link | LOW | Phase 2 |
| P2-M13 | Measure grouping by `display_folder` | LOW | Phase 2 |
| P2-#5 | CUBEVALUE member selector | HIGH | Phase 2 |
| P2-#9 | Filter zone member selection UI | HIGH | Phase 2 |

### Partially Implemented -- Ready to Complete (3 items)

| ID | Title | Severity | What Remains | Phase |
|---|---|---|---|---|
| P2-M14 | Variant measure indentation | LOW | Add sorting + CSS left margin | Phase 2 |
| P2-#25 | Table metadata incomplete | MEDIUM | Wire `projectId`/`modelId`/`semanticQuery` through `doInsertAndTag` | Phase 2 |
| P3-M4 | Breadcrumb up-level navigation | MEDIUM | Add drill-options per level (may need backend support -- verify) | Phase 3 |

### Structural / Performance / Cosmetic -- Ready (8 items)

| ID | Title | Severity | Phase |
|---|---|---|---|
| P2-M10 | Virtualized lists (`react-window`) | MEDIUM | Phase 2 |
| P2-M11 | Metadata caching by `deployed_version_id` | LOW | Phase 2 |
| P2-#27 | `HierarchyLibrary` as separate component file | LOW | Phase 2 |
| P2-#28 | `MeasureLibrary`/`DimensionLibrary` as separate files | LOW | Phase 2 |
| P3-M6 | Ribbon integration for Phase 3 features | LOW | Phase 3 |
| P3-16 | Multi-measure CUBE drill-through | LOW | Phase 3 |
| P1-DEF-10 | Manifest GUID placeholder replacement | LOW | Phase 1 |
| P3-M4 partial | Breadcrumb up-level (if backend already returns drill-options) | MEDIUM | Phase 3 |

---

## Still Blocked (8 items)

### Requires Backend API Change (4 items)

| ID | Title | Severity | Blocker | Phase |
|---|---|---|---|---|
| P2-M5 | Search across alias map | MEDIUM | No alias-map API endpoint in model-service | Phase 2 |
| P2-M8 | Formula validation before insert | MEDIUM | `POST /api/v1/validate` not implemented in query-router | Phase 2 |
| P3-M7 | Agent-provided chart type hints | MEDIUM | Requires agent-service response metadata contract change | Phase 3 |
| P3-M8 | DrillThroughSet column configuration | MEDIUM | `GET .../drill-through-set` endpoint not available in query-router | Phase 3 |

### Requires Office.js / Excel Desktop Host (4 items)

| ID | Title | Severity | Blocker | Phase |
|---|---|---|---|---|
| P0-T2 | Verify Office host support | HIGH | Requires physical Excel desktop (Windows/Mac/Web) | Phase 0 |
| P0-T3 | Run `runCompatibilitySpike()` | HIGH | Requires physical Excel desktop | Phase 0 |
| P0-T4 | Compatibility matrix | HIGH | Depends on P0-T2/T3 | Phase 0 |
| P2-M9 | XMLA connection check before CUBE insert | HIGH | `workbook.connections.load()` not in standard Office.js types | Phase 2 |

---

## Summary

| Status | Count |
|---|---|
| Ready to implement (backend unblocked + no blocker + partial + structural) | 26 |
| Still blocked (backend API needed) | 4 |
| Still blocked (Office.js / Excel host needed) | 4 |
| **Total remaining** | **34** |

---

## Recommended Execution Order

**Sprint 1 -- Persona filtering (unblocks M1/M2/M3):**
1. P3-M1: Wire `persona_id` into API client, hooks, types, ReportBuilder
2. P3-M2: Persona-filtered glossary
3. P3-M3: Persona-filtered CUBE catalog

**Sprint 2 -- High-severity gaps:**
4. P2-#5: CUBEVALUE member selector
5. P2-#9: Filter zone member selection UI
6. P2-#25: Complete table metadata
7. P1-DEF-16: Diagnostics module

**Sprint 3 -- UX polish:**
8. P2-M6: Measure card expanded view
9. P2-M7: Dimension preview members
10. P1-DEF-12: Conversation management
11. P1-DEF-13: Number formatting
12. P1-DEF-14: Loading skeletons

**Sprint 4 -- Remaining no-blocker items:**
13. P2-M14, P2-M10, P2-M11, P2-M13, P2-#27/28, P1-DEF-8/11/15, P3-M6, P3-16, P1-DEF-10

---

*End of ready vs blocked analysis. 26 items ready to implement, 8 still blocked.*
