# Named Lists and KPIs with MDX Expressions — Teeallite front end Excel plugin Specification

**Product area:** Excel Add-in / Semantic Analytics Layer  
**Audience:** Product, Front-End, Back-End, Data Platform, QA  
**Status:** Draft for implementation planning  
**Author:** Product/Architecture Specification  
**Primary goal:** Allow users to create, reuse, preview, insert, and govern business-friendly **Named Lists** and **KPIs** that compile to MDX expressions, without forcing most users to understand MDX.

---

## 1. Executive Summary

The tesssallite front end and Teeallite front end Excel plugin must support two related semantic asset types:

1. **Named Lists** — reusable business selections that compile to MDX set expressions.
2. **KPIs** — reusable business performance indicators that compile to MDX value, goal, status, and trend expressions.

The tesssallite front end and Teeallite front end Excel plugin must support reusable business lists such as:

- Top 20 Customers by Revenue
- Active UK Corporate Accounts
- High-Risk Merchants This Month
- Branches Below Target
- Manually Selected VIP Customers
- Products with Declining Sales

It must also support governed KPIs such as:

- Total Transaction Count This Year
- Revenue vs Target
- Customer Growth Rate
- Fraud Rate vs Threshold
- Processing SLA Health
- Margin Performance

Internally, Named Lists are represented as MDX set expressions or semantic definitions that compile to MDX. Externally, they are presented as **Named Lists**: governed, reusable, business-friendly objects that can be inserted into Excel reports, used as filters, used as row/column selectors, or referenced by formulas.

Internally, KPIs are represented as MDX measure references and MDX expressions. Externally, they are presented as **business KPIs** with friendly names, targets, traffic-light status, trend arrows, display folders, descriptions, ownership, and governance metadata.

The design must hide MDX from standard business users, expose a guided builder for analysts, and provide an advanced MDX editor only for authorised power users/admins.

The system should treat Named Lists as first-class semantic-layer assets, not as Excel-only workbook artefacts. This allows the same list to be reused later by Excel, web analytics, APIs, scheduled reports, and conversational analytics agents.

---

## 2. Core Product Principle

The user should think:

> “I am choosing a trusted business list.”

Not:

> “I am writing an MDX named set.”

The platform should therefore expose:

| Technical Concept           | User-Facing Concept          |
| --------------------------- | ---------------------------- |
| MDX named set               | Named List                   |
| Set expression              | List Rule                    |
| Member                      | Item                         |
| Tuple                       | Combination                  |
| Static set                  | Fixed List                   |
| Dynamic set                 | Recalculating List           |
| Query-scoped set            | Temporary List               |
| Session/workbook-scoped set | Workbook List                |
| Cube/model-scoped set       | Shared List / Certified List |

---

## 3. Objectives

### 3.1 Functional Objectives

The Teeallite front end Excel plugin and backend must allow users to:

1. Browse available Named Lists.
2. Preview the items returned by a Named List.
3. Insert a Named List into Excel as:
   - A filter/slicer input.
   - A row list.
   - A column list.
   - A helper cell/set reference.
   - A reusable workbook parameter.
4. Create simple Named Lists without MDX.
5. Create advanced Named Lists with MDX if authorised.
6. Validate Named Lists before saving or execution.
7. Store Named Lists centrally in the semantic layer.
8. Control sharing, certification, versioning, ownership, and permissions.
9. Explain the business meaning of a Named List in plain English.
10. Protect the backend from expensive or unsafe MDX expressions.

### 3.2 UX Objectives

The UI must be:

- Native-feeling inside Excel, not like a generic web page embedded in a side panel.
- Compact, task-focused, and optimised for narrow Excel task pane width.
- Business-language-first.
- Progressive in complexity: simple users see simple controls; advanced users can expand technical details.
- Safe by default: preview and validation before insertion.
- Transparent: users can understand what a list does before using it.

### 3.3 Technical Objectives

The implementation must:

- Separate user-facing list definitions from compiled MDX.
- Provide a stable backend API for list creation, validation, preview, insertion, and execution.
- Support both native Excel cube-function integration and plugin-managed custom functions where applicable.
- Enforce permissions and row-level security during preview and execution.
- Maintain auditability and version history.
- Provide cost and performance guardrails.

---

## 4. Non-Goals for Initial Release

The initial release does **not** need to support:

1. Full arbitrary MDX authoring for all users.
2. Every MDX function.
3. Cross-cube list composition.
4. Complex writeback scenarios.
5. Natural-language creation of lists.

These can be considered later.

---

## 5. User Personas

### 5.1 Business User

Example: Finance manager, sales lead, operations manager.

Needs:

- Use existing trusted lists.
- Insert lists into reports.
- Understand what each list means.
- Avoid MDX or technical configuration.

Should see:

- Certified lists.
- My recently used lists.
- Plain-English descriptions.
- Preview results.
- Simple insert actions.

Should not see by default:

- Raw MDX.
- Cube hierarchy syntax.
- Technical validation details unless an error occurs.

### 5.2 Analyst

Example: BI analyst, data analyst, power Excel user.

Needs:

- Create list definitions using guided rules.
- Build Top N, filtered, threshold, exception, and fixed lists.
- Preview and refine lists.
- Share with a team.

Should see:

- List builder.
- Rule chips.
- Result preview.
- Optional technical details.

### 5.3 Power User / Admin

Example: Semantic model owner, BI platform engineer, data product owner.

Needs:

- Author advanced MDX list expressions.
- Validate complex expressions.
- Control certification.
- Govern permissions and performance.
- Diagnose list execution issues.

Should see:

- Advanced MDX editor.
- MDX explain/validation output.
- Cost estimate.
- Dependency graph.
- Version history.

---

## 6. Named List Types

The product must support the following list types.

### 6.1 Fixed List

A fixed list contains explicitly selected items.

Example:

- VIP Customers
- Strategic Suppliers
- Regulatory Watchlist Entities

Internal MDX example:

```mdx
{
  [Customer].[Customer].&[1001],
  [Customer].[Customer].&[1002],
  [Customer].[Customer].&[1003]
}
```

User-facing wording:

> This list contains manually selected customers. The selected items do not change automatically.

Required capabilities:

- Search and select items.
- Bulk paste item keys/names.
- Validate missing or invalid members.
- Show item count.
- Optionally allow locked/certified fixed lists.

### 6.2 Dynamic Top N List

A recalculating list returning top/bottom items based on a measure.

Example:

- Top 20 Customers by Revenue
- Bottom 10 Branches by Sales Target Achievement

Internal MDX example:

```mdx
TopCount(
  [Customer].[Customer].Members,
  20,
  [Measures].[Revenue]
)
```

User-facing wording:

> This list recalculates to show the 20 customers with the highest revenue.

Required parameters:

- Subject/entity, e.g. Customer.
- Count, e.g. 20.
- Direction: Top or Bottom.
- Measure, e.g. Revenue.
- Optional filters.
- Optional tie-handling behaviour.

### 6.3 Filtered List

A dynamic list based on dimension attributes or measure thresholds.

Example:

- UK Corporate Customers
- Accounts with Balance Greater Than £1m
- Merchants with Risk Score Above 80

Internal MDX example:

```mdx
Filter(
  [Customer].[Customer].Members,
  [Measures].[Revenue] > 1000000
)
```

Required parameters:

- Subject/entity.
- Filter field.
- Operator.
- Value.
- Optional additional AND/OR conditions.

Supported operators for MVP:

- Equals
- Does not equal
- Contains
- Starts with
- Greater than
- Greater than or equal
- Less than
- Less than or equal
- Is blank
- Is not blank
- In selected values

### 6.4 Relative Time List

A list that adapts to a period context.

Example:

- Current Month
- Previous Quarter
- Last 12 Months
- Current Financial Year

Internal MDX examples will depend on the time hierarchy model.

Required parameters:

- Calendar/fiscal hierarchy.
- Relative period type.
- Anchor date/context.
- Optional offset.

### 6.5 Exception List

A list that identifies anomalies or business exceptions.

Example:

- Customers with Revenue Drop Greater Than 20%
- Branches Below Target for 3 Months
- Products with High Sales but Low Margin

This can be built using filtered list logic plus calculated measures.

Required parameters:

- Subject/entity.
- Comparison measure.
- Threshold.
- Period context.
- Optional comparison period.

### 6.6 Advanced MDX List

A list defined directly by an MDX set expression.

Example:

