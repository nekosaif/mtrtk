# mtrtk Phase 4: Frontend (Base Station UI) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A polished, instrument-panel web UI for the base station that shows everything the receiver knows — position, accuracy, satellites (sky plot, signal bars, table), RF health and spectrum, RTCM/NTRIP status, survey-in / sites, raw logs, history charts, events — and drives the daemon (mode, sites, receiver actions, settings). Dark by default, light available, usable on a phone in the field.

**Architecture:** React 19 + Vite + TypeScript SPA under `web/`, served by FastAPI (Phase 3). One WebSocket (`/ws`) feeds a zustand store (`useLive`) that every live panel reads; REST via TanStack Query for everything else. Routes: `/`, `/satellites`, `/receiver`, `/corrections`, `/site`, `/logs`, `/history`, `/events`, `/settings`, `/login` (rover routes arrive in Phase 6, PPK in Phase 8). Charts are SVG/canvas components written against the dataviz rules (thin marks, fixed categorical order, hover tooltips, legends, no dual axes). Design tokens live in `web/src/index.css` as Tailwind v4 `@theme` variables; shadcn/ui supplies primitives (button, dialog, table, tabs, select, switch, tooltip, toast).

**Tech Stack:** React 19, Vite 6, TypeScript 5, Tailwind v4, shadcn/ui, react-router 7, @tanstack/react-query 5, zustand 5, maplibre-gl 5, hand-written SVG charts, lucide-react, sonner, @fontsource-variable/instrument-sans, @fontsource/instrument-serif, vitest + @testing-library/react + jsdom, pnpm.

