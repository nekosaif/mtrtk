import { Outlet } from "react-router";
import { Rail } from "./Rail";
import { Tape } from "./Tape";

/**
 * App frame: rail + tape + page. The rail and the tape stay put; only the page
 * scrolls. Columns: 220 px rail ≥1024 px, 64 px icon rail 640–1023 px; below
 * 640 px the rail becomes a bottom tab bar and the grid turns into two rows.
 */
export function Shell() {
  return (
    <div
      data-slot="shell"
      className="grid h-full grid-cols-[220px_1fr] max-lg:grid-cols-[64px_1fr] max-sm:grid-cols-1 max-sm:grid-rows-[minmax(0,1fr)_auto]"
    >
      <aside className="min-h-0 overflow-y-auto max-sm:order-2 max-sm:overflow-visible">
        <Rail />
      </aside>
      <div className="flex min-h-0 min-w-0 flex-col">
        <Tape />
        <main className="min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-[1440px] px-6 py-5 max-sm:px-4">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  );
}