```mdx
TopCount(
  Filter(
    [Customer].[Customer].Members,
    [Measures].[Revenue] > 1000000
  ),
  20,
  [Measures].[Revenue]
)
```

Availability:

- Admins and authorised power users only.
- Must pass validation before save.
- Must be subject to function allowlist/denylist.
- Must show explain text and performance estimate.

---

## 7. Scope Levels

Named Lists must support four visibility/scope levels.

### 7.1 Temporary List

Created during the current interaction only.

Characteristics:

- Not saved centrally.
- Used for immediate preview or insert.
- May be stored in workbook metadata only.

### 7.2 Workbook List

Saved inside or associated with a workbook.

Characteristics:

- Available only in that workbook.
- Can be refreshed with the workbook.
- Can later be promoted to shared list.

### 7.3 Shared List

Saved centrally and visible to permitted users/groups.

Characteristics:

- Stored in semantic layer repository.
- Permission controlled.
- Can be reused across workbooks.

### 7.4 Certified List

A governed list approved by a semantic model owner or data steward.

Characteristics:

- Marked with certification badge.
- Read-only for general users.
- Has owner, description, lineage, and version history.
- Preferred in search results.

---

## 8. Teeallite front end Excel plugin User Experience

## 8.1 Task Pane Layout

The Named Lists area should exist inside the Teeallite front end Excel plugin task pane.

Recommended navigation:

```text
Tessallite
├── Ask
├── Data
├── Named Lists
├── Reports
└── Settings
```

The Named Lists section should include:

1. Search bar.
2. Filter chips.
3. List categories.
4. List cards.
5. Preview panel.
6. Create button.

### 8.1.1 Named Lists Landing Screen

Required sections:

- Certified Lists
- My Lists
- Shared with Me
- Workbook Lists
- Recently Used

Each list card should show:

```text
Top 20 Customers by Revenue
Customer · Dynamic · Certified
Recalculates with report filters
Used in 4 workbook cells
[Insert] [Preview] [More]
```

Required card metadata:

- Name.
- Entity/subject area.
- List type.
- Certification status.
- Description preview.
- Item count if recently computed.
- Last updated/validated.
- Owner.

### 8.1.2 Search and Filters

Users must be able to filter lists by:

- Entity: Customer, Account, Product, Branch, Merchant, Date, etc.
- List type: Fixed, Dynamic, Top N, Filtered, Advanced MDX.
- Certification: Certified, Shared, Mine, Workbook.
- Owner.
- Recently used.

Search should match:

- List name.
- Description.
- Entity.
- Measure names.
- Tags.

---

## 8.2 Preview Experience

Before insertion, the user should be able to preview the list.

Preview panel must show:

```text
Top 20 Customers by Revenue

Description:
The 20 customers with the highest revenue in the selected period.

Current context:
Period: FY2026
Country: United Kingdom
Segment: All

Preview:
1. Customer A — £12.4m
2. Customer B — £10.9m
3. Customer C — £9.8m
...

20 items found
Estimated query cost: Low
Last validated: Today
```

Preview controls:

- Refresh preview.
- Change context/filter if permitted.
- Show business explanation.
- Show technical details for advanced users.
- Insert.

Preview must not return excessive rows. MVP preview limit: 100 items.

If a list returns more than the preview limit, show:

> Showing first 100 items. This list returns 3,240 items in the current context.

---

## 8.3 Insert Experience

When the user clicks **Insert**, show insert options.

Required options:

1. Insert as rows.
2. Insert as columns.
3. Insert as report filter.
4. Insert as hidden helper set.
5. Insert as named workbook parameter.
6. Insert formula only.

### 8.3.1 Insert as Rows

Behaviour:

- Insert list members vertically starting from selected cell.
- Include member caption by default.
- Optionally include unique key.
- Optionally include measure value if list is Top N or filtered by measure.

Example output:

| Customer   | Revenue    |
| ---------- | ---------- |
| Customer A | 12,400,000 |
| Customer B | 10,900,000 |

### 8.3.2 Insert as Columns

Behaviour:

- Insert list members horizontally starting from selected cell.
- Suitable for period lists or scenario lists.

### 8.3.3 Insert as Report Filter

Behaviour:

- Insert/set list reference as a filter input for generated Tessallite formulas.
- If using native cube formulas, create a CUBESET helper cell.
- If using custom functions, create a `TESS.LIST()` reference.

### 8.3.4 Insert as Hidden Helper Set

Behaviour:

- Create a hidden worksheet or hidden named range containing the list formula/reference.
- Report formulas reference the helper cell/range.
- User sees clean report cells.

Example native cube helper formula:

```excel
=CUBESET("Tessallite", "TopCount([Customer].[Customer].Members, 20, [Measures].[Revenue])", "Top 20 Customers by Revenue")
```

Example custom function helper formula:

```excel
=TESS.LIST("Top 20 Customers by Revenue")
```

### 8.3.5 Insert as Named Workbook Parameter

Behaviour:

- Add a workbook-level named item, e.g. `TESS_LIST_TOP_20_CUSTOMERS`.
- Formulas can reference it consistently.
- The plugin must track dependencies and support refresh.

---

## 8.4 Create Named List Flow

The Create flow should use a guided wizard.

### Step 1 — Choose Subject

User selects the business entity:

- Customer
- Account
- Product
- Branch
- Merchant
- Date
- Region
- Other model-defined entities

System maps selected entity to:

- Cube/catalog.
- Dimension.
- Hierarchy.
- Key attribute.
- Caption attribute.

### Step 2 — Choose List Type

Options:

- Fixed List
- Top/Bottom N List
- Filtered List
- Relative Time List
- Exception List
- Advanced MDX List

Only show Advanced MDX if user has permission.

### Step 3 — Configure Rule

For Top N:

```text
Show [Top/Bottom] [N] [Customers]
by [Revenue]
where [optional filters]
```

For Filtered List:

```text
Show [Customers]
where [Country] [equals] [United Kingdom]
and [Revenue] [greater than] [1,000,000]
```

For Fixed List:

```text
Search and select items
or paste item IDs/names
```

### Step 4 — Preview and Validate

System must:

- Compile semantic definition to MDX.
- Validate MDX.
- Execute preview query with limits.
- Apply user permissions and row-level security.
- Show item count.
- Show estimated cost.
- Show errors/warnings.

### Step 5 — Name and Save

Required fields:

- Name.
- Description.
- Scope: Workbook, Shared, Certified candidate.
- Tags.
- Owner/team.

Suggested description should be auto-generated from the rule but editable.

Example:

> Returns the top 20 customers by revenue in the current report context.

### Step 6 — Insert or Finish

After saving:

- Insert into workbook now.
- Close.
- Open list details.

---

## 9. Advanced MDX Editor

The advanced editor must be available only to authorised users.

### 9.1 Required Features

The editor must provide:

- Monospace MDX input.
- Syntax highlighting if feasible.
- Autocomplete for dimensions, hierarchies, members, measures, and common functions.
- Validate button.
- Preview button.
- Explain button.
- Cost estimate.
- Result count.
- Function warnings.
- Formatting button.

### 9.2 MDX Validation Requirements

Validation must check:

1. Syntax validity.
2. Expression returns a set, not a scalar value.
3. Referenced dimensions exist.
4. Referenced hierarchies exist.
5. Referenced measures exist.
6. User has permission to referenced model objects.
7. Expression does not use blocked functions.
8. Expression does not exceed complexity limits.
9. Expression does not return more than max allowed members unless explicitly approved.
10. Expression does not cross model/catalog boundaries unless supported.

### 9.3 Plain-English Explanation

The system should generate deterministic explanation text from the parsed expression where possible.

Example MDX:

```mdx
TopCount([Customer].[Customer].Members, 20, [Measures].[Revenue])
```

Explanation:

> Returns the 20 customers with the highest Revenue value in the current report context.

For expressions that cannot be confidently explained:

> This list uses an advanced MDX expression. Review the technical definition before using it in certified reports.

### 9.4 Error Messages

Errors must be translated into friendly language.

Bad:

```text
MDX parser failed near token MEMBERS at position 34.
```

Good:

```text
The Customer hierarchy could not be found. Check that the selected model contains Customer and that you have access to it.
```

Bad:

```text
Query exceeded resource limit.
```

Good:

```text
This list is too broad to preview. Add a filter, reduce the number of items, or use a more specific hierarchy.
```

---

## 10. Backend Data Model

Named Lists must be stored centrally.