**Spec:** `docs/superpowers/specs/2026-09-18-mtrtk-design.md` — sections *frontend*, *web API*, *Phase 4*. Prerequisites: Phases 1–3 complete (the API shapes below are those plans' outputs). Verify JSON shapes against `GET /api/state` on a running replay before typing `types.ts`.

## Global Constraints

- **Design system (binding — see next section):** tokens, fonts, palette, layout and principles are fixed by this plan. No cream/terracotta, no near-black + acid green, no all-caps labels, no tracked eyebrows, no `→` in buttons, no drop shadows, no uniform card kit; brass accent is spent on the sky plot and active/primary states only.
- **Charts:** constellation colors in fixed order GPS, GLONASS, Galileo, BeiDou, QZSS, SBAS (never cycled; other systems use muted ink); status colors are reserved for fix/verification states and always paired with an icon or label; one y-axis per chart; every multi-series chart has a legend; hover tooltips on every plotted chart; text never in series color.
- **Live data:** exactly one WebSocket per tab, reconnecting with exponential backoff (1 s → 30 s); UI shows a visible "reconnecting" state; stale data is greyed after 5 s without an epoch.
- **Formatting:** coordinates switchable DD / DMS / UTM / ECEF (persisted per browser in `localStorage`, wrapped in try/catch); heights show ellipsoidal and MSL; times UTC with a local-time tooltip; `font-variant-numeric: tabular-nums` on all numeric readouts.
- **Accessibility:** visible keyboard focus (brass ring), `prefers-reduced-motion` respected, color never the sole carrier of meaning, every chart has a table alternative (`Table` tab or `<details>`).
- **Responsive:** rail nav ≥1024px, icon rail 640–1023px, bottom tab bar <640px; no horizontal page scroll at 360px width.
- **Auth:** on any 401 the app routes to `/login`; the token is kept only in the cookie set by `/api/login`; `?token=` is appended to the WebSocket URL when a bearer token was pasted manually.
- **Build:** `pnpm --dir web build` → `web/dist`; the Docker image copies it (Phase 1 Dockerfile); `pnpm --dir web test` (vitest) and `pnpm --dir web lint` (tsc) run in CI.
- Commit per task, Conventional Commits, trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## Design system (decided after a review against the generic-defaults list)

**Subject and job.** A geodetic instrument for surveyors, drone and robotics engineers. The page's job is glance-then-dig: is the base healthy (fix, accuracy, satellites, corrections flowing), then every detail. The visual vocabulary comes from survey and nautical instruments: a polar sky chart, elevation rings, brass bezels, precise numerals — not from SaaS dashboards.

**Palette (dark default, light selected — not an automatic flip).**

| Token | Dark | Light | Use |
|---|---|---|---|
| `--bg` | `#0f1420` deep navy | `#f3f4f7` | page |
| `--panel` | `#161d2e` | `#ffffff` | panels |
| `--panel-2` | `#1e2740` | `#e9ebf1` | raised / hover |
| `--line` | `#2a3452` | `#d3d7e2` | 1px rules, borders |
| `--ink` | `#f2eee6` warm off-white | `#141a2b` | primary text |
| `--ink-2` | `#a9b0c3` | `#4a5266` | secondary text |
| `--ink-3` | `#6f778c` | `#7a8296` | muted, disabled, non-primary systems |
| `--brass` | `#e0b25a` | `#8a6a1f` | accent: sky rings, active nav, primary button |
| `--brass-2` | `#f0cf8a` | `#a8842e` | hover/focus ring |

Constellations (categorical, validated with the dataviz validator on both surfaces; all checks pass, light needs direct labels which the legend/table provide): GPS `#3987e5`/`#2a78d6`, GLONASS `#d95926`/`#eb6834`, Galileo `#199e70`/`#1baf7a`, BeiDou `#c98500`/`#eda100`, QZSS `#d55181`/`#e87ba4`, SBAS `#9085e9`/`#4a3aa7` (dark/light). IMES, NavIC and unknown → `--ink-3`.

Status (fixed, never themed): good `#0ca30c` (RTK fixed, site verified, 1005 flowing), warning `#fab219` (RTK float, survey-in running, reconnecting), serious `#ec835a` (2D fix, corrections stale, disk warning), critical `#d03b3b` (no fix, receiver disconnected, site mismatch, disk low). Always with an icon and a word.

**Type.** Instrument Serif (400) for the two display moments only: the page title in the header and the hero coordinate readout. Instrument Sans (variable, 400/500/600) for everything else, `tabular-nums` on numbers. Scale (px/line-height): 12/16, 14/20, 16/24, 20/28, 28/34, 40/44, 64/64. Sentence case everywhere; no uppercase labels; units set in `--ink-2` at one step smaller than the value.

**Layout.** Left rail (220 px) with the wordmark, page links and a compact connection badge; a persistent **tape** across the top of the content: UTC clock · fix badge · sats used/tracked · hAcc · RTCM rate · receiver link state — the same six readings on every page. Content max-width 1440 px, 12-column grid, 16 px gutters, panels separated by `--line` rules with 6 px radius; no shadows. Dashboard hero (ASCII):

```
┌ rail ┐┌──────────────── tape: 16:47:34 UTC · RTK fixed · sats 27/50 · hAcc 0.012 m · RTCM 1.9 kB/s · USB ──┐
│      ││ Dashboard                                                                                         │
│ ●    ││ ┌─────────────────────────────┐ ┌───────────────────────────┐ ┌──────────────────────────────────┐ │
│ ○    ││ │ 23°50'14.4622" N            │ │       sky plot            │ │ map (OSM / imagery)              │ │
│ ○    ││ │ 90°15'45.1807" E            │ │   brass rings 30° 60°     │ │ base marker + accuracy circle    │ │
│ ○    ││ │ h -36.27 m  · MSL 13.36 m   │ │   coloured sats by system │ │ connected rovers (GGA)           │ │
│ ○    ││ │ ± 0.012 m h  ± 0.018 m v    │ │                           │ │                                  │ │
│      ││ └─────────────────────────────┘ └───────────────────────────┘ └──────────────────────────────────┘ │
│      ││ ┌ sats per system ─┐ ┌ survey-in / site ─┐ ┌ corrections ──┐ ┌ last hour: hAcc · sats · CNO ─────┐ │
```
Left-aligned text throughout; numbers right-aligned in tables.

**Principles.** (1) The numbers are the design: big, tabular, calm. (2) One memorable element per page — on the dashboard the sky plot with brass rings; elsewhere the chart or table that page exists for. (3) Structure encodes information: a rule separates sections, a badge encodes a state, nothing is decorative. (4) Motion only where data moves (satellite discs glide to new az/el; bars ease) and where a person acted; nothing animates on page load. (5) Copy is plain and specific: "Freeze survey-in as site", "Corrections are flowing to 2 rovers", "No receiver on /dev/ttyACM0 — check the USB cable".

Review against the defaults list: the first draft used near-black `#0b0b0b` and a monospace for readouts — both are template tells; replaced with deep navy + tabular Instrument Sans. The dashboard hero was "big number + small label + gradient"; replaced with a coordinate readout in the display serif paired with the sky plot, which is the one element specific to this subject.

---

## File structure (this plan, all under `web/`)

| Path | Responsibility |
|---|---|
| `package.json`, `vite.config.ts`, `tsconfig*.json`, `components.json` | toolchain (Phase 1 skeleton, extended) |
| `src/index.css` | tokens (`@theme`), light/dark, base styles, focus ring, reduced motion |
| `src/main.tsx`, `src/app/router.tsx`, `src/app/App.tsx` | providers (Query, Router, Toaster), routes |
| `src/app/Shell.tsx`, `src/app/Rail.tsx`, `src/app/Tape.tsx`, `src/app/PageHeader.tsx` | frame: rail nav, status tape, page title |
| `src/lib/api.ts`, `src/lib/types.ts`, `src/lib/queries.ts` | REST client, TypeScript types mirroring the API, query hooks |
| `src/lib/live.ts` | WebSocket client + zustand store (`useLive`) |
| `src/lib/format.ts`, `src/lib/geo.ts` | number/coordinate/time formatting, LLH↔ECEF/UTM (client-side) |
| `src/lib/palette.ts` | constellation + status colors, `systemColor(gnssId)` |
| `src/components/ui/*` | shadcn primitives (generated) |
| `src/components/Panel.tsx`, `Readout.tsx`, `StatusBadge.tsx`, `Stat.tsx`, `EmptyState.tsx`, `DataTable.tsx`, `CopyButton.tsx`, `ConfirmDialog.tsx` | shared building blocks |
| `src/components/charts/SkyPlot.tsx`, `CnoBars.tsx`, `Spectrum.tsx`, `TimeSeries.tsx`, `Sparkline.tsx`, `AvailabilityStrip.tsx`, `Legend.tsx`, `ChartTooltip.tsx` | charts |
| `src/components/MapPanel.tsx` | MapLibre map |
| `src/pages/Dashboard.tsx`, `Satellites.tsx`, `Receiver.tsx`, `Corrections.tsx`, `Site.tsx`, `Logs.tsx`, `History.tsx`, `Events.tsx`, `Settings.tsx`, `Login.tsx` | pages |
| `src/test/setup.ts`, `src/**/*.test.tsx` | vitest |
| `docs/ui.md` | how to run/dev the UI |

---

### Task 1: Toolchain, design tokens, app shell

**Files:**
- Modify: `web/package.json`, `web/vite.config.ts`, `web/index.html`, `web/src/main.tsx`, `web/src/index.css`, `web/src/App.tsx` (delete; replaced by `src/app/App.tsx`)
- Create: `web/src/app/App.tsx`, `web/src/app/router.tsx`, `web/src/app/Shell.tsx`, `web/src/app/Rail.tsx`, `web/src/app/Tape.tsx`, `web/src/app/PageHeader.tsx`, `web/src/lib/palette.ts`, `web/src/test/setup.ts`, `web/src/app/Shell.test.tsx`, shadcn components under `web/src/components/ui/`

**Interfaces:**
- Produces: routes registered in `router.tsx` (page components are stubs until their task); `Shell` renders `Rail` + `Tape` + `<Outlet/>`; `Tape` reads `useLive()` (Task 2) — in this task it reads a placeholder store with the same shape (`web/src/lib/live.ts` exporting `useLive` with `connected:false`, `state:null`, `status:"connecting"`); `palette.ts` exports `SYSTEM_ORDER`, `systemColor(gnssId, theme)`, `STATUS`; `PageHeader({title, children})` sets `document.title = "<title> · mtrtk"`.

- [ ] **Step 1: Install dependencies**

```bash
cd /home/nekosaif/github/mtrtk/web
pnpm add react-router@^7 @tanstack/react-query@^5 zustand@^5 maplibre-gl@^5 lucide-react sonner @fontsource-variable/instrument-sans @fontsource/instrument-serif
pnpm add -D vitest@^3 @testing-library/react@^16 @testing-library/jest-dom@^6 @testing-library/user-event@^14 jsdom@^26 @types/geojson
pnpm dlx shadcn@latest init -d
pnpm dlx shadcn@latest add button badge dialog dropdown-menu input label select switch table tabs tooltip sonner sheet separator scroll-area progress alert
```
`shadcn init` rewrites `src/index.css`; the next step replaces it with the token file below (keep shadcn's `@custom-variant dark` line if the generated file has one).

- [ ] **Step 2: Write `web/src/index.css`**

```css
@import "tailwindcss";
@import "@fontsource-variable/instrument-sans";
@import "@fontsource/instrument-serif";

@custom-variant dark (&:where([data-theme="dark"], [data-theme="dark"] *));

/* ---- mtrtk tokens: dark is the default; light is selected via data-theme="light" ---- */
:root {
  --bg: #0f1420;
  --panel: #161d2e;
  --panel-2: #1e2740;
  --line: #2a3452;
  --ink: #f2eee6;
  --ink-2: #a9b0c3;
  --ink-3: #6f778c;
  --brass: #e0b25a;
  --brass-2: #f0cf8a;
  --sys-gps: #3987e5;
  --sys-glonass: #d95926;
  --sys-galileo: #199e70;
  --sys-beidou: #c98500;
  --sys-qzss: #d55181;
  --sys-sbas: #9085e9;
  --status-good: #0ca30c;
  --status-warning: #fab219;
  --status-serious: #ec835a;
  --status-critical: #d03b3b;
  color-scheme: dark;
}
:root[data-theme="light"] {
  --bg: #f3f4f7;
  --panel: #ffffff;
  --panel-2: #e9ebf1;
  --line: #d3d7e2;
  --ink: #141a2b;
  --ink-2: #4a5266;
  --ink-3: #7a8296;
  --brass: #8a6a1f;
  --brass-2: #a8842e;
  --sys-gps: #2a78d6;
  --sys-glonass: #eb6834;
  --sys-galileo: #1baf7a;
  --sys-beidou: #eda100;
  --sys-qzss: #e87ba4;
  --sys-sbas: #4a3aa7;
  color-scheme: light;
}

/* ---- map tokens onto shadcn's variables so generated components follow the theme ---- */
@theme inline {
  --font-sans: "Instrument Sans Variable", ui-sans-serif, system-ui, sans-serif;
  --font-display: "Instrument Serif", Georgia, serif;
  --color-background: var(--bg);
  --color-foreground: var(--ink);
  --color-card: var(--panel);
  --color-card-foreground: var(--ink);
  --color-popover: var(--panel-2);
  --color-popover-foreground: var(--ink);
  --color-primary: var(--brass);
  --color-primary-foreground: #0f1420;
  --color-secondary: var(--panel-2);
  --color-secondary-foreground: var(--ink);
  --color-muted: var(--panel-2);
  --color-muted-foreground: var(--ink-2);
  --color-accent: var(--panel-2);
  --color-accent-foreground: var(--ink);
  --color-destructive: var(--status-critical);
  --color-destructive-foreground: #ffffff;
  --color-border: var(--line);
  --color-input: var(--line);
  --color-ring: var(--brass-2);
  --color-ink-2: var(--ink-2);
  --color-ink-3: var(--ink-3);
  --color-brass: var(--brass);
  --color-brass-2: var(--brass-2);
  --color-panel: var(--panel);
  --color-panel-2: var(--panel-2);
  --color-line: var(--line);
  --color-status-good: var(--status-good);
  --color-status-warning: var(--status-warning);
  --color-status-serious: var(--status-serious);
  --color-status-critical: var(--status-critical);
  --radius: 6px;
  --radius-sm: 4px;
  --radius-md: 6px;
  --radius-lg: 8px;
  --radius-xl: 12px;
}

html, body, #root { height: 100%; }
body {
  background: var(--bg);
  color: var(--ink);
  font-family: var(--font-sans);
  font-size: 14px;
  line-height: 20px;
  -webkit-font-smoothing: antialiased;
}
.num { font-variant-numeric: tabular-nums; }
.display { font-family: var(--font-display); font-weight: 400; letter-spacing: -0.01em; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); }
:focus-visible { outline: 2px solid var(--brass-2); outline-offset: 2px; }
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; }
}
```

- [ ] **Step 3: Configure vitest in `web/vite.config.ts` and write `web/src/test/setup.ts`**

Add to `defineConfig`:
```ts
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
```
and change the top import to `import { defineConfig } from "vitest/config";`. Add `"test": "vitest run"` and `"test:watch": "vitest"` to `package.json` scripts.

`web/src/test/setup.ts`:
```ts
import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => cleanup());

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
```

- [ ] **Step 4: Write `web/src/lib/palette.ts` and the placeholder `web/src/lib/live.ts`**

`web/src/lib/palette.ts`:
```ts
/** Fixed categorical order for GNSS systems (dataviz rule: never cycled). */
export const SYSTEM_ORDER = ["GPS", "GLONASS", "Galileo", "BeiDou", "QZSS", "SBAS"] as const;
export type SystemName = (typeof SYSTEM_ORDER)[number];

export const GNSS_ID_TO_NAME: Record<number, string> = { 0: "GPS", 1: "SBAS", 2: "Galileo", 3: "BeiDou", 4: "IMES", 5: "QZSS", 6: "GLONASS", 7: "NavIC" };

const SYSTEM_VAR: Record<SystemName, string> = {
  GPS: "var(--sys-gps)", GLONASS: "var(--sys-glonass)", Galileo: "var(--sys-galileo)",
  BeiDou: "var(--sys-beidou)", QZSS: "var(--sys-qzss)", SBAS: "var(--sys-sbas)",
};

/** CSS color for a system; non-primary systems fall back to muted ink. */
export function systemColor(name: string): string {
  return (SYSTEM_VAR as Record<string, string>)[name] ?? "var(--ink-3)";
}

export const STATUS = {
  good: "var(--status-good)",
  warning: "var(--status-warning)",
  serious: "var(--status-serious)",
  critical: "var(--status-critical)",
} as const;
export type StatusLevel = keyof typeof STATUS;
```

`web/src/lib/live.ts` (placeholder; Task 2 replaces it):
```ts
import { create } from "zustand";

export type LiveStatus = "connecting" | "open" | "reconnecting";

interface LiveStore {
  status: LiveStatus;
  state: null;
  lastEpochAt: number | null;
}

export const useLive = create<LiveStore>(() => ({ status: "connecting", state: null, lastEpochAt: null }));
```

- [ ] **Step 5: Write the shell**

`web/src/app/Rail.tsx`:
```tsx
import { NavLink } from "react-router";
import { Activity, Compass, Radio, MapPin, Files, LineChart, Bell, Settings, Satellite } from "lucide-react";
import { cn } from "@/lib/utils";

export const NAV = [
  { to: "/", label: "Dashboard", icon: Activity },
  { to: "/satellites", label: "Satellites", icon: Satellite },
  { to: "/receiver", label: "Receiver", icon: Compass },
  { to: "/corrections", label: "Corrections", icon: Radio },
  { to: "/site", label: "Site", icon: MapPin },
  { to: "/logs", label: "Logs", icon: Files },
  { to: "/history", label: "History", icon: LineChart },
  { to: "/events", label: "Events", icon: Bell },
  { to: "/settings", label: "Settings", icon: Settings },
] as const;

export function Rail() {
  return (
    <nav aria-label="Main" className="flex h-full flex-col border-r border-line bg-panel">
      <div className="px-4 py-5">
        <span className="display text-[28px] leading-none">mtrtk</span>
        <div className="mt-1 text-[12px] leading-4 text-ink-2">base station</div>
      </div>
      <ul className="flex flex-1 flex-col gap-0.5 px-2">
        {NAV.map(({ to, label, icon: Icon }) => (
          <li key={to}>
            <NavLink
              to={to}
              end={to === "/"}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-3 rounded-md px-3 py-2 text-[14px] text-ink-2 hover:bg-panel-2 hover:text-ink",
                  isActive && "bg-panel-2 text-ink shadow-[inset_3px_0_0_var(--brass)]",
                )
              }
            >
              <Icon className="size-4 shrink-0" aria-hidden />
              <span className="rail-label">{label}</span>
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  );
}
```

`web/src/app/Tape.tsx`:
```tsx
import { useEffect, useState } from "react";
import { useLive } from "@/lib/live";
import { cn } from "@/lib/utils";

function utcClock(): string {
  const d = new Date();
  return d.toISOString().slice(11, 19) + " UTC";
}

export function Tape() {
  const status = useLive((s) => s.status);
  const [clock, setClock] = useState(utcClock());
  useEffect(() => {
    const id = setInterval(() => setClock(utcClock()), 1000);
    return () => clearInterval(id);
  }, []);
  return (
    <div className="flex items-center gap-6 border-b border-line bg-panel px-6 py-2 text-[14px]" role="status" aria-live="off">
      <span className="num text-ink">{clock}</span>
      <span className="text-ink-3">no receiver data yet</span>
      <span className={cn("ml-auto flex items-center gap-2 text-ink-2")}>
        <span className={cn("inline-block size-2 rounded-full", status === "open" ? "bg-status-good" : "bg-status-warning")} aria-hidden />
        {status === "open" ? "live" : status === "reconnecting" ? "reconnecting" : "connecting"}
      </span>
    </div>
  );
}
```
(Task 2 replaces the "no receiver data yet" placeholder with the six readings.)

`web/src/app/PageHeader.tsx`:
```tsx
import { useEffect, type ReactNode } from "react";

export function PageHeader({ title, children }: { title: string; children?: ReactNode }) {
  useEffect(() => {
    document.title = `${title} · mtrtk`;
  }, [title]);
  return (
    <header className="mb-4 flex items-end justify-between gap-4">
      <h1 className="display text-[40px] leading-[44px]">{title}</h1>
      {children ? <div className="flex items-center gap-2">{children}</div> : null}
    </header>
  );
}
```

`web/src/app/Shell.tsx`:
```tsx
import { Outlet } from "react-router";
import { Rail } from "./Rail";
import { Tape } from "./Tape";

export function Shell() {
  return (
    <div className="grid h-full grid-cols-[220px_1fr] max-lg:grid-cols-[64px_1fr] max-sm:grid-cols-1 max-sm:grid-rows-[1fr_auto]">
      <aside className="max-sm:order-2 [&_.rail-label]:max-lg:hidden [&_.rail-label]:max-sm:inline">
        <Rail />
      </aside>
      <div className="flex min-w-0 flex-col">
        <Tape />
        <main className="mx-auto w-full max-w-[1440px] flex-1 px-6 py-5 max-sm:px-4">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
```

`web/src/app/router.tsx`:
```tsx
import { createBrowserRouter } from "react-router";
import { Shell } from "./Shell";
import { PageHeader } from "./PageHeader";

function Stub({ title }: { title: string }) {
  return (
    <>
      <PageHeader title={title} />
      <p className="text-ink-2">This page arrives in a later task.</p>
    </>
  );
}

export const routes = [
  {
    path: "/",
    element: <Shell />,
    children: [
      { index: true, element: <Stub title="Dashboard" /> },
      { path: "satellites", element: <Stub title="Satellites" /> },
      { path: "receiver", element: <Stub title="Receiver" /> },
      { path: "corrections", element: <Stub title="Corrections" /> },
      { path: "site", element: <Stub title="Site" /> },
      { path: "logs", element: <Stub title="Logs" /> },
      { path: "history", element: <Stub title="History" /> },
      { path: "events", element: <Stub title="Events" /> },
      { path: "settings", element: <Stub title="Settings" /> },
    ],
  },
  { path: "/login", element: <Stub title="Sign in" /> },
];

export const router = createBrowserRouter(routes);
```

`web/src/app/App.tsx`:
```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "react-router";
import { Toaster } from "@/components/ui/sonner";
import { router } from "./router";

export const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 5_000, retry: 1, refetchOnWindowFocus: false } },
});

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
      <Toaster position="bottom-right" />
    </QueryClientProvider>
  );
}
```

`web/src/main.tsx`: change `import App from "./App";` to `import App from "./app/App";` and delete `web/src/App.tsx`. In `web/index.html` set `<html lang="en" data-theme="dark">` (replace `class="dark"`).

- [ ] **Step 6: Write the shell test**

`web/src/app/Shell.test.tsx`:
```tsx
import { render, screen } from "@testing-library/react";
import { RouterProvider, createMemoryRouter } from "react-router";
import { routes } from "./router";

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  return render(<RouterProvider router={router} />);
}

describe("Shell", () => {
  it("renders the rail with every page link and marks the active one", () => {
    renderAt("/satellites");
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(nav).toHaveTextContent("Dashboard");
    expect(nav).toHaveTextContent("Settings");
    expect(screen.getByRole("link", { name: /satellites/i })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Satellites");
    expect(document.title).toBe("Satellites · mtrtk");
  });

  it("shows the tape with a UTC clock and connection state", () => {
    renderAt("/");
    expect(screen.getByRole("status")).toHaveTextContent(/UTC/);
    expect(screen.getByRole("status")).toHaveTextContent(/connecting/);
  });
});
```

- [ ] **Step 7: Run, build, commit**

Run: `cd web && pnpm test && pnpm build && pnpm lint`
Expected: 2 tests pass; `dist/` built; no TS errors.

```bash
cd /home/nekosaif/github/mtrtk
git add web
git commit -m "feat(web): design tokens, fonts, app shell with rail and status tape, vitest setup

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: API client, types, live WebSocket store, tape readings

**Files:**
- Create: `web/src/lib/types.ts`, `web/src/lib/api.ts`, `web/src/lib/queries.ts`, `web/src/lib/status.ts`, `web/src/components/StatusBadge.tsx`, `web/src/lib/live.test.ts`, `web/src/lib/api.test.ts`
- Modify: `web/src/lib/live.ts` (real store), `web/src/app/Tape.tsx` (six readings), `web/src/app/App.tsx` (connect on mount)

**Interfaces:**
- Produces: TS types mirroring the API (`ReceiverState` and sections, `SystemStats`, `EventItem`, `Site`, `LogFile`, `Job`, `NtripClient`, `WsMessage`); `api<T>(path, init?)`, `ApiError`, helpers `get/post/put/patch/del`; `useLive` store with `status`, `state`, `lastEpochAt`, `role`, `ntripClients`, `events`, `system`, `receiverConnected`, `base`, `jobs`, `applyMessage(msg)`, `connect()`, `disconnect()`; `useStale(ms=5000)`; `fixLevel(fix, receiverConnected) -> {level, label}`; `StatusBadge({level, label, icon?})`; query hooks `useStatus, useSystem, useConfig, useSites, useLogs, useEvents, useNtrip, useNtripClients, useNtripHistory, useHistory, useJobs, useBaseMode, useReceiver`.

- [ ] **Step 1: Write `web/src/lib/types.ts`**

```ts
// Mirrors src/mtrtk/core/state.py and the Phase 3 API. Keep field names identical to the JSON.
export interface Position { lat: number | null; lon: number | null; height_m: number | null; hmsl_m: number | null; ecef_x_m: number | null; ecef_y_m: number | null; ecef_z_m: number | null; invalid_llh: boolean }
export interface Accuracy { h_acc_m: number | null; v_acc_m: number | null; p_acc_m: number | null; t_acc_ns: number | null; s_acc_mps: number | null; head_acc_deg: number | null }
export interface Dops { g: number | null; p: number | null; t: number | null; v: number | null; h: number | null; n: number | null; e: number | null }
export interface FixInfo { fix_type: number; fix_type_name: string; gnss_fix_ok: boolean; diff_soln: boolean; carr_soln: number; carr_soln_name: string; num_sv: number; last_correction_age: number; psm_state: number; spoof_det_state: number; ttff_ms: number | null; uptime_ms: number | null }
export interface Velocity { vel_n_mps: number | null; vel_e_mps: number | null; vel_d_mps: number | null; ground_speed_mps: number | null; heading_motion_deg: number | null }
export interface TimeInfo { utc: string | null; itow_ms: number | null; gps_week: number | null; gps_tow_s: number | null; leap_s: number | null; valid_date: boolean; valid_time: boolean; fully_resolved: boolean; valid_utc: boolean; utc_standard: number | null; t_acc_ns: number | null; clk_bias_ns: number | null; clk_drift_nsps: number | null; f_acc_psps: number | null; leap_source: number | null; time_to_leap_event_s: number | null; leap_change: number | null }
export interface Signal { sig_id: number; name: string; freq_id: number; cno: number; pr_res_m: number; quality_ind: number; corr_source: number; iono_model: number; health: number; pr_used: boolean; cr_used: boolean; do_used: boolean }
export interface Satellite { gnss_id: number; gnss: string; sv_id: number; cno: number; elev: number | null; azim: number | null; pr_res_m: number; quality_ind: number; used: boolean; health: number; diff_corr: boolean; smoothed: boolean; orbit_source: number; eph_avail: boolean; alm_avail: boolean; signals: Signal[] }
export interface SatSummary { tracked: number; used: number; per_gnss: Record<string, { tracked: number; used: number }> }
export interface Hardware { ant_status: number; ant_status_name: string; ant_power: number; ant_power_name: string; noise_per_ms: number; agc_cnt: number; jam_ind: number; jamming_state: number; jamming_state_name: string; rtc_calib: boolean; safe_boot: boolean; xtal_absent: boolean }
export interface RfBlock { block_id: number; jamming_state: number; jamming_state_name: string; ant_status: number; ant_status_name: string; ant_power: number; ant_power_name: string; post_status: number; noise_per_ms: number; agc_cnt: number; jam_ind: number; ofs_i: number; mag_i: number; ofs_q: number; mag_q: number }
export interface Spectrum { block_id: number; span_hz: number; res_hz: number; center_hz: number; pga_db: number; bins: number[] }
export interface PortStats { port_id: number; tx_pending: number; tx_bytes: number; tx_usage: number; tx_peak_usage: number; rx_pending: number; rx_bytes: number; rx_usage: number; rx_peak_usage: number; overrun_errs: number; skipped: number }
export interface SurveyIn { active: boolean; valid: boolean; dur_s: number; obs: number; mean_x_m: number | null; mean_y_m: number | null; mean_z_m: number | null; mean_acc_m: number | null }
export interface RtcmMsgStats { count: number; bytes: number; last_seen_mono: number | null }
export interface RtcmStats { messages: Record<string, RtcmMsgStats>; total_count: number; total_bytes: number; bytes_per_s: number }
export interface Firmware { sw_version: string; hw_version: string; fw_version: string; protver: string; module: string; extensions: string[] }
export interface ReceiverState {
  connected: boolean; source: string; position: Position; accuracy: Accuracy; dops: Dops; fix: FixInfo; velocity: Velocity; time: TimeInfo;
  sats: Satellite[]; sat_summary: SatSummary; hardware: Hardware | null; rf: RfBlock[]; spectrum: Spectrum[]; ports: PortStats[];
  survey_in: SurveyIn; rtcm_out: RtcmStats; firmware: Firmware; epoch_count: number; raw_epochs: number; last_epoch_mono: number | null;
}

export interface SystemStats { cpu_pct: number; mem_pct: number; disk_free_gb: number; disk_used_pct: number; uptime_s: number; temp_c: number | null; load1: number | null; ts_utc: string }
export type Level = "info" | "warning" | "error";
export interface EventItem { id: number; ts_utc: string; level: Level; kind: string; message: string; meta: Record<string, unknown>; acked: boolean }
export interface Site { id: number | null; name: string; x: number; y: number; z: number; lat: number | null; lon: number | null; height_m: number | null; sigma_x: number | null; sigma_y: number | null; sigma_z: number | null; frame: string; epoch: string | null; source: string; notes: string | null; created_utc: string | null; active: boolean }
export interface LogFile { name: string; hour_utc: string; bytes: number; complete: boolean; keep: boolean; msg_counts: Record<string, number>; start_utc: string | null; end_utc: string | null }
export interface LogsResponse { files: LogFile[]; total_bytes: number; hours: number; disk_free_gb: number }
export interface HourSlot { hour_utc: string; available: boolean; bytes: number; complete: boolean }
export interface Job { id: string; kind: string; status: "queued" | "running" | "done" | "failed"; created_utc: string; updated_utc: string | null; progress: number; message: string | null; params: Record<string, unknown>; result: Record<string, unknown> | null; error: string | null }
export interface NtripClient { id: number; ip: string; port: number; mountpoint: string; user_agent: string; username: string | null; version: number; connected_utc: string; bytes_sent: number; dropped_frames: number; last_gga_lat: number | null; last_gga_lon: number | null; last_gga_utc: string | null }
export interface NtripInfo { running: boolean; host: string; port: number; mountpoint: string; anonymous: boolean; username: string | null; bind_mode: string; connection_url: string; sourcetable: string | null }
export interface BaseModeView { available: boolean; mode: "survey-in" | "fixed" | "off"; site: string | null; verified: boolean; last_1005: { station_id: number; x: number; y: number; z: number } | null; svin: { min_duration_s: number; acc_limit_m: number } }
export interface StatusSummary { role: string; version: string; uptime_s: number; connected: boolean; source: string; firmware: Pick<Firmware, "fw_version" | "protver" | "module">; fix: Pick<FixInfo, "fix_type_name" | "carr_soln_name" | "num_sv">; position: Pick<Position, "lat" | "lon" | "height_m">; accuracy: Pick<Accuracy, "h_acc_m" | "v_acc_m">; survey_in: Pick<SurveyIn, "active" | "valid" | "dur_s" | "mean_acc_m">; ntrip_clients: number; rtcm_bytes_per_s: number; epoch_count: number; capabilities: { supported: string[]; unsupported: string[] } | null }
export interface ReceiverInfo { connected: boolean; passive: boolean; source: string; capabilities: { protver: string; fw_version: string; module: string; supported: string[]; unsupported: string[] } | null; firmware: Firmware }
export interface ConfigResponse { values: Record<string, unknown>; env_file: string; secret_keys: string[]; live_keys: string[] }
export interface HistoryResponse { res: "1s" | "1m"; columns: string[]; rows: (number | null)[][] }

// WebSocket protocol (Phase 3)
export type EpochSections = { pvt?: Pick<ReceiverState, "position" | "accuracy" | "dops" | "fix" | "velocity" | "time">; sats?: Pick<ReceiverState, "sats" | "sat_summary">; rtcm?: RtcmStats; svin?: SurveyIn };
export type WsMessage =
  | { type: "snapshot"; role: string; topics: string[]; state: ReceiverState }
  | ({ type: "epoch"; t: number | null } & EpochSections)
  | { type: "update"; topic: string; source: string; data: unknown };
```

- [ ] **Step 2: Write `web/src/lib/api.ts`**

```ts
export class ApiError extends Error {
  constructor(public status: number, public detail: string) {
    super(`${status}: ${detail}`);
  }
}

function onUnauthorized(): void {
  if (!window.location.pathname.startsWith("/login")) {
    const next = encodeURIComponent(window.location.pathname + window.location.search);
    window.location.assign(`/login?next=${next}`);
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { credentials: "same-origin", ...init, headers });
  if (response.status === 401) {
    onUnauthorized();
    throw new ApiError(401, "authentication required");
  }
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (body.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(response.status, detail);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const get = <T>(path: string) => api<T>(path);
export const post = <T>(path: string, body?: unknown) => api<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
export const put = <T>(path: string, body: unknown) => api<T>(path, { method: "PUT", body: JSON.stringify(body) });
export const patch = <T>(path: string, body: unknown) => api<T>(path, { method: "PATCH", body: JSON.stringify(body) });
export const del = <T>(path: string) => api<T>(path, { method: "DELETE" });

export function wsUrl(token?: string | null): string {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  const url = new URL(`${proto}://${window.location.host}/ws`);
  if (token) url.searchParams.set("token", token);
  return url.toString();
}
```

- [ ] **Step 3: Write `web/src/lib/live.ts` (replaces the placeholder)**

```ts
import { create } from "zustand";
import { wsUrl } from "./api";
import type { EventItem, Job, NtripClient, ReceiverState, SystemStats, WsMessage } from "./types";

export type LiveStatus = "connecting" | "open" | "reconnecting";
export const STALE_AFTER_MS = 5000;
const MAX_EVENTS = 50;

export interface BaseInfo { mode: string | null; site: string | null; reason: string | null; verified: boolean | null }

interface LiveStore {
  status: LiveStatus;
  role: string | null;
  state: ReceiverState | null;
  lastEpochAt: number | null;
  receiverConnected: boolean | null;
  ntripClients: NtripClient[];
  events: EventItem[];
  system: SystemStats | null;
  base: BaseInfo;
  jobs: Record<string, Job>;
  applyMessage: (msg: WsMessage, now?: number) => void;
  setStatus: (status: LiveStatus) => void;
  connect: (token?: string | null) => void;
  disconnect: () => void;
}

let socket: WebSocket | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let backoffMs = 1000;

export const useLive = create<LiveStore>((set, get) => ({
  status: "connecting",
  role: null,
  state: null,
  lastEpochAt: null,
  receiverConnected: null,
  ntripClients: [],
  events: [],
  system: null,
  base: { mode: null, site: null, reason: null, verified: null },
  jobs: {},

  setStatus: (status) => set({ status }),

  applyMessage: (msg, now = Date.now()) => {
    if (msg.type === "snapshot") {
      set({ state: msg.state, role: msg.role, receiverConnected: msg.state.connected, lastEpochAt: msg.state.epoch_count ? now : null });
      return;
    }
    if (msg.type === "epoch") {
      const prev = get().state;
      if (!prev) return;
      const next: ReceiverState = { ...prev, ...(msg.pvt ?? {}), ...(msg.sats ?? {}) };
      if (msg.rtcm) next.rtcm_out = msg.rtcm;
      if (msg.svin) next.survey_in = msg.svin;
      next.epoch_count = prev.epoch_count + 1;
      set({ state: next, lastEpochAt: now });
      return;
    }
    const prev = get().state;
    switch (msg.topic) {
      case "rf":
        if (!prev) return;
        set({ state: msg.source === "state.hardware" ? { ...prev, hardware: msg.data as ReceiverState["hardware"] } : { ...prev, rf: msg.data as ReceiverState["rf"] } });
        return;
      case "span":
        if (prev) set({ state: { ...prev, spectrum: msg.data as ReceiverState["spectrum"] } });
        return;
      case "ntrip":
        set({ ntripClients: msg.data as NtripClient[] });
        return;
      case "events":
        set({ events: [msg.data as EventItem, ...get().events].slice(0, MAX_EVENTS) });
        return;
      case "system":
        set({ system: msg.data as SystemStats });
        return;
      case "receiver":
        if (msg.source === "receiver.connected") set({ receiverConnected: true });
        else if (msg.source === "receiver.disconnected") set({ receiverConnected: false });
        return;
      case "base": {
        const data = msg.data as Record<string, unknown>;
        const base = { ...get().base };
        if (msg.source === "base.mode") Object.assign(base, { mode: data.mode ?? null, site: data.site ?? null, reason: data.reason ?? null, verified: null });
        if (msg.source === "base.site_verified") base.verified = true;
        if (msg.source === "base.site_mismatch") base.verified = false;
        set({ base });
        return;
      }
      case "jobs": {
        const job = msg.data as Job;
        set({ jobs: { ...get().jobs, [job.id]: job } });
        return;
      }
      default:
        return;
    }
  },

  connect: (token) => {
    if (socket) return;
    const open = () => {
      const ws = new WebSocket(wsUrl(token));
      socket = ws;
      ws.onopen = () => {
        backoffMs = 1000;
        set({ status: "open" });
      };
      ws.onmessage = (ev) => {
        try {
          get().applyMessage(JSON.parse(ev.data as string) as WsMessage);
        } catch (err) {
          console.warn("bad ws message", err);
        }
      };
      ws.onclose = (ev) => {
        socket = null;
        if (ev.code === 1008) {
          window.location.assign("/login");
          return;
        }
        set({ status: "reconnecting" });
        reconnectTimer = setTimeout(open, backoffMs);
        backoffMs = Math.min(backoffMs * 2, 30_000);
      };
      ws.onerror = () => ws.close();
    };
    open();
  },

  disconnect: () => {
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = null;
    socket?.close();
    socket = null;
  },
}));

/** True when no epoch has arrived for STALE_AFTER_MS (re-evaluated every second by the caller). */
export function isStale(lastEpochAt: number | null, now = Date.now()): boolean {
  return lastEpochAt === null || now - lastEpochAt > STALE_AFTER_MS;
}
```

- [ ] **Step 4: Write `web/src/lib/status.ts` and `web/src/components/StatusBadge.tsx`**

`web/src/lib/status.ts`:
```ts
import type { FixInfo } from "./types";
import type { StatusLevel } from "./palette";

export interface StatusText { level: StatusLevel; label: string }

/** Map receiver fix + link state to a status level and a plain-language label. */
export function fixLevel(fix: FixInfo | null | undefined, receiverConnected: boolean | null, stale: boolean): StatusText {
  if (receiverConnected === false) return { level: "critical", label: "Receiver disconnected" };
  if (!fix || stale) return { level: "warning", label: "Waiting for data" };
  if (fix.carr_soln === 2) return { level: "good", label: "RTK fixed" };
  if (fix.carr_soln === 1) return { level: "warning", label: "RTK float" };
  if (fix.fix_type === 5) return { level: "good", label: "Fixed position" };
  if (fix.fix_type >= 3) return { level: "good", label: fix.diff_soln ? "3D DGNSS" : "3D fix" };
  if (fix.fix_type === 2) return { level: "serious", label: "2D fix" };
  return { level: "critical", label: "No fix" };
}

export function levelForEvent(level: "info" | "warning" | "error"): StatusLevel {
  return level === "error" ? "critical" : level === "warning" ? "warning" : "good";
}
```

`web/src/components/StatusBadge.tsx`:
```tsx
import { AlertTriangle, CheckCircle2, CircleDashed, XCircle } from "lucide-react";
import { STATUS, type StatusLevel } from "@/lib/palette";
import { cn } from "@/lib/utils";

const ICON = { good: CheckCircle2, warning: CircleDashed, serious: AlertTriangle, critical: XCircle } as const;

export function StatusBadge({ level, label, className }: { level: StatusLevel; label: string; className?: string }) {
  const Icon = ICON[level];
  return (
    <span className={cn("inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[14px]", className)} style={{ borderColor: STATUS[level], color: "var(--ink)" }}>
      <Icon className="size-3.5" style={{ color: STATUS[level] }} aria-hidden />
      {label}
    </span>
  );
}
```

- [ ] **Step 5: Write `web/src/lib/queries.ts`**

```ts
import { useQuery } from "@tanstack/react-query";
import { get } from "./api";
import type { BaseModeView, ConfigResponse, EventItem, HistoryResponse, HourSlot, Job, LogsResponse, NtripClient, NtripInfo, ReceiverInfo, Site, StatusSummary, SystemStats } from "./types";

export const useStatus = () => useQuery({ queryKey: ["status"], queryFn: () => get<StatusSummary>("/api/status"), refetchInterval: 5000 });
export const useSystem = () => useQuery({ queryKey: ["system"], queryFn: () => get<{ hostname: string; tailscale_ip: string | null; data_dir: string; stats: SystemStats | null; versions: Record<string, string> }>("/api/system"), refetchInterval: 10_000 });
export const useConfig = () => useQuery({ queryKey: ["config"], queryFn: () => get<ConfigResponse>("/api/config") });
export const useReceiver = () => useQuery({ queryKey: ["receiver"], queryFn: () => get<ReceiverInfo>("/api/receiver"), refetchInterval: 10_000 });
export const useBaseMode = () => useQuery({ queryKey: ["base", "mode"], queryFn: () => get<BaseModeView>("/api/base/mode"), refetchInterval: 5000 });
export const useSites = () => useQuery({ queryKey: ["base", "sites"], queryFn: () => get<Site[]>("/api/base/sites") });
export const useNtrip = () => useQuery({ queryKey: ["ntrip"], queryFn: () => get<NtripInfo>("/api/ntrip"), refetchInterval: 10_000 });
export const useNtripClients = () => useQuery({ queryKey: ["ntrip", "clients"], queryFn: () => get<NtripClient[]>("/api/ntrip/clients"), refetchInterval: 5000 });
export const useNtripHistory = (limit = 50) => useQuery({ queryKey: ["ntrip", "history", limit], queryFn: () => get<Array<Record<string, unknown>>>(`/api/ntrip/history?limit=${limit}`) });
export const useLogs = () => useQuery({ queryKey: ["logs"], queryFn: () => get<LogsResponse>("/api/logs"), refetchInterval: 30_000 });
export const useAvailability = (from: string, to: string) => useQuery({ queryKey: ["logs", "availability", from, to], queryFn: () => get<HourSlot[]>(`/api/logs/availability?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`) });
export const useEvents = (level?: string, limit = 200) => useQuery({ queryKey: ["events", level ?? "all", limit], queryFn: () => get<EventItem[]>(`/api/events?limit=${limit}${level ? `&level=${level}` : ""}`) });
export const useHistory = (metrics: string[], from: string, to: string, res: "auto" | "1s" | "1m" = "auto") =>
  useQuery({
    queryKey: ["history", metrics.join(","), from, to, res],
    queryFn: () => get<HistoryResponse>(`/api/history?metrics=${metrics.join(",")}&from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}&res=${res}`),
    enabled: metrics.length > 0,
  });
export const useJobs = (kind?: string) => useQuery({ queryKey: ["jobs", kind ?? "all"], queryFn: () => get<Job[]>(`/api/jobs${kind ? `?kind=${kind}` : ""}`), refetchInterval: 5000 });
```

- [ ] **Step 6: Replace `web/src/app/Tape.tsx` with the six readings and connect in `App.tsx`**

`Tape.tsx`:
```tsx
import { useEffect, useState } from "react";
import { isStale, useLive } from "@/lib/live";
import { fixLevel } from "@/lib/status";
import { fmtAcc, fmtRate } from "@/lib/format";
import { StatusBadge } from "@/components/StatusBadge";
import { cn } from "@/lib/utils";

export function Tape() {
  const { status, state, lastEpochAt, receiverConnected, ntripClients } = useLive();
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  const stale = isStale(lastEpochAt, now);
  const clock = state?.time.utc && !stale ? state.time.utc.slice(11, 19) : new Date(now).toISOString().slice(11, 19);
  const fix = fixLevel(state?.fix, receiverConnected, stale);
  const dim = stale ? "text-ink-3" : "text-ink";
  return (
    <div className="flex flex-wrap items-center gap-x-6 gap-y-1 border-b border-line bg-panel px-6 py-2 text-[14px] max-sm:px-4" role="status" aria-live="off">
      <span className={cn("num", dim)} title={state?.time.utc && !stale ? "Receiver time" : "Browser clock"}>{clock} UTC</span>
      <StatusBadge level={fix.level} label={fix.label} />
      {state ? (
        <>
          <span className={cn("num", dim)}>sats {state.sat_summary.used}<span className="text-ink-2">/{state.sat_summary.tracked}</span></span>
          <span className={cn("num", dim)}>hAcc {fmtAcc(state.accuracy.h_acc_m)}</span>
          <span className={cn("num", dim)}>RTCM {fmtRate(state.rtcm_out.bytes_per_s)}</span>
          <span className={cn("num", dim)}>{ntripClients.length} rover{ntripClients.length === 1 ? "" : "s"}</span>
        </>
      ) : null}
      <span className="ml-auto flex items-center gap-2 text-ink-2">
        <span className={cn("inline-block size-2 rounded-full", status === "open" ? "bg-status-good" : "bg-status-warning")} aria-hidden />
        {status === "open" ? (stale ? "live · waiting for epochs" : "live") : status === "reconnecting" ? "reconnecting" : "connecting"}
      </span>
    </div>
  );
}
```
In `App.tsx` add:
```tsx
import { useEffect } from "react";
import { useLive } from "@/lib/live";
…
export default function App() {
  const connect = useLive((s) => s.connect);
  useEffect(() => {
    const token = new URLSearchParams(window.location.search).get("token");
    connect(token);
  }, [connect]);
  …
```
`fmtAcc`/`fmtRate` come from Task 3; add a minimal `web/src/lib/format.ts` now containing only those two (Task 3 completes the file):
```ts
export function fmtAcc(m: number | null | undefined): string {
  if (m == null) return "—";
  return m >= 10 ? `${m.toFixed(0)} m` : m >= 1 ? `${m.toFixed(2)} m` : `${(m * 100).toFixed(1)} cm`;
}
export function fmtRate(bytesPerS: number | null | undefined): string {
  if (bytesPerS == null || bytesPerS <= 0) return "0 B/s";
  return bytesPerS >= 1024 ? `${(bytesPerS / 1024).toFixed(1)} kB/s` : `${bytesPerS.toFixed(0)} B/s`;
}
```

- [ ] **Step 7: Write the tests**

`web/src/lib/live.test.ts`:
```ts
import { useLive } from "./live";
import type { ReceiverState, WsMessage } from "./types";

function baseState(): ReceiverState {
  return {
    connected: true, source: "file", epoch_count: 3, raw_epochs: 3, last_epoch_mono: null,
    position: { lat: 23.8, lon: 90.2, height_m: -36, hmsl_m: 13, ecef_x_m: null, ecef_y_m: null, ecef_z_m: null, invalid_llh: false },
    accuracy: { h_acc_m: 1.0, v_acc_m: 1.5, p_acc_m: null, t_acc_ns: null, s_acc_mps: null, head_acc_deg: null },
    dops: { g: null, p: 1.2, t: null, v: null, h: null, n: null, e: null },
    fix: { fix_type: 3, fix_type_name: "3D", gnss_fix_ok: true, diff_soln: false, carr_soln: 0, carr_soln_name: "None", num_sv: 20, last_correction_age: 0, psm_state: 0, spoof_det_state: 0, ttff_ms: null, uptime_ms: null },
    velocity: { vel_n_mps: 0, vel_e_mps: 0, vel_d_mps: 0, ground_speed_mps: 0, heading_motion_deg: 0 },
    time: { utc: "2026-09-18T16:47:34+00:00", itow_ms: 1, gps_week: null, gps_tow_s: null, leap_s: null, valid_date: true, valid_time: true, fully_resolved: true, valid_utc: false, utc_standard: null, t_acc_ns: null, clk_bias_ns: null, clk_drift_nsps: null, f_acc_psps: null, leap_source: null, time_to_leap_event_s: null, leap_change: null },
    sats: [], sat_summary: { tracked: 0, used: 0, per_gnss: {} }, hardware: null, rf: [], spectrum: [], ports: [],
    survey_in: { active: false, valid: false, dur_s: 0, obs: 0, mean_x_m: null, mean_y_m: null, mean_z_m: null, mean_acc_m: null },
    rtcm_out: { messages: {}, total_count: 0, total_bytes: 0, bytes_per_s: 0 },
    firmware: { sw_version: "", hw_version: "", fw_version: "", protver: "", module: "", extensions: [] },
  };
}

describe("live store reducer", () => {
  beforeEach(() => useLive.setState({ state: null, events: [], jobs: {}, ntripClients: [], receiverConnected: null }));

  it("applies snapshot then merges epochs", () => {
    const apply = useLive.getState().applyMessage;
    apply({ type: "snapshot", role: "base", topics: ["pvt"], state: baseState() } as WsMessage, 1000);
    expect(useLive.getState().state?.fix.fix_type).toBe(3);
    apply({ type: "epoch", t: 1, pvt: { ...baseState(), accuracy: { ...baseState().accuracy, h_acc_m: 0.02 } } as never, rtcm: { messages: {}, total_count: 5, total_bytes: 50, bytes_per_s: 10 } } as WsMessage, 2000);
    const s = useLive.getState();
    expect(s.state?.accuracy.h_acc_m).toBe(0.02);
    expect(s.state?.rtcm_out.total_count).toBe(5);
    expect(s.state?.epoch_count).toBe(4);
    expect(s.lastEpochAt).toBe(2000);
  });

  it("routes updates to the right slice", () => {
    const apply = useLive.getState().applyMessage;
    apply({ type: "snapshot", role: "base", topics: [], state: baseState() } as WsMessage);
    apply({ type: "update", topic: "rf", source: "state.hardware", data: { jam_ind: 7 } });
    apply({ type: "update", topic: "events", source: "events.new", data: { id: 1, kind: "jamming", level: "warning", message: "x", ts_utc: "t", meta: {}, acked: false } });
    apply({ type: "update", topic: "receiver", source: "receiver.disconnected", data: "unplugged" });
    apply({ type: "update", topic: "jobs", source: "jobs.update", data: { id: "abc", kind: "export", status: "running", progress: 0.5 } });
    apply({ type: "update", topic: "base", source: "base.site_verified", data: { site: "roof" } });
    const s = useLive.getState();
    expect((s.state?.hardware as { jam_ind: number }).jam_ind).toBe(7);
    expect(s.events[0].kind).toBe("jamming");
    expect(s.receiverConnected).toBe(false);
    expect(s.jobs.abc.progress).toBe(0.5);
    expect(s.base.verified).toBe(true);
  });

  it("caps the event ring buffer at 50", () => {
    const apply = useLive.getState().applyMessage;
    for (let i = 0; i < 60; i++) apply({ type: "update", topic: "events", source: "events.new", data: { id: i, kind: "k", level: "info", message: "", ts_utc: "", meta: {}, acked: false } });
    expect(useLive.getState().events).toHaveLength(50);
    expect(useLive.getState().events[0].id).toBe(59);
  });
});
```

`web/src/lib/api.test.ts`:
```ts
import { ApiError, api, wsUrl } from "./api";

describe("api", () => {
  const originalFetch = globalThis.fetch;
  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it("returns JSON and sends JSON bodies", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: 1 }), { status: 200 }));
    globalThis.fetch = fetchMock as typeof fetch;
    await expect(api("/api/x", { method: "POST", body: JSON.stringify({ a: 1 }) })).resolves.toEqual({ ok: 1 });
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/json");
    expect(init.credentials).toBe("same-origin");
  });

  it("throws ApiError with the server detail", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "no site named 'x'" }), { status: 404 })) as typeof fetch;
    await expect(api("/api/x")).rejects.toMatchObject<Partial<ApiError>>({ status: 404, detail: "no site named 'x'" });
  });

  it("redirects to /login on 401", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(new Response("", { status: 401 })) as typeof fetch;
    const assign = vi.fn();
    Object.defineProperty(window, "location", { value: { ...window.location, pathname: "/logs", search: "", assign }, writable: true });
    await expect(api("/api/x")).rejects.toBeInstanceOf(ApiError);
    expect(assign).toHaveBeenCalledWith("/login?next=%2Flogs");
  });

  it("builds the websocket url with an optional token", () => {
    Object.defineProperty(window, "location", { value: { ...window.location, protocol: "http:", host: "base:8080" }, writable: true });
    expect(wsUrl()).toBe("ws://base:8080/ws");
    expect(wsUrl("abc")).toBe("ws://base:8080/ws?token=abc");
  });
});
```

- [ ] **Step 8: Run, build, commit**

Run: `cd web && pnpm test && pnpm lint && pnpm build`
Expected: all tests pass (shell tests still pass with the real store).

```bash
cd /home/nekosaif/github/mtrtk
git add web
git commit -m "feat(web): API client, TypeScript types, live WebSocket store, status tape readings

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Formatting and client-side geodesy

**Files:**
- Modify: `web/src/lib/format.ts` (complete)
- Create: `web/src/lib/geo.ts`, `web/src/lib/format.test.ts`, `web/src/lib/geo.test.ts`, `web/src/lib/prefs.ts`

**Interfaces:**
- Produces: `format.ts`: `fmtAcc`, `fmtRate`, `fmtBytes(n)`, `fmtMeters(m, digits=3)`, `fmtDuration(s)` (`1h 02m 03s`), `fmtUtc(iso)` (`16:47:34`), `fmtUtcDate(iso)` (`2026-09-18 16:47:34 UTC`), `relTime(iso, now)` (`12 s ago`), `fmtDms(deg, isLat, decimals=4)`, `fmtCoord(lat, lon, mode)`; `geo.ts`: `llhToEcef`, `ecefToLlh`, `utmZone`, `llhToUtm` (same formulas as `mtrtk/core/geo.py`); `prefs.ts`: `getPref<T>(key, fallback)`, `setPref(key, value)` wrapping `localStorage` in try/catch, `usePref` hook; coordinate mode type `CoordMode = "dd" | "dms" | "utm" | "ecef"`.

- [ ] **Step 1: Write the failing tests**

