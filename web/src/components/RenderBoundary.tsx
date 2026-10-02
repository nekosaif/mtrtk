import { Component, type ReactNode } from "react";

/**
 * Catches a render error below it and shows `fallback` in its place, with a `reset` that renders
 * the children again. Used where one failure must not take the rest of the screen with it: the
 * lazy maps (a chunk that fails to load) and the Shell's page outlet (the rail and the tape stay).
 * React 19 already reports a caught error to the console, so this does not log it again.
 */
export class RenderBoundary extends Component<
  { children: ReactNode; fallback: (error: Error, reset: () => void) => ReactNode },
  { error: Error | null }
> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: unknown) {
    return { error: error instanceof Error ? error : new Error(String(error)) };
  }

  reset = () => this.setState({ error: null });

  render() {
    return this.state.error ? this.props.fallback(this.state.error, this.reset) : this.props.children;
  }
}
