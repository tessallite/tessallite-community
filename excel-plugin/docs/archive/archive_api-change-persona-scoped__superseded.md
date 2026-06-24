# API Change Spec -- Persona-Scoped Measures and Dimensions

Date: 2026-05-19
Status: Draft - security revision
Blocks: Excel Plugin M1 (full persona filtering), M2 (persona-filtered glossary), M3 (persona-filtered CUBE catalog)

---

## 1. Problem

The Excel plugin's persona switcher UI shows persona-scoped catalog intent, but the measure and dimension lists are not actually persona-scoped. The `usePersonaFiltered` hook currently returns full lists regardless of which persona is active.

This is not only a UX gap. In Tessallite, personas can prevent users from seeing specific data elements. A persona can restrict measures, dimensions, hierarchies, and the business view a user is allowed to use. Therefore `persona_id` must be treated as part of the authorization context, not as a client-side display filter.

The backend ORM (`Persona`) already stores allow-lists:

- `included_measure_ids`
- `included_dimension_ids`
- `included_hierarchy_ids`

The model-service list endpoints do not yet apply those allow-lists for the Excel plugin's metadata calls.

## 2. Current State

### Endpoints

| Endpoint | File |
|---|---|
| `GET /api/v1/projects/{pid}/models/{mid}/measures` | `services/model-service/src/api/measures.py` |
| `GET /api/v1/projects/{pid}/models/{mid}/dimensions` | `services/model-service/src/api/dimensions.py` |
| `GET /api/v1/projects/{pid}/models/{mid}/personas` | `services/model-service/src/api/personas.py` |

### ORM Model

`shared/db/models.py` -- `Persona` has:

- `included_measure_ids: JSONB` -- `list[str]` of UUIDs. Empty list means unrestricted for that element type.
- `included_dimension_ids: JSONB` -- same semantics for dimensions.
- `included_hierarchy_ids: JSONB` -- same semantics for hierarchies.

### Persona Response Schema

`shared/schemas/pydantic_models.py` -- `PersonaResponse` already returns:

- `included_measure_ids`
- `included_dimension_ids`
- `included_hierarchy_ids`

The Excel plugin type must include these fields if it consumes persona metadata directly.

## 3. Impact Analysis

### 3.1 Product impact

This change aligns the Excel plugin with Tessallite's core promise: business users can explore governed semantic data in Excel without seeing data elements outside their persona.

Expected positive impact:

- Persona switching becomes real, not cosmetic.
- Report Builder, drill-through, glossary/CUBE catalog, and Ask Tessallite can share one consistent persona context.
- Users no longer see measures/dimensions they cannot safely use.
- Excel workflows become safer for governed deployments because metadata discovery and execution can be aligned.

Product risk if implemented incorrectly:

- If invalid or omitted `persona_id` returns all metadata, the plugin can disclose hidden measures/dimensions.
- If metadata is filtered but execution is not, users may still query hidden elements manually or through stale workbook state.
- If Excel caches unrestricted metadata across persona switches, a user may see fields from a previous persona.

### 3.2 Backend impact

Affected backend services:

| Area | Impact |
|---|---|
| `model-service` metadata APIs | Add persona-aware filtering to measures, dimensions, and preferably hierarchies. |
| shared auth/current user context | Must expose locked persona context when present, or a reliable equivalent. |
| query execution/query-router path | Must receive and enforce the effective persona, not only client-requested persona. |
| drill-through | Must reject hidden measures/dimensions and conflicting persona context. |
| persona CRUD/listing | No schema change required, but persona availability policy must be clear. |
| tests | Add fail-closed security tests and compatibility tests. |

The biggest backend design decision is whether regular users may request any persona in a model or only assigned personas. If assigned-persona policy does not exist, this API still improves metadata scoping for selected personas, but it should not be described as a complete authorization boundary until assignment policy is implemented.

### 3.3 Excel plugin impact

Affected plugin areas:

| Area | Impact |
|---|---|
| `modelService.ts` | Metadata calls need optional `persona_id` query params. |
| `useModel.ts` | Hooks need `personaId` inputs and persona-aware query keys. |
| `usePersona.ts` | Current pass-through filtering must be removed or changed to consume server-scoped responses. |
| `types/tessallite.ts` | Persona type needs allow-list fields returned by backend. |
| `ReportBuilder` | Active persona must flow into metadata and execution. |
| `AskTessallite` / drill-through | Active persona must be included wherever user actions leave metadata browsing and execute work. |

