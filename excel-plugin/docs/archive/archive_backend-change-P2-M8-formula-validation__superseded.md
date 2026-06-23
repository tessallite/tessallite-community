# Backend Change Spec -- Formula Validation Endpoint

Date: 2026-05-20
Status: Draft
Blocks: Excel Plugin P2-M8 (formula validation before CUBE insert)
Priority: Medium

---

## 1. Problem

The Excel plugin's `CubeFormulaWizard` inserts CUBEVALUE/CUBEMEMBER formulas without validating them. The wizard shows "Ready to insert" regardless of whether the formula is syntactically correct or semantically valid. Users discover errors only after the formula returns `#NAME?` or `#N/A` in Excel.

## 2. Current State

### Endpoint already exists but is not called from the plugin

**File**: `services/query-router/src/api/routes.py:352-365`

```python
@router.post("/validate", response_model=ValidateResponse)
async def validate_query(body: ExecuteRequest, ...) -> ValidateResponse:
    """Parse + bind without routing or executing."""
```

**Pipeline**: `_parse(body)` -> `bind_query_to_model()` -> optional `apply_persona_gate()` -> returns `ValidateResponse`

### ValidateResponse

```python
class ValidateResponse(BaseModel):
    ok: bool
    errors: list[str] = []
    warnings: list[str] = []
    requested_measures: list[str] = []
    requested_dimensions: list[str] = []
    query_fingerprint: Optional[str] = None
    filters: Optional[list[dict[str, Any]]] = None
    grain: list[str] = []
```

### ExecuteRequest (input)

```python
class ExecuteRequest(BaseModel):
    model_id: str
    raw_query: str
    protocol: str = "jdbc"
    dialect: Optional[str] = None
    include_hidden: bool = False
    persona_id: Optional[str] = None
```

## 3. What Is Missing

The endpoint exists and works. The gap is **plugin-side wiring** only. The plugin's `queryRouter.ts` already exports a `validateQuery` function:

```typescript
export async function validateQuery(query: string): Promise<...> {
  return apiClient.post('/api/v1/validate', { raw_query: query, ... });
}
```

But no UI component calls it. The `CubeFormulaWizard` skips validation.

## 4. No Backend Change Required

This item was miscategorized as "requires backend API change." The backend endpoint is fully implemented. The work is entirely frontend (plugin-side):

1. Call `validateQuery()` from `CubeFormulaWizard` before enabling the "Insert" button
2. Show validation errors inline in the wizard
3. Disable insert when `ok === false`

## 5. Files to Change (Plugin Only)

| File | Change |
|---|---|
| `src/components/CubeFunctions/CubeFormulaWizard.tsx` | Add validation step; call `validateQuery()` on formula preview; show errors |
| `src/api/queryRouter.ts` | Verify `validateQuery` function signature matches `ValidateResponse` |

---

*End of spec. No backend change needed -- endpoint exists and works.*