### 10.1 Named List Entity

Recommended schema:

```json
{
  "id": "nl_01JABC123",
  "name": "Top 20 Customers by Revenue",
  "description": "Returns the top 20 customers by revenue in the current report context.",
  "subjectArea": "Sales",
  "entity": "Customer",
  "modelId": "model_sales_v1",
  "catalog": "sales",
  "cube": "Sales Analytics",
  "dimension": "Customer",
  "hierarchy": "Customer",
  "grain": "Customer",
  "listType": "dynamic_top_n",
  "scope": "shared",
  "certificationStatus": "certified",
  "visibility": "team",
  "ownerUserId": "user_123",
  "ownerDisplayName": "Finance Analytics",
  "tags": ["revenue", "customer", "certified"],
  "builderDefinition": {},
  "compiledMdx": "TopCount([Customer].[Customer].Members, 20, [Measures].[Revenue])",
  "parameters": [],
  "maxResultCount": 1000,
  "previewLimit": 100,
  "estimatedCostBand": "low",
  "createdBy": "user_123",
  "createdAt": "2026-05-23T10:00:00Z",
  "updatedBy": "user_123",
  "updatedAt": "2026-05-23T10:00:00Z",
  "lastValidatedAt": "2026-05-23T10:00:00Z",
  "version": 3,
  "isArchived": false
}
```

### 10.2 Builder Definition Examples

#### Top N

```json
{
  "operation": "topN",
  "entity": "Customer",
  "hierarchy": "Customer.Customer",
  "direction": "top",
  "count": 20,
  "measure": "Revenue",
  "filters": [
    {
      "field": "Country",
      "operator": "equals",
      "value": "United Kingdom"
    }
  ],
  "contextBehaviour": "respectWorkbookContext"
}
```

#### Fixed List

```json
{
  "operation": "fixedMembers",
  "entity": "Customer",
  "members": [
    {
      "key": "1001",
      "caption": "Customer A",
      "uniqueName": "[Customer].[Customer].&[1001]"
    },
    {
      "key": "1002",
      "caption": "Customer B",
      "uniqueName": "[Customer].[Customer].&[1002]"
    }
  ]
}
```

#### Filtered List

```json
{
  "operation": "filter",
  "entity": "Customer",
  "hierarchy": "Customer.Customer",
  "conditions": [
    {
      "field": "Country",
      "operator": "equals",
      "value": "United Kingdom"
    },
    {
      "field": "Revenue",
      "operator": "greaterThan",
      "value": 1000000
    }
  ],
  "logicalOperator": "and"
}
```

### 10.3 Version History

Each change to a Shared or Certified list must create a new version.

Version record:

```json
{
  "namedListId": "nl_01JABC123",
  "version": 4,
  "changedBy": "user_456",
  "changedAt": "2026-05-23T11:30:00Z",
  "changeSummary": "Changed Top N count from 10 to 20.",
  "builderDefinition": {},
  "compiledMdx": "...",
  "validationStatus": "valid"
}
```

Certified lists must retain historical versions for audit.

---

## 11. API Specification

Base path:

```text
/api/v1/named-lists
```

All endpoints must enforce authentication, authorisation, model permissions, and row-level security.

---

## 11.1 List Named Lists

```http
GET /api/v1/named-lists
```

Query parameters:

- `modelId`
- `entity`
- `scope`
- `certificationStatus`
- `owner`
- `search`
- `tags`
- `limit`
- `offset`

Response:

```json
{
  "items": [
    {
      "id": "nl_01JABC123",
      "name": "Top 20 Customers by Revenue",
      "description": "Returns the top 20 customers by revenue.",
      "entity": "Customer",
      "listType": "dynamic_top_n",
      "scope": "shared",
      "certificationStatus": "certified",
      "ownerDisplayName": "Finance Analytics",
      "lastValidatedAt": "2026-05-23T10:00:00Z",
      "estimatedCostBand": "low"
    }
  ],
  "total": 1
}
```

---

## 11.2 Get Named List Details

```http
GET /api/v1/named-lists/{id}
```

Response must include:

- Full metadata.
- Builder definition if user has permission.
- Compiled MDX if user has technical permission.
- Version.
- Dependencies.
- Usage summary.

---

## 11.3 Create Named List

```http
POST /api/v1/named-lists
```

Request:

```json
{
  "name": "Top 20 Customers by Revenue",
  "description": "Returns the top 20 customers by revenue.",
  "modelId": "model_sales_v1",
  "entity": "Customer",
  "listType": "dynamic_top_n",
  "scope": "shared",
  "builderDefinition": {
    "operation": "topN",
    "entity": "Customer",
    "direction": "top",
    "count": 20,
    "measure": "Revenue",
    "filters": []
  }
}
```

Backend must:

1. Validate user permission.
2. Compile builder definition to MDX.
3. Validate compiled MDX.
4. Store both builder definition and compiled MDX.
5. Return saved object.

---

## 11.4 Update Named List

```http
PATCH /api/v1/named-lists/{id}
```

Rules:

- Workbook lists can be edited by workbook owner/editor.
- Shared lists can be edited by owner/admin.
- Certified lists require certifier/admin permission.
- Updating a certified list should either:
  - Create a draft version requiring re-certification, or
  - Remove certification until revalidated, depending on governance policy.

---

## 11.5 Validate Named List

```http
POST /api/v1/named-lists/validate
```

Request:

```json
{
  "modelId": "model_sales_v1",
  "listType": "advanced_mdx",
  "builderDefinition": null,
  "compiledMdx": "TopCount([Customer].[Customer].Members, 20, [Measures].[Revenue])",
  "context": {
    "filters": [],
    "locale": "en-GB"
  }
}
```

Response:

```json
{
  "isValid": true,
  "returnsSet": true,
  "warnings": [],
  "errors": [],
  "estimatedCostBand": "low",
  "estimatedMaxMembers": 20,
  "plainEnglishExplanation": "Returns the 20 customers with the highest Revenue value in the current report context."
}
```

Error response example:

```json
{
  "isValid": false,
  "errors": [
    {
      "code": "UNKNOWN_HIERARCHY",
      "message": "The Customer hierarchy could not be found.",
      "technicalDetail": "Hierarchy [Customer].[Customer] not found in model model_sales_v1."
    }
  ],
  "warnings": []
}
```

---

## 11.6 Preview Named List

```http
POST /api/v1/named-lists/{id}/preview
```

Request:

```json
{
  "context": {
    "filters": [
      {
        "field": "FinancialYear",
        "value": "FY2026"
      }
    ],
    "limit": 100
  }
}
```

Response:

```json
{
  "namedListId": "nl_01JABC123",
  "name": "Top 20 Customers by Revenue",
  "items": [
    {
      "ordinal": 1,
      "uniqueName": "[Customer].[Customer].&[1001]",
      "key": "1001",
      "caption": "Customer A",
      "measureValues": {
        "Revenue": 12400000
      }
    }
  ],
  "returnedCount": 1,
  "totalCountKnown": true,
  "totalCount": 20,
  "truncated": false,
  "estimatedCostBand": "low"
}
```

---

## 11.7 Compile Builder Definition

```http
POST /api/v1/named-lists/compile
```

Purpose:

- Used by UI to show technical users the generated MDX.
- Used internally before validation/save.

Request:

```json
{
  "modelId": "model_sales_v1",
  "builderDefinition": {
    "operation": "topN",
    "entity": "Customer",
    "direction": "top",
    "count": 20,
    "measure": "Revenue",
    "filters": []
  }
}
```

Response:

```json
{
  "compiledMdx": "TopCount([Customer].[Customer].Members, 20, [Measures].[Revenue])",
  "explanation": "Returns the 20 customers with the highest Revenue value."
}
```

---

## 11.8 Record Workbook Usage

```http
POST /api/v1/named-lists/{id}/usage
```

Purpose:

- Track where lists are used.
- Support impact analysis when a list changes.

Request:

```json
{
  "workbookId": "wb_123",
  "worksheetName": "Executive Summary",
  "cellReference": "B4",
  "usageType": "helper_set",
  "formula": "=TESS.LIST(\"Top 20 Customers by Revenue\")"
}
```

---

## 12. MDX Compiler Requirements

The platform must include a compiler that converts semantic builder definitions into safe MDX.

### 12.1 Compiler Inputs

- Model metadata.
- Entity selection.
- Hierarchy selection.
- Measure selection.
- Filters.
- Sort direction.
- Count/limit.
- Context behaviour.

