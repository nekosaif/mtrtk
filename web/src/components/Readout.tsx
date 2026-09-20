import { cn } from "@/lib/utils";

/** A labelled figure: small label above, tabular value, the unit one step smaller in `--ink-2`. */
export function Readout({
  label,
  value,
  unit,
  size = "md",
  className,
}: {
  label: string;
  value: string;
  unit?: string;
  size?: "sm" | "md" | "lg";
  className?: string;
}) {
  const valueClass = size === "lg" ? "text-[28px] leading-[34px]" : size === "sm" ? "text-[14px] leading-5" : "text-[20px] leading-7";
  return (
    <div className={cn("flex min-w-0 flex-col gap-0.5", className)}>
      <span className="text-[12px] leading-4 text-ink-2">{label}</span>
      <span className={cn("num", valueClass)}>
        {value}
        {unit ? <span className="ml-1 text-[0.7em] text-ink-2">{unit}</span> : null}
      </span>
    </div>
  );
}
