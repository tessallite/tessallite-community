# Backend Change Spec -- DrillThroughSet Column Configuration API

Date: 2026-05-20
Status: Draft
Blocks: Excel Plugin P3-M8 (DrillThroughSet column configuration in UI)
Priority: Medium

---

## 1. Problem

The Excel plugin's drill-through panel shows detail columns determined automatically by the backend. There is no UI for users to see which columns are configured for a measure's drill-through, or to change them. The backend already has full DrillThroughSet CRUD but the plugin does not consume it.

## 2. Current State

### ORM Model -- fully implemented

**File**: `shared/db/models.py:603-641`

```python
class DrillThroughSet(TenantBase):
    __tablename__ = "drill_through_sets"
    id, measure_id, source_table_id, detail_columns (JSONB),
    joined_dimension_ids (JSONB), row_limit_override, source_join_path (JSONB)
```

### CRUD API -- fully implemented in model-service

**File**: `services/model-service/src/api/measures.py:1235-1350`

| Method | Path | Description |
|---|---|---|
| GET | `/{measure_id}/drill-through-set` | Read config |
| PATCH | `/{measure_id}/drill-through-set` | Update config |
| DELETE | `/{measure_id}/drill-through-set` | Reset to defaults |
| POST | `/{measure_id}/drill-through-set/join-paths` | Compute join paths |

### Pydantic schemas -- fully implemented

**File**: `shared/schemas/pydantic_models.py:1409-1484`

- `DrillThroughSetUpdate`
- `DrillThroughSetResponse`
- `DrillJoinPathsResponse`

### Drill-through execution -- fully implemented

**File**: `services/query-router/src/api/drill_routes.py:191-205`

## 3. No Backend Change Required

This item was miscategorized as "requires backend API change." The entire backend is implemented:

- ORM model exists
- CRUD endpoints exist
- Schemas exist
- Execution pipeline exists

The gap is **plugin-side wiring** only:

1. Add `getDrillThroughSet(projectId, modelId, measureId)` to `src/api/modelService.ts`
2. Add `useDrillThroughSet` hook
3. Show configured detail columns in `DrillPanel` when a measure is selected
4. Optional: add a column picker UI for curating which columns appear

## 4. Files to Change (Plugin Only)

| File | Change |
|---|---|
| `src/api/modelService.ts` | Add `getDrillThroughSet`, `updateDrillThroughSet` |
| `src/hooks/useModel.ts` | Add `useDrillThroughSet` hook |
| `src/components/DrillThrough/DrillPanel.tsx` | Show configured columns; optional column picker |
| `src/types/tessallite.ts` | Add `DrillThroughSetResponse` type |

---

*End of spec. No backend change needed -- CRUD API and execution pipeline already fully implemented.*