### 12.2 Compiler Outputs

- MDX set expression.
- Explanation.
- Dependency list.
- Estimated complexity.
- Validation warnings.

### 12.3 Dependency List

Example:

```json
{
  "dimensions": ["Customer"],
  "hierarchies": ["Customer.Customer"],
  "measures": ["Revenue"],
  "attributes": ["Country"],
  "calculatedMembers": []
}
```

### 12.4 Compilation Rules

The compiler must:

1. Use model metadata, not hardcoded hierarchy strings.
2. Escape member names safely.
3. Use unique names for members.
4. Avoid generating unbounded `.Members` expressions where possible.
5. Apply result limits for preview.
6. Respect workbook/report context if configured.
7. Generate deterministic MDX for the same builder input.
8. Produce friendly warnings where performance may be poor.

---

## 13. MDX Safety and Performance Guardrails

### 13.1 Function Allowlist for MVP

For advanced MDX, initially allow only a controlled subset of MDX functions.

Recommended MVP allowlist:

- `{ ... }` set literals
- `Members`
- `Children`
- `Descendants`
- `Filter`
- `TopCount`
- `BottomCount`
- `Order`
- `Head`
- `Tail`
- `CrossJoin` only with strict limits
- `Exists` if supported by backend
- Simple measure comparisons

### 13.2 Restricted or High-Risk Patterns

Block or warn for:

- Unbounded crossjoins.
- Recursive expressions.
- Expressions returning extremely large sets.
- Cross-model references.
- Unknown calculated members.
- Unsupported VBA/string functions.
- Expressions that bypass security filters.
- Excessively nested filters.
- Repeated expensive measure evaluations.

### 13.3 Complexity Scoring

Each expression should receive a complexity band:

- Low
- Medium
- High
- Blocked

Factors:

- Number of referenced hierarchies.
- Use of `.Members` on high-cardinality hierarchy.
- Use of `CrossJoin`.
- Use of `Filter` with measure evaluation.
- Estimated result cardinality.
- Model storage/source cost.

### 13.4 Hard Limits

Recommended defaults:

| Setting                                          | Default    |
| ------------------------------------------------ | ---------- |
| Preview row limit                                | 100        |
| Maximum saved list result count without approval | 10,000     |
| Maximum Top N count                              | 1,000      |
| Maximum nested expression depth                  | 8          |
| Maximum CrossJoin dimensions                     | 2          |
| Query timeout for preview                        | 10 seconds |
| Query timeout for insert/refresh                 | 60 seconds |

These should be configurable per tenant/model.

---

## 14. Security and Permissions

### 14.1 Permission Model

Required permissions:

| Permission                | Description                   |
| ------------------------- | ----------------------------- |
| `named_list.read`         | View permitted lists          |
| `named_list.preview`      | Preview permitted lists       |
| `named_list.use`          | Insert/use list in workbook   |
| `named_list.create`       | Create workbook/private lists |
| `named_list.share`        | Share list with team          |
| `named_list.edit`         | Edit owned/shared lists       |
| `named_list.certify`      | Certify governed lists        |
| `named_list.advanced_mdx` | Use raw MDX editor            |
| `named_list.admin`        | Manage all lists              |

### 14.2 Row-Level Security

All preview and execution calls must apply the current user’s security context.

This means:

- Two users previewing the same list may see different results.
- Cached results must be security-aware.
- Saved MDX must not bypass security filters.

### 14.3 Sensitive Data

If the list references sensitive dimensions or restricted measures:

- Show sensitivity indicator if user has permission.
- Hide restricted metadata if not permitted.
- Prevent sharing with users/groups lacking access.

---

## 15. Excel Formula Integration

The system should support two formula strategies.

---

## 15.1 Strategy A — Native Excel Cube Functions

Useful where Excel is connected to an XMLA/MDX-compatible endpoint.

### 15.1.1 Helper Set Formula

```excel
=CUBESET("Tessallite", "TopCount([Customer].[Customer].Members, 20, [Measures].[Revenue])", "Top 20 Customers by Revenue")
```

### 15.1.2 Retrieve Ranked Members

```excel
=CUBERANKEDMEMBER("Tessallite", $B$2, ROW(A1))
```

Where `$B$2` contains the `CUBESET` formula.

### 15.1.3 Use Set in Value Formula

```excel
=CUBEVALUE("Tessallite", $B$2, "[Measures].[Revenue]")
```

### Pros

- Native Excel semantics.
- Works well with cube-aware users.
- No need for custom function runtime for every calculation.

### Cons

- MDX may appear in cells unless hidden.
- Formula strings can be hard to read.
- More difficult to provide rich metadata.
- Requires robust XMLA/MDX endpoint compatibility.

---

## 15.2 Strategy B — Tessallite Custom Functions

Useful where the plugin controls formula abstraction.

### 15.2.1 List Reference

```excel
=TESS.LIST("Top 20 Customers by Revenue")
```

### 15.2.2 List Members

```excel
=TESS.LISTMEMBERS("Top 20 Customers by Revenue")
```

### 15.2.3 Value with List Filter

```excel
=TESS.VALUE("Revenue", TESS.LIST("Top 20 Customers by Revenue"))
```

### Pros

- Cleaner user experience.
- Hides MDX fully.
- Easier to support named list IDs instead of fragile names.
- Enables richer platform-managed behaviour.

### Cons

- Requires custom function implementation.
- Needs careful refresh behaviour.
- May not behave exactly like native cube formulas.

---

## 15.3 Recommended Approach

Support both, but use a product setting per connection/model:

1. **Native Cube Mode** for users who rely heavily on Excel cube functions and XMLA semantics.
2. **Tessallite Function Mode** for business-friendly managed reporting.

Default recommendation for user-friendly experience:

```text
Use Tessallite custom functions for clean user-facing reports.
Use hidden native cube helper formulas where deep Excel compatibility is required.
```

The plugin should hide implementation details behind insert options.

---

## 16. Workbook Metadata

The plugin must track workbook usage of Named Lists.

### 16.1 Workbook Manifest

Store workbook-level metadata, either in Office document settings/custom properties or a hidden worksheet.

Example:

```json
{
  "workbookId": "wb_123",
  "connectionId": "conn_tessallite_prod",
  "namedLists": [
    {
      "namedListId": "nl_01JABC123",
      "version": 3,
      "displayName": "Top 20 Customers by Revenue",
      "usageLocations": [
        {
          "worksheet": "Executive Summary",
          "cell": "B4",
          "usageType": "helper_set"
        }
      ]
    }
  ]
}
```

### 16.2 Version Warnings

If a workbook uses an older version of a shared/certified list, the plugin should show:

> A newer version of this Named List is available.

Options:

- Keep current version.
- Update to latest version.
- View changes.

Certified report workbooks may require explicit version pinning.

---

## 17. List Details Page

Each Named List must have a details view.

Required sections:

1. Name and description.
2. Certification badge.
3. Owner.
4. Entity and model.
5. List type.
6. Business rule summary.
7. Preview.
8. Usage count.
9. Version history.
10. Permissions/sharing.
11. Technical details for authorised users.

Business rule summary examples:

```text
Top 20 Customers by Revenue
Entity: Customer
Measure: Revenue
Context: Respects workbook filters
```

```text
UK Corporate Customers
Entity: Customer
Rule: Country equals United Kingdom and Segment equals Corporate
```

Technical details should be collapsed by default.

---

## 18. Governance Workflow

### 18.1 Certification States

Required statuses:

- Draft
- Shared
- Certified
- Certification Requested
- Deprecated
- Archived

### 18.2 Certification Requirements

A list can be certified only if:

- It has a description.
- It has an owner.
- It validates successfully.
- It has passed preview.
- It does not use blocked/high-risk MDX patterns.
- It has a clear entity/grain.
- It has acceptable estimated cost.

### 18.3 Deprecation

A list owner/admin can mark a list as deprecated.

Deprecated lists:

- Remain usable in existing workbooks.
- Are hidden from default search.
- Show warning when inserted.
- Should recommend replacement list if available.

Warning:

> This list is deprecated. Use “Top Customers by Net Revenue” instead.

---

## 19. Error and Empty States

### 19.1 No Lists Available

```text
No Named Lists are available for this model.
Create a list or ask your data owner to share certified lists with you.
```

### 19.2 No Permission

```text
You do not have permission to use this Named List.
Contact the list owner or your data administrator.
```

