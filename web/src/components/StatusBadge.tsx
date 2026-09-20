import { AlertTriangle, CheckCircle2, CircleDashed, XCircle } from "lucide-react";
import { STATUS, type StatusLevel } from "@/lib/palette";
import { cn } from "@/lib/utils";

// Status colour is never the only signal: every level has its own icon next to the word.
const ICON = { good: CheckCircle2, warning: CircleDashed, serious: AlertTriangle, critical: XCircle } as const;

export function StatusBadge({ level, label, className }: { level: StatusLevel; label: string; className?: string }) {
  const Icon = ICON[level];
  return (
    <span
      className={cn("inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[14px]", className)}
      style={{ borderColor: STATUS[level], color: "var(--ink)" }}
      data-level={level}
    >
      <Icon className="size-3.5" style={{ color: STATUS[level] }} aria-hidden />
      {label}
    </span>
  );
}
