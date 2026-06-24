# Tessallite Excel Plugin

Office Add-in for Excel that integrates with the Tessallite semantic layer platform.

## Requirements

- Node.js 18+
- npm 9+

## Setup

```bash
npm install
```

## Development

```bash
npm run dev        # Start Vite dev server with HTTPS on port 3000
npm run build      # TypeScript check + production build
npm run preview    # Preview production build
npm test           # Run unit tests
npm run test:watch # Run tests in watch mode
```

### Sideloading in Excel

1. Run `npm run dev` to start the dev server
2. Open Excel (Desktop or Web)
3. Go to **Insert > Add-ins > Upload My Add-in**
4. Select `manifest.xml` from this directory
5. The Tessallite tab will appear on the ribbon

## Architecture

The task pane has three tabs: **Analyse** (Report Builder), **KPIs**, and **Ask** (conversational agent).

- `src/App.tsx` — Root application shell (3-tab switcher, header, footer)
- `src/api/` — API clients (auth, agent service, model service, query router)
- `src/components/ReportBuilder/` — Report Builder panel (measures, dimensions, query execution)
- `src/components/KpiPanel/` — KPI scorecard panel (batch evaluation, insert table/chart)
- `src/components/AskTessallite/` — Chat panel, message bubbles, insert actions
- `src/components/LoginScreen/` — Login form
- `src/components/Toast/` — Toast notification system
- `src/hooks/` — React hooks (useAuth, useExcel, useModel)
- `src/utils/` — Storage abstraction, Excel formulas, Office.js spike, metadata persistence
- `src/types/` — TypeScript type definitions

## Storage

All auth data uses `OfficeRuntime.storage` for secure, sandboxed persistence. Passwords are never stored.

## Non-Excel BI Clients

Looker Studio/Data Studio direct does not run through the Office.js add-in or
Excel/XMLA surface. It uses the Tessallite PostgreSQL wire gateway without
LookML. A customer-supplied Looker instance can optionally consume a generated
LookML adapter. See
`docs/architecture/looker-and-looker-cloud-core-client-specs.md` and the
integration help pages under `../help/integrations/`.

## Design Tokens

Tokens in `src/theme.ts` mirror the main Tessallite frontend (`frontend/src/theme/tokens.ts`). Keep them in sync.

## Testing

Unit tests cover utility functions (formula generation, storage). Excel integration tests require a running Excel host.