### 19.3 Broken List

```text
This Named List can no longer be validated because one or more referenced fields have changed.
```

Actions:

- View details.
- Revalidate.
- Edit list.
- Contact owner.

### 19.4 Empty Preview

```text
This list returned no items in the current context.
Try changing the report filters or reviewing the list rule.
```

### 19.5 Expensive List

```text
This list may be expensive to calculate. Add a filter, reduce the item count, or ask a model owner to optimise it.
```

---

## 20. Accessibility and Usability Requirements

The task pane must:

- Support keyboard navigation.
- Use clear focus states.
- Avoid relying on colour alone.
- Support screen-reader labels for buttons and list cards.
- Provide accessible error text.
- Support narrow pane widths.
- Avoid horizontal scrolling.
- Keep primary actions visible.

Minimum supported pane width should be defined by the UI team, but the design should assume a narrow Excel side panel.

---

## 21. Telemetry and Usage Analytics

Track events:

- Named list searched.
- Named list previewed.
- Named list inserted.
- Named list created.
- Named list validation failed.
- Advanced MDX editor opened.
- Expensive list warning shown.
- Deprecated list used.
- List version updated in workbook.

Telemetry payload example:

```json
{
  "event": "named_list_inserted",
  "namedListId": "nl_01JABC123",
  "listType": "dynamic_top_n",
  "scope": "shared",
  "certificationStatus": "certified",
  "insertMode": "helper_set",
  "modelId": "model_sales_v1",
  "workbookId": "wb_123"
}
```

Do not log sensitive member names or user data unless explicitly approved by data governance.

---

## 22. Caching Requirements

### 22.1 Metadata Caching

The plugin may cache:

- List metadata.
- Model metadata.
- Recent search results.
- User permissions.

Cache should refresh on:

- Plugin startup.
- Connection change.
- Manual refresh.
- Named list change notification if available.

### 22.2 Result Caching

Preview results may be cached only if:

- Cache key includes user ID/security context.
- Cache key includes model ID.
- Cache key includes named list ID and version.
- Cache key includes context filters.

Do not use shared result cache across users unless security trimming is guaranteed.

---

## 23. Testing Requirements

### 23.1 Unit Tests

Back-end tests:

- Builder definition compiles to expected MDX.
- Invalid definitions are rejected.
- Advanced MDX validation catches syntax errors.
- Unsupported functions are blocked.
- Permissions are enforced.
- Version history is created.
- Deprecated lists behave correctly.

Front-end tests:

- List cards render correctly.
- Search and filters work.
- Create wizard validates required fields.
- Preview handles loading, success, error, empty, and expensive states.
- Insert options produce expected formula/metadata payload.

### 23.2 Integration Tests

Scenarios:

1. User creates Top 20 Customers by Revenue.
2. Backend compiles to MDX.
3. User previews list.
4. User inserts list as helper set.
5. Workbook metadata records usage.
6. User refreshes workbook.
7. Backend applies security and returns correct results.

### 23.3 Security Tests

- User cannot preview list referencing restricted dimension.
- User cannot share list with group lacking access.
- User cannot use advanced MDX without permission.
- Raw MDX cannot bypass row-level security.
- Cached preview result is not leaked across users.

### 23.4 Performance Tests

- Preview of certified list returns within 2 seconds for low-cost expressions.
- Expensive expressions are blocked or warned before execution.
- Search over 10,000 list metadata records remains responsive.
- Plugin startup does not block Excel interaction.

### 23.5 Excel Compatibility Tests

Test against supported Excel versions/channels:

- Excel desktop Windows.
- Excel desktop Mac if supported.
- Excel web if supported.
- Office 2016/2019/365 compatibility depending on product target.

Test formula modes:

- Native CUBESET insertion.
- CUBERANKEDMEMBER retrieval.
- CUBEVALUE usage with set filter.
- Tessallite custom function mode.
- Hidden helper worksheet mode.

---

## 24. KPI Support Requirements

KPIs must be treated as first-class semantic-layer assets, not merely as Excel formatting rules. A KPI should define what business performance is being tracked, how the target is calculated, how status is calculated, how trend is calculated, and how it should be displayed in Excel.

A KPI must be reusable across:

- Teeallite front end Excel plugin cards.
- Excel worksheet formulas.
- Dashboards.
- Conversational analytics responses.
- Semantic model metadata.
- Future API consumers.

User-facing positioning:

> KPIs let users track trusted business performance metrics with consistent value, target, status, and trend logic across Excel reports.

The product should not describe the feature to normal users as a screen for writing MDX KPI expressions.

---

## 24.1 KPI Creation Form

The current **Add KPI** form must be formalised with the following fields.

### 24.1.1 Identity Fields

| Field          | Required    | Description                                                      | Example                                                               |
| -------------- | ----------- | ---------------------------------------------------------------- | --------------------------------------------------------------------- |
| Name           | Yes         | Stable technical identifier. Should be unique within model/cube. | `total_transaction_count_this_year`                                   |
| Display Name   | Yes         | Friendly name shown to users in Excel and dashboards.            | `Total Transaction Count This Year`                                   |
| Description    | Recommended | Plain-English explanation of what the KPI tracks.                | `Tracks total transaction count for the current year against target.` |
| Display Folder | Optional    | Business folder/group used in UI navigation.                     | `Financial KPIs`                                                      |

Validation rules:

- `Name` must be stable and should not contain spaces.
- `Name` should use lower snake case or another agreed internal naming convention.
- `Display Name` should be human-readable.
- `Name` should not change after publication unless migration/versioning is handled.
- `Display Folder` may use `/` for hierarchy, e.g. `Financial KPIs/Transactions`.

---

### 24.1.2 Measurement Fields

| Field         | Required | Description                                        | Example                                     |
| ------------- | -------- | -------------------------------------------------- | ------------------------------------------- |
| Value Measure | Yes      | The actual value being tracked.                    | `[Measures].[Transaction Count YTD]`        |
| Goal Measure  | Yes      | The target or benchmark value used for comparison. | `[Measures].[Transaction Count Target YTD]` |

Example KPI:

```text
Display Name: Total Transaction Count This Year
Value Measure: [Measures].[Transaction Count YTD]
Goal Measure: [Measures].[Transaction Count Target YTD]
```

Business explanation:

> This KPI compares the year-to-date transaction count against the target transaction count for the same period.

Validation rules:

- Value Measure must reference an existing measure.
- Goal Measure must reference an existing measure or calculated measure.
- Both measures must be accessible to the current user.
- Both measures must belong to the same model/cube.
- Both measures should return numeric scalar values.

---

### 24.1.3 Status Expression

The Status Expression is an MDX expression that evaluates KPI performance against its goal.

Required return values:

| Return Value | Meaning      | UI Interpretation       |
| ------------ | ------------ | ----------------------- |
| `1`          | Good         | Green / positive status |
| `0`          | OK / neutral | Amber / neutral status  |
| `-1`         | Bad          | Red / negative status   |

Current example:

```mdx
IIf([Measures].[Revenue] >= [Measures].[Target], 1, -1)
```

Recommended example for the transaction-count KPI:

```mdx
IIf(
  [Measures].[Transaction Count YTD] >= [Measures].[Transaction Count Target YTD],
  1,
  -1
)
```

Optional three-state example:

```mdx
IIf(
  [Measures].[Transaction Count YTD] >= [Measures].[Transaction Count Target YTD],
  1,
  IIf(
    [Measures].[Transaction Count YTD] >= [Measures].[Transaction Count Target YTD] * 0.95,
    0,
    -1
  )
)
```

User-facing explanation:

> Shows whether the current KPI value is good, acceptable, or below target.

Validation rules:

- Expression must return only `1`, `0`, or `-1`.
- Expression must reference valid measures.
- Expression must not return a set.
- Expression must not use blocked MDX functions.
- Expression must be evaluated under the current user’s security context.
- If expression may return any value outside `1`, `0`, or `-1`, validation must fail unless explicit normalisation is configured.

---

### 24.1.4 Trend Expression

The Trend Expression is an MDX expression that evaluates movement over time.

Required return values:

| Return Value | Meaning          | UI Interpretation           |
| ------------ | ---------------- | --------------------------- |
| `1`          | Up / improving   | Up arrow                    |
| `0`          | Flat / unchanged | Flat arrow / neutral marker |
| `-1`         | Down / worsening | Down arrow                  |

The current form mentions `1` or `-1`. The platform should support `0` as a neutral trend value for completeness, even if MVP UI initially uses only up/down.

