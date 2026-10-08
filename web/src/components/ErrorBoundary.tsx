import { Component, type ErrorInfo, type ReactNode } from "react";

// Per-page error boundary: a render error on ONE screen (unexpected data, a
// crashing library) no longer takes the whole app down to a black screen -- the
// rail/bar stay alive and the user can navigate elsewhere or retry. `resetKey`
// (the current route) clears the error on navigation.
export class ErrorBoundary extends Component<{ children: ReactNode; resetKey?: string }, { error: Error | null }> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("[ui] render error caught", error, info.componentStack);
  }

  componentDidUpdate(prev: { resetKey?: string }) {
    if (prev.resetKey !== this.props.resetKey && this.state.error) this.setState({ error: null });
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="flex h-full items-center justify-center p-6">
        <div className="max-w-sm rounded-2xl border border-line bg-surface p-6 text-center">
          <p className="text-base font-semibold text-ink">Algo salió mal en esta pantalla</p>
          <p className="mt-1 text-sm text-ink-dim">El resto de la plataforma sigue funcionando. Puedes reintentar o ir a otra sección.</p>
          <button
            onClick={() => this.setState({ error: null })}
            className="mt-4 rounded-xl bg-brand-600 px-4 py-2 text-sm font-medium text-white hover:bg-brand-500"
          >
            Reintentar
          </button>
        </div>
      </div>
    );
  }
}
