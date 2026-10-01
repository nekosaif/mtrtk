import { Gauge } from "@/components/charts/Gauge";
import { CORR_AGE_MAX_S, corrAgeLevel } from "@/lib/status";

/**
 * The rover's correction age on a 0–30 s bar, shared by the Dashboard and the RTK page. With no
 * corrections the bar is full and critical; the meter's text says "no corrections" so a screen
 * reader does not announce an age that does not exist.
 */
export function CorrAgeGauge({ age }: { age: number | null | undefined }) {
  const text = age == null ? "no corrections" : `${age.toFixed(1)} s`;
  return <Gauge label="Correction age" value={age ?? CORR_AGE_MAX_S} max={CORR_AGE_MAX_S} level={corrAgeLevel(age)} format={() => text} valueText={text} />;
}