Current example:

```mdx
IIf([Measures].[Revenue] > ([Measures].[Revenue], [Date].[Year].PrevMember), 1, -1)
```

Recommended example for the transaction-count KPI:

```mdx
IIf(
  [Measures].[Transaction Count YTD] >
    ([Measures].[Transaction Count YTD], [Date].[Year].CurrentMember.PrevMember),
  1,
  -1
)
```

Optional three-state example:

```mdx
IIf(
  [Measures].[Transaction Count YTD] >
    ([Measures].[Transaction Count YTD], [Date].[Year].CurrentMember.PrevMember),
  1,
  IIf(
    [Measures].[Transaction Count YTD] =
      ([Measures].[Transaction Count YTD], [Date].[Year].CurrentMember.PrevMember),
    0,
    -1
  )
)
```

User-facing explanation:

> Shows whether the KPI is moving up, down, or staying broadly flat compared with the previous period.

Validation rules:

- Expression must return `1`, `0`, or `-1`; MVP may allow only `1` and `-1` if current implementation requires it.
- Expression must reference a valid time hierarchy if previous-period comparison is used.
- Expression must fail gracefully when no previous period exists.
- Expression must not return a set.
- Expression must not use blocked MDX functions.

---

### 24.1.5 KPI Graphics

The KPI form must support display graphics.

#### Status Graphic

Current option:

```text
Traffic Light
```

Supported MVP values:

| Value           | Description                                    |
| --------------- | ---------------------------------------------- |
| `traffic_light` | Red/amber/green status indicator               |
| `shapes`        | Circle/triangle/cross or similar status marker |
| `none`          | No status graphic                              |

Recommended default:

```text
Traffic Light
```

#### Trend Graphic

Current option:

```text
Standard Arrow
```

Supported MVP values:

| Value            | Description         |
| ---------------- | ------------------- |
| `standard_arrow` | Up/flat/down arrows |
| `simple_arrow`   | Minimal arrow style |
| `none`           | No trend graphic    |

Recommended default:

```text
Standard Arrow
```

UX requirements:

- The form should show a small preview of the selected graphic style.
- The Excel task pane should render status and trend consistently with the selected styles.
- Excel worksheet insertion may use icons, text symbols, conditional formatting, generated values, or plugin-rendered output depending on technical constraints.

---

### 24.1.6 KPI Weight

| Field  | Required | Description                                | Example |
| ------ | -------- | ------------------------------------------ | ------- |
| Weight | Optional | Relative importance when aggregating KPIs. | `1.0`   |

Default:

```text
1.0
```

Use cases:

- Composite scorecards.
- Parent KPI rollups.
- Weighted health scores.
- Executive summaries.

Validation rules:

- Must be numeric.
- Must be greater than or equal to `0`.
- Recommended normal range: `0.0` to `10.0`.
- Default should be `1.0` if not provided.

---

### 24.1.7 Parent KPI

| Field      | Required | Description                                                   | Example                  |
| ---------- | -------- | ------------------------------------------------------------- | ------------------------ |
| Parent KPI | Optional | Parent KPI used to build KPI hierarchies or scorecard groups. | `financial_health_score` |

Rules:

- Parent KPI must exist in the same model/workspace unless cross-model hierarchy is explicitly supported.
- Circular parent-child relationships must be blocked.
- A KPI cannot be its own parent.
- If parent KPI is deprecated or archived, show a warning.

Example hierarchy:

```text
Financial Health
├── Revenue Growth
├── Total Transaction Count This Year
├── Margin Performance
└── Cost Efficiency
```

---

## 24.2 KPI Data Model

Recommended schema:

```json
{
  "id": "kpi_01JXYZ123",
  "name": "total_transaction_count_this_year",
  "displayName": "Total Transaction Count This Year",
  "description": "Tracks the total number of transactions in the current year against the target.",
  "modelId": "model_sales_v1",
  "catalog": "sales",
  "cube": "Sales Analytics",
  "displayFolder": "Financial KPIs",
  "valueMeasure": "[Measures].[Transaction Count YTD]",
  "goalMeasure": "[Measures].[Transaction Count Target YTD]",
  "statusExpression": "IIf([Measures].[Transaction Count YTD] >= [Measures].[Transaction Count Target YTD], 1, -1)",
  "trendExpression": "IIf([Measures].[Transaction Count YTD] > ([Measures].[Transaction Count YTD], [Date].[Year].CurrentMember.PrevMember), 1, -1)",
  "statusGraphic": "traffic_light",
  "trendGraphic": "standard_arrow",
  "weight": 1.0,
  "parentKpiId": null,
  "scope": "shared",
  "certificationStatus": "draft",
  "visibility": "team",
  "ownerUserId": "user_123",
  "ownerDisplayName": "Finance Analytics",
  "tags": ["transactions", "financial", "ytd"],
  "dependencies": {
    "measures": [
      "Transaction Count YTD",
      "Transaction Count Target YTD"
    ],
    "dimensions": ["Date"],
    "hierarchies": ["Date.Year"]
  },
  "estimatedCostBand": "low",
  "createdBy": "user_123",
  "createdAt": "2026-05-23T10:00:00Z",
  "updatedBy": "user_123",
  "updatedAt": "2026-05-23T10:00:00Z",
  "lastValidatedAt": "2026-05-23T10:00:00Z",
  "version": 1,
  "isArchived": false
}
```

---

## 24.3 KPI API Specification

Base path:

```text
/api/v1/kpis
```

All endpoints must enforce authentication, authorisation, model permissions, and row-level security.

### 24.3.1 List KPIs

```http
GET /api/v1/kpis
```

Query parameters:

- `modelId`
- `displayFolder`
- `certificationStatus`
- `owner`
- `search`
- `tags`
- `parentKpiId`
- `limit`
- `offset`

Response:

```json
{
  "items": [
    {
      "id": "kpi_01JXYZ123",
      "name": "total_transaction_count_this_year",
      "displayName": "Total Transaction Count This Year",
      "description": "Tracks the total number of transactions in the current year against the target.",
      "displayFolder": "Financial KPIs",
      "statusGraphic": "traffic_light",
      "trendGraphic": "standard_arrow",
      "weight": 1.0,
      "parentKpiId": null,
      "certificationStatus": "draft",
      "ownerDisplayName": "Finance Analytics",
      "lastValidatedAt": "2026-05-23T10:00:00Z"
    }
  ],
  "total": 1
}
```

### 24.3.2 Get KPI Details

```http
GET /api/v1/kpis/{id}
```

Response must include:

- Full metadata.
- Value measure.
- Goal measure.
- Status expression if user has permission.
- Trend expression if user has permission.
- Display graphics.
- Parent KPI.
- Display folder.
- Dependencies.
- Version history.
- Usage summary.

### 24.3.3 Create KPI

```http
POST /api/v1/kpis
```

Request:

```json
{
  "name": "total_transaction_count_this_year",
  "displayName": "Total Transaction Count This Year",
  "description": "Tracks the total number of transactions in the current year against the target.",
  "modelId": "model_sales_v1",
  "displayFolder": "Financial KPIs",
  "valueMeasure": "[Measures].[Transaction Count YTD]",
  "goalMeasure": "[Measures].[Transaction Count Target YTD]",
  "statusExpression": "IIf([Measures].[Transaction Count YTD] >= [Measures].[Transaction Count Target YTD], 1, -1)",
  "trendExpression": "IIf([Measures].[Transaction Count YTD] > ([Measures].[Transaction Count YTD], [Date].[Year].CurrentMember.PrevMember), 1, -1)",
  "statusGraphic": "traffic_light",
  "trendGraphic": "standard_arrow",
  "weight": 1.0,
  "parentKpiId": null,
  "scope": "shared"
}
```

Backend must:

1. Validate user permission.
2. Validate referenced measures.
3. Validate status expression.
4. Validate trend expression.
5. Validate parent KPI relationship.
6. Extract dependencies.
7. Store KPI metadata.
8. Return saved object.

### 24.3.4 Update KPI

```http
PATCH /api/v1/kpis/{id}
```

Rules:

- Shared KPIs can be edited by owner/admin.
- Certified KPIs require certifier/admin permission.
- Updating a certified KPI should either create a draft version or remove certification pending revalidation.
- Changing `Name` should be restricted because formulas and external references may depend on it.
- Prefer stable `id` references in formulas and workbook metadata.