The plugin must treat persona switches as cache-boundary events. It should clear or refetch stale metadata when persona changes.

### 3.4 Compatibility impact

The query parameter can remain optional to avoid breaking existing clients. Compatibility is acceptable only when these rules hold:

- Non-persona callers that are allowed base catalog access can omit `persona_id` and keep current behavior.
- Persona-locked callers cannot bypass persona restrictions by omitting `persona_id`.
- Invalid `persona_id` returns `403` or `404`, not full metadata.

This is therefore a backward-compatible API shape, but not a backward-compatible security interpretation. The previous "ignore invalid persona" behavior must not ship.

### 3.5 Performance impact

Expected overhead is small:

- One persona lookup per scoped metadata request.
- Additional `IN (...)` predicates when allow-lists are populated.
- Larger React Query cache surface because persona ID becomes part of metadata cache keys.

Potential performance risks:

- Very large allow-lists could create large SQL `IN` clauses. If personas commonly include thousands of IDs, consider using a normalized join table or PostgreSQL array/JSON containment strategy later.
- Frequent persona switching in Excel can increase metadata refetches. Cache per persona, but never reuse unrestricted metadata in scoped mode.

### 3.6 Security impact

Security posture improves if implemented fail-closed. It regresses if the client controls persona enforcement.

Primary threats addressed:

- Metadata disclosure through unrestricted measures/dimensions lists.
- Querying hidden fields after seeing them in cached plugin state.
- Persona mismatch attacks by changing `persona_id` in HTTP requests.
- Cross-model persona confusion.

Required security posture:

- Server resolves effective persona.
- Server rejects conflicts.
- Server preserves model scope enforcement.
- Server applies the same persona to metadata and execution.
- Client-side filtering is never treated as authorization.

### 3.7 Operational and rollout impact

Recommended rollout:

1. Backend helper and metadata endpoints.
2. Backend execution enforcement.
3. Excel plugin client and cache-key changes.
4. Acceptance test pass using at least one restrictive persona.

Rollout risk is medium because the feature touches authorization semantics and Excel caching. Ship with targeted tests before exposing it to business users.

## 4. Security Contract

`persona_id` may be optional at the HTTP API level for backward compatibility, but persona enforcement is not optional when the caller is persona-scoped.

The backend must resolve an effective persona server-side:

```text
effective_persona_id =
  locked persona from token/session, if present
  else requested persona_id, if provided
  else no persona only if the caller is allowed unrestricted/base catalog access
```

### Required Rules

1. If the authenticated user/session/token has a locked persona, the backend must use that persona even when the request omits `persona_id`.
2. If the authenticated user/session/token has a locked persona and the request supplies a different `persona_id`, return `403 Forbidden`.
3. If `persona_id` is supplied, it must belong to the same tenant and the same model as `model_id`.
4. If `persona_id` is supplied but does not exist, belongs to another model, belongs to another tenant, or is not available to the caller, return `403 Forbidden` or `404 Not Found`.
5. Invalid, missing, or mismatched persona input must never fall back to the full catalog.
6. Existing model-scope enforcement such as `enforce_model_scope(current_user, model_id)` must remain in place.
7. Metadata filtering must not be the only enforcement layer. Query execution, drill-through, glossary/CUBE catalog, and agent calls that use persona context must enforce the same effective persona.

### Explicit Non-Goal

This change must not rely on the Excel client to protect data. The client should send the active persona for correctness and caching, but the server is authoritative.

## 5. Proposed API Change

### 5.1 Measures

`GET /api/v1/projects/{pid}/models/{mid}/measures?persona_id={uuid}`

Behavior:

1. Authenticate caller and require at least viewer access.
2. Verify the model belongs to the project and the caller is allowed to access the model.
3. Resolve the effective persona using the security contract above.
4. Start with `select(Measure).where(Measure.model_id == model_id)`.
5. If effective persona has an empty `included_measure_ids`, return all measures for the model.
6. If effective persona has a populated `included_measure_ids`, return only measures whose IDs are in the allow-list.
7. Preserve existing ordering, response shape, and redundant partner enrichment.