`web/src/lib/format.test.ts`:
```ts
import { fmtAcc, fmtBytes, fmtCoord, fmtDms, fmtDuration, fmtRate, fmtUtc, relTime } from "./format";

describe("format", () => {
  it("accuracy switches units", () => {
    expect(fmtAcc(0.012)).toBe("1.2 cm");
    expect(fmtAcc(1.234)).toBe("1.23 m");
    expect(fmtAcc(12.3)).toBe("12 m");
    expect(fmtAcc(null)).toBe("—");
  });
  it("rates and bytes", () => {
    expect(fmtRate(0)).toBe("0 B/s");
    expect(fmtRate(1900)).toBe("1.9 kB/s");
    expect(fmtBytes(0)).toBe("0 B");
    expect(fmtBytes(1536)).toBe("1.5 kB");
    expect(fmtBytes(5.5e9)).toBe("5.5 GB");
  });
  it("durations and times", () => {
    expect(fmtDuration(3723)).toBe("1h 02m 03s");
    expect(fmtDuration(59)).toBe("59s");
    expect(fmtUtc("2026-09-18T16:47:34+00:00")).toBe("16:47:34");
    expect(relTime("2026-09-18T16:47:34+00:00", Date.parse("2026-09-18T16:47:46Z"))).toBe("12 s ago");
    expect(relTime("2026-09-18T16:00:00+00:00", Date.parse("2026-09-18T16:47:46Z"))).toBe("47 min ago");
  });
  it("DMS matches the backend formatter", () => {
    expect(fmtDms(23.8373506, true)).toBe("23°50'14.4622\"N");
    expect(fmtDms(-90.2625502, false)).toBe("90°15'45.1807\"W");
    expect(fmtDms(45.99999999, true, 2)).toBe("46°00'00.00\"N");
  });
  it("fmtCoord modes", () => {
    expect(fmtCoord(23.8373506, 90.2625502, "dd")).toEqual(["23.8373506°", "90.2625502°"]);
    expect(fmtCoord(23.8373506, 90.2625502, "dms")[0]).toBe("23°50'14.4622\"N");
    expect(fmtCoord(23.8373506, 90.2625502, "utm")).toEqual(["46N", "E 221150.294  N 2638912.702"]);
    expect(fmtCoord(null, null, "dd")).toEqual(["—", "—"]);
  });
});
```

`web/src/lib/geo.test.ts`:
```ts
import { ecefToLlh, llhToEcef, llhToUtm, utmZone } from "./geo";

// Reference values computed with pyproj (WGS84) during planning.
describe("geo", () => {
  it("llh -> ecef matches pyproj", () => {
    const [x, y, z] = llhToEcef(23.8373506, 90.2625502, -36.268);
    expect(x).toBeCloseTo(-26748.172, 3);
    expect(y).toBeCloseTo(5837156.6184, 3);
    expect(z).toBeCloseTo(2561801.2607, 3);
    const [sx, sy, sz] = llhToEcef(-33.8688, 151.2093, 25.0);
    expect([sx, sy, sz].map((v) => Math.round(v * 1000) / 1000)).toEqual([-4646069.464, 2553216.34, -3534386.32].map((v) => Math.round(v * 1000) / 1000));
  });
  it("round trips", () => {
    const [lat, lon, h] = ecefToLlh(...llhToEcef(23.8373506, 90.2625502, -36.268));
    expect(lat).toBeCloseTo(23.8373506, 8);
    expect(lon).toBeCloseTo(90.2625502, 8);
    expect(h).toBeCloseTo(-36.268, 3);
    expect(llhToEcef(0, 0, 0)[0]).toBeCloseTo(6378137, 6);
  });
  it("utm matches pyproj", () => {
    expect(utmZone(23.8373506, 90.2625502)).toBe(46);
    expect(utmZone(60.39, 5.32)).toBe(32);
    expect(utmZone(78.22, 15.63)).toBe(33);
    const d = llhToUtm(23.8373506, 90.2625502);
    expect(d.zone).toBe(46);
    expect(d.hemisphere).toBe("N");
    expect(d.easting).toBeCloseTo(221150.294, 2);
    expect(d.northing).toBeCloseTo(2638912.702, 2);
    const s = llhToUtm(-33.8688, 151.2093);
    expect(s.label).toBe("56S");
    expect(s.easting).toBeCloseTo(334368.634, 2);
    expect(s.northing).toBeCloseTo(6250948.345, 2);
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd web && pnpm test`
Expected: FAIL (`geo.ts` missing; format functions missing).

- [ ] **Step 3: Write `web/src/lib/geo.ts`**

```ts
// WGS84 geodesy, same formulas as src/mtrtk/core/geo.py (Snyder transverse Mercator series).
const A = 6378137.0;
const F = 1 / 298.257223563;
const B = A * (1 - F);
const E2 = F * (2 - F);
const EP2 = E2 / (1 - E2);
const K0 = 0.9996;
const rad = (d: number) => (d * Math.PI) / 180;
const deg = (r: number) => (r * 180) / Math.PI;

export function llhToEcef(latDeg: number, lonDeg: number, h: number): [number, number, number] {
  const lat = rad(latDeg), lon = rad(lonDeg);
  const s = Math.sin(lat), c = Math.cos(lat);
  const n = A / Math.sqrt(1 - E2 * s * s);
  return [(n + h) * c * Math.cos(lon), (n + h) * c * Math.sin(lon), (n * (1 - E2) + h) * s];
}

export function ecefToLlh(x: number, y: number, z: number): [number, number, number] {
  const lon = Math.atan2(y, x);
  const p = Math.hypot(x, y);
  if (p < 1e-9) return [z >= 0 ? 90 : -90, deg(lon), Math.abs(z) - B];
  let lat = Math.atan2(z, p * (1 - E2));
  let n = A;
  let h = 0;
  for (let i = 0; i < 20; i++) {
    const s = Math.sin(lat);
    n = A / Math.sqrt(1 - E2 * s * s);
    h = p / Math.cos(lat) - n;
    const next = Math.atan2(z, p * (1 - (E2 * n) / (n + h)));
    if (Math.abs(next - lat) < 1e-14) { lat = next; break; }
    lat = next;
  }
  const s = Math.sin(lat);
  n = A / Math.sqrt(1 - E2 * s * s);
  return [deg(lat), deg(lon), p / Math.cos(lat) - n];
}

export function utmZone(latDeg: number, lonDeg: number): number {
  let zone = Math.floor((lonDeg + 180) / 6) + 1;
  if (latDeg >= 56 && latDeg < 64 && lonDeg >= 3 && lonDeg < 12) zone = 32;
  if (latDeg >= 72 && lonDeg >= 0 && lonDeg < 42) zone = lonDeg < 9 ? 31 : lonDeg < 21 ? 33 : lonDeg < 33 ? 35 : 37;
  return Math.min(Math.max(zone, 1), 60);
}

export interface Utm { zone: number; hemisphere: "N" | "S"; easting: number; northing: number; label: string }

export function llhToUtm(latDeg: number, lonDeg: number): Utm {
  const zone = utmZone(latDeg, lonDeg);
  const hemisphere: "N" | "S" = latDeg >= 0 ? "N" : "S";
  const lat = rad(latDeg);
  const dLon = rad(lonDeg) - rad((zone - 1) * 6 - 180 + 3);
  const s = Math.sin(lat), c = Math.cos(lat), t = Math.tan(lat);
  const n = A / Math.sqrt(1 - E2 * s * s);
  const T = t * t, C = EP2 * c * c, a = dLon * c;
  const m = A * ((1 - E2 / 4 - (3 * E2 ** 2) / 64 - (5 * E2 ** 3) / 256) * lat
    - ((3 * E2) / 8 + (3 * E2 ** 2) / 32 + (45 * E2 ** 3) / 1024) * Math.sin(2 * lat)
    + ((15 * E2 ** 2) / 256 + (45 * E2 ** 3) / 1024) * Math.sin(4 * lat)
    - ((35 * E2 ** 3) / 3072) * Math.sin(6 * lat));
  let easting = K0 * n * (a + ((1 - T + C) * a ** 3) / 6 + ((5 - 18 * T + T * T + 72 * C - 58 * EP2) * a ** 5) / 120) + 500000;
  let northing = K0 * (m + n * t * ((a * a) / 2 + ((5 - T + 9 * C + 4 * C * C) * a ** 4) / 24 + ((61 - 58 * T + T * T + 600 * C - 330 * EP2) * a ** 6) / 720));
  if (hemisphere === "S") northing += 10_000_000;
  easting = Math.round(easting * 1e6) / 1e6;
  northing = Math.round(northing * 1e6) / 1e6;
  return { zone, hemisphere, easting, northing, label: `${zone}${hemisphere}` };
}
```

- [ ] **Step 4: Complete `web/src/lib/format.ts` and write `web/src/lib/prefs.ts`**

`format.ts` (replace the file):
```ts
import { llhToEcef, llhToUtm } from "./geo";

export type CoordMode = "dd" | "dms" | "utm" | "ecef";
export const COORD_MODES: { value: CoordMode; label: string }[] = [
  { value: "dd", label: "Decimal degrees" }, { value: "dms", label: "Degrees minutes seconds" }, { value: "utm", label: "UTM" }, { value: "ecef", label: "ECEF" },
];
const DASH = "—";

export function fmtAcc(m: number | null | undefined): string {
  if (m == null) return DASH;
  return m >= 10 ? `${m.toFixed(0)} m` : m >= 1 ? `${m.toFixed(2)} m` : `${(m * 100).toFixed(1)} cm`;
}
export function fmtRate(bytesPerS: number | null | undefined): string {
  if (bytesPerS == null || bytesPerS <= 0) return "0 B/s";
  return bytesPerS >= 1024 ? `${(bytesPerS / 1024).toFixed(1)} kB/s` : `${bytesPerS.toFixed(0)} B/s`;
}
export function fmtBytes(n: number | null | undefined): string {
  if (n == null) return DASH;
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} kB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  return `${(n / 1e9).toFixed(1)} GB`;
}
export function fmtMeters(m: number | null | undefined, digits = 3): string {
  return m == null ? DASH : `${m.toFixed(digits)} m`;
}
export function fmtDuration(s: number | null | undefined): string {
  if (s == null) return DASH;
  const total = Math.round(s);
  const h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), sec = total % 60;
  if (h > 0) return `${h}h ${String(m).padStart(2, "0")}m ${String(sec).padStart(2, "0")}s`;
  if (m > 0) return `${m}m ${String(sec).padStart(2, "0")}s`;
  return `${sec}s`;
}
export function fmtUtc(iso: string | null | undefined): string {
  if (!iso) return DASH;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? DASH : d.toISOString().slice(11, 19);
}
export function fmtUtcDate(iso: string | null | undefined): string {
  if (!iso) return DASH;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? DASH : `${d.toISOString().slice(0, 10)} ${d.toISOString().slice(11, 19)} UTC`;
}
export function relTime(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return DASH;
  const dt = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
  if (dt < 60) return `${dt} s ago`;
  if (dt < 3600) return `${Math.floor(dt / 60)} min ago`;
  if (dt < 86400) return `${Math.floor(dt / 3600)} h ago`;
  return `${Math.floor(dt / 86400)} d ago`;
}
export function fmtDms(value: number, isLat: boolean, decimals = 4): string {
  const hemi = isLat ? (value >= 0 ? "N" : "S") : value >= 0 ? "E" : "W";
  const total = Number(Math.abs(value) * 3600).toFixed(decimals);
  let totalSeconds = Number(total);
  const d = Math.floor(totalSeconds / 3600);
  totalSeconds -= d * 3600;
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds - m * 60;
  const width = decimals ? 3 + decimals : 2;
  return `${d}°${String(m).padStart(2, "0")}'${s.toFixed(decimals).padStart(width, "0")}"${hemi}`;
}
export function fmtCoord(lat: number | null | undefined, lon: number | null | undefined, mode: CoordMode, h = 0): [string, string] {
  if (lat == null || lon == null) return [DASH, DASH];
  switch (mode) {
    case "dd": return [`${lat.toFixed(7)}°`, `${lon.toFixed(7)}°`];
    case "dms": return [fmtDms(lat, true), fmtDms(lon, false)];
    case "utm": { const u = llhToUtm(lat, lon); return [u.label, `E ${u.easting.toFixed(3)}  N ${u.northing.toFixed(3)}`]; }
    case "ecef": { const [x, y, z] = llhToEcef(lat, lon, h); return [`X ${x.toFixed(4)}  Y ${y.toFixed(4)}`, `Z ${z.toFixed(4)}`]; }
  }
}
```

`prefs.ts`:
```ts
import { useCallback, useState } from "react";

const PREFIX = "mtrtk:";

export function getPref<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(PREFIX + key);
    return raw == null ? fallback : (JSON.parse(raw) as T);
  } catch {
    return fallback;
  }
}
export function setPref<T>(key: string, value: T): void {
  try {
    localStorage.setItem(PREFIX + key, JSON.stringify(value));
  } catch {
    /* private mode or storage blocked: preference lives for this page only */
  }
}
export function usePref<T>(key: string, fallback: T): [T, (v: T) => void] {
  const [value, setValue] = useState<T>(() => getPref(key, fallback));
  const update = useCallback((v: T) => { setValue(v); setPref(key, v); }, [key]);
  return [value, update];
}
```

- [ ] **Step 5: Run, build, commit**

Run: `cd web && pnpm test && pnpm lint`
Expected: all pass. If `fmtDms(45.99999999, true, 2)` yields `45°60'00.00"N`, the seconds rounding must carry: compute `totalSeconds = Number((abs*3600).toFixed(decimals))` first (as written) — the carry then happens naturally because 165599.99999 rounds to 165600.00.

```bash
cd /home/nekosaif/github/mtrtk
git add web/src/lib
git commit -m "feat(web): formatting helpers, client-side WGS84/UTM geodesy and persisted preferences

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Dashboard — building blocks, sky plot, map, sparklines

**Files:**
- Create: `web/src/components/Panel.tsx`, `web/src/components/Readout.tsx`, `web/src/components/Stat.tsx`, `web/src/components/EmptyState.tsx`, `web/src/components/CopyButton.tsx`, `web/src/components/charts/SkyPlot.tsx`, `web/src/components/charts/Legend.tsx`, `web/src/components/charts/Sparkline.tsx`, `web/src/components/MapPanel.tsx`, `web/src/components/CoordinateReadout.tsx`, `web/src/components/SystemChips.tsx`, `web/src/pages/Dashboard.tsx`, `web/src/components/charts/SkyPlot.test.tsx`, `web/src/pages/Dashboard.test.tsx`, `web/src/test/fixtures.ts`
- Modify: `web/src/app/router.tsx` (route `/` → `Dashboard`)

**Interfaces:**
- Produces: `Panel({title, actions?, children, className?, as?})` (section with `h2`, 1px rule under the title); `Readout({label, value, unit?, size?})`; `Stat({label, value, hint?, level?})`; `EmptyState({title, body, action?})`; `CopyButton({text})`; `SkyPlot({sats, size=320, onHover?})` — polar chart, elevation rings 0/30/60 in brass, N/E/S/W ticks, disc radius 3.5–8 px by C/N0 (0–55 dB-Hz), filled when used, hollow when tracked only, CSS transitions on `cx/cy/r`, a `<title>` per disc, `role="img"` with an `aria-label` summary; `Legend({items})` with system swatches and counts; `Sparkline({points: {t, v}[], unit, format, height=48, color})` with crosshair tooltip; `MapPanel({lat, lon, hAcc, rovers, height})` — MapLibre with OSM raster style and an imagery toggle (Esri World Imagery), base marker, accuracy circle, rover markers; shows an offline notice when tiles fail; `CoordinateReadout({position, accuracy})` with the persisted `CoordMode` selector (`prefs` key `coordMode`); `SystemChips({summary})`.
- `test/fixtures.ts`: `sampleState()` (a `ReceiverState` with 8 satellites across 4 systems, survey-in active) reused by every page test.

- [ ] **Step 1: Write `web/src/test/fixtures.ts`**

```ts
import type { ReceiverState, Satellite } from "@/lib/types";

function sat(gnss_id: number, gnss: string, sv_id: number, cno: number, elev: number, azim: number, used = true): Satellite {
  return { gnss_id, gnss, sv_id, cno, elev, azim, pr_res_m: 0.3, quality_ind: 7, used, health: 1, diff_corr: false, smoothed: false, orbit_source: 1, eph_avail: true, alm_avail: true,
    signals: [{ sig_id: 0, name: "L1C/A", freq_id: 0, cno, pr_res_m: 0.3, quality_ind: 7, corr_source: 0, iono_model: 0, health: 1, pr_used: used, cr_used: used, do_used: used }] };
}

export function sampleState(): ReceiverState {
  const sats = [
    sat(0, "GPS", 5, 45, 72, 120), sat(0, "GPS", 12, 38, 35, 210), sat(0, "GPS", 25, 22, 8, 300, false),
    sat(6, "GLONASS", 3, 40, 50, 40), sat(6, "GLONASS", 9, 30, 20, 330),
    sat(2, "Galileo", 4, 42, 60, 180), sat(3, "BeiDou", 21, 36, 44, 260), sat(5, "QZSS", 194, 28, 15, 95, false),
  ];
  return {
    connected: true, source: "serial:/dev/ttyACM0", epoch_count: 120, raw_epochs: 120, last_epoch_mono: 1,
    position: { lat: 23.8373506, lon: 90.2625502, height_m: -36.268, hmsl_m: 13.363, ecef_x_m: -26748.172, ecef_y_m: 5837156.618, ecef_z_m: 2561801.261, invalid_llh: false },
    accuracy: { h_acc_m: 0.012, v_acc_m: 0.018, p_acc_m: 0.02, t_acc_ns: 20, s_acc_mps: 0.01, head_acc_deg: 1 },
    dops: { g: 1.5, p: 1.2, t: 0.8, v: 1.0, h: 0.7, n: 0.5, e: 0.4 },
    fix: { fix_type: 3, fix_type_name: "3D", gnss_fix_ok: true, diff_soln: false, carr_soln: 0, carr_soln_name: "None", num_sv: 6, last_correction_age: 0, psm_state: 0, spoof_det_state: 0, ttff_ms: 2500, uptime_ms: 600000 },
    velocity: { vel_n_mps: 0, vel_e_mps: 0, vel_d_mps: 0, ground_speed_mps: 0, heading_motion_deg: 0 },
    time: { utc: "2026-09-18T16:47:34+00:00", itow_ms: 492472000, gps_week: 2436, gps_tow_s: 492472, leap_s: 18, valid_date: true, valid_time: true, fully_resolved: true, valid_utc: true, utc_standard: 3, t_acc_ns: 20, clk_bias_ns: 1500, clk_drift_nsps: -7, f_acc_psps: 300, leap_source: 2, time_to_leap_event_s: 100000, leap_change: 0 },
    sats, sat_summary: { tracked: 8, used: 6, per_gnss: { GPS: { tracked: 3, used: 2 }, GLONASS: { tracked: 2, used: 2 }, Galileo: { tracked: 1, used: 1 }, BeiDou: { tracked: 1, used: 1 }, QZSS: { tracked: 1, used: 0 } } },
    hardware: { ant_status: 2, ant_status_name: "OK", ant_power: 1, ant_power_name: "On", noise_per_ms: 90, agc_cnt: 3000, jam_ind: 12, jamming_state: 1, jamming_state_name: "OK", rtc_calib: true, safe_boot: false, xtal_absent: false },
    rf: [{ block_id: 0, jamming_state: 1, jamming_state_name: "OK", ant_status: 2, ant_status_name: "OK", ant_power: 1, ant_power_name: "On", post_status: 0, noise_per_ms: 80, agc_cnt: 4000, jam_ind: 10, ofs_i: 1, mag_i: 100, ofs_q: -1, mag_q: 99 },
         { block_id: 1, jamming_state: 1, jamming_state_name: "OK", ant_status: 2, ant_status_name: "OK", ant_power: 1, ant_power_name: "On", post_status: 0, noise_per_ms: 70, agc_cnt: 5000, jam_ind: 5, ofs_i: 0, mag_i: 90, ofs_q: 0, mag_q: 91 }],
    spectrum: [{ block_id: 0, span_hz: 100_000_000, res_hz: 390_625, center_hz: 1_580_000_000, pga_db: 20, bins: Array.from({ length: 256 }, (_, i) => 60 + Math.round(30 * Math.exp(-((i - 128) ** 2) / 800))) }],
    ports: [{ port_id: 0x0300, tx_pending: 10, tx_bytes: 123456, tx_usage: 5, tx_peak_usage: 40, rx_pending: 0, rx_bytes: 999, rx_usage: 1, rx_peak_usage: 3, overrun_errs: 0, skipped: 0 }],
    survey_in: { active: true, valid: false, dur_s: 120, obs: 118, mean_x_m: -26748.1, mean_y_m: 5837156.6, mean_z_m: 2561801.3, mean_acc_m: 1.9 },
    rtcm_out: { messages: { "1005": { count: 120, bytes: 3000, last_seen_mono: 1 }, "1077": { count: 120, bytes: 40000, last_seen_mono: 1 }, "1230": { count: 24, bytes: 400, last_seen_mono: 1 } }, total_count: 264, total_bytes: 43400, bytes_per_s: 1900 },
    firmware: { sw_version: "EXT CORE 1.00 (f10c36)", hw_version: "00190000", fw_version: "HPG 1.13", protver: "27.12", module: "ZED-F9P", extensions: ["FWVER=HPG 1.13", "PROTVER=27.12"] },
  };
}
```

- [ ] **Step 2: Write the failing tests**

`web/src/components/charts/SkyPlot.test.tsx`:
```tsx
import { render, screen } from "@testing-library/react";
import { sampleState } from "@/test/fixtures";
import { SkyPlot } from "./SkyPlot";

describe("SkyPlot", () => {
  it("draws one disc per satellite with elevation mapped to radius", () => {
    const sats = sampleState().sats;
    render(<SkyPlot sats={sats} size={300} />);
    const img = screen.getByRole("img", { name: /8 satellites, 6 used/ });
    const discs = img.querySelectorAll("circle[data-sat]");
    expect(discs).toHaveLength(8);
    const zenith = img.querySelector('circle[data-sat="GPS-5"]')!; // elev 72 → close to centre
    const horizon = img.querySelector('circle[data-sat="GPS-25"]')!; // elev 8 → near the outer ring
    const dist = (c: Element) => Math.hypot(Number(c.getAttribute("cx")) - 150, Number(c.getAttribute("cy")) - 150);
    expect(dist(zenith)).toBeLessThan(dist(horizon));
    expect(horizon.getAttribute("fill")).toBe("none"); // tracked only → hollow
    expect(zenith.getAttribute("fill")).toBe("var(--sys-gps)");
  });

  it("places azimuth 90° to the east (right of centre)", () => {
    const sats = [{ ...sampleState().sats[0], elev: 45, azim: 90, gnss: "GPS", sv_id: 1 }];
    render(<SkyPlot sats={sats} size={200} />);
    const disc = screen.getByRole("img").querySelector("circle[data-sat]")!;
    expect(Number(disc.getAttribute("cx"))).toBeGreaterThan(100);
    expect(Math.abs(Number(disc.getAttribute("cy")) - 100)).toBeLessThan(1);
  });

  it("shows an empty message without satellites", () => {
    render(<SkyPlot sats={[]} />);
    expect(screen.getByText(/no satellites tracked/i)).toBeInTheDocument();
  });
});
```

`web/src/pages/Dashboard.test.tsx`:
```tsx
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { useLive } from "@/lib/live";
import { sampleState } from "@/test/fixtures";
import Dashboard from "./Dashboard";

vi.mock("maplibre-gl", () => {
  class Map {
    on() { return this; }
    once() { return this; }
    addControl() { return this; }
    remove() {}
    getSource() { return undefined; }
    addSource() {}
    addLayer() {}
    setStyle() {}
    easeTo() {}
    resize() {}
  }
  class Marker { setLngLat() { return this; } addTo() { return this; } remove() {} setPopup() { return this; } }
  class Popup { setText() { return this; } setHTML() { return this; } }
  class NavigationControl {}
  return { default: { Map, Marker, Popup, NavigationControl }, Map, Marker, Popup, NavigationControl };
});

function renderDashboard() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><Dashboard /></MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Dashboard", () => {
  beforeEach(() => {
    useLive.setState({ state: sampleState(), status: "open", lastEpochAt: Date.now(), receiverConnected: true, ntripClients: [] });
    globalThis.fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ res: "1s", columns: ["ts", "h_acc_m", "nsat_used", "cno_mean"], rows: [[1, 0.5, 20, 40], [2, 0.4, 21, 41]] }), { status: 200 })) as typeof fetch;
  });

  it("shows the coordinate hero, sky plot, systems and survey-in", async () => {
    renderDashboard();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Dashboard");
    const hero = screen.getByTestId("coordinate-readout");
    expect(hero).toHaveTextContent("23°50'14.4622\"N");
    expect(hero).toHaveTextContent("90°15'45.1807\"E");
    expect(hero).toHaveTextContent("1.2 cm");
    expect(screen.getByRole("img", { name: /8 satellites/ })).toBeInTheDocument();
    expect(screen.getByText("GLONASS")).toBeInTheDocument();
    expect(screen.getByText(/survey-in running/i)).toBeInTheDocument();
    expect(await screen.findByText(/last hour/i)).toBeInTheDocument();
  });

  it("switches coordinate format and remembers it", async () => {
    renderDashboard();
    const hero = screen.getByTestId("coordinate-readout");
    await userEvent.selectOptions(within(hero).getByLabelText(/coordinate format/i), "dd");
    expect(hero).toHaveTextContent("23.8373506°");
    expect(localStorage.getItem("mtrtk:coordMode")).toBe('"dd"');
  });
});
```

- [ ] **Step 3: Run to verify failure**

Run: `cd web && pnpm test`
Expected: FAIL (modules missing).

- [ ] **Step 4: Write the building blocks**

`web/src/components/Panel.tsx`:
```tsx
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export function Panel({ title, actions, children, className, bodyClassName }: { title?: string; actions?: ReactNode; children: ReactNode; className?: string; bodyClassName?: string }) {
  return (
    <section className={cn("panel flex min-w-0 flex-col", className)}>
      {title ? (
        <header className="flex items-center justify-between gap-3 border-b border-line px-4 py-2.5">
          <h2 className="text-[16px] font-medium leading-6">{title}</h2>
          {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
        </header>
      ) : null}
      <div className={cn("min-w-0 flex-1 p-4", bodyClassName)}>{children}</div>
    </section>
  );
}
```

`web/src/components/Readout.tsx`:
```tsx
import { cn } from "@/lib/utils";

export function Readout({ label, value, unit, size = "md", className }: { label: string; value: string; unit?: string; size?: "sm" | "md" | "lg"; className?: string }) {
  const valueClass = size === "lg" ? "text-[28px] leading-[34px]" : size === "sm" ? "text-[14px] leading-5" : "text-[20px] leading-7";
  return (
    <div className={cn("flex flex-col gap-0.5", className)}>
      <span className="text-[12px] leading-4 text-ink-2">{label}</span>
      <span className={cn("num", valueClass)}>
        {value}
        {unit ? <span className="ml-1 text-[0.7em] text-ink-2">{unit}</span> : null}
      </span>
    </div>
  );
}
```

`web/src/components/Stat.tsx`:
```tsx
import { STATUS, type StatusLevel } from "@/lib/palette";

export function Stat({ label, value, hint, level }: { label: string; value: string; hint?: string; level?: StatusLevel }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-line py-1.5 last:border-0">
      <span className="text-ink-2">{label}</span>
      <span className="num text-right" style={level ? { color: STATUS[level] } : undefined} title={hint}>{value}</span>
    </div>
  );
}
```

`web/src/components/EmptyState.tsx`:
```tsx
import type { ReactNode } from "react";

export function EmptyState({ title, body, action }: { title: string; body?: string; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-start gap-2 rounded-md border border-dashed border-line p-6 text-ink-2">
      <p className="text-ink">{title}</p>
      {body ? <p className="max-w-[60ch]">{body}</p> : null}
      {action}
    </div>
  );
}
```

`web/src/components/CopyButton.tsx`:
```tsx
import { Check, Copy } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <Button variant="outline" size="sm" onClick={async () => { try { await navigator.clipboard.writeText(text); setDone(true); setTimeout(() => setDone(false), 1500); } catch { /* clipboard blocked */ } }}>
      {done ? <Check className="size-3.5" aria-hidden /> : <Copy className="size-3.5" aria-hidden />}
      {done ? "Copied" : label}
    </Button>
  );
}
```

- [ ] **Step 5: Write the charts**

`web/src/components/charts/Legend.tsx`:
```tsx
export function Legend({ items }: { items: { label: string; color: string; value?: string }[] }) {
  if (items.length < 2) return null;
  return (
    <ul className="flex flex-wrap gap-x-4 gap-y-1 text-[12px] leading-4 text-ink-2">
      {items.map((it) => (
        <li key={it.label} className="flex items-center gap-1.5">
          <span className="inline-block size-2.5 rounded-sm" style={{ background: it.color }} aria-hidden />
          <span className="text-ink">{it.label}</span>
          {it.value ? <span className="num">{it.value}</span> : null}
        </li>
      ))}
    </ul>
  );
}
```

`web/src/components/charts/SkyPlot.tsx`:
```tsx
import { useMemo, useState } from "react";
import type { Satellite } from "@/lib/types";
import { SYSTEM_ORDER, systemColor } from "@/lib/palette";
import { Legend } from "./Legend";

