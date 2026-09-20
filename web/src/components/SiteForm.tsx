import { useId, useState, type ChangeEvent, type FormEvent } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { describeError } from "@/lib/api";
import type { SiteBody } from "@/lib/types";

/** What the form submits: the API's `SiteBody`, with exactly one coordinate triple present. */
export type SiteInput = SiteBody;

type CoordTab = "ecef" | "llh";

const num = (v: string): number | undefined => (v.trim() === "" ? undefined : Number(v));
const isNum = (v: number | undefined): v is number => v !== undefined && Number.isFinite(v);

/** The client-side check the daemon would otherwise answer with a 422; null when the input is fine. */
export function validateSiteInput(f: { name: string; sigma: string; lat?: string; lon?: string }, tab: CoordTab, coords: (number | undefined)[]): string | null {
  if (!f.name.trim()) return "Give the site a name.";
  if (coords.some((v) => !isNum(v))) return tab === "ecef" ? "All three ECEF coordinates (X, Y, Z in metres) are required." : "All three of latitude, longitude and height are required.";
  if (tab === "llh") {
    const [lat, lon] = coords as number[];
    if (Math.abs(lat) > 90) return "Latitude must be between -90 and 90 degrees.";
    if (Math.abs(lon) > 180) return "Longitude must be between -180 and 180 degrees.";
  }
  const sigma = num(f.sigma);
  if (sigma !== undefined && (!Number.isFinite(sigma) || sigma < 0)) return "Sigma is a distance in metres, zero or more.";
  return null;
}

/**
 * Name and coordinates of a site, as ECEF metres or as geodetic degrees plus ellipsoidal
 * height — one triple or the other, never both, which is what the API insists on. Sigma,
 * source, frame, epoch and notes are optional. `onSubmit` receives the body; a rejection (the
 * daemon's 409 for a duplicate name, say) is shown verbatim under the fields and the form
 * stays open for a correction.
 */
export function SiteForm({ initial, onSubmit, submitLabel = "Save site" }: { initial?: Partial<SiteInput>; onSubmit: (site: SiteInput) => Promise<unknown> | void; submitLabel?: string }) {
  const id = useId();
  const [tab, setTab] = useState<CoordTab>(initial?.lat != null ? "llh" : "ecef");
  const [f, setF] = useState({
    name: initial?.name ?? "",
    x: initial?.x?.toString() ?? "",
    y: initial?.y?.toString() ?? "",
    z: initial?.z?.toString() ?? "",
    lat: initial?.lat?.toString() ?? "",
    lon: initial?.lon?.toString() ?? "",
    h: initial?.height_m?.toString() ?? "",
    sigma: initial?.sigma_m?.toString() ?? "",
    source: initial?.source ?? "manual",
    frame: initial?.frame ?? "ITRF2020",
    epoch: initial?.epoch ?? "",
    notes: initial?.notes ?? "",
  });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (k: keyof typeof f) => (e: ChangeEvent<HTMLInputElement>) => setF((prev) => ({ ...prev, [k]: e.target.value }));

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    const coords = tab === "ecef" ? [num(f.x), num(f.y), num(f.z)] : [num(f.lat), num(f.lon), num(f.h)];
    const problem = validateSiteInput(f, tab, coords);
    if (problem) return setError(problem);
    const [a, b, c] = coords as [number, number, number];
    const site: SiteInput = { name: f.name.trim(), ...(tab === "ecef" ? { x: a, y: b, z: c } : { lat: a, lon: b, height_m: c }) };
    const sigma = num(f.sigma);
    if (sigma !== undefined) site.sigma_m = sigma;
    if (f.source.trim()) site.source = f.source.trim();
    if (f.frame.trim()) site.frame = f.frame.trim();
    if (f.epoch.trim()) site.epoch = f.epoch.trim();
    if (f.notes.trim()) site.notes = f.notes.trim();
    setBusy(true);
    try {
      await onSubmit(site);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  };

  const field = (key: keyof typeof f, label: string, extra: Partial<React.ComponentProps<typeof Input>> = {}) => (
    <div className="flex min-w-0 flex-col gap-1">
      <Label htmlFor={`${id}-${key}`} className="text-[12px] leading-4 text-ink-2">
        {label}
      </Label>
      <Input id={`${id}-${key}`} value={f[key]} onChange={set(key)} autoComplete="off" spellCheck={false} {...extra} />
    </div>
  );
  const coord = (key: keyof typeof f, label: string, placeholder?: string) => field(key, label, { inputMode: "decimal", placeholder, className: "num" });

  return (
    <form onSubmit={submit} className="flex flex-col gap-3" noValidate>
      {field("name", "Name", { placeholder: "roof-2026", required: true })}
      <Tabs value={tab} onValueChange={(v) => setTab(v as CoordTab)}>
        <TabsList>
          <TabsTrigger value="ecef">ECEF (metres)</TabsTrigger>
          <TabsTrigger value="llh">Lat / Lon / Height</TabsTrigger>
        </TabsList>
        <TabsContent value="ecef" className="grid grid-cols-1 gap-2 pt-2 sm:grid-cols-3">
          {coord("x", "X", "3980123.4567")}
          {coord("y", "Y", "123456.7890")}
          {coord("z", "Z", "4966789.0123")}
        </TabsContent>
        <TabsContent value="llh" className="grid grid-cols-1 gap-2 pt-2 sm:grid-cols-3">
          {coord("lat", "Latitude (°)", "51.4778000")}
          {coord("lon", "Longitude (°)", "-0.0014000")}
          {coord("h", "Height (ellipsoid, m)", "45.123")}
        </TabsContent>
      </Tabs>
      <p className="text-[12px] leading-4 text-ink-2">ECEF is what the receiver broadcasts and what PPP reports give; geodetic input is converted once, on save.</p>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {coord("sigma", "Sigma per axis (m)", "0.005")}
        {field("source", "Source", { placeholder: "csrs-ppp" })}
        {field("frame", "Frame")}
        {field("epoch", "Epoch", { placeholder: "2026.71" })}
      </div>
      {field("notes", "Notes")}
      {error ? (
        <Alert variant="destructive">
          <AlertDescription className="text-[14px] leading-5">{error}</AlertDescription>
        </Alert>
      ) : null}
      <div className="flex justify-end">
        <Button type="submit" disabled={busy}>
          {submitLabel}
        </Button>
      </div>
    </form>
  );
}
