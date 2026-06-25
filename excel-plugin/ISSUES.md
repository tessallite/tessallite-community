# Excel Plugin — Open Issues

## 1. Black icon — FIXED
The Tessallite button in the Excel ribbon showed a black/placeholder icon. Root cause: manifest used HTTP URLs; Excel requires HTTPS for add-in resources. Also needed `office-addin-dev-certs` to install a trusted CA.

**Fix:** Added HTTPS on nginx port 3443 with Office-trusted certs. Manifest updated to `https://localhost:3443`.

## 2. "Add Chart" creates blank worksheet instead of chart — FIXED
Clicking insert chart created a new worksheet with a table but no chart rendered.

**Root cause:** Two bugs: (a) `dataRange.address` accessed without `range.load('address')` causing PropertyNotLoaded error that killed the Excel.run before chart creation; (b) nested `Excel.run` from `setTableMetadata` invalidated outer context proxies; (c) `ChartSeriesBy.auto` failed with mixed text/numeric columns.

**Fix:** Added `range.load('address')`, moved `setTableMetadata` outside `Excel.run`, created separate chart data range with category-first layout and `ChartSeriesBy.columns`.

## 3. "Connection lost. Retrying..." appears frequently — FIXED
The plugin health check called `GET /health` which didn't match any nginx proxy location, so nginx returned the SPA HTML. The JSON parse failed, triggering the "Connection lost" banner.

**Fix:** Added `location = /health` proxy route in nginx.conf.template to forward to model-service.

## 4. "Insert failed" even when values are correctly inserted — FIXED
Same root cause as #2: `range.address` accessed without `range.load('address')` in `officeSpike.ts:insertResultTable`. Data was written and synced successfully, then the PropertyNotLoaded error triggered the catch block showing "Insert failed".

**Fix:** Added `range.load('address')` before `context.sync()` in `insertResultTable`.

## 5. Agent (Ask Tessallite) does not work — FIXED
The backend SSE stream sends `{"text":"..."}` for narration deltas and `{"turn_id":..., "answer_text":..., "result_sample":...}` for completion, but the plugin expected `{"content":"..."}` and `{"message_id":..., "query_result":{"data":...}}`. Field name mismatch caused all streamed text to be silently dropped.

**Fix:** Mapped `text`→`content`, `turn_id`→`message_id`, `result_sample`→`query_result.data`, `answer_text` as fallback content.

## 6. Manifest deployment — DONE

### Problem
The manifest XML must be delivered to each user's Excel. There is no way to point Office at an HTTP URL as an add-in catalog (Office requires a UNC network share or centralized deployment). Currently the manifest is manually placed in a shared folder.

### Required feature: "Download Excel Add-in Manifest" button
The Tessallite frontend should provide a UI to generate and download the manifest file. The manifest must contain the customer's own Tessallite server URL (configurable, stored in settings or database). Plugin static files are already served from `/excel-plugin/` on nginx.

### Customer deployment flows (document both)

**Flow A — Shared folder (works everywhere including Excel 2016, on-prem)**
1. Modeller/admin clicks "Download Excel Add-in Manifest" in Tessallite UI
2. Admin sends the manifest file to IT
3. IT places it in a network share (e.g., `\\fileserver\office-addins\`)
4. IT configures the share as a "Trusted Catalog" via Group Policy or per-user Excel settings
5. IT manages user access to the share (LDAP group, NTFS permissions, etc.)
6. Each user opens Excel → Office Add-ins → Shared Folder tab → enables Tessallite
7. IT handles onboarding/offboarding by managing share access

**Flow B — Microsoft 365 Centralized Deployment (recommended for M365 orgs)**
1. Modeller/admin clicks "Download Excel Add-in Manifest" in Tessallite UI
2. IT admin uploads the manifest to the Microsoft 365 Admin Center (admin.microsoft.com → Integrated Apps)
3. IT admin assigns the add-in to users or Azure AD/LDAP security groups
4. The add-in appears automatically in every assigned user's Excel — no per-machine setup
5. Onboarding/offboarding is automatic via group membership

### What Tessallite provides (same for both flows)
- A "Download Excel Add-in Manifest" button in the frontend UI (endpoint settings or model explorer)
- The manifest is generated server-side with the customer's configured base URL
- The endpoint appears in the Model Explorer alongside JDBC, XMLA, and REST endpoints
- Plugin static files served at `/excel-plugin/` on the customer's own nginx (zero call-home)

## 7. No model picker in plugin UI — FIXED
The plugin auto-selected the first project and first model with no way to switch.

**Fix:** Added a compact model Select dropdown in the header bar between the Tessallite title and action icons. Changing the model resets the conversation and persona. Project/model lists are stored during initial load.

## 8. No persona picker present — NOT A BUG
The PersonaDropdown in the footer bar only renders when the model has personas defined. The demo model has no personas, so the picker correctly doesn't appear. Works as designed.

## 9. Agent never suggests charts — FIXED
The ChatPanel's `recommendedAction` didn't check the agent's `chart_type` field, and the `Message` interface was missing `chartType`.

**Fix:** Added `chartType` to the ChatPanel Message interface. Updated `recommendedAction` to prefer 'chart' when the agent returns a non-kpi `chart_type`. The Chart button now gets the gold highlight border when the agent recommends a chart.

## 10. Generated worksheets missing metadata footer
When the plugin inserts a table, chart, or pivot table into a worksheet, there is no visible footer showing provenance metadata — last updated timestamp, source system, model name, tenant, query, persona, etc. This information is stored via hidden named ranges (`setTableMetadata`) but is not surfaced to the user on the sheet itself.

**Expected:** Every generated worksheet should have a footer block below the data (or in the sheet footer) showing at minimum: timestamp, tenant/project, model name, source query or semantic description, and persona (if active). This gives analysts traceability and auditors a paper trail without needing to inspect hidden metadata.

**Fix (F-025-23):** RESOLVED. `insertProvenanceFooter` now renders `Source: Tessallite | Model: <name> | Viewing as: <persona> | <timestamp> UTC` below every inserted table; the model name and active persona are threaded through `InsertMetadata` (`modelLabel`/`personaLabel`) from the Report Builder. The full semantic query, project/tenant, and version remain in the hidden named-range metadata (`setTableMetadata`), readable via the Query Trace modal. Tenant and the verbatim query are intentionally kept out of the visible cell footer to avoid leaking identifiers into shared workbooks; the visible footer covers the analyst-facing traceability fields (model, persona, timestamp).