const RING_ELEVATIONS = [0, 30, 60];
const CARDINALS: [string, number][] = [["N", 0], ["E", 90], ["S", 180], ["W", 270]];

function radiusForCno(cno: number): number {
  return 3.5 + 4.5 * Math.min(1, Math.max(0, cno) / 55);
}

export function SkyPlot({ sats, size = 320, className }: { sats: Satellite[]; size?: number; className?: string }) {
  const [hover, setHover] = useState<Satellite | null>(null);
  const c = size / 2;
  const R = c - 18;
  const placed = useMemo(
    () => sats.filter((s) => s.elev != null && s.azim != null).map((s) => {
      const r = ((90 - (s.elev as number)) / 90) * R;
      const a = ((s.azim as number) * Math.PI) / 180;
      return { s, cx: c + r * Math.sin(a), cy: c - r * Math.cos(a) };
    }),
    [sats, R, c],
  );
  const used = sats.filter((s) => s.used).length;
  if (sats.length === 0) return <p className="text-ink-2">No satellites tracked yet.</p>;
  const legendItems = SYSTEM_ORDER.filter((name) => sats.some((s) => s.gnss === name)).map((name) => ({ label: name, color: systemColor(name), value: `${sats.filter((s) => s.gnss === name && s.used).length}/${sats.filter((s) => s.gnss === name).length}` }));
  return (
    <div className={className}>
      <svg role="img" aria-label={`Sky plot: ${sats.length} satellites, ${used} used`} viewBox={`0 0 ${size} ${size}`} width="100%" style={{ maxWidth: size }} className="block mx-auto">
        {RING_ELEVATIONS.map((elev) => (
          <circle key={elev} cx={c} cy={c} r={((90 - elev) / 90) * R} fill={elev === 0 ? "var(--panel-2)" : "none"} stroke="var(--brass)" strokeOpacity={elev === 0 ? 0.9 : 0.45} strokeWidth={elev === 0 ? 1.5 : 1} />
        ))}
        <line x1={c} y1={c - R} x2={c} y2={c + R} stroke="var(--line)" />
        <line x1={c - R} y1={c} x2={c + R} y2={c} stroke="var(--line)" />
        {RING_ELEVATIONS.slice(1).map((elev) => (
          <text key={elev} x={c + 3} y={c - ((90 - elev) / 90) * R - 3} className="num" fontSize={10} fill="var(--ink-3)">{elev}°</text>
        ))}
        {CARDINALS.map(([label, az]) => {
          const a = (az * Math.PI) / 180;
          return <text key={label} x={c + (R + 11) * Math.sin(a)} y={c - (R + 11) * Math.cos(a) + 4} textAnchor="middle" fontSize={12} fill="var(--ink-2)">{label}</text>;
        })}
        {placed.map(({ s, cx, cy }) => {
          const color = systemColor(s.gnss);
          return (
            <circle
              key={`${s.gnss}-${s.sv_id}`}
              data-sat={`${s.gnss}-${s.sv_id}`}
              cx={cx} cy={cy} r={radiusForCno(s.cno)}
              fill={s.used ? color : "none"} stroke={color} strokeWidth={1.5}
              style={{ transition: "cx 0.8s ease, cy 0.8s ease, r 0.4s ease" }}
              onMouseEnter={() => setHover(s)} onMouseLeave={() => setHover(null)}
            >
              <title>{`${s.gnss} ${s.sv_id}: ${s.cno} dB-Hz, elev ${s.elev}°, az ${s.azim}°${s.used ? ", used" : ""}`}</title>
            </circle>
          );
        })}
      </svg>
      <div className="mt-2 flex items-start justify-between gap-3">
        <Legend items={legendItems} />
        <span className="num min-h-4 text-[12px] leading-4 text-ink-2">{hover ? `${hover.gnss} ${hover.sv_id} · ${hover.cno} dB-Hz · el ${hover.elev}° az ${hover.azim}°` : "hollow = tracked, filled = used in fix"}</span>
      </div>
    </div>
  );
}
```

`web/src/components/charts/Sparkline.tsx`:
```tsx
import { useId, useState } from "react";

export interface SparkPoint { t: number; v: number | null }

export function Sparkline({ points, format, height = 48, color = "var(--brass)", label }: { points: SparkPoint[]; format: (v: number) => string; height?: number; color?: string; label: string }) {
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  const id = useId();
  const valid = points.filter((p) => p.v != null) as { t: number; v: number }[];
  if (valid.length < 2) return <p className="text-[12px] text-ink-3">{label}: not enough data yet</p>;
  const width = 240;
  const min = Math.min(...valid.map((p) => p.v)), max = Math.max(...valid.map((p) => p.v));
  const t0 = points[0].t, t1 = points[points.length - 1].t;
  const x = (t: number) => ((t - t0) / Math.max(1e-9, t1 - t0)) * width;
  const y = (v: number) => height - 4 - ((v - min) / Math.max(1e-9, max - min)) * (height - 8);
  const path = valid.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");
  const hovered = hoverIdx != null ? valid[hoverIdx] : null;
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-baseline justify-between text-[12px] leading-4 text-ink-2">
        <span>{label}</span>
        <span className="num text-ink">{hovered ? format(hovered.v) : `${format(min)} – ${format(max)}`}</span>
      </div>
      <svg role="img" aria-labelledby={id} viewBox={`0 0 ${width} ${height}`} width="100%" height={height} preserveAspectRatio="none"
        onMouseMove={(e) => { const rect = e.currentTarget.getBoundingClientRect(); const fx = ((e.clientX - rect.left) / rect.width) * width; let best = 0; for (let i = 1; i < valid.length; i++) if (Math.abs(x(valid[i].t) - fx) < Math.abs(x(valid[best].t) - fx)) best = i; setHoverIdx(best); }}
        onMouseLeave={() => setHoverIdx(null)}>
        <title id={id}>{label} over time</title>
        <path d={path} fill="none" stroke={color} strokeWidth={2} vectorEffect="non-scaling-stroke" />
        {hovered ? <line x1={x(hovered.t)} x2={x(hovered.t)} y1={0} y2={height} stroke="var(--ink-3)" strokeDasharray="2 2" /> : null}
      </svg>
    </div>
  );
}
```

- [ ] **Step 6: Write `web/src/components/MapPanel.tsx`**

```tsx
import maplibregl, { type Map as MlMap, type StyleSpecification } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import type { NtripClient } from "@/lib/types";

