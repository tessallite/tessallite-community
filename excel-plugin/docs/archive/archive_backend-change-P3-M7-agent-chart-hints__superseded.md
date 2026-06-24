# Backend Change Spec -- Agent Chart Type Hints in API Response

Date: 2026-05-20
Status: Draft
Blocks: Excel Plugin P3-M7 (agent-provided chart hints)
Priority: Medium

---

## 1. Problem

The Excel plugin cannot show the user what chart type the agent recommends. When the agent processes a query, it selects a chart type (via `auto` rules or `llm` tool call), renders it as HTML, and returns the HTML in `rendered_output`. But the structured chart type (e.g., `"bar"`, `"line"`, `"pie"`) is not exposed in the API response. The plugin would need this to insert the correct Excel chart type without running its own recommendation heuristic.

## 2. Current State

### TurnResponse (API response)

**File**: `services/agent-service/src/api/conversations.py:100-121`

```python
class TurnResponse(BaseModel):
    id, conversation_id, turn_index, user_message, answer_text,
    status, latency_ms, llm_plan, thought_summary, semantic_query,
    routed_sql, route, citations, user_feedback, judge_verdict,
    judge_reasoning, judge_metrics, guardrail_actions,
    usage_input_tokens, usage_output_tokens, rendered_output
```

No `chart_type` field.

### Chart selection

**File**: `services/agent-service/src/pipeline.py:668-700`

The pipeline selects a chart type via `select_chart_type()` (auto mode) or LLM tool call. The result is used to call `render_chart()`. The chart type string is consumed internally but never returned to the caller.

### Auto chart selector

**File**: `services/agent-service/src/charts/selector.py:67-100`

Returns: `"kpi"`, `"multi_line"`, `"line"`, `"grouped_bar"`, `"h_bar"`, `"pie"`, `"bar"`, or `None`.

## 3. Proposed Backend Change

### 3.1 Add `chart_type` field to TurnResponse

```python
class TurnResponse(BaseModel):
    # ... existing fields ...
    chart_type: Optional[str] = None  # "bar" | "line" | "pie" | "kpi" | "grouped_bar" | "h_bar" | "multi_line" | "stacked_bar" | None
```

### 3.2 Populate in pipeline

**File**: `services/agent-service/src/pipeline.py`

After chart selection (line ~680), store the chart type on the turn record:

```python
chart_type = select_chart_type(result, config.chart_max_rows)
# existing: render_chart(chart_type, ...)
# new: store chart_type on the turn for API response
```

### 3.3 Store in database

Add `chart_type` column to the `turns` table:

```python
# shared/db/models.py -- AgentTurn
chart_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
```

Alembic migration needed.

### 3.4 Return in API

The `TurnResponse` builder reads from the turn record and includes `chart_type`.

### 3.5 Tests

1. Auto-mode query with time series data returns `chart_type: "line"`
2. Auto-mode query with KPI data returns `chart_type: "kpi"`
3. LLM-mode query returns the LLM-selected `chart_type`
4. Query with no chart returns `chart_type: null`
5. Historical turns without chart_type return `null` (backward compatible)

## 4. Files to Change

| File | Change |
|---|---|
| `shared/db/models.py` | Add `chart_type` column to `AgentTurn` |
| `shared/db/migrations/versions/` | New migration: add `chart_type` column |
| `services/agent-service/src/api/conversations.py` | Add `chart_type` to `TurnResponse` |
| `services/agent-service/src/pipeline.py` | Store selected chart type on turn |
| `services/agent-service/tests/` | Test chart type in response |

## 5. Plugin-Side Integration (After Backend Ships)

- Read `chart_type` from `AgentMessage` in `types/tessallite.ts`
- In `App.tsx`, pass agent's `chart_type` to `excelInsertChart` instead of running local `recommendChartType` heuristic
- Fall back to local heuristic when `chart_type` is null

---

*End of spec. 1 new column, 1 migration, 1 response field addition.*
