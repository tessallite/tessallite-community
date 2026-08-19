# shared-ui

Source-only shared package for the conversational agent chat UI. Contains types,
components, streaming engine, and state management consumed by three apps:

- **Main SPA** (`tessallite/frontend`) — full agent chat with trace drawer, judge UI, personas, feedback
- **Conversational client** (`conversational-client/frontend`) — standalone chat with model picker, embed mode
- **Excel plugin** (`tessallite/excel-plugin`) — full shared chat canvas with Excel-specific Insert Table/Chart/Pivot actions

## How it works

No build step, no `package.json`, no `node_modules`. Consumers resolve imports
via tsconfig path alias (`@tessallite/shared-ui/*` -> `shared-ui/src/*`) and
Vite `resolve.alias`. Each consumer's `node_modules` provides React, MUI,
Zustand, and other dependencies.

### Local development

tsc follows path aliases into shared-ui source and needs to resolve third-party
types from the consumer's `node_modules`. Create a symlink one directory above
shared-ui to simulate npm workspace hoisting:

```bash
# From tessallite/ (parent of shared-ui, frontend, excel-plugin):
ln -sfn frontend/node_modules node_modules
```

This lets `npx tsc --noEmit` in any consumer resolve shared-ui's imports.
Without the symlink, tsc errors like "Cannot find module 'react'" appear for
shared-ui files even though the consumer builds fine with Vite (Vite uses
`resolve.alias` and doesn't follow tsc's module resolution).

### Docker builds

The same hoisting approach works in Docker:

```dockerfile
RUN ln -s /app/frontend/node_modules /app/node_modules
COPY shared-ui/ /app/shared-ui/
```

## Package structure

```
src/
  index.ts                  # barrel export
  types/                    # adapter, turn, conversation, config, streaming
  providers/ChatProvider    # React context: adapter + t() + config + projectId
  stores/conversationStore  # Zustand store (active conv, streaming state)
  streaming/messagesStream  # SSE engine with retry, timeout, idempotency guard
  hooks/useAutoScroll       # auto-scroll with near-bottom detection
  utils/chartSpec           # auto-chart spec builder
  components/               # 22 components (ChatCanvas, AssistantTurn, RenderedOutput, etc.)
```

## Adapter pattern

Each consumer implements `AgentChatAdapter` to bridge its API transport layer
(axios+CSRF, fetch+Bearer, custom client) to the shared components. The adapter
returns a raw `Response` from `streamMessageRaw`; the shared SSE engine handles
parsing, retry, and timeout.

## Security

All rendered HTML passes through DOMPurify and renders inside a sandboxed iframe
(`sandbox="allow-same-origin"`, never `allow-scripts`). Consumers must not
render `rendered_output` via raw `dangerouslySetInnerHTML`.

## i18n

Zero i18n dependencies. Receives a `t(key, params?)` function via `ChatProvider`
context. Each consumer provides its own translation backend.
