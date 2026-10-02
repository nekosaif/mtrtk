/**
 * Loads the two map modules before a test file's tests run, so `components/LazyMap`'s React.lazy
 * wrappers resolve from the module cache within a microtask. Without it the first map in a file
 * waits on a cold transform inside the test itself, and a loaded machine can push that past
 * Vitest's 5 s test timeout. Use as `beforeAll(preloadMaps)` (hooks get 10 s). A file that mocks
 * `@/components/MapPanel` gets its mock here, as its pages do.
 */
export async function preloadMaps() {
  await Promise.all([import("@/components/MapPanel"), import("@/components/TrackMap")]);
}
