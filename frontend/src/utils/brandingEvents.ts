// Bug-5982: shared event name for "tenant branding changed" notifications.
//
// This lives in its own side-effect-free module (rather than being
// exported from main.tsx, where it originated) because main.tsx has a
// top-level `ReactDOM.createRoot(...).render(...)` call — importing
// anything from main.tsx into another module would execute that render
// call as an import side effect, which breaks both production bundling
// assumptions and unit tests (no `#root` element, double-render).
//
// Dispatchers: BrandingPanel.tsx (after a successful branding save),
// Login.tsx and SsoCallback.tsx (after localStorage.tenant_id is set).
// Listener: main.tsx Root (re-reads tenant_id and refetches branding so
// the global MUI theme picks up the change without a full page reload).
export const BRANDING_CHANGED_EVENT = "tessallite-branding-changed";