const OSM: StyleSpecification = {
  version: 8,
  sources: { osm: { type: "raster", tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"], tileSize: 256, attribution: "© OpenStreetMap contributors" } },
  layers: [{ id: "osm", type: "raster", source: "osm" }],
};
const IMAGERY: StyleSpecification = {
  version: 8,
  sources: { esri: { type: "raster", tiles: ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"], tileSize: 256, attribution: "Esri World Imagery" } },
  layers: [{ id: "esri", type: "raster", source: "esri" }],
};

function circlePolygon(lat: number, lon: number, radiusM: number): GeoJSON.Feature<GeoJSON.Polygon> {
  const pts: [number, number][] = [];
  const dLat = radiusM / 111_320;
  const dLon = radiusM / (111_320 * Math.cos((lat * Math.PI) / 180));
  for (let i = 0; i <= 64; i++) {
    const a = (i / 64) * 2 * Math.PI;
    pts.push([lon + dLon * Math.cos(a), lat + dLat * Math.sin(a)]);
  }
  return { type: "Feature", properties: {}, geometry: { type: "Polygon", coordinates: [pts] } };
}

export function MapPanel({ lat, lon, hAcc, rovers = [], height = 320 }: { lat: number | null; lon: number | null; hAcc: number | null; rovers?: NtripClient[]; height?: number }) {
  const container = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MlMap | null>(null);
  const baseMarker = useRef<maplibregl.Marker | null>(null);
  const roverMarkers = useRef<Map<number, maplibregl.Marker>>(new Map());
  const [imagery, setImagery] = useState(false);
  const [offline, setOffline] = useState(false);

  useEffect(() => {
    if (!container.current || mapRef.current) return;
    const map = new maplibregl.Map({ container: container.current, style: OSM, center: [lon ?? 0, lat ?? 0], zoom: lat != null ? 16 : 1, attributionControl: false });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    map.on("error", (e: { error?: { status?: number } }) => { if (e?.error && "status" in e.error) setOffline(true); });
    map.on("sourcedata", () => setOffline(false));
    mapRef.current = map;
    return () => { map.remove(); mapRef.current = null; };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    map.setStyle(imagery ? IMAGERY : OSM);
  }, [imagery]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || lat == null || lon == null) return;
    if (!baseMarker.current) {
      const el = document.createElement("div");
      el.className = "size-3 rounded-full border-2 border-[var(--bg)]";
      el.style.background = "var(--brass)";
      baseMarker.current = new maplibregl.Marker({ element: el }).setLngLat([lon, lat]).addTo(map);
      map.easeTo({ center: [lon, lat], zoom: 17, duration: 0 });
    } else {
      baseMarker.current.setLngLat([lon, lat]);
    }
    const draw = () => {
      const data = circlePolygon(lat, lon, Math.max(hAcc ?? 0, 0.05));
      const src = map.getSource("acc") as maplibregl.GeoJSONSource | undefined;
      if (src) src.setData(data);
      else {
        map.addSource("acc", { type: "geojson", data });
        map.addLayer({ id: "acc-fill", type: "fill", source: "acc", paint: { "fill-color": "#e0b25a", "fill-opacity": 0.15 } });
        map.addLayer({ id: "acc-line", type: "line", source: "acc", paint: { "line-color": "#e0b25a", "line-width": 1.5 } });
      }
    };
    if (map.isStyleLoaded()) draw(); else map.once("styledata", draw);
  }, [lat, lon, hAcc, imagery]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const seen = new Set<number>();
    for (const r of rovers) {
      if (r.last_gga_lat == null || r.last_gga_lon == null) continue;
      seen.add(r.id);
      let m = roverMarkers.current.get(r.id);
      if (!m) {
        const el = document.createElement("div");
        el.className = "size-2.5 rounded-sm";
        el.style.background = "var(--sys-gps)";
        el.title = `${r.user_agent || "rover"} (${r.ip})`;
        m = new maplibregl.Marker({ element: el }).addTo(map);
        roverMarkers.current.set(r.id, m);
      }
      m.setLngLat([r.last_gga_lon, r.last_gga_lat]);
    }
    for (const [id, m] of roverMarkers.current) if (!seen.has(id)) { m.remove(); roverMarkers.current.delete(id); }
  }, [rovers]);

  return (
    <div className="relative" style={{ height }}>
      <div ref={container} className="h-full w-full rounded-md" data-testid="map" />
      <div className="absolute left-2 top-2 flex gap-1">
        <Button size="sm" variant={imagery ? "outline" : "default"} onClick={() => setImagery(false)}>Map</Button>
        <Button size="sm" variant={imagery ? "default" : "outline"} onClick={() => setImagery(true)}>Imagery</Button>
      </div>
      {offline ? <div className="pointer-events-none absolute inset-x-2 bottom-2 rounded-md bg-panel/90 px-3 py-1.5 text-[12px] text-ink-2">Map tiles unavailable (offline). Position and accuracy still update.</div> : null}
      {lat == null ? <div className="pointer-events-none absolute inset-0 flex items-center justify-center text-ink-2">Waiting for a position fix</div> : null}
    </div>
  );
}
```

- [ ] **Step 7: Write `CoordinateReadout`, `SystemChips` and the `Dashboard` page**

`web/src/components/CoordinateReadout.tsx`:
```tsx
import { COORD_MODES, fmtAcc, fmtCoord, fmtMeters, type CoordMode } from "@/lib/format";
import { usePref } from "@/lib/prefs";
import type { Accuracy, Position } from "@/lib/types";

export function CoordinateReadout({ position, accuracy }: { position: Position; accuracy: Accuracy }) {
  const [mode, setMode] = usePref<CoordMode>("coordMode", "dms");
  const [a, b] = fmtCoord(position.lat, position.lon, mode, position.height_m ?? 0);
  return (
    <div data-testid="coordinate-readout" className="flex h-full flex-col justify-between gap-4">
      <div>
        <label className="text-[12px] leading-4 text-ink-2">
          Coordinate format
          <select aria-label="Coordinate format" value={mode} onChange={(e) => setMode(e.target.value as CoordMode)} className="ml-2 rounded-md border border-line bg-panel-2 px-2 py-0.5 text-[12px] text-ink">
            {COORD_MODES.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
          </select>
        </label>
        <div className="display num mt-3 text-[40px] leading-[44px] max-sm:text-[28px] max-sm:leading-[34px]">
          <div>{a}</div>
          <div>{b}</div>
        </div>
      </div>
      <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-[14px]">
        <div><dt className="text-ink-2">Height (ellipsoid)</dt><dd className="num">{fmtMeters(position.height_m, 2)}</dd></div>
        <div><dt className="text-ink-2">Height (MSL)</dt><dd className="num">{fmtMeters(position.hmsl_m, 2)}</dd></div>
        <div><dt className="text-ink-2">Horizontal ±</dt><dd className="num">{fmtAcc(accuracy.h_acc_m)}</dd></div>
        <div><dt className="text-ink-2">Vertical ±</dt><dd className="num">{fmtAcc(accuracy.v_acc_m)}</dd></div>
      </dl>
    </div>
  );
}
```

`web/src/components/SystemChips.tsx`:
```tsx
import { SYSTEM_ORDER, systemColor } from "@/lib/palette";
import type { SatSummary } from "@/lib/types";

export function SystemChips({ summary }: { summary: SatSummary }) {
  const names = [...SYSTEM_ORDER.filter((n) => summary.per_gnss[n]), ...Object.keys(summary.per_gnss).filter((n) => !(SYSTEM_ORDER as readonly string[]).includes(n))];
  if (names.length === 0) return <p className="text-ink-2">No satellites yet.</p>;
  return (
    <ul className="grid grid-cols-2 gap-2 sm:grid-cols-3">
      {names.map((name) => {
        const s = summary.per_gnss[name];
        return (
          <li key={name} className="flex items-center gap-2 rounded-md border border-line px-3 py-2">
            <span className="inline-block size-2.5 rounded-sm" style={{ background: systemColor(name) }} aria-hidden />
            <span className="flex-1">{name}</span>
            <span className="num">{s.used}<span className="text-ink-2">/{s.tracked}</span></span>
          </li>
        );
      })}
    </ul>
  );
}
```

`web/src/pages/Dashboard.tsx`:
```tsx
import { useState } from "react";
import { Link } from "react-router";
import { PageHeader } from "@/app/PageHeader";
import { Panel } from "@/components/Panel";
import { Stat } from "@/components/Stat";
import { EmptyState } from "@/components/EmptyState";
import { CoordinateReadout } from "@/components/CoordinateReadout";
import { SystemChips } from "@/components/SystemChips";
import { MapPanel } from "@/components/MapPanel";
import { SkyPlot } from "@/components/charts/SkyPlot";
import { Sparkline } from "@/components/charts/Sparkline";
import { useLive } from "@/lib/live";
import { useHistory } from "@/lib/queries";
import { fmtAcc, fmtDuration, fmtRate } from "@/lib/format";

function lastHourRange(): [string, string] {
  const to = new Date();
  const from = new Date(to.getTime() - 3600_000);
  return [from.toISOString(), to.toISOString()];
}

export default function Dashboard() {
  const { state, ntripClients, base } = useLive();
  const [[from, to]] = useState(lastHourRange); // stable for this mount: a new range every render would refetch forever
  const history = useHistory(["h_acc_m", "nsat_used", "cno_mean"], from, to, "1s");
  if (!state) {
    return (
      <>
        <PageHeader title="Dashboard" />
        <EmptyState title="Waiting for the receiver" body="The daemon has not reported a state yet. Check the Receiver page if this persists." />
      </>
    );
  }
  const svin = state.survey_in;
  const col = (name: string) => history.data ? history.data.rows.map((r) => ({ t: r[0] as number, v: r[history.data!.columns.indexOf(name)] })) : [];
  return (
    <>
      <PageHeader title="Dashboard" />
      <div className="grid grid-cols-12 gap-4">
        <Panel className="col-span-12 lg:col-span-4" bodyClassName="h-full"><CoordinateReadout position={state.position} accuracy={state.accuracy} /></Panel>
        <Panel className="col-span-12 md:col-span-6 lg:col-span-4" title="Sky"><SkyPlot sats={state.sats} /></Panel>
        <Panel className="col-span-12 md:col-span-6 lg:col-span-4" title="Map" bodyClassName="p-0"><MapPanel lat={state.position.lat} lon={state.position.lon} hAcc={state.accuracy.h_acc_m} rovers={ntripClients} /></Panel>

        <Panel className="col-span-12 md:col-span-6 lg:col-span-3" title="Satellites by system" actions={<Link to="/satellites" className="text-[12px] text-brass">Details</Link>}><SystemChips summary={state.sat_summary} /></Panel>
        <Panel className="col-span-12 md:col-span-6 lg:col-span-3" title="Position mode" actions={<Link to="/site" className="text-[12px] text-brass">Manage</Link>}>
          {svin.active && !svin.valid ? (
            <>
              <p className="mb-2">Survey-in running</p>
              <Stat label="Elapsed" value={fmtDuration(svin.dur_s)} />
              <Stat label="Observations" value={String(svin.obs)} />
              <Stat label="Mean 3D accuracy" value={fmtAcc(svin.mean_acc_m)} level="warning" />
            </>
          ) : svin.valid ? (
            <>
              <p className="mb-2">Survey-in complete</p>
              <Stat label="Mean 3D accuracy" value={fmtAcc(svin.mean_acc_m)} level="good" />
              <Stat label="Duration" value={fmtDuration(svin.dur_s)} />
            </>
          ) : base.mode === "fixed" ? (
            <>
              <p className="mb-2">Fixed site {base.site ?? ""}</p>
              <Stat label="RTCM 1005 check" value={base.verified == null ? "pending" : base.verified ? "verified" : "mismatch"} level={base.verified == null ? "warning" : base.verified ? "good" : "critical"} />
            </>
          ) : (
            <p className="text-ink-2">Position mode not active. Start a survey-in or activate a site.</p>
          )}
        </Panel>
        <Panel className="col-span-12 md:col-span-6 lg:col-span-3" title="Corrections" actions={<Link to="/corrections" className="text-[12px] text-brass">Details</Link>}>
          <Stat label="RTCM out" value={fmtRate(state.rtcm_out.bytes_per_s)} level={state.rtcm_out.bytes_per_s > 0 ? "good" : "serious"} />
          <Stat label="Message types" value={String(Object.keys(state.rtcm_out.messages).length)} />
          <Stat label="Rovers connected" value={String(ntripClients.length)} />
        </Panel>
        <Panel className="col-span-12 md:col-span-6 lg:col-span-3" title="Last hour">
          {history.isLoading ? <p className="text-ink-3">Loading…</p> : history.isError ? <p className="text-ink-3">History unavailable</p> : (
            <div className="flex flex-col gap-3">
              <Sparkline label="Horizontal accuracy" points={col("h_acc_m")} format={(v) => fmtAcc(v)} />
              <Sparkline label="Satellites used" points={col("nsat_used")} format={(v) => v.toFixed(0)} color="var(--sys-gps)" />
              <Sparkline label="Mean C/N0" points={col("cno_mean")} format={(v) => `${v.toFixed(0)} dB-Hz`} color="var(--sys-galileo)" />
            </div>
          )}
        </Panel>
      </div>
    </>
  );
}
```

Register the route in `router.tsx`: `import Dashboard from "@/pages/Dashboard";` and `{ index: true, element: <Dashboard /> }`.

- [ ] **Step 8: Run, build, commit**

Run: `cd web && pnpm test && pnpm lint && pnpm build`
Expected: all pass. Then a visual check: `NTRIP_PASSWORD= WEB_BIND=lan WEB_ALLOW_INSECURE=1 uv run mtrtk replay tests/fixtures/f9p_hpg113_base_30s.ubx --loop` in one terminal, `pnpm --dir web dev` in another, open `http://localhost:5173/` — coordinates in the display serif, sky plot with brass rings and coloured discs, map centred on the base, chips, survey-in card, sparklines. Take a screenshot for the report if the Playwright MCP is available.

```bash
cd /home/nekosaif/github/mtrtk
git add web
git commit -m "feat(web): dashboard with coordinate hero, sky plot, map, system chips and hourly sparklines

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Satellites page — signal bars and sortable table

**Files:**
- Create: `web/src/components/DataTable.tsx`, `web/src/components/charts/CnoBars.tsx`, `web/src/pages/Satellites.tsx`, `web/src/components/charts/CnoBars.test.tsx`, `web/src/pages/Satellites.test.tsx`
- Modify: `web/src/app/router.tsx`

**Interfaces:**
- Produces: `DataTable<T>({columns, rows, rowKey, initialSort?, dense?})` where `columns: {key, header, cell(row), sortValue?(row), align?}`; header click toggles sort (aria-sort set); `CnoBars({sats, height=180})` — one bar per signal, grouped per satellite, satellites ordered by system (fixed order) then SV id, bar fill = system color (second and later signals at 55 % opacity with a 2 px surface gap), 20 dB-Hz and 40 dB-Hz reference lines, hover tooltip, legend, `role="img"` label; `Satellites` page with tabs `Sky | Signals | Table` and system filter chips (persisted pref `satSystems`).

- [ ] **Step 1: Write the failing tests**

`web/src/components/charts/CnoBars.test.tsx`:
```tsx
import { render, screen } from "@testing-library/react";
import { sampleState } from "@/test/fixtures";
import { CnoBars } from "./CnoBars";

describe("CnoBars", () => {
  it("draws one bar per signal, grouped by system order", () => {
    render(<CnoBars sats={sampleState().sats} />);
    const img = screen.getByRole("img", { name: /signal strength/i });
    const bars = img.querySelectorAll("rect[data-signal]");
    expect(bars).toHaveLength(8); // one L1 signal per fixture satellite
    const order = Array.from(bars).map((b) => b.getAttribute("data-signal")!.split(":")[0]);
    expect(order.slice(0, 3)).toEqual(["GPS", "GPS", "GPS"]);
    expect(order[3]).toBe("GLONASS");
  });

  it("scales height by C/N0", () => {
    const sats = sampleState().sats;
    render(<CnoBars sats={sats} height={200} />);
    const strong = screen.getByRole("img").querySelector('rect[data-signal="GPS:5:L1C/A"]')!;
    const weak = screen.getByRole("img").querySelector('rect[data-signal="GPS:25:L1C/A"]')!;
    expect(Number(strong.getAttribute("height"))).toBeGreaterThan(Number(weak.getAttribute("height")));
  });
});
```

`web/src/pages/Satellites.test.tsx`:
```tsx
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { useLive } from "@/lib/live";
import { sampleState } from "@/test/fixtures";
import Satellites from "./Satellites";

describe("Satellites page", () => {
  beforeEach(() => useLive.setState({ state: sampleState(), status: "open", lastEpochAt: Date.now() }));

  it("lists satellites in the table tab and sorts by C/N0", async () => {
    render(<MemoryRouter><Satellites /></MemoryRouter>);
    await userEvent.click(screen.getByRole("tab", { name: "Table" }));
    const table = screen.getByRole("table");
    expect(within(table).getAllByRole("row")).toHaveLength(9); // header + 8
    await userEvent.click(within(table).getByRole("columnheader", { name: /C\/N0/ }));
    const firstCells = within(within(table).getAllByRole("row")[1]).getAllByRole("cell");
    expect(firstCells[0]).toHaveTextContent("GPS");
    expect(firstCells[1]).toHaveTextContent("5"); // 45 dB-Hz is the strongest
  });

  it("filters by system", async () => {
    render(<MemoryRouter><Satellites /></MemoryRouter>);
    await userEvent.click(screen.getByRole("tab", { name: "Table" }));
    await userEvent.click(screen.getByRole("button", { name: /GPS/ }));
    expect(within(screen.getByRole("table")).getAllByRole("row")).toHaveLength(6); // header + 5 non-GPS
  });
});
```

- [ ] **Step 2: Run to verify failure** — `cd web && pnpm test` → FAIL (modules missing).

- [ ] **Step 3: Write `web/src/components/DataTable.tsx`**

```tsx
import { useMemo, useState, type ReactNode } from "react";
import { ArrowDown, ArrowUp } from "lucide-react";
import { cn } from "@/lib/utils";

export interface Column<T> { key: string; header: string; cell: (row: T) => ReactNode; sortValue?: (row: T) => number | string | null; align?: "left" | "right"; width?: string }

export function DataTable<T>({ columns, rows, rowKey, initialSort, dense, emptyText = "Nothing to show" }: { columns: Column<T>[]; rows: T[]; rowKey: (row: T) => string; initialSort?: { key: string; dir: "asc" | "desc" }; dense?: boolean; emptyText?: string }) {
  const [sort, setSort] = useState(initialSort ?? null);
  const sorted = useMemo(() => {
    if (!sort) return rows;
    const col = columns.find((c) => c.key === sort.key);
    if (!col?.sortValue) return rows;
    const sv = col.sortValue;
    return [...rows].sort((a, b) => {
      const va = sv(a), vb = sv(b);
      if (va == null) return 1;
      if (vb == null) return -1;
      const cmp = typeof va === "number" && typeof vb === "number" ? va - vb : String(va).localeCompare(String(vb));
      return sort.dir === "asc" ? cmp : -cmp;
    });
  }, [rows, sort, columns]);
  const toggle = (key: string) => setSort((s) => (s?.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: "desc" }));
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[14px]">
        <thead>
          <tr className="border-b border-line text-left text-ink-2">
            {columns.map((c) => {
              const active = sort?.key === c.key;
              return (
                <th key={c.key} scope="col" style={{ width: c.width }} aria-sort={active ? (sort!.dir === "asc" ? "ascending" : "descending") : "none"} className={cn("py-1.5 pr-3 font-medium", c.align === "right" && "text-right")}>
                  {c.sortValue ? (
                    <button type="button" onClick={() => toggle(c.key)} className={cn("inline-flex items-center gap-1 hover:text-ink", active && "text-ink")}>
                      {c.header}{active ? (sort!.dir === "asc" ? <ArrowUp className="size-3" aria-hidden /> : <ArrowDown className="size-3" aria-hidden />) : null}
                    </button>
                  ) : c.header}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {sorted.length === 0 ? <tr><td colSpan={columns.length} className="py-6 text-center text-ink-3">{emptyText}</td></tr> : sorted.map((row) => (
            <tr key={rowKey(row)} className="border-b border-line/60 last:border-0 hover:bg-panel-2/60">
              {columns.map((c) => <td key={c.key} className={cn("pr-3", dense ? "py-1" : "py-1.5", c.align === "right" && "num text-right")}>{c.cell(row)}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
```

- [ ] **Step 4: Write `web/src/components/charts/CnoBars.tsx`**

```tsx
import { useState } from "react";
import type { Satellite } from "@/lib/types";
import { SYSTEM_ORDER, systemColor } from "@/lib/palette";
import { Legend } from "./Legend";

interface Bar { key: string; system: string; sv: number; signal: string; cno: number; used: boolean; idx: number }

function systemRank(name: string): number {
  const i = (SYSTEM_ORDER as readonly string[]).indexOf(name);
  return i === -1 ? 99 : i;
}

export function CnoBars({ sats, height = 180 }: { sats: Satellite[]; height?: number }) {
  const [hover, setHover] = useState<Bar | null>(null);
  const ordered = [...sats].sort((a, b) => systemRank(a.gnss) - systemRank(b.gnss) || a.sv_id - b.sv_id);
  const bars: Bar[] = [];
  for (const s of ordered) {
    const signals = s.signals.length ? s.signals : [{ name: "L1", cno: s.cno, pr_used: s.used }];
    signals.forEach((sig, idx) => bars.push({ key: `${s.gnss}:${s.sv_id}:${sig.name}`, system: s.gnss, sv: s.sv_id, signal: sig.name, cno: sig.cno, used: "pr_used" in sig ? Boolean(sig.pr_used) : s.used, idx }));
  }
  if (bars.length === 0) return <p className="text-ink-2">No signals tracked yet.</p>;
  const width = Math.max(320, bars.length * 14 + 40);
  const left = 28, bottom = 22, top = 6;
  const plotH = height - top - bottom;
  const barW = (width - left - 8) / bars.length;
  const y = (cno: number) => top + plotH - (Math.min(55, Math.max(0, cno)) / 55) * plotH;
  const legend = SYSTEM_ORDER.filter((n) => bars.some((b) => b.system === n)).map((n) => ({ label: n, color: systemColor(n) }));
  return (
    <div>
      <svg role="img" aria-label={`Signal strength: ${bars.length} signals from ${sats.length} satellites`} viewBox={`0 0 ${width} ${height}`} width="100%" style={{ maxWidth: width }} onMouseLeave={() => setHover(null)}>
        {[20, 40].map((ref) => (
          <g key={ref}>
            <line x1={left} x2={width - 4} y1={y(ref)} y2={y(ref)} stroke="var(--line)" strokeDasharray="3 3" />
            <text x={left - 4} y={y(ref) + 3} textAnchor="end" fontSize={10} fill="var(--ink-3)" className="num">{ref}</text>
          </g>
        ))}
        <line x1={left} x2={width - 4} y1={top + plotH} y2={top + plotH} stroke="var(--line)" />
        {bars.map((b, i) => {
          const h = top + plotH - y(b.cno);
          const x = left + i * barW + 1;
          return (
            <rect key={b.key} data-signal={b.key} x={x} width={Math.max(2, barW - 2)} y={y(b.cno)} height={Math.max(0, h)} rx={2}
              fill={systemColor(b.system)} fillOpacity={b.idx === 0 ? 1 : 0.55} stroke={hover?.key === b.key ? "var(--ink)" : "none"}
              style={{ transition: "y 0.4s ease, height 0.4s ease" }} onMouseEnter={() => setHover(b)}>
              <title>{`${b.system} ${b.sv} ${b.signal}: ${b.cno} dB-Hz${b.used ? " (used)" : ""}`}</title>
            </rect>
          );
        })}
        {ordered.map((s, i) => {
          const first = bars.findIndex((b) => b.system === s.gnss && b.sv === s.sv_id);
          const count = s.signals.length || 1;
          return i % Math.ceil(ordered.length / 24) === 0 ? <text key={`${s.gnss}-${s.sv_id}`} x={left + (first + count / 2) * barW} y={height - 8} textAnchor="middle" fontSize={10} fill="var(--ink-2)" className="num">{s.sv_id}</text> : null;
        })}
      </svg>
      <div className="mt-2 flex items-start justify-between gap-3">
        <Legend items={legend} />
        <span className="num text-[12px] leading-4 text-ink-2">{hover ? `${hover.system} ${hover.sv} ${hover.signal} · ${hover.cno} dB-Hz${hover.used ? " · used" : ""}` : "dB-Hz · dashed lines at 20 and 40"}</span>
      </div>
    </div>
  );
}
```

- [ ] **Step 5: Write `web/src/pages/Satellites.tsx`**

```tsx
import { useMemo } from "react";
import { Check } from "lucide-react";
import { PageHeader } from "@/app/PageHeader";
import { Panel } from "@/components/Panel";
import { EmptyState } from "@/components/EmptyState";
import { DataTable, type Column } from "@/components/DataTable";
import { SkyPlot } from "@/components/charts/SkyPlot";
import { CnoBars } from "@/components/charts/CnoBars";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useLive } from "@/lib/live";
import { SYSTEM_ORDER, systemColor } from "@/lib/palette";
import { usePref } from "@/lib/prefs";
import type { Satellite } from "@/lib/types";
import { cn } from "@/lib/utils";

const QUALITY = ["No signal", "Searching", "Acquired", "Unusable", "Code locked", "Code+carrier", "Code+carrier", "Code+carrier"];

const columns: Column<Satellite>[] = [
  { key: "system", header: "System", cell: (s) => <span className="flex items-center gap-2"><span className="inline-block size-2.5 rounded-sm" style={{ background: systemColor(s.gnss) }} aria-hidden />{s.gnss}</span>, sortValue: (s) => `${(SYSTEM_ORDER as readonly string[]).indexOf(s.gnss)}-${s.sv_id}` },
  { key: "sv", header: "SV", cell: (s) => s.sv_id, sortValue: (s) => s.sv_id, align: "right" },
  { key: "signals", header: "Signals", cell: (s) => s.signals.map((g) => `${g.name} ${g.cno}`).join(" · ") || "—" },
  { key: "cno", header: "C/N0", cell: (s) => `${s.cno} dB-Hz`, sortValue: (s) => s.cno, align: "right" },
  { key: "elev", header: "Elev", cell: (s) => (s.elev == null ? "—" : `${s.elev}°`), sortValue: (s) => s.elev, align: "right" },
  { key: "az", header: "Az", cell: (s) => (s.azim == null ? "—" : `${s.azim}°`), sortValue: (s) => s.azim, align: "right" },
  { key: "used", header: "Used", cell: (s) => (s.used ? <Check className="size-4 text-status-good" aria-label="used" /> : <span className="text-ink-3">no</span>), sortValue: (s) => Number(s.used) },
  { key: "health", header: "Health", cell: (s) => (s.health === 1 ? "healthy" : s.health === 2 ? <span className="text-status-critical">unhealthy</span> : "unknown"), sortValue: (s) => s.health },
  { key: "quality", header: "Quality", cell: (s) => QUALITY[s.quality_ind] ?? s.quality_ind, sortValue: (s) => s.quality_ind },
  { key: "res", header: "PR residual", cell: (s) => `${s.pr_res_m.toFixed(1)} m`, sortValue: (s) => Math.abs(s.pr_res_m), align: "right" },
  { key: "eph", header: "Eph / Alm", cell: (s) => `${s.eph_avail ? "E" : "–"} ${s.alm_avail ? "A" : "–"}` },
];

export default function Satellites() {
  const state = useLive((s) => s.state);
  const [hidden, setHidden] = usePref<string[]>("satSystemsHidden", []);
  const systems = useMemo(() => (state ? Object.keys(state.sat_summary.per_gnss).sort((a, b) => (SYSTEM_ORDER as readonly string[]).indexOf(a) - (SYSTEM_ORDER as readonly string[]).indexOf(b)) : []), [state]);
  if (!state) return <><PageHeader title="Satellites" /><EmptyState title="Waiting for the receiver" /></>;
  const visible = state.sats.filter((s) => !hidden.includes(s.gnss));
  const toggle = (name: string) => setHidden(hidden.includes(name) ? hidden.filter((h) => h !== name) : [...hidden, name]);
  return (
    <>
      <PageHeader title="Satellites">
        <span className="num text-ink-2">{state.sat_summary.used} used of {state.sat_summary.tracked} tracked</span>
      </PageHeader>
      <div className="mb-3 flex flex-wrap gap-2">
        {systems.map((name) => {
          const on = !hidden.includes(name);
          const s = state.sat_summary.per_gnss[name];
          return (
            <button key={name} type="button" aria-pressed={on} onClick={() => toggle(name)} className={cn("flex items-center gap-2 rounded-full border px-3 py-1 text-[14px]", on ? "border-line bg-panel" : "border-line/50 text-ink-3")}>
              <span className="inline-block size-2.5 rounded-sm" style={{ background: on ? systemColor(name) : "var(--ink-3)" }} aria-hidden />
              {name} <span className="num text-ink-2">{s.used}/{s.tracked}</span>
            </button>
          );
        })}
      </div>
      <Tabs defaultValue="sky">
        <TabsList><TabsTrigger value="sky">Sky</TabsTrigger><TabsTrigger value="signals">Signals</TabsTrigger><TabsTrigger value="table">Table</TabsTrigger></TabsList>
        <TabsContent value="sky"><Panel><SkyPlot sats={visible} size={480} /></Panel></TabsContent>
        <TabsContent value="signals"><Panel><CnoBars sats={visible} height={220} /></Panel></TabsContent>
        <TabsContent value="table"><Panel bodyClassName="p-2"><DataTable columns={columns} rows={visible} rowKey={(s) => `${s.gnss}-${s.sv_id}`} initialSort={{ key: "system", dir: "asc" }} dense /></Panel></TabsContent>
      </Tabs>
    </>
  );
}
```
Register `{ path: "satellites", element: <Satellites /> }` in the router.

- [ ] **Step 6: Run, build, commit**

Run: `cd web && pnpm test && pnpm lint && pnpm build`. If the shadcn `Tabs` renders triggers with `role="tab"` but the test cannot find "Table", check the trigger text; the test uses the accessible name.

```bash
cd /home/nekosaif/github/mtrtk && git add web && git commit -m "feat(web): satellites page with system filters, signal bars and sortable table

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Receiver page — RF health, spectrum, firmware, time, actions

**Files:**
- Create: `web/src/components/charts/Spectrum.tsx`, `web/src/components/charts/Gauge.tsx`, `web/src/components/ConfirmDialog.tsx`, `web/src/pages/Receiver.tsx`, `web/src/pages/Receiver.test.tsx`
- Modify: `web/src/app/router.tsx`

**Interfaces:**
- Produces: `Gauge({value, max, label, level})` (horizontal bar with value text — for jamming indicator 0–255 and AGC 0–8191); `Spectrum({spectra})` — one SVG polyline per RF block (block 0 = brass, block 1 = GPS blue), x axis in MHz from `center ± span/2`, y = bin amplitude (0–255), crosshair tooltip showing MHz and level, legend; `ConfirmDialog({trigger, title, body, confirmLabel, destructive?, onConfirm, requireText?})`; `Receiver` page with panels: RF blocks, Antenna & hardware, Spectrum (or "not supported by this firmware" when `capabilities.unsupported` includes `MON-SPAN`), Firmware & capabilities, Ports, Time, Actions (Re-apply profile · Reset… · Poll a message…).

- [ ] **Step 1: Write the failing test**

`web/src/pages/Receiver.test.tsx`:
```tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { useLive } from "@/lib/live";
import { sampleState } from "@/test/fixtures";
import Receiver from "./Receiver";

const receiverInfo = { connected: true, passive: false, source: "auto", capabilities: { protver: "27.12", fw_version: "HPG 1.13", module: "ZED-F9P", supported: ["MON-COMMS"], unsupported: ["MON-SPAN"] }, firmware: sampleState().firmware };

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><MemoryRouter><Receiver /></MemoryRouter></QueryClientProvider>);
}

describe("Receiver page", () => {
  beforeEach(() => {
    useLive.setState({ state: sampleState(), status: "open", lastEpochAt: Date.now(), receiverConnected: true });
    globalThis.fetch = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      const path = typeof url === "string" ? url : url.toString();
      if (path.endsWith("/api/receiver") && !init?.method) return new Response(JSON.stringify(receiverInfo), { status: 200 });
      if (path.endsWith("/api/receiver/reset")) return new Response(JSON.stringify({ ok: true, kind: "warm" }), { status: 200 });
      return new Response("{}", { status: 200 });
    }) as typeof fetch;
  });

  it("shows RF blocks, firmware and the spectrum-unsupported notice", async () => {
    renderPage();
    expect(await screen.findByText("HPG 1.13")).toBeInTheDocument();
    expect(screen.getAllByText(/RF block/)).toHaveLength(2);
    expect(screen.getByText(/jamming indicator/i)).toBeInTheDocument();
    expect(await screen.findByText(/spectrum .*not supported by this firmware/i)).toBeInTheDocument();
    expect(screen.getByText(/GPS week/)).toBeInTheDocument();
    expect(screen.getByText("2436")).toBeInTheDocument();
  });

  it("reset requires confirmation and posts the kind", async () => {
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: /reset/i }));
    await userEvent.selectOptions(screen.getByLabelText(/reset type/i), "warm");
    await userEvent.click(screen.getByRole("button", { name: /confirm reset/i }));
    const calls = (globalThis.fetch as unknown as { mock: { calls: [string, RequestInit][] } }).mock.calls;
    const reset = calls.find(([u]) => String(u).endsWith("/api/receiver/reset"))!;
    expect(JSON.parse(reset[1].body as string)).toEqual({ kind: "warm" });
  });
});
```

- [ ] **Step 2: Run to verify failure** — FAIL (modules missing).

- [ ] **Step 3: Write `Gauge`, `Spectrum`, `ConfirmDialog`**

`web/src/components/charts/Gauge.tsx`:
```tsx
import { STATUS, type StatusLevel } from "@/lib/palette";

export function Gauge({ value, max, label, level, format = (v: number) => String(v) }: { value: number; max: number; label: string; level: StatusLevel; format?: (v: number) => string }) {
  const pct = Math.max(0, Math.min(1, value / max));
  return (
    <div className="flex flex-col gap-1" role="meter" aria-valuemin={0} aria-valuemax={max} aria-valuenow={value} aria-label={label}>
      <div className="flex justify-between text-[12px] leading-4 text-ink-2"><span>{label}</span><span className="num text-ink">{format(value)}</span></div>
      <div className="h-1.5 w-full rounded-full bg-panel-2"><div className="h-full rounded-full" style={{ width: `${pct * 100}%`, background: STATUS[level], transition: "width 0.4s ease" }} /></div>
    </div>
  );
}
```

`web/src/components/charts/Spectrum.tsx`:
```tsx
import { useState } from "react";
import type { Spectrum as SpectrumT } from "@/lib/types";
import { Legend } from "./Legend";

const COLORS = ["var(--brass)", "var(--sys-gps)", "var(--sys-galileo)"];

export function Spectrum({ spectra, height = 200 }: { spectra: SpectrumT[]; height?: number }) {
  const [hover, setHover] = useState<{ mhz: number; values: number[] } | null>(null);
  if (spectra.length === 0) return <p className="text-ink-2">No spectrum data yet.</p>;
  const width = 640, left = 32, bottom = 22, top = 6;
  const plotW = width - left - 8, plotH = height - top - bottom;
  const blocks = spectra.map((s, i) => {
    const start = (s.center_hz - s.span_hz / 2) / 1e6, step = s.span_hz / 1e6 / Math.max(1, s.bins.length - 1);
    const pts = s.bins.map((v, j) => ({ mhz: start + j * step, v }));
    return { s, pts, color: COLORS[i % COLORS.length], path: pts.map((p, j) => `${j ? "L" : "M"}${(left + (j / Math.max(1, pts.length - 1)) * plotW).toFixed(1)},${(top + plotH - (Math.min(255, p.v) / 255) * plotH).toFixed(1)}`).join(" ") };
  });
  const ticks = blocks.flatMap((b) => [b.pts[0].mhz, b.s.center_hz / 1e6, b.pts[b.pts.length - 1].mhz]);
  return (
    <div>
      <svg role="img" aria-label={`RF spectrum, ${spectra.length} block${spectra.length > 1 ? "s" : ""}`} viewBox={`0 0 ${width} ${height}`} width="100%"
        onMouseMove={(e) => { const rect = e.currentTarget.getBoundingClientRect(); const frac = Math.max(0, Math.min(1, ((e.clientX - rect.left) / rect.width * width - left) / plotW)); const idx = Math.round(frac * (blocks[0].pts.length - 1)); setHover({ mhz: blocks[0].pts[idx]?.mhz ?? 0, values: blocks.map((b) => b.pts[idx]?.v ?? 0) }); }}
        onMouseLeave={() => setHover(null)}>
        {[64, 128, 192].map((lvl) => <line key={lvl} x1={left} x2={width - 8} y1={top + plotH - (lvl / 255) * plotH} y2={top + plotH - (lvl / 255) * plotH} stroke="var(--line)" strokeDasharray="3 3" />)}
        {blocks.map((b) => <path key={b.s.block_id} d={b.path} fill="none" stroke={b.color} strokeWidth={1.5} />)}
        {ticks.map((mhz, i) => <text key={i} x={left + ((i % 3) / 2) * plotW} y={height - 6} textAnchor={i % 3 === 0 ? "start" : i % 3 === 2 ? "end" : "middle"} fontSize={10} fill="var(--ink-3)" className="num">{mhz.toFixed(0)} MHz</text>).slice(0, 3)}
        {hover ? <line x1={left + ((hover.mhz - blocks[0].pts[0].mhz) / (blocks[0].pts[blocks[0].pts.length - 1].mhz - blocks[0].pts[0].mhz)) * plotW} x2={left + ((hover.mhz - blocks[0].pts[0].mhz) / (blocks[0].pts[blocks[0].pts.length - 1].mhz - blocks[0].pts[0].mhz)) * plotW} y1={top} y2={top + plotH} stroke="var(--ink-3)" strokeDasharray="2 2" /> : null}
      </svg>
      <div className="mt-2 flex items-start justify-between gap-3">
        <Legend items={blocks.map((b) => ({ label: `RF block ${b.s.block_id} · centre ${(b.s.center_hz / 1e6).toFixed(1)} MHz · PGA ${b.s.pga_db} dB`, color: b.color }))} />
        <span className="num text-[12px] leading-4 text-ink-2">{hover ? `${hover.mhz.toFixed(2)} MHz · ${hover.values.join(" / ")}` : "amplitude 0–255 per bin"}</span>
      </div>
    </div>
  );
}
```
`web/src/components/ConfirmDialog.tsx`:
```tsx
import { useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";

export function ConfirmDialog({ trigger, title, body, confirmLabel, destructive, onConfirm, requireText, children }: { trigger: ReactNode; title: string; body?: ReactNode; confirmLabel: string; destructive?: boolean; onConfirm: () => Promise<void> | void; requireText?: string; children?: ReactNode }) {
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const ok = !requireText || typed === requireText;
  return (
    <Dialog open={open} onOpenChange={(o) => { setOpen(o); setTyped(""); }}>
      <DialogTrigger asChild>{trigger}</DialogTrigger>
      <DialogContent>
        <DialogHeader><DialogTitle>{title}</DialogTitle>{body ? <DialogDescription>{body}</DialogDescription> : null}</DialogHeader>
        {children}
        {requireText ? <label className="text-[14px]">Type <span className="num font-medium">{requireText}</span> to continue<Input value={typed} onChange={(e) => setTyped(e.target.value)} className="mt-1" /></label> : null}
        <DialogFooter>
          <Button variant="outline" onClick={() => setOpen(false)}>Cancel</Button>
          <Button variant={destructive ? "destructive" : "default"} disabled={!ok || busy} onClick={async () => { setBusy(true); try { await onConfirm(); setOpen(false); } finally { setBusy(false); } }}>{confirmLabel}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
```

- [ ] **Step 4: Write `web/src/pages/Receiver.tsx`**

```tsx
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { PageHeader } from "@/app/PageHeader";
import { Panel } from "@/components/Panel";
import { Stat } from "@/components/Stat";
import { StatusBadge } from "@/components/StatusBadge";
import { EmptyState } from "@/components/EmptyState";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Gauge } from "@/components/charts/Gauge";
import { Spectrum } from "@/components/charts/Spectrum";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { post } from "@/lib/api";
import { fmtBytes, fmtDuration, fmtUtcDate } from "@/lib/format";
import { useLive } from "@/lib/live";
import { useReceiver } from "@/lib/queries";
import type { StatusLevel } from "@/lib/palette";

const JAM_LEVEL = (state: number): StatusLevel => (state >= 3 ? "critical" : state === 2 ? "warning" : "good");
const ANT_LEVEL = (status: number): StatusLevel => (status === 2 ? "good" : status === 3 || status === 4 ? "critical" : "warning");

export default function Receiver() {
  const state = useLive((s) => s.state);
  const info = useReceiver();
  const qc = useQueryClient();
  const [pollClass, setPollClass] = useState("MON");
  const [pollId, setPollId] = useState("MON-VER");
  const [pollResult, setPollResult] = useState<string | null>(null);
  const [resetKind, setResetKind] = useState<"hot" | "warm" | "cold" | "factory">("hot");
  const reapply = useMutation({ mutationFn: () => post("/api/receiver/reapply"), onSuccess: () => { toast.success("Profile re-applied"); qc.invalidateQueries({ queryKey: ["receiver"] }); }, onError: (e) => toast.error(String(e)) });
  const reset = useMutation({ mutationFn: (kind: string) => post("/api/receiver/reset", { kind }), onSuccess: (_, kind) => toast.success(`${kind} reset sent; the receiver reconnects in a few seconds`), onError: (e) => toast.error(String(e)) });
  const poll = useMutation({ mutationFn: () => post<Record<string, unknown>>("/api/receiver/poll", { msg_class: pollClass, msg_id: pollId }), onSuccess: (data) => setPollResult(JSON.stringify(data, null, 2)), onError: (e) => setPollResult(String(e)) });
  if (!state) return <><PageHeader title="Receiver" /><EmptyState title="Waiting for the receiver" /></>;
  const caps = info.data?.capabilities;
  const spanSupported = !caps || !caps.unsupported.includes("MON-SPAN");
  const hw = state.hardware;
  const t = state.time;
  return (
    <>
      <PageHeader title="Receiver">
        <StatusBadge level={info.data?.connected ? "good" : "critical"} label={info.data?.connected ? `Connected · ${info.data.source}` : "Disconnected"} />
      </PageHeader>
      <div className="grid grid-cols-12 gap-4">
        {state.rf.map((b) => (
          <Panel key={b.block_id} className="col-span-12 md:col-span-6" title={`RF block ${b.block_id}`} actions={<StatusBadge level={JAM_LEVEL(b.jamming_state)} label={`Interference ${b.jamming_state_name.toLowerCase()}`} />}>
            <div className="flex flex-col gap-3">
              <Gauge label="Jamming indicator" value={b.jam_ind} max={255} level={b.jam_ind >= 200 ? "critical" : b.jam_ind >= 100 ? "warning" : "good"} />
              <Gauge label="AGC" value={b.agc_cnt} max={8191} level="good" />
              <Stat label="Noise per ms" value={String(b.noise_per_ms)} />
              <Stat label="Antenna" value={`${b.ant_status_name} · power ${b.ant_power_name.toLowerCase()}`} level={ANT_LEVEL(b.ant_status)} />
              <Stat label="I/Q offset · magnitude" value={`${b.ofs_i}/${b.ofs_q} · ${b.mag_i}/${b.mag_q}`} />
            </div>
          </Panel>
        ))}
        {state.rf.length === 0 && hw ? (
          <Panel className="col-span-12 md:col-span-6" title="Hardware">
            <Gauge label="Jamming indicator" value={hw.jam_ind} max={255} level={hw.jam_ind >= 200 ? "critical" : "good"} />
            <Stat label="Antenna" value={`${hw.ant_status_name} · power ${hw.ant_power_name.toLowerCase()}`} level={ANT_LEVEL(hw.ant_status)} />
            <Stat label="AGC / noise" value={`${hw.agc_cnt} / ${hw.noise_per_ms}`} />
          </Panel>
        ) : null}
        <Panel className="col-span-12" title="Spectrum">
          {spanSupported ? <Spectrum spectra={state.spectrum} /> : <p className="text-ink-2">Spectrum analyser (MON-SPAN) is not supported by this firmware. Upgrade to HPG 1.32 or newer to see it.</p>}
        </Panel>
        <Panel className="col-span-12 md:col-span-4" title="Firmware">
          <Stat label="Module" value={state.firmware.module || caps?.module || "—"} />
          <Stat label="Firmware" value={state.firmware.fw_version || caps?.fw_version || "—"} />
          <Stat label="Protocol" value={state.firmware.protver || caps?.protver || "—"} />
          <Stat label="Core" value={state.firmware.sw_version || "—"} />
          {caps ? <p className="mt-3 text-[12px] leading-4 text-ink-2">Supported: {caps.supported.join(", ") || "—"}<br />Unsupported: {caps.unsupported.join(", ") || "—"}</p> : null}
        </Panel>
        <Panel className="col-span-12 md:col-span-4" title="Time">
          <Stat label="UTC" value={fmtUtcDate(t.utc)} />
          <Stat label="GPS week" value={t.gps_week == null ? "—" : String(t.gps_week)} />
          <Stat label="Time of week" value={t.gps_tow_s == null ? "—" : `${t.gps_tow_s.toFixed(3)} s`} />
          <Stat label="Leap seconds" value={t.leap_s == null ? "—" : `${t.leap_s} s`} />
          <Stat label="Clock bias · drift" value={`${t.clk_bias_ns ?? "—"} ns · ${t.clk_drift_nsps ?? "—"} ns/s`} />
          <Stat label="Time accuracy" value={t.t_acc_ns == null ? "—" : `${t.t_acc_ns} ns`} />
          <Stat label="Receiver uptime" value={state.fix.uptime_ms == null ? "—" : fmtDuration(state.fix.uptime_ms / 1000)} />
        </Panel>
        <Panel className="col-span-12 md:col-span-4" title="Ports">
          {state.ports.length === 0 ? <p className="text-ink-2">Port statistics (MON-COMMS) not available.</p> : state.ports.map((p) => (
            <div key={p.port_id} className="mb-2">
              <p className="num text-ink-2">Port 0x{p.port_id.toString(16).padStart(4, "0")}</p>
              <Stat label="TX" value={`${fmtBytes(p.tx_bytes)} · ${p.tx_usage}% (peak ${p.tx_peak_usage}%)`} level={p.tx_peak_usage > 80 ? "warning" : undefined} />
              <Stat label="RX" value={`${fmtBytes(p.rx_bytes)} · ${p.rx_usage}%`} />
              <Stat label="Overruns · skipped" value={`${p.overrun_errs} · ${p.skipped}`} level={p.overrun_errs ? "serious" : undefined} />
            </div>
          ))}
        </Panel>
        <Panel className="col-span-12" title="Actions">
          <div className="flex flex-wrap gap-2">
            <ConfirmDialog trigger={<Button variant="outline">Re-apply profile</Button>} title="Re-apply the receiver profile?" body="Rewrites every configuration key (RAM, BBR and Flash) and verifies the result. Takes a few seconds; corrections pause briefly." confirmLabel="Re-apply" onConfirm={() => reapply.mutateAsync().then(() => undefined)} />
            <ConfirmDialog trigger={<Button variant="outline">Reset…</Button>} title="Reset the receiver" body="Hot keeps everything; warm drops ephemeris; cold drops all satellite data; factory clears every setting and re-applies the profile afterwards." confirmLabel="Confirm reset" destructive={resetKind === "factory"} requireText={resetKind === "factory" ? "factory" : undefined} onConfirm={() => reset.mutateAsync(resetKind).then(() => undefined)}>
              <label className="text-[14px]">Reset type
                <select aria-label="Reset type" value={resetKind} onChange={(e) => setResetKind(e.target.value as typeof resetKind)} className="ml-2 rounded-md border border-line bg-panel-2 px-2 py-1">
                  <option value="hot">hot</option><option value="warm">warm</option><option value="cold">cold</option><option value="factory">factory</option>
                </select>
              </label>
            </ConfirmDialog>
            <Dialog>
              <DialogTrigger asChild><Button variant="outline">Poll a message…</Button></DialogTrigger>
              <DialogContent className="max-w-2xl">
                <DialogHeader><DialogTitle>Poll a UBX message</DialogTitle></DialogHeader>
                <div className="flex gap-2">
                  <Input aria-label="Message class" value={pollClass} onChange={(e) => setPollClass(e.target.value.toUpperCase())} className="w-28" />
                  <Input aria-label="Message id" value={pollId} onChange={(e) => setPollId(e.target.value.toUpperCase())} />
                  <Button onClick={() => poll.mutate()} disabled={poll.isPending}>Poll</Button>
                </div>
                {pollResult ? <pre className="max-h-80 overflow-auto rounded-md bg-panel-2 p-3 font-mono text-[12px]">{pollResult}</pre> : <p className="text-ink-2">Try MON-VER, MON-HW, NAV-STATUS, SEC-UNIQID.</p>}
              </DialogContent>
            </Dialog>
          </div>
        </Panel>
      </div>
    </>
  );
}
```
Register `{ path: "receiver", element: <Receiver /> }`.

- [ ] **Step 5: Run, build, commit**

`cd web && pnpm test && pnpm lint && pnpm build`; then
```bash
cd /home/nekosaif/github/mtrtk && git add web && git commit -m "feat(web): receiver page with RF health gauges, spectrum, firmware, time, ports and actions

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Corrections page — RTCM stream and NTRIP clients

**Files:**
- Create: `web/src/lib/rates.ts`, `web/src/pages/Corrections.tsx`, `web/src/lib/rates.test.ts`, `web/src/pages/Corrections.test.tsx`
- Modify: `web/src/app/router.tsx`

**Interfaces:**
- Produces: `useMessageRates(messages)` hook → `Record<type, {hz, bytesPerS}>` computed from count/byte deltas over a 10 s sliding window; `useRing(value, seconds=300)` → `{t, v}[]` ring buffer sampled once per second (for the bitrate sparkline); `Corrections` page: RTCM table (type, description, count, rate Hz, bytes/s, last seen), bitrate sparkline (5 min), NTRIP panel (connection URL + copy, bind/mountpoint/auth), live clients table, recent connections table.
- RTCM descriptions: 1005 Station ARP, 1074/1077 GPS MSM4/MSM7, 1084/1087 GLONASS MSM4/7, 1094/1097 Galileo MSM4/7, 1124/1127 BeiDou MSM4/7, 1230 GLONASS code-phase biases, 4072 u-blox proprietary.

- [ ] **Step 1: Write the failing tests**

`web/src/lib/rates.test.ts`:
```ts
import { RateTracker } from "./rates";

describe("RateTracker", () => {
  it("computes Hz and bytes/s from count deltas", () => {
    const tr = new RateTracker(10);
    tr.push({ "1077": { count: 100, bytes: 40_000, last_seen_mono: 1 } }, 1000);
    tr.push({ "1077": { count: 105, bytes: 42_000, last_seen_mono: 1 } }, 6000);
    expect(tr.rates()["1077"].hz).toBeCloseTo(1.0, 5);
    expect(tr.rates()["1077"].bytesPerS).toBeCloseTo(400, 5);
  });
  it("drops samples older than the window and handles new types", () => {
    const tr = new RateTracker(10);
    tr.push({ "1005": { count: 1, bytes: 25, last_seen_mono: 1 } }, 0);
    tr.push({ "1005": { count: 2, bytes: 50, last_seen_mono: 1 } }, 5000);
    tr.push({ "1005": { count: 4, bytes: 100, last_seen_mono: 1 }, "1230": { count: 1, bytes: 10, last_seen_mono: 1 } }, 20000);
    expect(tr.rates()["1005"].hz).toBeCloseTo(2 / 15, 5);
    expect(tr.rates()["1230"].hz).toBe(0);
  });
});
```

`web/src/pages/Corrections.test.tsx`:
```tsx
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { useLive } from "@/lib/live";
import { sampleState } from "@/test/fixtures";
import Corrections from "./Corrections";

describe("Corrections page", () => {
  beforeEach(() => {
    useLive.setState({ state: sampleState(), status: "open", lastEpochAt: Date.now(), ntripClients: [{ id: 1, ip: "100.100.50.12", port: 5000, mountpoint: "MTRK", user_agent: "NTRIP SWMaps", username: "rover", version: 2, connected_utc: new Date().toISOString(), bytes_sent: 12345, dropped_frames: 0, last_gga_lat: 23.8, last_gga_lon: 90.2, last_gga_utc: null }] });
    globalThis.fetch = vi.fn(async (url: string | URL | Request) => {
      const p = String(url);
      if (p.endsWith("/api/ntrip")) return new Response(JSON.stringify({ running: true, host: "100.100.50.10", port: 2101, mountpoint: "MTRK", anonymous: false, username: "rover", bind_mode: "tailscale", connection_url: "ntrip://rover:***@100.100.50.10:2101/MTRK", sourcetable: "STR;MTRK;...\r\nENDSOURCETABLE\r\n" }), { status: 200 });
      if (p.includes("/api/ntrip/history")) return new Response(JSON.stringify([{ id: 9, ip: "100.100.50.13", mountpoint: "MTRK", user_agent: "str2str", username: "rover", connected_utc: "2026-09-18T10:00:00+00:00", disconnected_utc: "2026-09-18T10:30:00+00:00", bytes_sent: 555, last_lat: null, last_lon: null, reason: "client closed" }]), { status: 200 });
      return new Response("[]", { status: 200 });
    }) as typeof fetch;
  });

  it("shows RTCM types, the connection string and live clients", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={qc}><MemoryRouter><Corrections /></MemoryRouter></QueryClientProvider>);
    expect(screen.getByText("1077")).toBeInTheDocument();
    expect(screen.getByText(/GPS MSM7/)).toBeInTheDocument();
    expect(await screen.findByText("ntrip://rover:***@100.100.50.10:2101/MTRK")).toBeInTheDocument();
    expect(screen.getByText("NTRIP SWMaps")).toBeInTheDocument();
    expect(await screen.findByText("str2str")).toBeInTheDocument();
    expect(screen.getByText(/1 rover connected/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Write `web/src/lib/rates.ts`**

```ts
import { useEffect, useRef, useState } from "react";
import type { RtcmMsgStats } from "./types";

type Counts = Record<string, RtcmMsgStats>;
interface Sample { t: number; counts: Record<string, { count: number; bytes: number }> }

export class RateTracker {
  private samples: Sample[] = [];
  constructor(private windowS = 10) {}
  push(messages: Counts, tMs: number): void {
    const counts: Sample["counts"] = {};
    for (const [k, v] of Object.entries(messages)) counts[k] = { count: v.count, bytes: v.bytes };
    this.samples.push({ t: tMs, counts });
    const cutoff = tMs - this.windowS * 1000;
    while (this.samples.length > 2 && this.samples[0].t < cutoff) this.samples.shift();
  }
  rates(): Record<string, { hz: number; bytesPerS: number }> {
    const out: Record<string, { hz: number; bytesPerS: number }> = {};
    if (this.samples.length < 2) return out;
    const first = this.samples[0], last = this.samples[this.samples.length - 1];
    const dt = (last.t - first.t) / 1000;
    for (const [k, v] of Object.entries(last.counts)) {
      const before = first.counts[k];
      out[k] = before && dt > 0 ? { hz: (v.count - before.count) / dt, bytesPerS: (v.bytes - before.bytes) / dt } : { hz: 0, bytesPerS: 0 };
    }
    return out;
  }
}

export function useMessageRates(messages: Counts): Record<string, { hz: number; bytesPerS: number }> {
  const tracker = useRef(new RateTracker(10));
  const [rates, setRates] = useState<Record<string, { hz: number; bytesPerS: number }>>({});
  useEffect(() => {
    tracker.current.push(messages, Date.now());
    setRates(tracker.current.rates());
  }, [messages]);
  return rates;
}

/** Keep the last `seconds` values of a live number, sampled at most once per second. */
export function useRing(value: number, seconds = 300): { t: number; v: number }[] {
  const [ring, setRing] = useState<{ t: number; v: number }[]>([]);
  useEffect(() => {
    const now = Date.now();
    setRing((r) => {
      if (r.length && now - r[r.length - 1].t < 1000) return r;
      const next = [...r, { t: now, v: value }];
      const cutoff = now - seconds * 1000;
      return next.filter((p) => p.t >= cutoff);
    });
  }, [value, seconds]);
  return ring;
}
```

- [ ] **Step 4: Write `web/src/pages/Corrections.tsx`**

```tsx
import { PageHeader } from "@/app/PageHeader";
import { Panel } from "@/components/Panel";
import { Stat } from "@/components/Stat";
import { CopyButton } from "@/components/CopyButton";
import { EmptyState } from "@/components/EmptyState";
import { DataTable, type Column } from "@/components/DataTable";
import { Sparkline } from "@/components/charts/Sparkline";
import { StatusBadge } from "@/components/StatusBadge";
import { fmtBytes, fmtRate, fmtUtcDate, relTime } from "@/lib/format";
import { useLive } from "@/lib/live";
import { useNtrip, useNtripHistory } from "@/lib/queries";
import { useMessageRates, useRing } from "@/lib/rates";
import type { NtripClient } from "@/lib/types";

const RTCM_NAMES: Record<string, string> = {
  "1005": "Station ARP (base position)", "1006": "Station ARP + antenna height", "1074": "GPS MSM4", "1077": "GPS MSM7", "1084": "GLONASS MSM4", "1087": "GLONASS MSM7",
  "1094": "Galileo MSM4", "1097": "Galileo MSM7", "1124": "BeiDou MSM4", "1127": "BeiDou MSM7", "1230": "GLONASS code-phase biases", "4072": "u-blox proprietary",
};

const clientColumns: Column<NtripClient>[] = [
  { key: "ip", header: "Address", cell: (c) => `${c.ip}:${c.port}` },
  { key: "ua", header: "Client", cell: (c) => c.user_agent || "—" },
  { key: "v", header: "NTRIP", cell: (c) => `v${c.version}` },
  { key: "user", header: "User", cell: (c) => c.username ?? "anonymous" },
  { key: "since", header: "Connected", cell: (c) => relTime(c.connected_utc), sortValue: (c) => c.connected_utc },
  { key: "bytes", header: "Sent", cell: (c) => fmtBytes(c.bytes_sent), sortValue: (c) => c.bytes_sent, align: "right" },
  { key: "pos", header: "Reported position", cell: (c) => (c.last_gga_lat == null ? "—" : `${c.last_gga_lat.toFixed(6)}, ${c.last_gga_lon!.toFixed(6)}`) },
];

export default function Corrections() {
  const { state, ntripClients } = useLive();
  const ntrip = useNtrip();
  const history = useNtripHistory(50);
  const rates = useMessageRates(state?.rtcm_out.messages ?? {});
  const ring = useRing(state?.rtcm_out.bytes_per_s ?? 0);
  if (!state) return <><PageHeader title="Corrections" /><EmptyState title="Waiting for the receiver" /></>;
  const types = Object.keys(state.rtcm_out.messages).sort((a, b) => Number(a) - Number(b));
  const flowing = state.rtcm_out.bytes_per_s > 0;
  return (
    <>
      <PageHeader title="Corrections">
        <StatusBadge level={flowing ? "good" : "serious"} label={flowing ? `Corrections flowing · ${ntripClients.length} rover${ntripClients.length === 1 ? "" : "s"} connected` : "No RTCM output"} />
      </PageHeader>
      <div className="grid grid-cols-12 gap-4">
        <Panel className="col-span-12 lg:col-span-7" title="RTCM 3 output" bodyClassName="p-2">
          {types.length === 0 ? <p className="p-2 text-ink-2">No RTCM messages yet. The receiver starts sending observations once the base position is known (survey-in valid or fixed site).</p> : (
            <table className="w-full text-[14px]">
              <thead><tr className="border-b border-line text-left text-ink-2"><th className="py-1.5 pr-3 font-medium">Type</th><th className="py-1.5 pr-3 font-medium">Content</th><th className="py-1.5 pr-3 text-right font-medium">Count</th><th className="py-1.5 pr-3 text-right font-medium">Rate</th><th className="py-1.5 pr-3 text-right font-medium">Bytes/s</th></tr></thead>
              <tbody>
                {types.map((t) => {
                  const m = state.rtcm_out.messages[t];
                  const r = rates[t];
                  return (
                    <tr key={t} className="border-b border-line/60 last:border-0">
                      <td className="num py-1.5 pr-3">{t}</td>
                      <td className="py-1.5 pr-3 text-ink-2">{RTCM_NAMES[t] ?? "RTCM message"}</td>
                      <td className="num py-1.5 pr-3 text-right">{m.count}</td>
                      <td className="num py-1.5 pr-3 text-right">{r ? `${r.hz.toFixed(2)} Hz` : "—"}</td>
                      <td className="num py-1.5 pr-3 text-right">{r ? fmtRate(r.bytesPerS) : "—"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </Panel>
        <Panel className="col-span-12 lg:col-span-5" title="Stream">
          <Stat label="Bitrate" value={fmtRate(state.rtcm_out.bytes_per_s)} />
          <Stat label="Total sent to bus" value={`${fmtBytes(state.rtcm_out.total_bytes)} · ${state.rtcm_out.total_count} messages`} />
          <div className="mt-3"><Sparkline label="Bitrate, last 5 minutes" points={ring} format={(v) => fmtRate(v)} /></div>
        </Panel>
        <Panel className="col-span-12 lg:col-span-5" title="NTRIP caster">
          {ntrip.data ? (
            <>
              <div className="mb-3 flex items-center gap-2">
                <code className="num flex-1 truncate rounded-md bg-panel-2 px-2 py-1 text-[13px]">{ntrip.data.connection_url}</code>
                <CopyButton text={ntrip.data.connection_url.replace(":***@", ntrip.data.anonymous ? "" : ":<password>@")} label="Copy" />
              </div>
              <Stat label="Listening" value={ntrip.data.running ? `${ntrip.data.host}:${ntrip.data.port}` : "not running"} level={ntrip.data.running ? "good" : "critical"} />
              <Stat label="Mountpoint" value={`/${ntrip.data.mountpoint}`} />
              <Stat label="Authentication" value={ntrip.data.anonymous ? "anonymous" : `user ${ntrip.data.username}`} />
              <Stat label="Bind mode" value={ntrip.data.bind_mode} />
              <p className="mt-2 text-[12px] leading-4 text-ink-2">Rovers use NTRIP v1 or v2; the password is the one in your .env. Replace &lt;password&gt; after copying.</p>
            </>
          ) : <p className="text-ink-2">Loading caster status…</p>}
        </Panel>
        <Panel className="col-span-12 lg:col-span-7" title={`Connected rovers (${ntripClients.length})`} bodyClassName="p-2">
          <DataTable columns={clientColumns} rows={ntripClients} rowKey={(c) => String(c.id)} emptyText="No rovers connected. Point an NTRIP client at the connection string." dense />
        </Panel>
        <Panel className="col-span-12" title="Recent connections" bodyClassName="p-2">
          <table className="w-full text-[14px]">
            <thead><tr className="border-b border-line text-left text-ink-2"><th className="py-1.5 pr-3 font-medium">Address</th><th className="py-1.5 pr-3 font-medium">Client</th><th className="py-1.5 pr-3 font-medium">Connected</th><th className="py-1.5 pr-3 font-medium">Disconnected</th><th className="py-1.5 pr-3 text-right font-medium">Sent</th><th className="py-1.5 pr-3 font-medium">Reason</th></tr></thead>
            <tbody>
              {(history.data ?? []).map((r) => (
                <tr key={String(r.id)} className="border-b border-line/60 last:border-0">
                  <td className="py-1.5 pr-3">{String(r.ip ?? "—")}</td><td className="py-1.5 pr-3">{String(r.user_agent ?? "—")}</td>
                  <td className="num py-1.5 pr-3">{fmtUtcDate(r.connected_utc as string)}</td><td className="num py-1.5 pr-3">{r.disconnected_utc ? fmtUtcDate(r.disconnected_utc as string) : "still connected"}</td>
                  <td className="num py-1.5 pr-3 text-right">{fmtBytes(r.bytes_sent as number)}</td><td className="py-1.5 pr-3 text-ink-2">{String(r.reason ?? "")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      </div>
    </>
  );
}
```
Register `{ path: "corrections", element: <Corrections /> }`.

- [ ] **Step 5: Run, build, commit**

`cd web && pnpm test && pnpm lint && pnpm build`; then
```bash
cd /home/nekosaif/github/mtrtk && git add web && git commit -m "feat(web): corrections page with RTCM rates, bitrate history, NTRIP caster info and clients

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Site page — position mode, survey-in, sites, PPP steps

**Files:**
- Create: `web/src/pages/Site.tsx`, `web/src/components/SiteForm.tsx`, `web/src/pages/Site.test.tsx`
- Modify: `web/src/app/router.tsx`

**Interfaces:**
- Produces: `SiteForm({initial?, onSubmit, submitLabel})` — tabs `ECEF | Lat/Lon/Height`, fields name, coordinates, sigma (m), source, frame, epoch, notes; `Site` page panels: Position mode (radio survey-in/fixed/off + survey params → `PUT /api/base/mode`), Survey-in progress (duration vs minimum, mean accuracy vs limit; "Freeze as site" dialog → `POST /api/base/survey/freeze`), Sites table (`GET /api/base/sites`; activate → `POST …/activate`; delete → `DELETE`; add → `POST /api/base/sites`), Verification (`GET /api/base/mode`: last 1005 vs active site, badge), PPP steps (data available last 24 h via `/api/logs/availability`; download window; submit links; enter result opens `SiteForm` with frame ITRF2020 and source `csrs-ppp`).

- [ ] **Step 1: Write the failing test**

`web/src/pages/Site.test.tsx`:
```tsx
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { useLive } from "@/lib/live";
import { sampleState } from "@/test/fixtures";
import Site from "./Site";

const sites = [
  { id: 1, name: "roof", x: 1234567.8912, y: -987654.3234, z: 5555555.0, lat: 61.1, lon: -38.6, height_m: 12.3, sigma_x: 0.004, sigma_y: 0.004, sigma_z: 0.004, frame: "ITRF2020", epoch: "2026.71", source: "csrs-ppp", notes: null, created_utc: "2026-09-18T10:00:00+00:00", active: true },
  { id: 2, name: "field", x: 1, y: 2, z: 3, lat: 0, lon: 0, height_m: 0, sigma_x: null, sigma_y: null, sigma_z: null, frame: "WGS84 (receiver)", epoch: null, source: "survey-in", notes: null, created_utc: "2026-09-18T11:00:00+00:00", active: false },
];
const mode = { available: true, mode: "fixed", site: "roof", verified: true, last_1005: { station_id: 0, x: 1234567.8912, y: -987654.3234, z: 5555555.0 }, svin: { min_duration_s: 300, acc_limit_m: 2.0 } };

let calls: [string, RequestInit | undefined][] = [];
function mockFetch() {
  calls = [];
  globalThis.fetch = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const p = String(url);
    calls.push([p, init]);
    if (p.endsWith("/api/base/sites") && !init?.method) return new Response(JSON.stringify(sites), { status: 200 });
    if (p.endsWith("/api/base/mode") && !init?.method) return new Response(JSON.stringify(mode), { status: 200 });
    if (p.includes("/api/logs/availability")) return new Response(JSON.stringify(Array.from({ length: 24 }, (_, i) => ({ hour_utc: `2026-09-18T${String(i).padStart(2, "0")}:00:00+00:00`, available: i < 20, bytes: 1, complete: true }))), { status: 200 });
    if (p.endsWith("/activate")) return new Response(JSON.stringify({ ...sites[1], active: true }), { status: 200 });
    if (p.endsWith("/api/base/sites") && init?.method === "POST") return new Response(JSON.stringify({ ...sites[0], id: 3, name: "new" }), { status: 200 });
    if (p.endsWith("/api/base/survey/freeze")) return new Response(JSON.stringify({ ...sites[1], name: "frozen" }), { status: 200 });
    return new Response("{}", { status: 200 });
  }) as typeof fetch;
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><MemoryRouter><Site /></MemoryRouter></QueryClientProvider>);
}

describe("Site page", () => {
  beforeEach(() => {
    useLive.setState({ state: sampleState(), status: "open", lastEpochAt: Date.now(), base: { mode: "fixed", site: "roof", reason: null, verified: true } });
    mockFetch();
  });

  it("lists sites, marks the active one and shows 1005 verification", async () => {
    renderPage();
    const table = await screen.findByRole("table", { name: /sites/i });
    expect(within(table).getAllByRole("row")).toHaveLength(3);
    expect(within(table).getByText("roof").closest("tr")).toHaveTextContent(/active/i);
    expect(await screen.findByText(/matches the active site/i)).toBeInTheDocument();
    expect(screen.getByText(/20 of 24 hours/i)).toBeInTheDocument();
  });

  it("activates a site", async () => {
    renderPage();
    const row = (await screen.findByText("field")).closest("tr")!;
    await userEvent.click(within(row).getByRole("button", { name: /activate/i }));
    expect(calls.some(([u, i]) => u.endsWith("/api/base/sites/field/activate") && i?.method === "POST")).toBe(true);
  });

  it("adds a site from the ECEF form", async () => {
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: /add site/i }));
    const dialog = screen.getByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText(/^name/i), "new");
    await userEvent.type(within(dialog).getByLabelText(/^x/i), "1234567.8912");
    await userEvent.type(within(dialog).getByLabelText(/^y/i), "-987654.3234");
    await userEvent.type(within(dialog).getByLabelText(/^z/i), "5555555.0");
    await userEvent.click(within(dialog).getByRole("button", { name: /save site/i }));
    const post = calls.find(([u, i]) => u.endsWith("/api/base/sites") && i?.method === "POST")!;
    expect(JSON.parse(post[1]!.body as string)).toMatchObject({ name: "new", x: 1234567.8912, y: -987654.3234, z: 5555555.0, frame: "ITRF2020" });
  });

  it("freeze is disabled while survey-in is not valid", async () => {
    renderPage();
    expect(await screen.findByRole("button", { name: /freeze as site/i })).toBeDisabled();
  });
});
```

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Write `web/src/components/SiteForm.tsx`**

```tsx
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

export interface SiteInput { name: string; x?: number; y?: number; z?: number; lat?: number; lon?: number; height_m?: number; sigma_m?: number; source: string; frame: string; epoch?: string; notes?: string }

const num = (v: string) => (v.trim() === "" ? undefined : Number(v));

export function SiteForm({ initial, onSubmit, submitLabel = "Save site" }: { initial?: Partial<SiteInput>; onSubmit: (site: SiteInput) => Promise<void>; submitLabel?: string }) {
  const [tab, setTab] = useState<"ecef" | "llh">(initial?.lat != null ? "llh" : "ecef");
  const [f, setF] = useState({ name: initial?.name ?? "", x: initial?.x?.toString() ?? "", y: initial?.y?.toString() ?? "", z: initial?.z?.toString() ?? "", lat: initial?.lat?.toString() ?? "", lon: initial?.lon?.toString() ?? "", h: initial?.height_m?.toString() ?? "", sigma: initial?.sigma_m?.toString() ?? "", source: initial?.source ?? "manual", frame: initial?.frame ?? "ITRF2020", epoch: initial?.epoch ?? "", notes: initial?.notes ?? "" });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement>) => setF({ ...f, [k]: e.target.value });
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    const site: SiteInput = { name: f.name.trim(), source: f.source, frame: f.frame, epoch: f.epoch || undefined, notes: f.notes || undefined, sigma_m: num(f.sigma) };
    if (tab === "ecef") Object.assign(site, { x: num(f.x), y: num(f.y), z: num(f.z) });
    else Object.assign(site, { lat: num(f.lat), lon: num(f.lon), height_m: num(f.h) });
    if (!site.name) return setError("Give the site a name.");
    const coords = tab === "ecef" ? [site.x, site.y, site.z] : [site.lat, site.lon, site.height_m];
    if (coords.some((v) => v === undefined || Number.isNaN(v))) return setError("All three coordinates are required.");
    setBusy(true);
    try { await onSubmit(site); } catch (err) { setError(String(err)); } finally { setBusy(false); }
  };
  return (
    <form onSubmit={submit} className="flex flex-col gap-3">
      <div><Label htmlFor="site-name">Name</Label><Input id="site-name" value={f.name} onChange={set("name")} placeholder="roof-2026" /></div>
      <Tabs value={tab} onValueChange={(v) => setTab(v as "ecef" | "llh")}>
        <TabsList><TabsTrigger value="ecef">ECEF (metres)</TabsTrigger><TabsTrigger value="llh">Lat / Lon / Height</TabsTrigger></TabsList>
        <TabsContent value="ecef" className="grid grid-cols-3 gap-2">
          <div><Label htmlFor="site-x">X</Label><Input id="site-x" inputMode="decimal" value={f.x} onChange={set("x")} /></div>
          <div><Label htmlFor="site-y">Y</Label><Input id="site-y" inputMode="decimal" value={f.y} onChange={set("y")} /></div>
          <div><Label htmlFor="site-z">Z</Label><Input id="site-z" inputMode="decimal" value={f.z} onChange={set("z")} /></div>
        </TabsContent>
        <TabsContent value="llh" className="grid grid-cols-3 gap-2">
          <div><Label htmlFor="site-lat">Latitude</Label><Input id="site-lat" inputMode="decimal" value={f.lat} onChange={set("lat")} /></div>
          <div><Label htmlFor="site-lon">Longitude</Label><Input id="site-lon" inputMode="decimal" value={f.lon} onChange={set("lon")} /></div>
          <div><Label htmlFor="site-h">Height (ellipsoid, m)</Label><Input id="site-h" inputMode="decimal" value={f.h} onChange={set("h")} /></div>
        </TabsContent>
      </Tabs>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <div><Label htmlFor="site-sigma">Sigma per axis (m)</Label><Input id="site-sigma" inputMode="decimal" value={f.sigma} onChange={set("sigma")} placeholder="0.005" /></div>
        <div><Label htmlFor="site-source">Source</Label><Input id="site-source" value={f.source} onChange={set("source")} placeholder="csrs-ppp" /></div>
        <div><Label htmlFor="site-frame">Frame</Label><Input id="site-frame" value={f.frame} onChange={set("frame")} /></div>
        <div><Label htmlFor="site-epoch">Epoch</Label><Input id="site-epoch" value={f.epoch} onChange={set("epoch")} placeholder="2026.71" /></div>
      </div>
      <div><Label htmlFor="site-notes">Notes</Label><Input id="site-notes" value={f.notes} onChange={set("notes")} /></div>
      {error ? <p className="text-status-critical">{error}</p> : null}
      <div className="flex justify-end"><Button type="submit" disabled={busy}>{submitLabel}</Button></div>
    </form>
  );
}
```

- [ ] **Step 4: Write `web/src/pages/Site.tsx`**

```tsx
import { useState } from "react";
import { Link } from "react-router";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { PageHeader } from "@/app/PageHeader";
import { Panel } from "@/components/Panel";
import { Stat } from "@/components/Stat";
import { StatusBadge } from "@/components/StatusBadge";
import { EmptyState } from "@/components/EmptyState";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { SiteForm, type SiteInput } from "@/components/SiteForm";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { del, post, put } from "@/lib/api";
import { fmtAcc, fmtDms, fmtDuration, fmtMeters } from "@/lib/format";
import { useLive } from "@/lib/live";
import { useAvailability, useBaseMode, useSites } from "@/lib/queries";
import type { Site as SiteT } from "@/lib/types";

const SERVICES = [
  { name: "CSRS-PPP (NRCan)", url: "https://webapp.csrs-scrs.nrcan-rncan.gc.ca/geod/tools-outils/ppp.php", note: "free, global, GPS+GLONASS, best with 24 h, ITRF result" },
  { name: "AUSPOS (Geoscience Australia)", url: "https://gnss.ga.gov.au/auspos", note: "free, global, GPS, 1 h minimum" },
  { name: "OPUS (NGS)", url: "https://geodesy.noaa.gov/OPUS/", note: "USA only, GPS L1/L2, 15 min – 48 h" },
];

export default function Site() {
  const state = useLive((s) => s.state);
  const qc = useQueryClient();
  const sites = useSites();
  const mode = useBaseMode();
  const [window24] = useState(() => { const now = new Date(); return [new Date(now.getTime() - 24 * 3600_000).toISOString(), now.toISOString()] as const; });
  const availability = useAvailability(window24[0], window24[1]);
  const [freezeName, setFreezeName] = useState("");
  const [freezeActivate, setFreezeActivate] = useState(true);
  const [addOpen, setAddOpen] = useState(false);
  const [pppOpen, setPppOpen] = useState(false);
  const [svinMin, setSvinMin] = useState<string | null>(null);
  const [svinAcc, setSvinAcc] = useState<string | null>(null);
  const invalidate = () => { qc.invalidateQueries({ queryKey: ["base"] }); };
  const setMode = useMutation({ mutationFn: (body: Record<string, unknown>) => put("/api/base/mode", body), onSuccess: () => { toast.success("Position mode updated"); invalidate(); }, onError: (e) => toast.error(String(e)) });
  const activate = useMutation({ mutationFn: (name: string) => post(`/api/base/sites/${encodeURIComponent(name)}/activate`), onSuccess: (_, name) => { toast.success(`${name} is now the active site`); invalidate(); }, onError: (e) => toast.error(String(e)) });
  const remove = useMutation({ mutationFn: (name: string) => del(`/api/base/sites/${encodeURIComponent(name)}`), onSuccess: () => { toast.success("Site deleted"); invalidate(); }, onError: (e) => toast.error(String(e)) });
  const add = useMutation({ mutationFn: (site: SiteInput) => post<SiteT>("/api/base/sites", site), onSuccess: (s) => { toast.success(`Saved site ${s.name}`); setAddOpen(false); setPppOpen(false); invalidate(); } });
  const freeze = useMutation({ mutationFn: () => post<SiteT>("/api/base/survey/freeze", { name: freezeName, activate: freezeActivate }), onSuccess: (s) => { toast.success(`Saved ${s.name} from survey-in`); invalidate(); }, onError: (e) => toast.error(String(e)) });

  if (!state) return <><PageHeader title="Site" /><EmptyState title="Waiting for the receiver" /></>;
  const svin = state.survey_in;
  const m = mode.data;
  const activeSite = sites.data?.find((s) => s.active);
  const verification = (() => {
    if (!m || m.mode !== "fixed" || !activeSite) return null;
    if (!m.last_1005) return { level: "warning" as const, label: "Waiting for RTCM 1005" };
    const d = Math.max(Math.abs(m.last_1005.x - activeSite.x), Math.abs(m.last_1005.y - activeSite.y), Math.abs(m.last_1005.z - activeSite.z));
    return d <= 0.0005 ? { level: "good" as const, label: `RTCM 1005 matches the active site (Δ ${(d * 1000).toFixed(1)} mm)` } : { level: "critical" as const, label: `RTCM 1005 differs from the active site by ${d.toFixed(3)} m` };
  })();
  const hours = availability.data?.filter((h) => h.available).length ?? 0;
  const minDur = svinMin ?? String(m?.svin.min_duration_s ?? state.survey_in.dur_s);
  const accLim = svinAcc ?? String(m?.svin.acc_limit_m ?? 2);

  return (
    <>
      <PageHeader title="Site">
        {m ? <StatusBadge level={m.mode === "fixed" ? (m.verified ? "good" : "warning") : m.mode === "survey-in" ? (svin.valid ? "good" : "warning") : "serious"} label={m.mode === "fixed" ? `Fixed · ${m.site ?? ""}` : m.mode === "survey-in" ? (svin.valid ? "Survey-in complete" : "Survey-in running") : "Position mode off"} /> : null}
      </PageHeader>
      <div className="grid grid-cols-12 gap-4">
        <Panel className="col-span-12 lg:col-span-4" title="Position mode">
          {!m?.available ? <p className="text-ink-2">The base mode manager is not running (replay or rover role).</p> : (
            <div className="flex flex-col gap-3">
              <div className="grid gap-2">
                <div><Label htmlFor="svin-min">Survey-in minimum duration (s)</Label><Input id="svin-min" inputMode="numeric" value={minDur} onChange={(e) => setSvinMin(e.target.value)} /></div>
                <div><Label htmlFor="svin-acc">Survey-in accuracy limit (m)</Label><Input id="svin-acc" inputMode="decimal" value={accLim} onChange={(e) => setSvinAcc(e.target.value)} /></div>
              </div>
              <div className="flex flex-wrap gap-2">
                <Button variant={m.mode === "survey-in" ? "default" : "outline"} onClick={() => setMode.mutate({ mode: "survey-in", svin_min_duration_s: Number(minDur), svin_acc_limit_m: Number(accLim) })}>Start survey-in</Button>
                <Button variant={m.mode === "fixed" ? "default" : "outline"} disabled={!activeSite} onClick={() => activeSite && setMode.mutate({ mode: "fixed", site: activeSite.name })}>Use active site</Button>
                <Button variant={m.mode === "off" ? "default" : "outline"} onClick={() => setMode.mutate({ mode: "off" })}>Off</Button>
              </div>
              <p className="text-[12px] leading-4 text-ink-2">Survey-in averages the receiver's own position (about 1–2 m absolute). A fixed site from PPP gives centimetre absolute accuracy and is what rovers inherit.</p>
            </div>
          )}
        </Panel>
        <Panel className="col-span-12 lg:col-span-4" title="Survey-in">
          <Stat label="State" value={svin.valid ? "complete" : svin.active ? "running" : "not running"} level={svin.valid ? "good" : svin.active ? "warning" : undefined} />
          <Stat label="Elapsed" value={`${fmtDuration(svin.dur_s)} of ${fmtDuration(m?.svin.min_duration_s ?? 0)}`} />
          <Stat label="Observations" value={String(svin.obs)} />
          <Stat label="Mean 3D accuracy" value={`${fmtAcc(svin.mean_acc_m)} (limit ${fmtAcc(m?.svin.acc_limit_m)})`} />
          <div className="mt-3 flex flex-col gap-2">
            <Label htmlFor="freeze-name">Site name</Label>
            <div className="flex gap-2">
              <Input id="freeze-name" value={freezeName} onChange={(e) => setFreezeName(e.target.value)} placeholder="roof-2026" />
              <Button disabled={!svin.valid || !freezeName.trim() || freeze.isPending} onClick={() => freeze.mutate()}>Freeze as site</Button>
            </div>
            <label className="flex items-center gap-2 text-[14px]"><input type="checkbox" checked={freezeActivate} onChange={(e) => setFreezeActivate(e.target.checked)} /> Switch to fixed mode with it</label>
          </div>
        </Panel>
        <Panel className="col-span-12 lg:col-span-4" title="Verification">
          {verification ? <StatusBadge level={verification.level} label={verification.label} /> : <p className="text-ink-2">Verification runs while a fixed site is active: the RTCM 1005 message the rovers receive is compared with the site coordinates.</p>}
          {m?.last_1005 ? <div className="mt-3"><Stat label="1005 X" value={fmtMeters(m.last_1005.x, 4)} /><Stat label="1005 Y" value={fmtMeters(m.last_1005.y, 4)} /><Stat label="1005 Z" value={fmtMeters(m.last_1005.z, 4)} /></div> : null}
        </Panel>

        <Panel className="col-span-12" title="Sites" bodyClassName="p-2" actions={
          <Dialog open={addOpen} onOpenChange={setAddOpen}>
            <DialogTrigger asChild><Button size="sm">Add site</Button></DialogTrigger>
            <DialogContent className="max-w-2xl"><DialogHeader><DialogTitle>Add a site</DialogTitle></DialogHeader><SiteForm onSubmit={(s) => add.mutateAsync(s).then(() => undefined)} /></DialogContent>
          </Dialog>
        }>
          <table className="w-full text-[14px]" aria-label="Sites">
            <thead><tr className="border-b border-line text-left text-ink-2"><th className="py-1.5 pr-3 font-medium">Name</th><th className="py-1.5 pr-3 font-medium">Position</th><th className="py-1.5 pr-3 font-medium">ECEF (m)</th><th className="py-1.5 pr-3 text-right font-medium">σ 3D</th><th className="py-1.5 pr-3 font-medium">Frame</th><th className="py-1.5 pr-3 font-medium">Source</th><th className="py-1.5 pr-3 font-medium"></th></tr></thead>
            <tbody>
              {(sites.data ?? []).map((s) => {
                const sigma = s.sigma_x != null && s.sigma_y != null && s.sigma_z != null ? Math.hypot(s.sigma_x, s.sigma_y, s.sigma_z) : null;
                return (
                  <tr key={s.name} className="border-b border-line/60 last:border-0">
                    <td className="py-1.5 pr-3">{s.name} {s.active ? <StatusBadge level="good" label="active" className="ml-1 text-[12px]" /> : null}</td>
                    <td className="num py-1.5 pr-3">{s.lat != null && s.lon != null ? `${fmtDms(s.lat, true)} ${fmtDms(s.lon, false)} · ${fmtMeters(s.height_m, 3)}` : "—"}</td>
                    <td className="num py-1.5 pr-3">{s.x.toFixed(4)} · {s.y.toFixed(4)} · {s.z.toFixed(4)}</td>
                    <td className="num py-1.5 pr-3 text-right">{sigma == null ? "—" : fmtAcc(sigma)}</td>
                    <td className="py-1.5 pr-3">{s.frame}{s.epoch ? ` @ ${s.epoch}` : ""}</td>
                    <td className="py-1.5 pr-3 text-ink-2">{s.source}</td>
                    <td className="py-1.5 pr-3 text-right">
                      <div className="flex justify-end gap-1">
                        {!s.active ? <Button size="sm" variant="outline" onClick={() => activate.mutate(s.name)}>Activate</Button> : null}
                        <ConfirmDialog trigger={<Button size="sm" variant="ghost">Delete</Button>} title={`Delete site ${s.name}?`} body={s.active ? "This site is active; the receiver keeps its current fixed position until you change mode." : undefined} confirmLabel="Delete" destructive onConfirm={() => remove.mutateAsync(s.name).then(() => undefined)} />
                      </div>
                    </td>
                  </tr>
                );
              })}
              {sites.data?.length === 0 ? <tr><td colSpan={7} className="py-6 text-center text-ink-3">No sites yet. Freeze a survey-in or add PPP coordinates.</td></tr> : null}
            </tbody>
          </table>
        </Panel>

        <Panel className="col-span-12" title="Centimetre-level site from PPP">
          <ol className="grid gap-4 md:grid-cols-4">
            <li className="rounded-md border border-line p-3"><p className="font-medium">1. Collect 24 hours of raw data</p><p className="num mt-1 text-ink-2">{availability.data ? `${hours} of 24 hours available` : "checking…"}</p><Link to="/logs" className="text-brass">Open logs</Link></li>
            <li className="rounded-md border border-line p-3"><p className="font-medium">2. Export the observation file</p><p className="mt-1 text-ink-2">Download the raw window from Logs. RINEX export presets arrive with the export feature; until then convert with RTKLIB convbin.</p></li>
            <li className="rounded-md border border-line p-3"><p className="font-medium">3. Submit to a PPP service</p><ul className="mt-1 flex flex-col gap-1">{SERVICES.map((s) => <li key={s.name}><a href={s.url} target="_blank" rel="noreferrer" className="text-brass">{s.name}</a><span className="block text-[12px] leading-4 text-ink-2">{s.note}</span></li>)}</ul></li>
            <li className="rounded-md border border-line p-3"><p className="font-medium">4. Enter the result</p><p className="mt-1 text-ink-2">Paste the ECEF X/Y/Z and sigma from the report, then activate the site.</p>
              <Dialog open={pppOpen} onOpenChange={setPppOpen}><DialogTrigger asChild><Button size="sm" className="mt-2">Enter PPP result</Button></DialogTrigger>
                <DialogContent className="max-w-2xl"><DialogHeader><DialogTitle>Site from PPP result</DialogTitle></DialogHeader><SiteForm initial={{ source: "csrs-ppp", frame: "ITRF2020" }} onSubmit={(s) => add.mutateAsync(s).then(() => undefined)} /></DialogContent></Dialog>
            </li>
          </ol>
        </Panel>
      </div>
    </>
  );
}
```
Register `{ path: "site", element: <Site /> }`.

- [ ] **Step 5: Run, build, commit**

`cd web && pnpm test && pnpm lint && pnpm build`; then
```bash
cd /home/nekosaif/github/mtrtk && git add web && git commit -m "feat(web): site page with position mode, survey-in freeze, sites table and PPP steps

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: Logs and History pages

**Files:**
- Create: `web/src/components/charts/AvailabilityStrip.tsx`, `web/src/components/charts/TimeSeries.tsx`, `web/src/pages/Logs.tsx`, `web/src/pages/History.tsx`, `web/src/pages/Logs.test.tsx`, `web/src/components/charts/TimeSeries.test.tsx`
- Modify: `web/src/app/router.tsx`

**Interfaces:**
- Produces: `AvailabilityStrip({slots, onSelect?})` — one cell per hour, brass = complete, muted = partial, empty = missing, hover title, click selects a window; `TimeSeries({points, label, unit, format, height=160, color})` — single-series line chart with left y axis (4 ticks), bottom time axis (UTC HH:MM), gaps where `v` is null, crosshair tooltip, a `<details>` table alternative; `Logs` page (summary, availability last 48 h, window download form, file table with keep toggle/download/delete); `History` page (range chips 1 h / 6 h / 24 h / 7 d / 90 d, metric checkboxes grouped Position · Satellites · RF · Corrections · System, one `TimeSeries` per selected metric — never two series on one axis).
- Metric catalogue (`HISTORY_METRICS`): `h_acc_m` Horizontal accuracy (m), `v_acc_m` Vertical accuracy (m), `nsat_used` Satellites used, `nsat_tracked` Satellites tracked, `pdop` PDOP, `cno_mean` Mean C/N0 (dB-Hz), `jam_ind` Jamming indicator, `agc_cnt` AGC, `noise_per_ms` Noise, `rtcm_bytes_per_s` RTCM bytes/s, `ntrip_clients` NTRIP clients, `cpu_pct` CPU %, `mem_pct` Memory %, `disk_free_gb` Disk free (GB), `temp_c` Temperature (°C). For `1m` resolution the API maps bare names to `_avg`.

- [ ] **Step 1: Write the failing tests**

`web/src/components/charts/TimeSeries.test.tsx`:
```tsx
import { render, screen } from "@testing-library/react";
import { TimeSeries } from "./TimeSeries";

describe("TimeSeries", () => {
  const points = Array.from({ length: 10 }, (_, i) => ({ t: 1_700_000_000 + i * 60, v: i === 5 ? null : i * 0.1 }));
  it("renders a path with a gap and a table alternative", () => {
    render(<TimeSeries points={points} label="Horizontal accuracy" unit="m" format={(v) => v.toFixed(2)} />);
    const img = screen.getByRole("img", { name: /horizontal accuracy/i });
    const d = img.querySelector("path[data-series]")!.getAttribute("d")!;
    expect(d.match(/M/g)).toHaveLength(2); // two segments around the null
    expect(screen.getByText(/show as table/i)).toBeInTheDocument();
    expect(img.querySelectorAll("text").length).toBeGreaterThan(4); // axis ticks
  });
  it("shows a message with too little data", () => {
    render(<TimeSeries points={[]} label="x" unit="" format={String} />);
    expect(screen.getByText(/no data in this range/i)).toBeInTheDocument();
  });
});
```

`web/src/pages/Logs.test.tsx`:
```tsx
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import Logs from "./Logs";

const files = [
  { name: "MTRK_20260918_10.ubx", hour_utc: "2026-09-18T10:00:00+00:00", bytes: 14_000_000, complete: true, keep: false, msg_counts: { "RXM-RAWX": 3600 }, start_utc: null, end_utc: null },
  { name: "MTRK_20260918_11.ubx", hour_utc: "2026-09-18T11:00:00+00:00", bytes: 7_000_000, complete: false, keep: true, msg_counts: { "RXM-RAWX": 1800 }, start_utc: null, end_utc: null },
];
let calls: [string, RequestInit | undefined][] = [];

describe("Logs page", () => {
  beforeEach(() => {
    calls = [];
    globalThis.fetch = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      const p = String(url);
      calls.push([p, init]);
      if (p.endsWith("/api/logs")) return new Response(JSON.stringify({ files, total_bytes: 21_000_000, hours: 2, disk_free_gb: 42.5 }), { status: 200 });
      if (p.includes("/api/logs/availability")) return new Response(JSON.stringify([{ hour_utc: "2026-09-18T10:00:00+00:00", available: true, bytes: 1, complete: true }, { hour_utc: "2026-09-18T11:00:00+00:00", available: true, bytes: 1, complete: false }, { hour_utc: "2026-09-18T12:00:00+00:00", available: false, bytes: 0, complete: false }]), { status: 200 });
      if (init?.method === "PATCH") return new Response(JSON.stringify({ ...files[0], keep: true }), { status: 200 });
      return new Response("{}", { status: 200 });
    }) as typeof fetch;
  });

  it("shows files, disk summary and toggles keep", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={qc}><MemoryRouter><Logs /></MemoryRouter></QueryClientProvider>);
    expect(await screen.findByText("MTRK_20260918_10.ubx")).toBeInTheDocument();
    expect(screen.getByText(/42\.5 GB free/)).toBeInTheDocument();
    expect(screen.getByText(/20\.0 MB in 2 hours/)).toBeInTheDocument();
    const row = screen.getByText("MTRK_20260918_10.ubx").closest("tr")!;
    await userEvent.click(within(row).getByRole("switch", { name: /keep/i }));
    const patch = calls.find(([, i]) => i?.method === "PATCH")!;
    expect(patch[0]).toMatch(/\/api\/logs\/MTRK_20260918_10\.ubx$/);
    expect(JSON.parse(patch[1]!.body as string)).toEqual({ keep: true });
    expect(within(row).getByRole("link", { name: /download/i })).toHaveAttribute("href", "/api/logs/MTRK_20260918_10.ubx");
  });
});
```

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Write the charts**

`web/src/components/charts/AvailabilityStrip.tsx`:
```tsx
import type { HourSlot } from "@/lib/types";
import { fmtBytes } from "@/lib/format";

export function AvailabilityStrip({ slots, selected, onSelect }: { slots: HourSlot[]; selected?: [string, string] | null; onSelect?: (hour: HourSlot) => void }) {
  if (slots.length === 0) return <p className="text-ink-2">No hours in range.</p>;
  return (
    <div className="flex flex-col gap-1">
      <div className="grid gap-px" style={{ gridTemplateColumns: `repeat(${slots.length}, minmax(0, 1fr))` }} role="list" aria-label="Hourly raw log availability">
        {slots.map((h) => {
          const inSel = selected && h.hour_utc >= selected[0] && h.hour_utc < selected[1];
          const bg = !h.available ? "var(--panel-2)" : h.complete ? "var(--brass)" : "var(--ink-3)";
          return (
            <button key={h.hour_utc} type="button" role="listitem" onClick={() => onSelect?.(h)} title={`${h.hour_utc.slice(0, 13)}:00 UTC · ${h.available ? (h.complete ? "complete" : "partial") : "missing"} · ${fmtBytes(h.bytes)}`}
              className="h-5 rounded-[2px] outline-offset-1" style={{ background: bg, boxShadow: inSel ? "inset 0 0 0 2px var(--ink)" : undefined }} aria-label={`${h.hour_utc.slice(11, 13)}:00 ${h.available ? (h.complete ? "complete" : "partial") : "missing"}`} />
          );
        })}
      </div>
      <div className="flex justify-between text-[12px] leading-4 text-ink-2"><span>{slots[0].hour_utc.slice(0, 16).replace("T", " ")} UTC</span><span>{slots[slots.length - 1].hour_utc.slice(0, 16).replace("T", " ")} UTC</span></div>
    </div>
  );
}
```

`web/src/components/charts/TimeSeries.tsx`:
```tsx
import { useId, useState } from "react";

export interface TsPoint { t: number; v: number | null }

function niceTicks(min: number, max: number, count = 4): number[] {
  if (!(max > min)) return [min];
  const raw = (max - min) / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw;
  const start = Math.ceil(min / step) * step;
  const out: number[] = [];
  for (let v = start; v <= max + 1e-9; v += step) out.push(Number(v.toFixed(10)));
  return out;
}

export function TimeSeries({ points, label, unit, format, height = 160, color = "var(--brass)" }: { points: TsPoint[]; label: string; unit: string; format: (v: number) => string; height?: number; color?: string }) {
  const id = useId();
  const [hover, setHover] = useState<TsPoint | null>(null);
  const valid = points.filter((p) => p.v != null) as { t: number; v: number }[];
  if (valid.length < 2) return <p className="text-ink-2">{label}: no data in this range.</p>;
  const width = 720, left = 48, right = 8, top = 8, bottom = 24;
  const plotW = width - left - right, plotH = height - top - bottom;
  const t0 = points[0].t, t1 = points[points.length - 1].t;
  let min = Math.min(...valid.map((p) => p.v)), max = Math.max(...valid.map((p) => p.v));
  if (min === max) { min -= 1; max += 1; }
  const x = (t: number) => left + ((t - t0) / Math.max(1e-9, t1 - t0)) * plotW;
  const y = (v: number) => top + plotH - ((v - min) / (max - min)) * plotH;
  let d = "";
  let pen = false;
  for (const p of points) {
    if (p.v == null) { pen = false; continue; }
    d += `${pen ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)} `;
    pen = true;
  }
  const yTicks = niceTicks(min, max);
  const xTicks = Array.from({ length: 5 }, (_, i) => t0 + ((t1 - t0) * i) / 4);
  const fmtT = (t: number) => new Date(t * 1000).toISOString().slice(11, 16);
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-baseline justify-between text-[14px]"><span>{label}{unit ? <span className="ml-1 text-ink-2">({unit})</span> : null}</span><span className="num text-ink-2">{hover && hover.v != null ? `${fmtT(hover.t)} UTC · ${format(hover.v)}` : `${format(min)} – ${format(max)}`}</span></div>
      <svg role="img" aria-labelledby={id} viewBox={`0 0 ${width} ${height}`} width="100%"
        onMouseMove={(e) => { const rect = e.currentTarget.getBoundingClientRect(); const fx = ((e.clientX - rect.left) / rect.width) * width; let best = valid[0]; for (const p of valid) if (Math.abs(x(p.t) - fx) < Math.abs(x(best.t) - fx)) best = p; setHover(best); }}
        onMouseLeave={() => setHover(null)}>
        <title id={id}>{label} over time</title>
        {yTicks.map((v) => <g key={v}><line x1={left} x2={width - right} y1={y(v)} y2={y(v)} stroke="var(--line)" /><text x={left - 6} y={y(v) + 3} textAnchor="end" fontSize={10} fill="var(--ink-3)" className="num">{format(v)}</text></g>)}
        {xTicks.map((t) => <text key={t} x={x(t)} y={height - 6} textAnchor="middle" fontSize={10} fill="var(--ink-3)" className="num">{fmtT(t)}</text>)}
        <path data-series d={d.trim()} fill="none" stroke={color} strokeWidth={1.5} />
        {hover && hover.v != null ? <><line x1={x(hover.t)} x2={x(hover.t)} y1={top} y2={top + plotH} stroke="var(--ink-3)" strokeDasharray="2 2" /><circle cx={x(hover.t)} cy={y(hover.v)} r={4} fill={color} stroke="var(--panel)" strokeWidth={2} /></> : null}
      </svg>
      <details className="text-[12px] leading-4 text-ink-2"><summary className="cursor-pointer">Show as table</summary>
        <table className="mt-1 w-full"><thead><tr><th className="text-left font-medium">Time (UTC)</th><th className="text-right font-medium">{label}</th></tr></thead>
          <tbody>{valid.filter((_, i) => i % Math.ceil(valid.length / 50) === 0).map((p) => <tr key={p.t}><td className="num">{new Date(p.t * 1000).toISOString().slice(0, 19).replace("T", " ")}</td><td className="num text-right">{format(p.v)}</td></tr>)}</tbody></table>
      </details>
    </div>
  );
}
```

- [ ] **Step 4: Write `web/src/pages/Logs.tsx`**

```tsx
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { PageHeader } from "@/app/PageHeader";
import { Panel } from "@/components/Panel";
import { Stat } from "@/components/Stat";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { AvailabilityStrip } from "@/components/charts/AvailabilityStrip";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { del, patch } from "@/lib/api";
import { fmtBytes, fmtUtcDate } from "@/lib/format";
import { useAvailability, useLogs } from "@/lib/queries";
import type { LogFile } from "@/lib/types";

function isoHour(d: Date): string { return new Date(Math.floor(d.getTime() / 3600_000) * 3600_000).toISOString(); }
function toLocalInput(iso: string): string { return iso.slice(0, 16); }

export default function Logs() {
  const qc = useQueryClient();
  const logs = useLogs();
  const [now] = useState(() => new Date());
  const availability = useAvailability(isoHour(new Date(now.getTime() - 48 * 3600_000)), isoHour(new Date(now.getTime() + 3600_000)));
  const [winFrom, setWinFrom] = useState(toLocalInput(new Date(now.getTime() - 24 * 3600_000).toISOString()));
  const [winTo, setWinTo] = useState(toLocalInput(now.toISOString()));
  const keep = useMutation({ mutationFn: ({ name, value }: { name: string; value: boolean }) => patch<LogFile>(`/api/logs/${name}`, { keep: value }), onSuccess: (f) => { toast.success(f.keep ? `${f.name} will never be pruned` : `${f.name} can be pruned`); qc.invalidateQueries({ queryKey: ["logs"] }); }, onError: (e) => toast.error(String(e)) });
  const remove = useMutation({ mutationFn: ({ name, force }: { name: string; force: boolean }) => del(`/api/logs/${name}${force ? "?force=1" : ""}`), onSuccess: () => { toast.success("Log deleted"); qc.invalidateQueries({ queryKey: ["logs"] }); }, onError: (e) => toast.error(String(e)) });
  const files = [...(logs.data?.files ?? [])].sort((a, b) => (a.hour_utc < b.hour_utc ? 1 : -1));
  const newest = logs.data?.files.reduce<LogFile | null>((m, f) => (!m || f.hour_utc > m.hour_utc ? f : m), null);
  const windowUrl = `/api/logs/window?from=${encodeURIComponent(`${winFrom}:00Z`)}&to=${encodeURIComponent(`${winTo}:00Z`)}`;
  return (
    <>
      <PageHeader title="Logs">{logs.data ? <span className="num text-ink-2">{fmtBytes(logs.data.total_bytes)} in {logs.data.hours} hours · {logs.data.disk_free_gb.toFixed(1)} GB free</span> : null}</PageHeader>
      <div className="grid grid-cols-12 gap-4">
        <Panel className="col-span-12 lg:col-span-8" title="Last 48 hours">
          {availability.data ? <AvailabilityStrip slots={availability.data} onSelect={(h) => { setWinFrom(toLocalInput(h.hour_utc)); setWinTo(toLocalInput(new Date(Date.parse(h.hour_utc) + 3600_000).toISOString())); }} /> : <p className="text-ink-2">Loading…</p>}
          <p className="mt-2 text-[12px] leading-4 text-ink-2">Brass = complete hour, grey = partial (being written or recovered), empty = missing. Click an hour to load it into the window below.</p>
        </Panel>
        <Panel className="col-span-12 lg:col-span-4" title="Download a raw window">
          <div className="flex flex-col gap-2">
            <div><Label htmlFor="win-from">From (UTC)</Label><Input id="win-from" type="datetime-local" value={winFrom} onChange={(e) => setWinFrom(e.target.value)} /></div>
            <div><Label htmlFor="win-to">To (UTC)</Label><Input id="win-to" type="datetime-local" value={winTo} onChange={(e) => setWinTo(e.target.value)} /></div>
            <Button asChild><a href={windowUrl} download>Download .ubx</a></Button>
            <p className="text-[12px] leading-4 text-ink-2">Concatenates every hourly file overlapping the window. Convert with RTKLIB convbin for PPP or PPK.</p>
          </div>
        </Panel>
        <Panel className="col-span-12" title="Files" bodyClassName="p-2">
          <table className="w-full text-[14px]">
            <thead><tr className="border-b border-line text-left text-ink-2"><th className="py-1.5 pr-3 font-medium">File</th><th className="py-1.5 pr-3 font-medium">Hour (UTC)</th><th className="py-1.5 pr-3 text-right font-medium">Size</th><th className="py-1.5 pr-3 text-right font-medium">RAWX epochs</th><th className="py-1.5 pr-3 font-medium">State</th><th className="py-1.5 pr-3 font-medium">Keep</th><th className="py-1.5 pr-3 font-medium"></th></tr></thead>
            <tbody>
              {files.map((f) => (
                <tr key={f.name} className="border-b border-line/60 last:border-0">
                  <td className="num py-1.5 pr-3">{f.name}</td>
                  <td className="num py-1.5 pr-3">{fmtUtcDate(f.hour_utc).slice(0, 16)}</td>
                  <td className="num py-1.5 pr-3 text-right">{fmtBytes(f.bytes)}</td>
                  <td className="num py-1.5 pr-3 text-right">{f.msg_counts["RXM-RAWX"] ?? 0}</td>
                  <td className="py-1.5 pr-3">{f.complete ? "complete" : <span className="text-status-warning">writing</span>}</td>
                  <td className="py-1.5 pr-3"><Switch aria-label={`Keep ${f.name}`} checked={f.keep} onCheckedChange={(v) => keep.mutate({ name: f.name, value: v })} /></td>
                  <td className="py-1.5 pr-3 text-right">
                    <div className="flex justify-end gap-1">
                      <Button size="sm" variant="outline" asChild><a href={`/api/logs/${f.name}`} download>Download</a></Button>
                      <ConfirmDialog trigger={<Button size="sm" variant="ghost">Delete</Button>} title={`Delete ${f.name}?`} body={f === newest ? "This is the newest file and is probably still being written." : "Raw data for this hour cannot be recovered."} confirmLabel="Delete" destructive onConfirm={() => remove.mutateAsync({ name: f.name, force: f === newest }).then(() => undefined)} />
                    </div>
                  </td>
                </tr>
              ))}
              {files.length === 0 ? <tr><td colSpan={7} className="py-6 text-center text-ink-3">No raw logs yet.</td></tr> : null}
            </tbody>
          </table>
        </Panel>
      </div>
    </>
  );
}
```

- [ ] **Step 5: Write `web/src/pages/History.tsx`**

```tsx
import { useMemo } from "react";
import { PageHeader } from "@/app/PageHeader";
import { Panel } from "@/components/Panel";
import { EmptyState } from "@/components/EmptyState";
import { TimeSeries } from "@/components/charts/TimeSeries";
import { fmtAcc } from "@/lib/format";
import { useHistory } from "@/lib/queries";
import { usePref } from "@/lib/prefs";
import { cn } from "@/lib/utils";

export const HISTORY_METRICS: { key: string; label: string; unit: string; group: string; format: (v: number) => string; color?: string }[] = [
  { key: "h_acc_m", label: "Horizontal accuracy", unit: "m", group: "Position", format: (v) => fmtAcc(v) },
  { key: "v_acc_m", label: "Vertical accuracy", unit: "m", group: "Position", format: (v) => fmtAcc(v) },
  { key: "pdop", label: "PDOP", unit: "", group: "Position", format: (v) => v.toFixed(2) },
  { key: "nsat_used", label: "Satellites used", unit: "", group: "Satellites", format: (v) => v.toFixed(0), color: "var(--sys-gps)" },
  { key: "nsat_tracked", label: "Satellites tracked", unit: "", group: "Satellites", format: (v) => v.toFixed(0), color: "var(--sys-gps)" },
  { key: "cno_mean", label: "Mean C/N0", unit: "dB-Hz", group: "Satellites", format: (v) => v.toFixed(1), color: "var(--sys-galileo)" },
  { key: "jam_ind", label: "Jamming indicator", unit: "", group: "RF", format: (v) => v.toFixed(0), color: "var(--status-serious)" },
  { key: "agc_cnt", label: "AGC", unit: "", group: "RF", format: (v) => v.toFixed(0) },
  { key: "noise_per_ms", label: "Noise per ms", unit: "", group: "RF", format: (v) => v.toFixed(0) },
  { key: "rtcm_bytes_per_s", label: "RTCM output", unit: "B/s", group: "Corrections", format: (v) => v.toFixed(0), color: "var(--sys-glonass)" },
  { key: "ntrip_clients", label: "NTRIP clients", unit: "", group: "Corrections", format: (v) => v.toFixed(0), color: "var(--sys-glonass)" },
  { key: "cpu_pct", label: "CPU", unit: "%", group: "System", format: (v) => v.toFixed(0), color: "var(--ink-2)" },
  { key: "mem_pct", label: "Memory", unit: "%", group: "System", format: (v) => v.toFixed(0), color: "var(--ink-2)" },
  { key: "disk_free_gb", label: "Disk free", unit: "GB", group: "System", format: (v) => v.toFixed(1), color: "var(--ink-2)" },
  { key: "temp_c", label: "Temperature", unit: "°C", group: "System", format: (v) => v.toFixed(0), color: "var(--status-serious)" },
];
const RANGES = [{ label: "1 h", s: 3600 }, { label: "6 h", s: 6 * 3600 }, { label: "24 h", s: 86400 }, { label: "7 d", s: 7 * 86400 }, { label: "90 d", s: 90 * 86400 }];

export default function History() {
  const [rangeS, setRangeS] = usePref("historyRange", 86400);
  const [selected, setSelected] = usePref<string[]>("historyMetrics", ["h_acc_m", "nsat_used", "cno_mean"]);
  const to = useMemo(() => new Date(), [rangeS, selected]); // eslint-disable-line react-hooks/exhaustive-deps
  const from = new Date(to.getTime() - rangeS * 1000);
  const data = useHistory(selected, from.toISOString(), to.toISOString(), "auto");
  const toggle = (k: string) => setSelected(selected.includes(k) ? selected.filter((s) => s !== k) : [...selected, k]);
  const groups = [...new Set(HISTORY_METRICS.map((m) => m.group))];
  const column = (key: string) => {
    if (!data.data) return [];
    const cols = data.data.columns;
    const idx = cols.indexOf(key) !== -1 ? cols.indexOf(key) : cols.indexOf(`${key}_avg`);
    return idx === -1 ? [] : data.data.rows.map((r) => ({ t: r[0] as number, v: r[idx] }));
  };
  return (
    <>
      <PageHeader title="History">
        <div className="flex gap-1">{RANGES.map((r) => <button key={r.s} type="button" onClick={() => setRangeS(r.s)} className={cn("num rounded-full border px-3 py-1 text-[14px]", rangeS === r.s ? "border-brass text-ink" : "border-line text-ink-2")}>{r.label}</button>)}</div>
      </PageHeader>
      <div className="grid grid-cols-12 gap-4">
        <Panel className="col-span-12 lg:col-span-3" title="Metrics">
          {groups.map((g) => (
            <div key={g} className="mb-3"><p className="mb-1 text-ink-2">{g}</p>
              {HISTORY_METRICS.filter((m) => m.group === g).map((m) => (
                <label key={m.key} className="flex items-center gap-2 py-0.5 text-[14px]"><input type="checkbox" checked={selected.includes(m.key)} onChange={() => toggle(m.key)} />{m.label}</label>
              ))}
            </div>
          ))}
        </Panel>
        <div className="col-span-12 flex flex-col gap-4 lg:col-span-9">
          {selected.length === 0 ? <EmptyState title="Pick a metric" body="Each metric gets its own chart so the scales stay honest." /> : null}
          {data.data ? <p className="num text-[12px] leading-4 text-ink-2">{data.data.rows.length} samples at {data.data.res === "1s" ? "1 second" : "1 minute averages"}</p> : null}
          {selected.map((k) => { const m = HISTORY_METRICS.find((x) => x.key === k)!; return <Panel key={k}><TimeSeries points={column(k)} label={m.label} unit={m.unit} format={m.format} color={m.color} /></Panel>; })}
        </div>
      </div>
    </>
  );
}
```
Register `logs` and `history` routes.

- [ ] **Step 6: Run, build, commit**

`cd web && pnpm test && pnpm lint && pnpm build`; then
```bash
cd /home/nekosaif/github/mtrtk && git add web && git commit -m "feat(web): logs page with availability strip, window download and keep/delete; history charts

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: Events, Settings and Login pages

**Files:**
- Create: `web/src/pages/Events.tsx`, `web/src/pages/Settings.tsx`, `web/src/pages/Login.tsx`, `web/src/components/ThemeToggle.tsx`, `web/src/pages/Settings.test.tsx`, `web/src/pages/Events.test.tsx`
- Modify: `web/src/app/router.tsx`, `web/src/app/Rail.tsx` (theme toggle at the rail's foot), `web/src/main.tsx` (apply saved theme before first paint)

**Interfaces:**
- Produces: `Events` page — level filter (all / info / warning / error), live prepend from `useLive().events`, ack button, `meta` expandable; `Settings` page — grouped form generated from `GET /api/config` (`SETTINGS_GROUPS` maps keys → group, label, control type: text / number / select / toggle / secret), secrets shown as password inputs prefilled `***`, unchanged secrets are sent back as `***`, `PUT /api/config` result toast, restart banner with a "Restart now" button (`POST /api/restart`) when `restart_required`; receiver profile summary; theme selector; `Login` page — password form → `POST /api/login` then navigate to `?next`; `ThemeToggle` persists `theme` pref and sets `document.documentElement.dataset.theme`.

- [ ] **Step 1: Write the failing tests**

`web/src/pages/Events.test.tsx`:
```tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { useLive } from "@/lib/live";
import Events from "./Events";

const rows = [
  { id: 2, ts_utc: "2026-09-18T16:40:00+00:00", level: "warning", kind: "jamming", message: "RF interference: jam_ind=210", meta: { jam_ind: 210 }, acked: false },
  { id: 1, ts_utc: "2026-09-18T16:00:00+00:00", level: "info", kind: "survey_in_valid", message: "survey-in complete", meta: {}, acked: true },
];

describe("Events page", () => {
  beforeEach(() => {
    useLive.setState({ events: [] });
    globalThis.fetch = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      const p = String(url);
      if (p.includes("level=warning")) return new Response(JSON.stringify([rows[0]]), { status: 200 });
      if (p.includes("/api/events") && !init?.method) return new Response(JSON.stringify(rows), { status: 200 });
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    }) as typeof fetch;
  });

  it("lists events, filters by level and acknowledges", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={qc}><MemoryRouter><Events /></MemoryRouter></QueryClientProvider>);
    expect(await screen.findByText(/RF interference/)).toBeInTheDocument();
    expect(screen.getByText(/survey-in complete/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /^warning$/i }));
    expect(await screen.findByText(/RF interference/)).toBeInTheDocument();
    expect(screen.queryByText(/survey-in complete/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /acknowledge/i }));
    const calls = (globalThis.fetch as unknown as { mock: { calls: [string, RequestInit][] } }).mock.calls;
    expect(calls.some(([u, i]) => String(u).endsWith("/api/events/2/ack") && i?.method === "POST")).toBe(true);
  });

  it("prepends live events", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={qc}><MemoryRouter><Events /></MemoryRouter></QueryClientProvider>);
    await screen.findByText(/RF interference/);
    useLive.setState({ events: [{ id: 3, ts_utc: "2026-09-18T16:50:00+00:00", level: "error", kind: "receiver_disconnected", message: "receiver disconnected: unplugged", meta: {}, acked: false }] });
    expect(await screen.findByText(/receiver disconnected/)).toBeInTheDocument();
  });
});
```

`web/src/pages/Settings.test.tsx`:
```tsx
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import Settings from "./Settings";

