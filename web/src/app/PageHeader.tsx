import { useEffect, type ReactNode } from "react";

/** Page title in the display serif; also sets the browser tab title. */
export function PageHeader({ title, children }: { title: string; children?: ReactNode }) {
  useEffect(() => {
    document.title = `${title} · mtrtk`;
  }, [title]);
  return (
    <header className="mb-4 flex flex-wrap items-end justify-between gap-4">
      <h1 className="display text-[40px] leading-[44px] max-sm:text-[28px] max-sm:leading-[34px]">{title}</h1>
      {children ? <div className="flex items-center gap-2">{children}</div> : null}
    </header>
  );
}
