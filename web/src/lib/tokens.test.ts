/**
 * Design-token contract for `src/index.css`.
 *
 * The tokens are the design system: two theme blocks and one `@theme inline` map that turns them
 * into Tailwind utilities. Three things can quietly break there and nothing else would catch it:
 * a token declared in one theme and not the other (the dark value leaks into light), a literal
 * colour written into `@theme inline` (theme-blind by construction — this is how the primary
 * button came to set near-black text on light brass at 3.65:1), and a text/surface pair that
 * drops below WCAG AA. Each gets an assertion here, contrast included, computed from the file.
 */
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

// Vitest runs with `css: false`, so a `?raw` import of a stylesheet comes back empty, and
// `import.meta.url` under jsdom is an http URL: find the file from the run directory instead
// (`pnpm --dir web test` puts it at web/, a bare `vitest` run from the repo root at the other).
const CSS_PATH = ["src/index.css", "web/src/index.css"].map((p) => resolve(process.cwd(), p)).find(existsSync);
if (!CSS_PATH) throw new Error("index.css not found from " + process.cwd());
const css = readFileSync(CSS_PATH, "utf8");

type Block = Record<string, string>;

/** The declarations of one top-level `{ … }` block, by selector. */
function block(selector: string): Block {
  const at = css.indexOf(selector + " {");
  if (at < 0) throw new Error(`no block for ${selector}`);
  const open = css.indexOf("{", at);
  const close = css.indexOf("\n}", open);
  const body = css.slice(open + 1, close).replace(/\/\*[\s\S]*?\*\//g, "");
  const out: Block = {};
  for (const line of body.split(";")) {
    const m = line.match(/(--[\w-]+)\s*:\s*(.+)/s);
    if (m) out[m[1]] = m[2].trim();
  }
  return out;
}

const dark = block(":root");
const light = block(':root[data-theme="light"]');
const theme = block("@theme inline");

/** `#rrggbb` → linear-light relative luminance (WCAG 2.x). */
function luminance(hex: string): number {
  const n = parseInt(hex.slice(1), 16);
  const ch = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((v) => {
    const s = v / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2];
}

function contrast(a: string, b: string): number {
  const [x, y] = [luminance(a), luminance(b)];
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
}

/** Resolve a token to its `#rrggbb` value within one theme, following `var(--…)` chains. */
function hex(tokens: Block, name: string, depth = 0): string {
  const raw = tokens[name];
  if (raw === undefined) throw new Error(`${name} is not declared in this theme`);
  if (raw.startsWith("#")) return raw;
  const ref = raw.match(/var\((--[\w-]+)\)/);
  if (ref && depth < 8) return hex(tokens, ref[1], depth + 1);
  throw new Error(`${name} does not resolve to a hex colour (${raw})`);
}

const THEMES: [string, Block][] = [
  ["dark", dark],
  ["light", { ...dark, ...light }], // light overrides dark, exactly as the cascade does
];

describe("design tokens (src/index.css)", () => {
  it("declares every themed token in both blocks, so no dark value can leak into light", () => {
    // The four status marks are the deliberate exception: the plan fixes them across both themes.
    const FIXED = ["--status-good", "--status-warning", "--status-serious", "--status-critical"];
    expect(Object.keys(dark).filter((k) => k.startsWith("--") && !FIXED.includes(k) && !(k in light))).toEqual([]);
    expect(Object.keys(light).filter((k) => k.startsWith("--") && !(k in dark))).toEqual([]);
  });

  it("carries the plan's palette table unchanged", () => {
    expect([dark["--bg"], dark["--panel"], dark["--ink"], dark["--brass"]]).toEqual(["#0f1420", "#161d2e", "#f2eee6", "#e0b25a"]);
    expect([light["--bg"], light["--panel"], light["--ink"], light["--brass"]]).toEqual(["#f3f4f7", "#ffffff", "#141a2b", "#8a6a1f"]);
  });

  it("keeps the status palette fixed: the same four values in both themes", () => {
    for (const level of ["good", "warning", "serious", "critical"]) {
      expect(light[`--status-${level}`]).toBeUndefined();
    }
    expect([dark["--status-good"], dark["--status-warning"], dark["--status-serious"], dark["--status-critical"]]).toEqual(["#0ca30c", "#fab219", "#ec835a", "#d03b3b"]);
  });

  it("writes no literal colour into @theme inline: every mapping goes through a themed token", () => {
    const literals = Object.entries(theme).filter(([k, v]) => k.startsWith("--color-") && !v.startsWith("var("));
    expect(literals).toEqual([]);
  });

  it("maps only tokens that both themes can resolve", () => {
    for (const [name, value] of Object.entries(theme)) {
      const ref = value.match(/var\((--[\w-]+)\)/);
      if (!ref || !name.startsWith("--color-")) continue;
      for (const [label, tokens] of THEMES) {
        expect(() => hex(tokens, ref[1]), `${label}: ${name} → ${ref[1]}`).not.toThrow();
      }
    }
  });

  it.each(THEMES)("%s: body text and the muted second tone clear AA on both surfaces", (_label, tokens) => {
    for (const surface of ["--bg", "--panel", "--panel-2"]) {
      expect(contrast(hex(tokens, "--ink"), hex(tokens, surface))).toBeGreaterThanOrEqual(4.5);
      expect(contrast(hex(tokens, "--ink-2"), hex(tokens, surface))).toBeGreaterThanOrEqual(4.5);
    }
  });

  it.each(THEMES)("%s: the primary button's label clears AA on the brass fill", (_label, tokens) => {
    expect(contrast(hex(tokens, "--on-brass"), hex(tokens, "--brass"))).toBeGreaterThanOrEqual(4.5);
    expect(contrast(hex(tokens, "--on-status"), hex(tokens, "--status-critical"))).toBeGreaterThanOrEqual(4.5);
  });

  it.each(THEMES)("%s: a status word clears AA on the page and on a panel", (_label, tokens) => {
    for (const level of ["good", "warning", "serious", "critical"]) {
      for (const surface of ["--bg", "--panel"]) {
        expect(contrast(hex(tokens, `--status-${level}-text`), hex(tokens, surface)), `--status-${level}-text on ${surface}`).toBeGreaterThanOrEqual(4.5);
      }
    }
  });

  it("maps the two utilities the components use but @theme never emitted", () => {
    // `.text-ink` is written 49 times across 23 files — 18 of them `hover:`/`data-[state=active]:`
    // variants — and without this mapping Tailwind emits no such class at all: the active rail
    // item never brightened, the empty state's title never lifted.
    expect(theme["--color-ink"]).toBe("var(--ink)");
    // Every server error in the app lands in an `AlertDescription`. `--color-destructive` is the
    // *fill* behind the destructive button, paired with `--on-status`; as body text it is
    // 3.04–4.17:1 and fails AA in both themes, so the alert takes the text form instead.
    expect(theme["--color-destructive-text"]).toBe("var(--status-critical-text)");
  });

  it.each(THEMES)("%s: an error message clears AA on every surface it can land on", (_label, tokens) => {
    for (const surface of ["--bg", "--panel", "--panel-2"]) {
      expect(contrast(hex(tokens, "--status-critical-text"), hex(tokens, surface)), `--status-critical-text on ${surface}`).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("keeps the focus ring and the reduced-motion guard the plan asks for", () => {
    expect(css).toContain(":focus-visible { outline: 2px solid var(--brass-2); outline-offset: 2px; }");
    expect(css).toContain("@media (prefers-reduced-motion: reduce)");
  });
});