const config = { values: { role: "base", station_id: "MTRK", rtcm_msm: 7, ntrip_password: "***", web_password: null, min_free_gb: 5, base_mode: "survey-in", receiver_strict: true, ntrip_bind: "tailscale", log_messages: ["RXM-RAWX", "RXM-SFRBX"] }, env_file: "/app/.env", secret_keys: ["ntrip_password", "web_password", "alert_webhook_url", "tunnel_token"], live_keys: ["base_mode", "svin_min_duration_s", "svin_acc_limit_m", "active_site"] };
let calls: [string, RequestInit | undefined][] = [];

describe("Settings page", () => {
  beforeEach(() => {
    calls = [];
    globalThis.fetch = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      calls.push([String(url), init]);
      if (String(url).endsWith("/api/config") && !init?.method) return new Response(JSON.stringify(config), { status: 200 });
      if (init?.method === "PUT") return new Response(JSON.stringify({ changed: ["station_id"], restart_required: true }), { status: 200 });
      return new Response("{}", { status: 200 });
    }) as typeof fetch;
  });

  it("renders grouped settings, keeps secrets masked and saves changes", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={qc}><MemoryRouter><Settings /></MemoryRouter></QueryClientProvider>);
    const station = await screen.findByLabelText(/station id/i);
    expect(station).toHaveValue("MTRK");
    expect(screen.getByLabelText(/ntrip password/i)).toHaveValue("***");
    await userEvent.clear(station);
    await userEvent.type(station, "BASE");
    await userEvent.click(screen.getByRole("button", { name: /save changes/i }));
    const put = calls.find(([, i]) => i?.method === "PUT")!;
    const body = JSON.parse(put[1]!.body as string) as { values: Record<string, unknown> };
    expect(body.values.station_id).toBe("BASE");
    expect(body.values.ntrip_password).toBe("***");
    const banner = await screen.findByRole("alert");
    expect(banner).toHaveTextContent(/restart/i);
    await userEvent.click(within(banner).getByRole("button", { name: /restart now/i }));
    expect(calls.some(([u, i]) => u.endsWith("/api/restart") && i?.method === "POST")).toBe(true);
  });
});
```

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Write `ThemeToggle` and apply the theme early**

`web/src/components/ThemeToggle.tsx`:
```tsx
import { Moon, Sun } from "lucide-react";
import { usePref } from "@/lib/prefs";
import { Button } from "@/components/ui/button";

