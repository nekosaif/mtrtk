/**
 * `@theme` coverage: every Tailwind colour utility written in a component must have a matching
 * `--color-*` in `src/index.css`'s `@theme inline`. Tailwind emits nothing for a utility it has no
 * colour for, silently — that is how `.text-ink` went missing from the build for 49 call sites
 * (18 of them `hover:` / `data-[state=active]:` variants) while every test passed. This scans the
 * source as text, so it also catches a typo in a token name the moment it is written.
 */
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join, relative, resolve } from "node:path";

const SRC = ["src", "web/src"].map((p) => resolve(process.cwd(), p)).find((p) => existsSync(join(p, "index.css")));
if (!SRC) throw new Error("web/src not found from " + process.cwd());

const css = readFileSync(join(SRC, "index.css"), "utf8");
const themeAt = css.indexOf("@theme inline {");
const themeBody = css.slice(themeAt, css.indexOf("\n}", themeAt));
const COLOURS = new Set([...themeBody.matchAll(/--color-([\w-]+)\s*:/g)].map((m) => m[1]));

/** Every `.tsx` under src, tests excluded (their strings are fixtures, not class lists). */
function sources(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const p = join(dir, e.name);
    if (e.isDirectory()) return sources(p);
    return e.name.endsWith(".tsx") && !e.name.includes(".test.") ? [p] : [];
  });
}

/** The class-like words of every string literal in a file ("…", '…' and `…` alike). */
function words(text: string): string[] {
  const out: string[] = [];
  for (const m of text.matchAll(/"([^"\n]*)"|'([^'\n]*)'|`([^`]*)`/g)) out.push(...(m[1] ?? m[2] ?? m[3]).split(/\s+/));
  return out.filter(Boolean);
}

// Utility prefixes that take a colour, longest first so `ring-offset-x` is not read as `ring-*`.
const PREFIXES = ["ring-offset", "decoration", "placeholder", "outline", "accent", "border-t", "border-b", "border-l", "border-r", "border-x", "border-y", "border-s", "border-e", "border", "divide", "stroke", "caret", "fill", "ring", "text", "bg"];
// Values those prefixes take that are not colours.
const NOT_COLOUR = new Set([
  // text
  "xs", "sm", "base", "lg", "xl", "2xl", "3xl", "4xl", "5xl", "left", "right", "center", "justify", "start", "end", "wrap", "nowrap", "balance", "pretty", "ellipsis", "clip",
  // border / outline / divide (and a bare side: `border-b` is the width utility)
  "t", "b", "l", "r", "s", "e", "solid", "dashed", "dotted", "double", "hidden", "none", "collapse", "separate", "x", "y", "reverse", "offset",
  // ring
  "inset",
  // bg
  "fixed", "local", "scroll", "cover", "contain", "auto", "repeat", "no-repeat", "top", "bottom",
  // not Tailwind at all: `box-sizing:border-box` in a marker's cssText, MapLibre's `fill-color` / `fill-opacity` paint keys
  "box", "color", "opacity",
]);
// Colour keywords Tailwind resolves without a theme entry, all theme-neutral by construction.
const KEYWORDS = new Set(["transparent", "current", "inherit"]);

/** `hover:data-[state=active]:text-ink/60` → `{prefix: "text", value: "ink"}`, or null if not a colour utility. */
export function colourUtility(word: string): { prefix: string; value: string } | null {
  // drop variants (`hover:`, `data-[state=active]:`, `[&_.num]:`) — a colon inside brackets is not a variant separator
  let depth = 0;
  let cut = 0;
  for (let i = 0; i < word.length; i++) {
    const ch = word[i];
    if (ch === "[" || ch === "(") depth++;
    else if (ch === "]" || ch === ")") depth--;
    else if (ch === ":" && depth === 0) cut = i + 1;
  }
  const base = word.slice(cut).replace(/^[!-]/, "").replace(/\/\d+$/, "");
  const prefix = PREFIXES.find((p) => base.startsWith(p + "-"));
  if (!prefix) return null;
  const value = base.slice(prefix.length + 1);
  if (!/^[a-z][a-z0-9-]*$/.test(value)) return null; // arbitrary values (`[14px]`, `(--x)`) and numbers
  if (NOT_COLOUR.has(value) || value.startsWith("offset-") || value.startsWith("spacing-")) return null;
  return { prefix, value };
}

const FILES = sources(SRC);

describe("@theme coverage", () => {
  it("parses the variants, the opacity and the non-colour forms the way Tailwind does", () => {
    expect(colourUtility("hover:text-ink")).toEqual({ prefix: "text", value: "ink" });
    expect(colourUtility("data-[state=active]:bg-panel-2/60")).toEqual({ prefix: "bg", value: "panel-2" });
    expect(colourUtility("[&_.num]:text-ink-3")).toEqual({ prefix: "text", value: "ink-3" });
    expect(colourUtility("focus-visible:ring-ring/50")).toEqual({ prefix: "ring", value: "ring" });
    expect(colourUtility("ring-offset-background")).toEqual({ prefix: "ring-offset", value: "background" });
    expect(colourUtility("border-t-transparent")).toEqual({ prefix: "border-t", value: "transparent" });
    for (const w of ["text-[14px]", "text-right", "border-b", "border-0", "ring-2", "ring-offset-2", "outline-none", "text-(--x)", "bg-[url(x)]", "fill-none", "max-sm:text-sm"]) expect(colourUtility(w), w).toBeNull();
  });

  it("finds the source it is meant to cover", () => {
    expect(FILES.length).toBeGreaterThan(50);
    expect(COLOURS.has("ink")).toBe(true);
  });

  it("has a --color-* in @theme inline for every colour utility in a component", () => {
    const missing: string[] = [];
    for (const file of FILES) {
      for (const w of words(readFileSync(file, "utf8"))) {
        const u = colourUtility(w);
        if (u && !KEYWORDS.has(u.value) && !COLOURS.has(u.value)) missing.push(`${relative(SRC, file)}: ${w}`);
      }
    }
    expect([...new Set(missing)]).toEqual([]);
  });

  // B4 — a status *word* never takes the fixed (mark) colour: on a light panel the amber is 1.8:1.
  // Text takes `text-status-*-text`, an icon or a dot `*-status-*-mark`; the bare token is the
  // fixed palette itself and is not written as a utility at all.
  it("never writes the fixed status colours as a utility: text takes -text, marks take -mark", () => {
    const bare: string[] = [];
    for (const file of FILES) {
      for (const w of words(readFileSync(file, "utf8"))) {
        const u = colourUtility(w);
        if (u && /^status-(good|warning|serious|critical)$/.test(u.value)) bare.push(`${relative(SRC, file)}: ${w}`);
      }
    }
    expect(bare).toEqual([]);
  });
});
