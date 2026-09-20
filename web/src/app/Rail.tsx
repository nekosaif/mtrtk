import { NavLink } from "react-router";
import { Activity, Bell, ChartLine, Compass, Files, MapPin, Radio, Satellite, Settings } from "lucide-react";
import { cn } from "@/lib/utils";

export const NAV = [
  { to: "/", label: "Dashboard", icon: Activity },
  { to: "/satellites", label: "Satellites", icon: Satellite },
  { to: "/receiver", label: "Receiver", icon: Compass },
  { to: "/corrections", label: "Corrections", icon: Radio },
  { to: "/site", label: "Site", icon: MapPin },
  { to: "/logs", label: "Logs", icon: Files },
  { to: "/history", label: "History", icon: ChartLine },
  { to: "/events", label: "Events", icon: Bell },
  { to: "/settings", label: "Settings", icon: Settings },
] as const;

/**
 * Main navigation. ≥1024 px: 220 px rail with labels; 640–1023 px: 64 px icon
 * rail (labels stay in the accessible name via sr-only); <640 px: horizontal
 * bottom tab bar (the Shell moves it below the content).
 */
export function Rail() {
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
        <div className="mt-1 text-[12px] leading-4 text-ink-2 max-lg:hidden">base station</div>
      </div>
      <ul className="flex flex-1 flex-col gap-0.5 px-2 max-sm:flex-row max-sm:gap-0 max-sm:px-0">
        {NAV.map(({ to, label, icon: Icon }) => (
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
    </nav>
  );
}
