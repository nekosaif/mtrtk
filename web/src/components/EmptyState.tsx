import type { ReactNode } from "react";

/** Nothing to show yet, said plainly: a title, an optional sentence, an optional action. */
export function EmptyState({ title, body, action }: { title: string; body?: string; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-start gap-2 rounded-md border border-dashed border-line p-6 text-ink-2">
      <p className="text-ink">{title}</p>
      {body ? <p className="max-w-[60ch]">{body}</p> : null}
      {action}
    </div>
  );
}