### 24.3.5 Validate KPI

```http
POST /api/v1/kpis/validate
```

Request:

```json
{
  "modelId": "model_sales_v1",
  "valueMeasure": "[Measures].[Transaction Count YTD]",
  "goalMeasure": "[Measures].[Transaction Count Target YTD]",
  "statusExpression": "IIf([Measures].[Transaction Count YTD] >= [Measures].[Transaction Count Target YTD], 1, -1)",
  "trendExpression": "IIf([Measures].[Transaction Count YTD] > ([Measures].[Transaction Count YTD], [Date].[Year].CurrentMember.PrevMember), 1, -1)",
  "parentKpiId": null
}
```

Response:

```json
{
  "isValid": true,
  "errors": [],
  "warnings": [],
  "dependencies": {
    "measures": [
      "Transaction Count YTD",
      "Transaction Count Target YTD"
    ],
    "dimensions": ["Date"],
    "hierarchies": ["Date.Year"]
  },
  "estimatedCostBand": "low",
  "plainEnglishExplanation": "Compares year-to-date transaction count with the year-to-date transaction target and shows whether performance is above or below target."
}
```

Error response example:

```json
{
  "isValid": false,
  "errors": [
    {
      "code": "INVALID_STATUS_RETURN_VALUE",
      "message": "The status expression must return 1, 0, or -1.",
      "technicalDetail": "Expression may return a value outside the allowed KPI status range."
    }
  ],
  "warnings": []
}
```

### 24.3.6 Evaluate KPI

```http
POST /api/v1/kpis/{id}/evaluate
```

Purpose:

- Used by Teeallite front end Excel plugin preview cards.
- Used by worksheet formula refresh.
- Used by dashboards and conversational analytics.

Request:

```json
{
  "context": {
    "filters": [
      {
        "field": "FinancialYear",
        "value": "FY2026"
      }
    ],
    "locale": "en-GB"
  }
}
```

Response:

```json
{
  "kpiId": "kpi_01JXYZ123",
  "displayName": "Total Transaction Count This Year",
  "value": 1250000,
  "goal": 1200000,
  "status": 1,
  "trend": 1,
  "statusLabel": "Good",
  "trendLabel": "Up",
  "statusGraphic": "traffic_light",
  "trendGraphic": "standard_arrow",
  "formattedValue": "1,250,000",
  "formattedGoal": "1,200,000",
  "contextSummary": "FY2026",
  "evaluatedAt": "2026-05-23T10:00:00Z"
}
```

---

## 24.4 KPI Tessallite Excel Plugin UX

The Excel task pane has a dedicated **KPIs** tab between the Report Builder (Analyse) and the conversational agent (Ask) tabs.

Implemented tab navigation (3-tab layout):

```text
[ Analyse ]  [ KPIs ]  [ Ask <agent> ]
```

The KPIs tab provides a self-contained view of all model KPIs with live evaluation results, filtering, and insert-to-worksheet actions. Named Lists remain accessible within the Analyse (Report Builder) tab.

### 24.4.1 KPI Landing Screen

Sections:

- Certified KPIs
- My KPIs
- Shared with Me
- Financial KPIs
- Recently Used

Each KPI card should show:

```text
Total Transaction Count This Year
Financial KPIs · Certified
Value: 1,250,000    Target: 1,200,000
Status: Green       Trend: Up
[Insert] [Preview] [More]
```

Required card fields:

- Display Name.
- Display Folder.
- Description preview.
- Status icon.
- Trend icon.
- Current value if recently evaluated.
- Goal value if recently evaluated.
- Certification status.
- Owner.

### 24.4.2 KPI Preview

Preview panel must show:

```text
Total Transaction Count This Year

Description:
Tracks the total number of transactions in the current year against the target.

Current context:
Period: FY2026
Region: All

Value: 1,250,000
Goal: 1,200,000
Status: Good
Trend: Up

Display:
Status Graphic: Traffic Light
Trend Graphic: Standard Arrow
Weight: 1.0
Display Folder: Financial KPIs
```

Advanced users may expand technical details:

```text
Value Measure:
[Measures].[Transaction Count YTD]

Goal Measure:
[Measures].[Transaction Count Target YTD]

Status Expression:
IIf([Measures].[Transaction Count YTD] >= [Measures].[Transaction Count Target YTD], 1, -1)

Trend Expression:
IIf([Measures].[Transaction Count YTD] > ([Measures].[Transaction Count YTD], [Date].[Year].CurrentMember.PrevMember), 1, -1)
```

### 24.4.3 KPI Insert Options

Users should be able to insert a KPI as:

1. KPI card.
2. Value only.
3. Value + goal.
4. Status icon only.
5. Trend icon only.
6. Full KPI table row.
7. Formula reference.

Example inserted table row:

| KPI                               | Value     | Goal      | Status | Trend |
| --------------------------------- | --------- | --------- | ------ | ----- |
| Total Transaction Count This Year | 1,250,000 | 1,200,000 | Good   | Up    |

---

## 24.5 KPI Excel Formula Integration

### 24.5.1 Custom Function Mode

Recommended user-friendly formulas:

```excel
=TESS.KPIVALUE("Total Transaction Count This Year")
```

```excel
=TESS.KPIGOAL("Total Transaction Count This Year")
```

```excel
=TESS.KPISTATUS("Total Transaction Count This Year")
```

```excel
=TESS.KPITREND("Total Transaction Count This Year")
```

Better internal/stable formula option:

```excel
=TESS.KPIVALUEBYID("kpi_01JXYZ123")
```

```excel
=TESS.KPISTATUSBYID("kpi_01JXYZ123")
```

Recommended principle:

- Display friendly names in the UI.
- Store stable KPI IDs in workbook metadata.
- Prefer ID-based formula references if acceptable for UX.

### 24.5.2 Native Cube Function Mode

If exposing through XMLA/MDX-compatible cube metadata, KPIs may be surfaced through cube KPI metadata and queried through supported cube functions or generated MDX.

The plugin should hide the technical implementation and insert either:

- Native cube KPI references where supported.
- Generated `CUBEVALUE` formulas for value/goal/status/trend.
- Hidden helper formulas.

---

## 24.6 KPI MDX Validation Rules

The KPI validator must check:

1. Value Measure exists.
2. Goal Measure exists.
3. Value Measure returns numeric/scalar output.
4. Goal Measure returns numeric/scalar output.
5. Status Expression is valid MDX.
6. Trend Expression is valid MDX.
7. Status Expression returns allowed status values.
8. Trend Expression returns allowed trend values.
9. Referenced date hierarchy exists if previous-period comparison is used.
10. Expressions do not return sets.
11. Expressions do not use blocked MDX functions.
12. Expressions do not reference inaccessible objects.
13. Parent KPI does not create a cycle.
14. Weight is numeric and valid.
15. Display folder is valid.

---

## 24.7 KPI Security and Governance

Required permissions:

| Permission         | Description                |
| ------------------ | -------------------------- |
| `kpi.read`         | View permitted KPIs        |
| `kpi.evaluate`     | Evaluate KPI values        |
| `kpi.use`          | Insert/use KPI in workbook |
| `kpi.create`       | Create KPIs                |
| `kpi.edit`         | Edit owned/shared KPIs     |
| `kpi.certify`      | Certify governed KPIs      |
| `kpi.advanced_mdx` | Edit KPI MDX expressions   |
| `kpi.admin`        | Manage all KPIs            |

Governance rules:

- Certified KPIs must have description, owner, valid value measure, valid goal measure, valid status expression, and valid trend expression.
- Certified KPIs should be versioned.
- Deprecated KPIs should remain usable in existing workbooks but hidden from default search.
- KPI updates should trigger workbook impact analysis if usage tracking exists.

---

## 24.8 KPI Testing Requirements

Back-end tests:

- Create KPI with valid value/goal/status/trend.
- Reject KPI with missing value measure.
- Reject KPI with invalid goal measure.
- Reject status expression that returns unsupported value.
- Reject trend expression that references unknown hierarchy.
- Reject circular parent KPI relationship.
- Validate KPI dependencies are extracted correctly.
- Evaluate KPI under security context.

Front-end tests:

- Add KPI form validates required fields.
- KPI card renders value, goal, status, and trend.
- KPI preview shows business and technical details correctly.
- Insert options generate expected worksheet output.
- Advanced MDX fields are hidden from users without permission.
- Friendly validation errors are shown.

Excel tests:

