/**
 * Theme: dark (the default the plan sets), light, or whatever the operating system asks for.
 *
 * The choice is a per-browser preference (`mtrtk:theme`, through `prefs`, so blocked storage
 * never throws) and it is applied by writing `data-theme` on `<html>` — the one switch
 * `index.css` listens to. `initTheme()` runs from `main.tsx` before React mounts, so a light
 * browser never flashes the dark palette (and vice versa), and it keeps a "system" choice
 * following `prefers-color-scheme` for as long as the tab lives.
 */
import { useEffect, useId } from "react";
import { Monitor, Moon, Sun } from "lucide-react";
import { Button } from "@/components/ui/button";
import { getPref, usePref } from "@/lib/prefs";
import { cn } from "@/lib/utils";

export type Theme = "dark" | "light" | "system";
/** What actually reaches `data-theme`. */
export type ResolvedTheme = "dark" | "light";

export const THEME_PREF = "theme";
export const DEFAULT_THEME: Theme = "dark";
export const THEMES: { value: Theme; label: string }[] = [
  { value: "dark", label: "Dark" },
  { value: "light", label: "Light" },
  { value: "system", label: "System" },
];

const LIGHT_QUERY = "(prefers-color-scheme: light)";
const ICON = { dark: Moon, light: Sun, system: Monitor } as const;

function isTheme(v: unknown): v is Theme {
  return v === "dark" || v === "light" || v === "system";
}

/** The saved choice, validated; anything unexpected (or unreadable storage) reads as the default. */
export function savedTheme(): Theme {
  const raw = getPref<unknown>(THEME_PREF, DEFAULT_THEME);
  return isTheme(raw) ? raw : DEFAULT_THEME;
}

function prefersLight(): boolean {
  try {
    return typeof window.matchMedia === "function" && window.matchMedia(LIGHT_QUERY).matches;
  } catch {
    return false; // a browser without matchMedia keeps the plan's default
  }
}

/** "system" asks the operating system; the other two are already the answer. */
export function resolveTheme(theme: Theme): ResolvedTheme {
  if (theme === "system") return prefersLight() ? "light" : "dark";
  return theme;
}

/** Write the theme onto `<html>`. The only place `data-theme` is set. */
export function applyTheme(theme: Theme): void {
  document.documentElement.dataset.theme = resolveTheme(theme);
}

let watching = false;

/**
 * Apply the saved theme and keep a "system" choice in step with the operating system. Called
 * from `main.tsx` before `createRoot`, so the first paint is already the right palette.
 */
export function initTheme(): void {
  applyTheme(savedTheme());
  if (watching || typeof window.matchMedia !== "function") return;
  try {
    const mql = window.matchMedia(LIGHT_QUERY);
    mql.addEventListener("change", () => {
      if (savedTheme() === "system") applyTheme("system");
    });
    watching = true;
  } catch {
    // no media-query support: "system" simply resolves once, at load
  }
}

/** `const [theme, setTheme] = useTheme()` — setting it also applies it. */
export function useTheme(): [Theme, (t: Theme) => void] {
  const [raw, write] = usePref<unknown>(THEME_PREF, DEFAULT_THEME);
  const theme = isTheme(raw) ? raw : DEFAULT_THEME;
  // A tab that never touches the toggle still has to obey a sibling tab's choice.
  useEffect(() => applyTheme(theme), [theme]);
  return [
    theme,
    (next: Theme) => {
      write(next);
      applyTheme(next);
    },
  ];
}

/**
 * The rail's foot control: one button that steps dark → light → system → dark. Its name says
 * what the next press does, so the choice is legible without opening anything.
 */
export function ThemeToggle({ className }: { className?: string }) {
  const [theme, setTheme] = useTheme();
  const next = THEMES[(THEMES.findIndex((t) => t.value === theme) + 1) % THEMES.length];
  const Icon = ICON[next.value];
  return (
    <Button
      variant="ghost"
      size="sm"
      className={cn("w-full justify-start gap-3 px-3 font-normal text-ink-2 hover:text-ink max-lg:justify-center max-lg:px-0", className)}
      aria-label={`Switch to ${next.label.toLowerCase()} theme`}
      onClick={() => setTheme(next.value)}
    >
      <Icon className="size-4 shrink-0" aria-hidden />
      <span className="rail-label max-lg:sr-only">{next.label} theme</span>
    </Button>
  );
}

/** The same preference as a named field, for the Settings page. */
export function ThemeSelect() {
  const [theme, setTheme] = useTheme();
  const id = useId();
  return (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div>
        <label htmlFor={id} className="text-[14px] leading-5">
          Theme
        </label>
        <p className="text-[12px] leading-4 text-ink-2">Kept in this browser only; the base station has no say in it.</p>
      </div>
      <select
        id={id}
        value={theme}
        onChange={(e) => setTheme(e.target.value as Theme)}
        className="h-9 rounded-md border border-line bg-panel-2 px-2 text-[14px] text-ink"
      >
        {THEMES.map((t) => (
          <option key={t.value} value={t.value}>
            {t.label}
          </option>
        ))}
      </select>
    </div>
  );
}
