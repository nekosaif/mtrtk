import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";
import { cleanup, configure } from "@testing-library/react";

afterEach(() => cleanup());

// findBy*/waitFor give up after 1 s by default, which a loaded machine can spend before the
// first render settles. They return as soon as the element shows up, so a longer limit only
// costs time when a test is already failing.
configure({ asyncUtilTimeout: 5000 });

// jsdom lacks these browser APIs used by charts and the map
class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver = ResizeObserverMock as unknown as typeof ResizeObserver;
Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: false, media: query, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
});
