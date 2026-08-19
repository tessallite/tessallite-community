import React from 'react';
import ReactDOM from 'react-dom/client';
import 'charts.css';
import App from './App';
import { strings } from './i18n/strings';
import { initialiseLocale } from './i18n/runtime';

// Phase A: shared runtime — import the custom functions module so
// TESSALLITE.* and TESS.* functions are registered in the same JS
// context as the taskpane. Without this import, the shared runtime
// never calls CustomFunctions.associate and Excel cannot resolve
// the function names.
import './functions';


class ErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { error: Error | null }
> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) { return { error }; }
  componentDidCatch(error: Error, info: React.ErrorInfo) {
    // Log the full stack trace to the console for debugging; never expose
    // it in the UI (Bug-5780).
    console.error('[Tessallite ErrorBoundary]', error, info.componentStack);
  }
  render() {
    if (this.state.error) {
      return (
        <div style={{ padding: 24, fontFamily: 'sans-serif', fontSize: 13, color: '#333', textAlign: 'center' }}>
          <b style={{ fontSize: 15 }}>{strings.errorBoundary.title}</b>
          <p style={{ marginTop: 8 }}>
            {strings.errorBoundary.description}
          </p>
          <p style={{ marginTop: 4, fontSize: 11, color: '#888' }}>
            {strings.errorBoundary.persistNote}
          </p>
        </div>
      );
    }
    return this.props.children;
  }
}

let rendered = false;

function renderApp() {
  if (rendered) return;
  rendered = true;
  initialiseLocale();
  ReactDOM.createRoot(document.getElementById('root')!).render(
    <React.StrictMode>
      <ErrorBoundary>
        <App />
      </ErrorBoundary>
    </React.StrictMode>,
  );
}

if (typeof Office !== 'undefined' && Office.onReady) {
  Office.onReady(renderApp);
  window.setTimeout(renderApp, 1500);
} else {
  renderApp();
}