### 5.2 Dimensions

`GET /api/v1/projects/{pid}/models/{mid}/dimensions?persona_id={uuid}`

Same behavior using `included_dimension_ids`.

### 5.3 Hierarchies

Personas also contain `included_hierarchy_ids`. The Excel plugin uses hierarchy metadata for report building and Pivot/CUBE workflows, so hierarchy scoping should be implemented in the same delivery slice unless a separate blocking issue is explicitly logged.

Recommended endpoint:

`GET /api/v1/projects/{pid}/models/{mid}/hierarchies?persona_id={uuid}`

Behavior:

1. Resolve the same effective persona.
2. If `included_hierarchy_ids` is empty, return all hierarchies for the model.
3. If populated, return only listed hierarchies.
4. Ensure dimensions returned with hierarchy payloads are also allowed by the effective persona when applicable.

## 6. Backend Implementation Guidance

### 6.1 Shared helper

Add a shared helper in the model-service API layer instead of duplicating persona checks in each endpoint.

Suggested shape:

```python
async def resolve_effective_persona(
    db: AsyncSession,
    *,
    current_user: CurrentUser,
    model_id: UUID,
    requested_persona_id: UUID | None,
) -> Persona | None:
    locked_persona_id = getattr(current_user, "persona_id", None)

    if locked_persona_id is not None:
        if requested_persona_id is not None and requested_persona_id != locked_persona_id:
            raise HTTPException(status_code=403, detail="Persona does not match authenticated context")
        persona_id = locked_persona_id
    else:
        persona_id = requested_persona_id

    if persona_id is None:
        return None

    result = await db.execute(
        select(Persona).where(
            Persona.id == persona_id,
            Persona.model_id == model_id,
        )
    )
    persona = result.scalar_one_or_none()
    if persona is None:
        raise HTTPException(status_code=404, detail="Persona not found for model")

    return persona
```

If the product has a user-to-persona assignment table or policy service, the helper must also verify that the requested persona is available to the caller. If that relationship does not exist yet, add a follow-up issue before shipping this as an authorization feature.

### 6.2 ID handling

Persona allow-lists are stored as JSON strings. Convert them to UUIDs before using `.in_(...)` against UUID columns.

```python
allowed_ids = [UUID(str(value)) for value in persona.included_measure_ids or []]
```

Invalid UUID values in persisted persona JSON should fail closed with a server warning and a `500` or validation error, not silently widen access.

### 6.3 Empty list semantics

Empty allow-list means unrestricted for that catalog type. This is existing persona semantics and should be preserved:

- `included_measure_ids == []` -> all measures for the effective model
- `included_dimension_ids == []` -> all dimensions for the effective model
- `included_hierarchy_ids == []` -> all hierarchies for the effective model

Do not confuse empty allow-list with "return nothing".

## 7. Client-Side Integration (Excel Plugin)

### 7.1 API client

Update `src/api/modelService.ts`:

```typescript
export async function getMeasures(
  projectId: string,
  modelId: string,
  personaId?: string | null,
): Promise<Measure[]> {
  const params = personaId ? `?persona_id=${encodeURIComponent(personaId)}` : '';
  return apiClient.get<Measure[]>(
    `/api/v1/projects/${projectId}/models/${modelId}/measures${params}`,
  );
}

export async function getDimensions(
  projectId: string,
  modelId: string,
  personaId?: string | null,
): Promise<Dimension[]> {
  const params = personaId ? `?persona_id=${encodeURIComponent(personaId)}` : '';
  return apiClient.get<Dimension[]>(
    `/api/v1/projects/${projectId}/models/${modelId}/dimensions${params}`,
  );
}
```

Apply the same pattern to hierarchy metadata if hierarchy scoping is included in the delivery slice.

### 7.2 React Query keys

Thread `personaId` through `useMeasures`, `useDimensions`, and hierarchy hooks. Include it in query keys:

```typescript
['measures', projectId, modelId, personaId ?? 'base']
['dimensions', projectId, modelId, personaId ?? 'base']
```

This prevents cached unrestricted metadata from being reused after the user switches persona.

### 7.3 Persona type

Update the Excel plugin `Persona` type to include:

