import { createContext, useContext, type ComponentProps } from "react";
import { cn } from "@/lib/utils";

const StaleContext = createContext(false);

/** True inside a `StaleScope` whose readings are stale: a status colour there would overstate them. */
export const useInStaleScope = () => useContext(StaleContext);

/**
 * The live page's stale rule in one place: when no epoch has arrived for 5 s (or the socket is
 * down) every `.num` inside greys to `--ink-3`, and `data-stale` says so for tests and styles.
 * The class alone cannot reach an inline status colour, so the flag also travels by context:
 * a `Stat` inside drops its level and greys with the rest instead of keeping a confident green.
 */
export function StaleScope({ stale, className, ...rest }: { stale: boolean } & ComponentProps<"div">) {
  return (
    <StaleContext value={stale}>
      <div data-stale={stale} className={cn(className, stale && "[&_.num]:text-ink-3")} {...rest} />
    </StaleContext>
  );
}