- Insert KPI as card.
- Insert KPI as value only.
- Insert KPI as full row.
- Refresh KPI values.
- Workbook metadata records KPI usage.
- Deprecated KPI warning appears.

---

## 24.9 KPI Acceptance Criteria

KPI support is accepted when:

1. Users can create a KPI with Name, Display Name, Value Measure, Goal Measure, Status Expression, Trend Expression, Status Graphic, Trend Graphic, Weight, Parent KPI, Display Folder, and Description.
2. The backend validates value and goal measures.
3. The backend validates status and trend expressions.
4. Users can preview KPI value, goal, status, and trend before insertion.
5. Users can insert KPI output into Excel.
6. KPI cards are available in the plugin task pane.
7. KPIs are searchable by name, display folder, owner, and tags.
8. KPI metadata is stored centrally.
9. KPI usage is recorded in workbook metadata where possible.
10. Advanced MDX expressions are permission-controlled.
11. Certified KPIs are clearly marked.
12. Deprecated KPIs show warnings.

---

## 24.10 Example KPI: Total Transaction Count This Year

### Business Definition

```text
Name: total_transaction_count_this_year
Display Name: Total Transaction Count This Year
Description: Tracks the total number of transactions in the current year against the target.
Display Folder: Financial KPIs
```

### Measurement

```text
Value Measure:
[Measures].[Transaction Count YTD]

Goal Measure:
[Measures].[Transaction Count Target YTD]
```

### Status Expression

```mdx
IIf(
  [Measures].[Transaction Count YTD] >= [Measures].[Transaction Count Target YTD],
  1,
  -1
)
```

### Trend Expression

```mdx
IIf(
  [Measures].[Transaction Count YTD] >
    ([Measures].[Transaction Count YTD], [Date].[Year].CurrentMember.PrevMember),
  1,
  -1
)
```

### Display

```text
Status Graphic: Traffic Light
Trend Graphic: Standard Arrow
Weight: 1.0
Parent KPI: None
```

### Plain-English Explanation

> This KPI compares the year-to-date transaction count with the year-to-date transaction target. A green status means the count is at or above target. The trend arrow shows whether the transaction count is higher or lower than the previous year.

---

## 25. Acceptance Criteria

### 24.1 MVP Acceptance Criteria

The MVP is accepted when:

1. Users can browse available Named Lists in the Excel task pane.
2. Users can preview a Named List before inserting it.
3. Users can insert a Named List into Excel as a helper set or row list.
4. Analysts can create a Top N list without writing MDX.
5. Analysts can create a fixed manual list.
6. Backend stores both semantic builder definition and compiled MDX.
7. Backend validates MDX before save/execution.
8. Advanced MDX editor is permission-controlled.
9. Row-level security is applied during preview and execution.
10. Friendly errors are shown for invalid, expensive, or inaccessible lists.
11. Workbook metadata records inserted list usage.
12. Certified lists are clearly marked in the UI.

### 24.2 Post-MVP Acceptance Criteria

Later releases may add:

1. Certification workflow.
2. List version pinning and upgrade prompts.
3. Full formula dependency tracking.
4. Natural-language list creation.
5. Usage analytics dashboard.
6. Automated optimisation suggestions.
7. Admin console for list governance.
8. Cross-tool reuse outside Excel.

---

## 25. Recommended Delivery Plan

### Phase 1 — Read and Insert Existing Lists

Deliver:

- Backend list metadata endpoint.
- Excel task pane list browser.
- Preview endpoint.
- Insert as row list.
- Insert as hidden helper set.
- Certified/shared/my lists sections.

Purpose:

- Prove the core consumption workflow.

### Phase 2 — Guided List Creation

Deliver:

- Fixed list builder.
- Top N builder.
- Filtered list builder.
- Compile and validate endpoint.
- Save list endpoint.

Purpose:

- Allow analysts to create useful lists without MDX.

### Phase 3 — Advanced MDX and Governance

Deliver:

- Permission-controlled MDX editor.
- Function allowlist/denylist.
- Cost scoring.
- Version history.
- Certification/deprecation status.

Purpose:

- Support power users and governed enterprise usage.

### Phase 4 — Enterprise Hardening

Deliver:

- Usage tracking.
- Workbook version warnings.
- Audit logs.
- Admin controls.
- Optimisation recommendations.
- Broader Excel compatibility hardening.

---

## 26. Open Decisions

The dev/product team must confirm:

1. Will MVP use native Excel cube formulas, Tessallite custom functions, or both?
2. Which Excel versions are officially supported?
3. Where will workbook metadata be stored?
4. What is the initial MDX function allowlist?
5. What are the default performance limits per tenant/model?
6. Who can certify lists?
7. Should certified list updates create a draft version or immediately remove certification?
8. Is row-level security enforced in the MDX engine, semantic layer, or query router?
9. Should Named Lists be model-specific or reusable across compatible models?
10. Should list names be unique globally, per model, per owner, or per workspace?

---

## 27. Developer Notes

### 27.1 Important Implementation Principle

Do not make Excel the system of record for Named Lists.

Excel should consume, reference, and cache Named Lists, but the authoritative definition should live in the semantic layer/backend.

### 27.2 Store IDs, Not Only Names

Formulas and workbook metadata should use stable IDs where possible.

Bad:

```excel
=TESS.LIST("Top 20 Customers by Revenue")
```

Better internally:

```excel
=TESS.LISTBYID("nl_01JABC123")
```

The UI can still display the friendly name.

### 27.3 Prefer Semantic Definitions Over Raw MDX

A list created through the builder should be stored as:

1. Structured semantic definition.
2. Compiled MDX.
3. Explanation.
4. Dependencies.

This makes the object editable, explainable, governable, and portable.

### 27.4 Do Not Show Raw MDX by Default

Raw MDX should be hidden behind:

- Technical details.
- Advanced editor.
- Admin/power-user permissions.

### 27.5 Friendly Language Is a Requirement

All user-facing errors and explanations must be business-readable. Technical details can be available under “Show technical details”.

---

## 28. Example End-to-End Scenario

### Scenario: Analyst Creates and Inserts “Top 20 Customers by Revenue”

1. Analyst opens Teeallite front end Excel plugin.
2. Goes to **Named Lists**.
3. Clicks **Create**.
4. Selects subject: **Customer**.
5. Selects list type: **Top/Bottom N**.
6. Configures:
   - Direction: Top
   - Count: 20
   - Measure: Revenue
   - Context: Respect workbook filters
7. Clicks **Preview**.
8. Backend compiles:

```mdx
TopCount([Customer].[Customer].Members, 20, [Measures].[Revenue])
```

9. Backend validates expression.
10. Backend previews first 20 customers under user security.
11. UI shows preview and explanation.
12. Analyst names list: **Top 20 Customers by Revenue**.
13. Analyst saves as Shared List.
14. Analyst inserts as hidden helper set.
15. Plugin writes formula or custom function to workbook.
16. Plugin records workbook usage.
17. The report now references the Named List as a reusable filter.

---

## 29. Summary of MVP Build Items

### Front-End

- Named Lists navigation item.
- List browser.
- List card component.
- Search/filter controls.
- Preview panel.
- Insert modal.
- Create wizard for Fixed List and Top N List.
- Technical details panel.
- Permission-aware Advanced MDX editor shell.

### Back-End

- Named List storage model.
- List metadata API.
- Create/update APIs.
- Compile API.
- Validate API.
- Preview API.
- Usage tracking API.
- Permission enforcement.
- MDX safety validator.
- MDX compiler for Fixed and Top N lists.

### Excel Integration

- Insert as rows.
- Insert as hidden helper set.
- Workbook metadata tracking.
- Refresh integration.
- Native cube formula mode and/or Tessallite custom function mode.

### QA

- Builder tests.
- MDX validation tests.
- Permission tests.
- Excel insertion tests.
- Performance guardrail tests.
- Error-state tests.

---

## 30. Final Product Positioning

This feature should be presented to users as:

> Named Lists let you define trusted business selections once and reuse them everywhere in Excel reports. They can be fixed, dynamic, filtered, or certified by your data team. Tessallite handles the technical expression behind the scenes.

KPIs should be presented as:

> KPIs let you define trusted business performance indicators once and reuse the same value, target, status, and trend logic across Excel reports.

It should not be presented as:

> A tool for writing MDX named sets and MDX KPI expressions.

The technical capability is MDX expression support. The product capability is reusable, governed, business-friendly Named Lists and KPIs.