```typescript
included_measure_ids: string[];
included_dimension_ids: string[];
included_hierarchy_ids: string[];
```

Do not depend on `measure_count` or `dimension_count` unless the backend explicitly returns those fields.

### 7.4 `usePersonaFiltered`

Remove identity filtering from `usePersonaFiltered` or reduce it to a thin helper around already-scoped API responses. The filtered lists should come from the server response, not from local client filtering.

### 7.5 Active persona propagation

The plugin should pass the active persona ID consistently to:

- measures list
- dimensions list
- hierarchy list, if implemented
- report execution/query calls
- drill-through
- Ask Tessallite/agent calls
- glossary/CUBE catalog calls when those are added

## 8. Validation

### Existing tests must still pass

When the caller is not persona-locked and omits `persona_id`, behavior remains compatible with existing clients.

### Required backend tests

1. `GET /measures?persona_id=<valid>` with a persona that has 3 of 10 measure IDs returns exactly 3 measures.
2. `GET /measures?persona_id=<valid>` with empty `included_measure_ids` returns all measures for the model.
3. `GET /dimensions?persona_id=<valid>` with populated `included_dimension_ids` returns only those dimensions.
4. `GET /dimensions?persona_id=<valid>` with empty `included_dimension_ids` returns all dimensions for the model.
5. `GET /measures?persona_id=<nonexistent>` returns `404` or `403`, not all measures.
6. `GET /measures?persona_id=<persona_from_different_model>` returns `404` or `403`, not all measures.
7. Persona-locked user omitting `persona_id` still receives persona-scoped metadata.
8. Persona-locked user sending a conflicting `persona_id` receives `403`.
9. Model-scope enforcement still rejects out-of-scope model access.
10. Invalid UUID strings in persona allow-list do not widen access.
11. Hierarchy endpoint applies `included_hierarchy_ids`, if hierarchy scoping ships in this slice.
12. Query execution path rejects or overrides conflicting persona context.

### Required plugin tests

1. `getMeasures` includes `persona_id` when active persona is selected.
2. `getDimensions` includes `persona_id` when active persona is selected.
3. Query keys include `personaId`.
4. Switching persona refetches measures and dimensions.
5. Cached unrestricted metadata is not reused for persona-scoped mode.
6. Report execution includes active persona context.

## 9. Scope Exclusions

These are excluded only if explicitly tracked as follow-up items:

- Persona assignment policy if the platform does not yet record which personas a user may select.
- Gateway JDBC/XMLA virtual catalog behavior, if it already enforces personas independently.
- Full glossary and CUBE catalog persona filtering, unless needed for the current Excel plugin milestone.

The following are not acceptable exclusions for M1:

- Returning all metadata when `persona_id` is invalid.
- Treating client-side persona filtering as an authorization boundary.
- Filtering measures/dimensions but leaving report execution unrestricted.

## 10. Files to Change

| File | Change |
|---|---|
| `tessallite/services/model-service/src/api/measures.py` | Add `persona_id`, resolve effective persona, apply `included_measure_ids` |
| `tessallite/services/model-service/src/api/dimensions.py` | Add `persona_id`, resolve effective persona, apply `included_dimension_ids` |
| `tessallite/services/model-service/src/api/hierarchies.py` | Recommended: add `persona_id`, resolve effective persona, apply `included_hierarchy_ids` |
| `tessallite/services/model-service/src/api/persona_scope.py` or equivalent | Recommended shared effective-persona helper |
| `tessallite/services/model-service/tests/` | Add security and scoping tests listed above |
| `tessallite/excel-plugin/src/api/modelService.ts` | Thread `personaId` into metadata calls |
| `tessallite/excel-plugin/src/hooks/useModel.ts` | Add `personaId` to hooks and query keys |
| `tessallite/excel-plugin/src/hooks/usePersona.ts` | Remove pass-through filtering or convert to server-scoped helper |
| `tessallite/excel-plugin/src/types/tessallite.ts` | Add persona allow-list fields |
| `tessallite/excel-plugin/src/components/ReportBuilder/ReportBuilder.tsx` | Pass active persona through metadata and execution flows |

---

*End of API change spec. This revision allows an optional `persona_id` for compatibility, but requires server-side effective-persona enforcement so persona omission or mismatch cannot widen access.*
