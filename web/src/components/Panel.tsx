import { useId, type ReactNode } from "react";
import { cn } from "@/lib/utils";

/**
 * A titled section: `h2` over a 1px rule, an optional action slot at the right of the title
 * (links, a small button), the body padded 16px. Grid placement comes from `className`;
 * `bodyClassName="p-0"` for content that fills the panel (a map). A titled panel is labelled by
 * its heading, so it is a named region for assistive tech (and for `getByRole("region")`).
 */
export function Panel({
  title,
  actions,
  children,
  className,
  bodyClassName,
  as: Tag = "section",
}: {
  title?: string;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
  as?: "section" | "article" | "div";
}) {
  const headingId = useId();
  return (
    <Tag className={cn("panel flex min-w-0 flex-col", className)} aria-labelledby={title ? headingId : undefined}>
      {title ? (
        <header className="flex items-center justify-between gap-3 border-b border-line px-4 py-2.5">
          <h2 id={headingId} className="text-[16px] leading-6 font-medium">{title}</h2>
          {actions ? <div className="flex items-center gap-3 text-[12px] leading-4">{actions}</div> : null}
        </header>
      ) : null}
      <div className={cn("min-w-0 flex-1 p-4", bodyClassName)}>{children}</div>
    </Tag>
  );
}
