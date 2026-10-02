/**
 * Shared pointer rules for the charts (mouse, pen and touch alike — `onPointer*`, never
 * `onMouse*`, which a finger never fires).
 *
 * - A continuous chart (a line over time or frequency) reads the pointer on `pointerdown` and
 *   `pointermove` and carries `SCRUB`: `touch-action: pan-y`, so a sideways drag scrubs the
 *   crosshair while a vertical swipe still scrolls the page — `none` would trap a phone's scroll
 *   on every chart.
 * - A chart of discrete marks (sky discs, signal bars) reads `pointerenter`/`pointerdown` on the
 *   mark and keeps the browser's own touch handling, so the frame still scrolls: a tap reads one.
 * - Leaving clears the readout only for a pointer that hovers: lifting a finger also fires
 *   `pointerleave`, and the reading the operator just tapped for should stay on screen.
 */
export const SCRUB = "touch-pan-y";

export const clearsOnLeave = (e: { pointerType: string }) => e.pointerType !== "touch";