export type Theme = "dark" | "light";

export function applyTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme;
}

export function ThemeToggle() {
  const [theme, setTheme] = usePref<Theme>("theme", "dark");
  const next: Theme = theme === "dark" ? "light" : "dark";
  return (
    <Button variant="ghost" size="sm" aria-label={`Switch to ${next} theme`} onClick={() => { setTheme(next); applyTheme(next); }}>
      {theme === "dark" ? <Sun className="size-4" aria-hidden /> : <Moon className="size-4" aria-hidden />}
      <span className="rail-label">{theme === "dark" ? "Light theme" : "Dark theme"}</span>
    </Button>
  );
}
```
In `web/src/main.tsx`, before `createRoot`: `import { getPref } from "./lib/prefs"; import { applyTheme } from "./components/ThemeToggle"; applyTheme(getPref("theme", "dark"));`. In `Rail.tsx`, add `<div className="border-t border-line p-2"><ThemeToggle /></div>` after the `<ul>`.

- [ ] **Step 4: Write `web/src/pages/Events.tsx`**

```tsx
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { PageHeader } from "@/app/PageHeader";
import { Panel } from "@/components/Panel";
import { StatusBadge } from "@/components/StatusBadge";
import { Button } from "@/components/ui/button";
import { post } from "@/lib/api";
import { fmtUtcDate, relTime } from "@/lib/format";
import { useLive } from "@/lib/live";
import { useEvents } from "@/lib/queries";
import { levelForEvent } from "@/lib/status";
import type { EventItem, Level } from "@/lib/types";
import { cn } from "@/lib/utils";

