# Backend Change Spec -- Alias Map Endpoint

Date: 2026-05-20
Status: Draft
Blocks: Excel Plugin P2-M5 (search across alias map)
Priority: Medium

---

## 1. Problem

The Excel plugin's Report Builder search matches against `display_name`, `name`, `description`, `display_folder`, and glossary synonyms. It does not match against user-defined aliases or common business abbreviations (e.g., "Rev" for "Revenue", "GM" for "Gross Margin"). Users who search using informal terms get no results.

## 2. Current State

### ORM Model -- ModelAliasMap exists but has no API

**File**: `shared/db/models.py:1861-1877`

```python
class ModelAliasMap(TenantBase):
    """Per-model phrase -> canonical attribute jsonb. Single row per model."""
    __tablename__ = "model_alias_maps"
    model_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("models.id", ondelete="CASCADE"), primary_key=True)
    alias_map: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    updated_at: Mapped[datetime] = ...
```

- One row per model. `alias_map` is a JSONB dict mapping `phrase -> canonical_attribute`.
- No CRUD endpoints exist.
- No population mechanism exists (no seed, no agent enrichment, no admin UI).

### Measure / Dimension tables

Neither `Measure` nor `Dimension` has an `aliases` field. The alias concept lives only in `ModelAliasMap`.

### Agent snapshot aliases

`AgentModelContext` has `calendar_aliases` and `dimension_aliases` but these are agent-internal snapshots, not the authoritative alias map.

## 3. Proposed Backend Change

### 3.1 Endpoint

`GET /api/v1/projects/{pid}/models/{mid}/alias-map`

Returns the alias map for a model:

```json
{
  "model_id": "uuid",
  "alias_map": {
    "Rev": "Revenue",
    "GM": "Gross Margin",
    "COGS": "Cost of Goods Sold",
    "YoY": "Year over Year"
  },
  "updated_at": "2026-05-20T00:00:00Z"
}
```

### 3.2 Endpoint

`PUT /api/v1/projects/{pid}/models/{mid}/alias-map`

Accepts:

```json
{
  "alias_map": {
    "Rev": "Revenue",
    "GM": "Gross Margin"
  }
}
```

Requires modeler or admin role.

### 3.3 Implementation

- Add route file `services/model-service/src/api/alias_map.py`
- Register router in `main.py`
- Query `ModelAliasMap` by `model_id`. If no row exists, create on first `PUT`.
- Validate that alias values reference existing `Measure.display_name` or `Dimension.display_name` on write (warn but do not reject if not found -- aliases may reference attributes not yet imported).

### 3.4 Tests

1. `GET /alias-map` with no existing row returns empty map
2. `PUT /alias-map` creates the row
3. `PUT /alias-map` updates existing row
4. `GET /alias-map` returns populated map after `PUT`
5. Viewer can read; only modeler/admin can write
6. Alias map scoped to model (cross-model isolation)

## 4. Files to Change

| File | Change |
|---|---|
| `services/model-service/src/api/alias_map.py` | New file: GET + PUT endpoints |
| `services/model-service/src/main.py` | Register router |
| `services/model-service/tests/test_alias_map.py` | New test file |

## 5. Plugin-Side Integration (After Backend Ships)

- Add `getAliasMap(projectId, modelId)` to `src/api/modelService.ts`
- Add `useAliasMap` hook to `src/hooks/useModel.ts`
- Merge alias keys into `filterBySearch` in `ReportBuilder.tsx` so searching "Rev" matches "Revenue"

---

*End of spec. 1 new route file, 1 test file, 0 schema changes (ORM model already exists).*
