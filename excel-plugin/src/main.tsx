import React from 'react';
import ReactDOM from 'react-dom/client';
import 'charts.css';
import App from './App';

class ErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { error: Error | null }
> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) { return { error }; }
  render() {
    if (this.state.error) {
      return (
        <div style={{ padding: 16, fontFamily: 'monospace', fontSize: 12, color: '#c00' }}>
          <b>Plugin crashed:</b>
          <pre style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
            {this.state.error.message}
            {'\n\n'}
            {this.state.error.stack}
          </pre>
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
