import { NavLink } from "react-router";
import { Activity, Bell, ChartLine, Compass, Crosshair, Files, Flag, MapPin, Radio, Route, Satellite, Settings } from "lucide-react";
import { useLive } from "@/lib/live";
import { SignOutButton, usePasswordConfigured } from "@/components/SignOutButton";
import { ThemeToggle } from "@/components/ThemeToggle";
import { cn } from "@/lib/utils";

export const NAV_BASE = [
  { to: "/", label: "Dashboard", icon: Activity },
  { to: "/satellites", label: "Satellites", icon: Satellite },
  { to: "/receiver", label: "Receiver", icon: Compass },
  { to: "/corrections", label: "Corrections", icon: Radio },
  { to: "/site", label: "Site", icon: MapPin },
  { to: "/logs", label: "Logs", icon: Files },
  { to: "/history", label: "History", icon: ChartLine },
  { to: "/ppk", label: "PPK", icon: Route },
  { to: "/events", label: "Events", icon: Bell },
  { to: "/settings", label: "Settings", icon: Settings },
] as const;

/** A rover has no caster or site to manage: RTK and Survey take Corrections' and Site's places. */
export const NAV_ROVER = [
  { to: "/", label: "Dashboard", icon: Activity },
  { to: "/satellites", label: "Satellites", icon: Satellite },
  { to: "/receiver", label: "Receiver", icon: Compass },
  { to: "/rtk", label: "RTK", icon: Crosshair },
  { to: "/survey", label: "Survey", icon: Flag },
  { to: "/logs", label: "Logs", icon: Files },
  { to: "/history", label: "History", icon: ChartLine },
  { to: "/ppk", label: "PPK", icon: Route },
  { to: "/events", label: "Events", icon: Bell },
  { to: "/settings", label: "Settings", icon: Settings },
] as const;

/** The base list: what the rail shows until the daemon's snapshot says which role it runs. */
export const NAV = NAV_BASE;

/**
 * Main navigation. ≥1024 px: 220 px rail with labels; 640–1023 px: 64 px icon
 * rail (labels stay in the accessible name via sr-only); <640 px: horizontal
 * bottom tab bar (the Shell moves it below the content). The list follows the daemon's role
 * (`NAV_ROVER` on a rover, `NAV_BASE` otherwise and until the snapshot arrives).
 */
export function Rail() {
  const passwordConfigured = usePasswordConfigured();
  const role = useLive((s) => s.role);
  const nav = role === "rover" ? NAV_ROVER : NAV_BASE;
  return (
    <nav
      aria-label="Main"
      className="flex h-full flex-col border-r border-line bg-panel max-sm:flex-row max-sm:border-r-0 max-sm:border-t max-sm:pb-[env(safe-area-inset-bottom)]"
    >
      <div className="px-4 py-5 max-lg:px-0 max-lg:text-center max-sm:hidden">
        <span className="display text-[28px] leading-none max-lg:hidden">mtrtk</span>
        <span className="display text-[28px] leading-none lg:hidden" aria-hidden>
          m
        </span>
        <div className="mt-1 text-[12px] leading-4 text-ink-2 max-lg:hidden">{role === "rover" ? "rover" : "base station"}</div>
      </div>
      <ul className="flex flex-1 flex-col gap-0.5 px-2 max-sm:flex-row max-sm:gap-0 max-sm:px-0">
        {nav.map(({ to, label, icon: Icon }) => (
          <li key={to} className="max-sm:min-w-0 max-sm:flex-1">
            <NavLink
              to={to}
              end={to === "/"}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-3 rounded-md px-3 py-2 text-[14px] text-ink-2 hover:bg-panel-2 hover:text-ink max-lg:justify-center max-sm:rounded-none max-sm:px-0 max-sm:py-3.5",
                  isActive &&
                    "bg-panel-2 text-ink shadow-[inset_3px_0_0_var(--brass)] max-sm:shadow-[inset_0_2px_0_var(--brass)]",
                )
              }
            >
              <Icon className="size-4 shrink-0" aria-hidden />
              <span className="rail-label max-lg:sr-only">{label}</span>
            </NavLink>
          </li>
        ))}
      </ul>
      {/* Below 640px the rail is a horizontal tab bar: the foot would fight it for width, so
          the theme and the session live on the Settings page there instead. */}
      <div className="flex flex-col gap-0.5 border-t border-line p-2 max-sm:hidden">
        <ThemeToggle />
        {passwordConfigured ? <SignOutButton /> : null}
      </div>
    </nav>
  );
}