const LEVELS: { value: Level | undefined; label: string }[] = [{ value: undefined, label: "All" }, { value: "info", label: "Info" }, { value: "warning", label: "Warning" }, { value: "error", label: "Error" }];

export default function Events() {
  const [level, setLevel] = useState<Level | undefined>(undefined);
  const qc = useQueryClient();
  const query = useEvents(level);
  const live = useLive((s) => s.events);
  const ack = useMutation({ mutationFn: (id: number) => post(`/api/events/${id}/ack`), onSuccess: () => qc.invalidateQueries({ queryKey: ["events"] }) });
  const fetched = query.data ?? [];
  const merged: EventItem[] = [...live.filter((e) => (!level || e.level === level) && !fetched.some((f) => f.id === e.id)), ...fetched];
  return (
    <>
      <PageHeader title="Events">
        <div className="flex gap-1">{LEVELS.map((l) => <button key={l.label} type="button" onClick={() => setLevel(l.value)} className={cn("rounded-full border px-3 py-1 text-[14px]", level === l.value ? "border-brass text-ink" : "border-line text-ink-2")}>{l.label}</button>)}</div>
      </PageHeader>
      <Panel bodyClassName="p-0">
        {merged.length === 0 ? <p className="p-6 text-center text-ink-3">No events{level ? ` at level ${level}` : ""}.</p> : (
          <ul>
            {merged.map((e) => (
              <li key={e.id} className={cn("flex flex-wrap items-start gap-3 border-b border-line/60 px-4 py-3 last:border-0", e.acked && "opacity-60")}>
                <StatusBadge level={levelForEvent(e.level)} label={e.level} className="min-w-[96px]" />
                <div className="min-w-0 flex-1">
                  <p>{e.message}</p>
                  <p className="num text-[12px] leading-4 text-ink-2" title={fmtUtcDate(e.ts_utc)}>{e.kind} · {relTime(e.ts_utc)}</p>
                  {Object.keys(e.meta).length ? <details className="text-[12px] leading-4 text-ink-2"><summary className="cursor-pointer">details</summary><pre className="mt-1 whitespace-pre-wrap font-mono">{JSON.stringify(e.meta, null, 2)}</pre></details> : null}
                </div>
                {!e.acked ? <Button size="sm" variant="outline" onClick={() => ack.mutate(e.id)}>Acknowledge</Button> : <span className="text-[12px] text-ink-3">acknowledged</span>}
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </>
  );
}
```

- [ ] **Step 5: Write `web/src/pages/Settings.tsx`**

```tsx
import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { PageHeader } from "@/app/PageHeader";
import { Panel } from "@/components/Panel";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { post, put } from "@/lib/api";
import { useConfig } from "@/lib/queries";

type Control = "text" | "number" | "toggle" | "secret" | "select" | "list";
interface Field { key: string; label: string; control: Control; options?: string[]; help?: string }
interface Group { title: string; fields: Field[] }

export const SETTINGS_GROUPS: Group[] = [
  { title: "Station", fields: [
    { key: "role", label: "Role", control: "select", options: ["base", "rover"] },
    { key: "station_id", label: "Station ID", control: "text", help: "4 upper-case characters; used in file names and RINEX headers" },
    { key: "marker_name", label: "Marker name", control: "text" },
    { key: "country", label: "Country code", control: "text" },
    { key: "antenna_type", label: "Antenna type", control: "text", help: "IGS code or NONE" },
    { key: "antenna_height_m", label: "Antenna height above mark (m)", control: "number" },
    { key: "observer", label: "Observer", control: "text" }, { key: "agency", label: "Agency", control: "text" },
  ] },
  { title: "Receiver", fields: [
    { key: "mtrtk_source", label: "Receiver device", control: "text", help: "auto, a serial device, or file:<path>" },
    { key: "baud", label: "Baud rate", control: "number" },
    { key: "receiver_strict", label: "Fail startup if a core setting is rejected", control: "toggle" },
    { key: "rtcm_msm", label: "RTCM observation format", control: "select", options: ["7", "4"], help: "MSM7 carries Doppler and full-precision phase; MSM4 is smaller" },
    { key: "rtcm_1230_rate", label: "RTCM 1230 interval (s)", control: "number" },
    { key: "rtcm_station_id", label: "RTCM station ID", control: "number" },
  ] },
  { title: "Corrections (NTRIP caster)", fields: [
    { key: "ntrip_bind", label: "Bind to", control: "text", help: "tailscale, lan, all or an IP" },
    { key: "ntrip_port", label: "Port", control: "number" },
    { key: "mountpoint", label: "Mountpoint", control: "text" },
    { key: "ntrip_user", label: "Rover username", control: "text" },
    { key: "ntrip_password", label: "NTRIP password", control: "secret", help: "empty = anonymous access" },
  ] },
  { title: "Web UI", fields: [
    { key: "web_bind", label: "Bind to", control: "text" }, { key: "web_port", label: "Port", control: "number" },
    { key: "web_password", label: "Web password", control: "secret", help: "required when the UI is reachable beyond Tailscale" },
  ] },
  { title: "Raw logging", fields: [
    { key: "log_messages", label: "Messages to log", control: "list" },
    { key: "min_free_gb", label: "Keep at least this much disk free (GB)", control: "number" },
    { key: "fsync_interval_s", label: "fsync interval (s)", control: "number" },
  ] },
  { title: "Alerts", fields: [{ key: "alert_webhook_url", label: "Webhook URL", control: "secret", help: "ntfy, Discord or Telegram-compatible JSON POST" }] },
];

export default function Settings() {
  const config = useConfig();
  const qc = useQueryClient();
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [restartNeeded, setRestartNeeded] = useState(false);
  useEffect(() => { if (config.data) setDraft(config.data.values); }, [config.data]);
  const save = useMutation({
    mutationFn: (values: Record<string, unknown>) => put<{ changed: string[]; restart_required: boolean }>("/api/config", { values }),
    onSuccess: (r) => { toast.success(r.changed.length ? `Saved ${r.changed.join(", ")}` : "No changes"); if (r.restart_required) setRestartNeeded(true); qc.invalidateQueries({ queryKey: ["config"] }); },
    onError: (e) => toast.error(String(e)),
  });
  const restart = useMutation({ mutationFn: () => post("/api/restart"), onSuccess: () => toast.success("Restarting; the page reconnects automatically") });
  if (!config.data) return <><PageHeader title="Settings" /><p className="text-ink-2">Loading…</p></>;
  const changed = Object.keys(draft).filter((k) => JSON.stringify(draft[k]) !== JSON.stringify(config.data!.values[k]));
  const set = (k: string, v: unknown) => setDraft({ ...draft, [k]: v });
  const render = (f: Field) => {
    const v = draft[f.key];
    const id = `s-${f.key}`;
    switch (f.control) {
      case "toggle": return <Switch id={id} checked={Boolean(v)} onCheckedChange={(c) => set(f.key, c)} aria-label={f.label} />;
      case "select": return <select id={id} value={String(v ?? "")} onChange={(e) => set(f.key, /^\d+$/.test(e.target.value) ? Number(e.target.value) : e.target.value)} className="rounded-md border border-line bg-panel-2 px-2 py-1.5 text-[14px]">{f.options!.map((o) => <option key={o} value={o}>{o}</option>)}</select>;
      case "number": return <Input id={id} inputMode="decimal" value={v == null ? "" : String(v)} onChange={(e) => set(f.key, e.target.value === "" ? null : Number(e.target.value))} />;
      case "secret": return <Input id={id} type="password" autoComplete="off" value={v == null ? "" : String(v)} onChange={(e) => set(f.key, e.target.value)} placeholder="not set" />;
      case "list": return <Input id={id} value={Array.isArray(v) ? v.join(",") : String(v ?? "")} onChange={(e) => set(f.key, e.target.value.split(",").map((s) => s.trim()).filter(Boolean))} />;
      default: return <Input id={id} value={v == null ? "" : String(v)} onChange={(e) => set(f.key, e.target.value)} />;
    }
  };
  return (
    <>
      <PageHeader title="Settings"><span className="num text-[12px] text-ink-2">{config.data.env_file}</span></PageHeader>
      {restartNeeded ? (
        <div role="alert" className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-md border border-status-warning px-4 py-3">
          <span>Saved to .env. Most settings take effect after a restart.</span>
          <Button size="sm" onClick={() => restart.mutate()}>Restart now</Button>
        </div>
      ) : null}
      <form onSubmit={(e) => { e.preventDefault(); save.mutate(draft); }} className="grid grid-cols-12 gap-4">
        {SETTINGS_GROUPS.map((g) => (
          <Panel key={g.title} className="col-span-12 lg:col-span-6" title={g.title}>
            <div className="flex flex-col gap-3">
              {g.fields.filter((f) => f.key in config.data!.values).map((f) => (
                <div key={f.key} className="grid grid-cols-[1fr_minmax(160px,220px)] items-center gap-3 max-sm:grid-cols-1">
                  <div><Label htmlFor={`s-${f.key}`}>{f.label}</Label>{f.help ? <p className="text-[12px] leading-4 text-ink-2">{f.help}</p> : null}</div>
                  {render(f)}
                </div>
              ))}
            </div>
          </Panel>
        ))}
        <div className="col-span-12 flex items-center justify-end gap-3">
          <span className="text-ink-2">{changed.length ? `${changed.length} change${changed.length === 1 ? "" : "s"}` : "No changes"}</span>
          <Button type="submit" disabled={changed.length === 0 || save.isPending}>Save changes</Button>
        </div>
      </form>
    </>
  );
}
```
The whole draft is sent on save: the API ignores unchanged values and treats `"***"` secrets as "leave unchanged", so masked secrets round-trip safely.

- [ ] **Step 6: Write `web/src/pages/Login.tsx`**

```tsx
import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ApiError, post } from "@/lib/api";

export default function Login() {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await post("/api/login", { password });
      navigate(params.get("next") || "/", { replace: true });
      window.location.reload(); // re-open the websocket with the new cookie
    } catch (err) {
      setError(err instanceof ApiError && err.status === 401 ? "That password is not right." : String(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <main className="flex min-h-full items-center justify-center p-6">
      <form onSubmit={submit} className="panel flex w-full max-w-sm flex-col gap-4 p-6">
        <div><span className="display text-[40px] leading-[44px]">mtrtk</span><p className="text-ink-2">This base station is password protected.</p></div>
        <div><Label htmlFor="pw">Password</Label><Input id="pw" type="password" autoFocus value={password} onChange={(e) => setPassword(e.target.value)} /></div>
        {error ? <p className="text-status-critical">{error}</p> : null}
        <Button type="submit" disabled={busy || !password}>Sign in</Button>
      </form>
    </main>
  );
}
```
Register `events`, `settings` and `/login` routes with the real components.

- [ ] **Step 7: Run, build, commit**

`cd web && pnpm test && pnpm lint && pnpm build`; then
```bash
cd /home/nekosaif/github/mtrtk && git add web && git commit -m "feat(web): events feed, settings editor with restart banner, login page, theme toggle

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: Integration, visual QA, docs

**Files:**
- Create: `docs/ui.md`
- Modify: `README.md`, `.github/workflows/ci.yml` (add `pnpm --dir web test`), `docs/superpowers/specs/2026-09-18-mtrtk-design.md` (record the design system decisions under *frontend*)

- [ ] **Step 1: Full build through Docker**

```bash
cd /home/nekosaif/github/mtrtk
docker build -f docker/Dockerfile -t mtrtk:dev .
docker run --rm -e NTRIP_PASSWORD= -e WEB_BIND=lan -e WEB_ALLOW_INSECURE=1 -e REPLAY_LOOP=1 --network host -v "$PWD/tests/fixtures:/fx:ro" mtrtk:dev replay /fx/f9p_hpg113_base_30s.ubx --loop
```
Open `http://localhost:8080/` — the built SPA is served by FastAPI; every page loads; the tape updates each second; Satellites and Receiver populate from the fixture; Corrections shows RTCM types; Site shows "base mode manager not running" (replay); Logs shows no files (replay does not log by default).

- [ ] **Step 2: Visual and responsive QA**

With the Playwright MCP (or a browser): screenshot `/`, `/satellites`, `/receiver` at 1440×900 and 390×844 (phone). Check: no horizontal scroll at 390 px, rail collapses to icons then bottom bar, fonts are Instrument Sans/Serif (inspect computed font-family), brass rings visible, every chart has a legend or single-series title, hover tooltips work on sparklines/time series/spectrum/sky plot, focus ring visible when tabbing. Toggle the light theme and re-check contrast of chips and badges. Fix anything found in this task (small CSS/props changes only) and note them in the report.

- [ ] **Step 3: Live receiver QA**

`uv run mtrtk base` with the F9P attached; browse from another tailnet device to `http://<tailscale-ip>:8080/`. Confirm satellites glide on the sky plot between epochs, the survey-in card counts up, the Corrections page shows 1005/MSM rates once survey-in is valid, and the Receiver page's reset dialog cold-resets the receiver and the UI shows "Receiver disconnected" then recovers.

- [ ] **Step 4: CI and docs**

Add to the `web` job in `.github/workflows/ci.yml`: `- run: pnpm --dir web test`. Write `docs/ui.md`:
```markdown
# Web UI

Served by the daemon at `http://<bind-host>:8080/` (Tailscale IP by default). Dark theme by default; toggle at the bottom of the rail.

Pages: Dashboard (coordinates, sky plot, map, summaries), Satellites (sky / signals / table with system filters), Receiver (RF health, spectrum, firmware, time, actions), Corrections (RTCM rates, NTRIP caster and clients), Site (survey-in, fixed sites, PPP steps), Logs (availability, downloads, keep/delete), History (per-metric charts), Events, Settings.

## Development
```bash
uv run mtrtk replay tests/fixtures/f9p_hpg113_base_30s.ubx --loop   # backend on :8080 (set WEB_BIND=lan WEB_ALLOW_INSECURE=1 NTRIP_PASSWORD=)
pnpm --dir web dev                                                   # Vite on :5173, proxies /api and /ws
pnpm --dir web test                                                  # vitest
```
The production bundle (`pnpm --dir web build`) is copied into the Docker image; the daemon serves it with SPA fallback.

## Design tokens
Colors, fonts and chart palette are defined once in `web/src/index.css` (`:root` and `:root[data-theme="light"]`). Constellation colours are fixed (GPS blue, GLONASS orange, Galileo green, BeiDou yellow, QZSS magenta, SBAS violet) and validated for colour-vision deficiency.
```
Update `README.md` *Status*: `Phase 4 (web UI) complete: dashboard, satellites, receiver, corrections, site, logs, history, events, settings. Next: RINEX export + PPP import (Phase 5).`
In the spec's *frontend* section, add one paragraph: "Design system (Phase 4): deep-navy dark theme with brass accent, Instrument Sans/Serif, fixed constellation palette validated with the dataviz checker; see plan 2026-09-19-phase4-frontend.md."

- [ ] **Step 5: Commit and tag**

```bash
git add docs/ui.md README.md .github/workflows/ci.yml docs/superpowers/specs/2026-09-18-mtrtk-design.md web
git commit -m "docs(web): UI guide, CI tests for the frontend, Phase 4 status

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git tag -a v0.4.0-phase4 -m "Phase 4: base station web UI"
```

Phase 5 (RINEX export + PPP import) is the next plan: `docs/superpowers/plans/2026-09-19-phase5-rinex-ppp.md`.
