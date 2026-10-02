import { Outlet } from "react-router";
import { Rail } from "./Rail";
import { Tape } from "./Tape";

/**
 * App frame: rail + tape + page. The rail and the tape stay put; only the page
 * scrolls. Columns: 220 px rail ≥1024 px, 64 px icon rail 640–1023 px; below
 * 640 px the rail becomes a bottom tab bar and the grid turns into two rows.
 *
 * The first Tab stop is a "Skip to content" link (visible only while focused) past the rail and
 * the tape to `<main>`. The rail stays first in the DOM at every width: on a phone it is painted
 * at the bottom, as tab bars are, but moving it after the page in the source would make every
 * desktop and tablet operator Tab through a whole page before reaching the navigation.
 */
export function Shell() {
  return (
    <div
      data-slot="shell"
      className="grid h-full grid-cols-[220px_1fr] max-lg:grid-cols-[64px_1fr] max-sm:grid-cols-1 max-sm:grid-rows-[minmax(0,1fr)_auto]"
    >
      <a
        href="#main"
        className="sr-only rounded-md bg-panel-2 px-3 py-2 text-ink focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-50"
      >
        Skip to content
      </a>
      <aside className="min-h-0 overflow-y-auto max-sm:order-2 max-sm:overflow-visible">
        <Rail />
      </aside>
      <div className="flex min-h-0 min-w-0 flex-col">
        <Tape />
        <main id="main" tabIndex={-1} className="min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-[1440px] px-6 py-5 max-sm:px-4">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  );
}
