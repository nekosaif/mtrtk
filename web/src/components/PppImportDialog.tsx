import { useId, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";
import { Stat } from "@/components/Stat";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ApiError, activateSite, addSite, describeError, importPppResult } from "@/lib/api";
import { fmtDms } from "@/lib/format";
import type { PppImportRefusal, PppResult, SiteResult } from "@/lib/types";

type PreferFrame = "itrf" | "nad83";

/** A per-axis 1-sigma in millimetres, or a dash when the report gave none. */
const sigma = (v: number | null) => (v == null ? "± —" : `± ${(v * 1000).toFixed(1)} mm`);

/** The parser's structured 422 (`{message, hint, head}`), or null for any other failure. */
export function refusalOf(err: unknown): PppImportRefusal | null {
  if (!(err instanceof ApiError) || err.status !== 422) return null;
  const raw = err.raw as Partial<PppImportRefusal> | null;
  if (!raw || typeof raw !== "object" || Array.isArray(raw) || typeof raw.message !== "string") return null;
  return { message: raw.message, hint: typeof raw.hint === "string" ? raw.hint : "", head: typeof raw.head === "string" ? raw.head : "" };
}

function UploadError({ error }: { error: unknown }) {
  const refusal = refusalOf(error);
  if (!refusal) {
    return (
      <Alert variant="destructive">
        <AlertDescription className="text-[14px] leading-5">{describeError(error)}</AlertDescription>
      </Alert>
    );
  }
  return (
    <Alert variant="destructive">
      <AlertDescription className="flex flex-col gap-2 text-[14px] leading-5">
        <p>{refusal.message}</p>
        {refusal.hint ? <p className="text-ink-2">{refusal.hint}</p> : null}
        {refusal.head ? (
          <div className="flex w-full min-w-0 flex-col gap-1">
            <span className="text-[12px] leading-4 text-ink-2">The file begins:</span>
            <pre className="num max-h-32 overflow-auto rounded-md border border-line bg-panel-2 p-2 text-[12px] leading-4 whitespace-pre-wrap break-all text-ink">{refusal.head}</pre>
          </div>
        ) : null}
      </AlertDescription>
    </Alert>
  );
}

/**
 * Read a PPP service's result file into a site. The file goes to `POST /api/base/ppp/import`,
 * which only parses it; the dialog shows what came back (source, frame and epoch as reported,
 * ECEF with per-axis 1-sigma, geodetic position, the parser's notes) before anything is saved.
 * Saving is `POST /api/base/sites` with those numbers under the suggested (editable) name, then,
 * when asked, `POST …/activate`. `onSaved(result, activated)` lets the page persist the mode and
 * say what the receiver did, as it does for any other activation.
 */
export function PppImportDialog({ onSaved }: { onSaved?: (result: SiteResult, activated: boolean) => void | Promise<void> }) {
  const ids = useId();
  const [open, setOpen] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [frame, setFrame] = useState<PreferFrame>("itrf");
  const [name, setName] = useState("");
  const [activate, setActivate] = useState(true);
  /** Set once the site row exists, so a failed activation is not followed by a duplicate add. */
  const [saved, setSaved] = useState<SiteResult | null>(null);

  const upload = useMutation({
    mutationFn: ({ f, pf }: { f: File; pf: PreferFrame }) => importPppResult(f, pf),
  });
  const result: PppResult | undefined = upload.data;

  const save = useMutation({
    mutationFn: async (r: PppResult) => {
      const siteName = name.trim();
      const added =
        saved ??
        (await addSite({
          name: siteName,
          x: r.x,
          y: r.y,
          z: r.z,
          sigma_x: r.sigma_x,
          sigma_y: r.sigma_y,
          sigma_z: r.sigma_z,
          source: r.source,
          frame: r.frame,
          epoch: r.epoch,
          notes: `Imported from ${file?.name ?? "a result file"} (${r.format})`,
        }));
      setSaved(added);
      if (!activate) return { result: added, activated: false };
      try {
        return { result: await activateSite(added.site.name), activated: true };
      } catch (err) {
        // The row exists although the activation failed: let the page refresh its table.
        void onSaved?.(added, false);
        throw err;
      }
    },
    onSuccess: async ({ result: siteResult, activated }) => {
      const description = !activated
        ? "Activate it from the table when the antenna is on the mark."
        : siteResult.applied
          ? "Activated: the base switches to it."
          : "Saved and marked active; the receiver was not changed (see the outcome on the page).";
      toast.success(`Saved site ${siteResult.site.name}`, { description });
      reset();
      setOpen(false);
      await onSaved?.(siteResult, activated);
    },
  });

  function reset() {
    setFile(null);
    setFrame("itrf");
    setName("");
    setActivate(true);
    setSaved(null);
    upload.reset();
    save.reset();
  }

  const read = (f: File | null, pf: PreferFrame) => {
    setFile(f);
    setSaved(null);
    save.reset();
    // A per-call callback runs only for the latest upload, so an earlier one answering last
    // cannot put its name over the file now shown.
    if (f) upload.mutate({ f, pf }, { onSuccess: (r) => setName(r.suggested_name) });
    else upload.reset();
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        setOpen(o);
        if (!o) reset();
      }}
    >
      <DialogTrigger asChild>
        <Button type="button" size="sm" variant="outline">
          Import PPP result
        </Button>
      </DialogTrigger>
      <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle className="text-[16px] leading-6 font-medium">Import a PPP result</DialogTitle>
          <DialogDescription className="text-[14px] leading-5 text-ink-2">The file is only read: check the numbers below, then save them as a site.</DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1">
            <Label htmlFor={`${ids}-file`}>Result file (.sum, .pos, the e-mailed .zip, SINEX .snx or OPUS text)</Label>
            <Input id={`${ids}-file`} type="file" disabled={saved != null} accept=".sum,.pos,.zip,.snx,.SNX,.txt" onChange={(e) => {
                read(e.target.files?.[0] ?? null, frame);
                // Cleared so that choosing the same file again (after a failed upload) reads it again.
                e.target.value = "";
              }}
            />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor={`${ids}-frame`}>OPUS frame</Label>
            <select
              id={`${ids}-frame`}
              value={frame}
              disabled={saved != null}
              onChange={(e) => {
                const pf = e.target.value as PreferFrame;
                setFrame(pf);
                if (file) read(file, pf);
              }}
              className="h-9 rounded-md border border-line bg-panel-2 px-2 text-[14px] text-ink disabled:opacity-50"
            >
              <option value="itrf">ITRF (default)</option>
              <option value="nad83">NAD83</option>
            </select>
            <p className="text-[12px] leading-4 text-ink-2">An OPUS report gives both; CSRS-PPP, AUSPOS and SINEX give one frame and ignore this.</p>
            {saved ? <p className="text-[12px] leading-4 text-ink-2">The site is saved; close the dialog to import another file.</p> : null}
          </div>

          {upload.isPending ? (
            <p role="status" className="text-ink-2">
              Reading the file…
            </p>
          ) : null}
          {upload.isError ? <UploadError error={upload.error} /> : null}

          {result ? (
            <div className="flex flex-col gap-3">
              <div>
                <Stat label="Source" value={`${result.source} (${result.format})`} />
                <Stat label="Frame" value={`${result.frame}${result.epoch ? ` @ ${result.epoch}` : ""}`} />
                <Stat label="X" value={`${result.x.toFixed(4)} m ${sigma(result.sigma_x)}`} />
                <Stat label="Y" value={`${result.y.toFixed(4)} m ${sigma(result.sigma_y)}`} />
                <Stat label="Z" value={`${result.z.toFixed(4)} m ${sigma(result.sigma_z)}`} />
                <Stat label="Position" value={`${fmtDms(result.lat, true)} ${fmtDms(result.lon, false)} · ${result.height_m.toFixed(3)} m`} hint="ellipsoidal height" />
              </div>
              {result.notes.length ? (
                <ul className="flex list-disc flex-col gap-1 pl-4 text-[12px] leading-4 text-ink-2">
                  {result.notes.map((n) => (
                    <li key={n}>{n}</li>
                  ))}
                </ul>
              ) : null}
              <p className="text-[12px] leading-4 text-ink-2">Sigmas are 1σ per ECEF axis.</p>
              <div className="flex flex-col gap-1">
                <Label htmlFor={`${ids}-name`}>Site name</Label>
                <Input id={`${ids}-name`} value={name} disabled={saved != null} onChange={(e) => setName(e.target.value)} />
              </div>
              <label className="flex items-start gap-2 text-[14px] leading-5">
                <input type="checkbox" className="mt-1 accent-brass" checked={activate} onChange={(e) => setActivate(e.target.checked)} />
                <span>Activate it: a running base switches to fixed mode on this site within 10 s and broadcasts it in RTCM 1005; rover positions shift by the offset between the sites. The mode and the site are then saved to .env.</span>
              </label>
              {save.isError ? (
                <Alert variant="destructive">
                  <AlertDescription className="text-[14px] leading-5">
                    {saved ? `Saved ${saved.site.name}, but activating it failed: ` : ""}
                    {describeError(save.error)}
                  </AlertDescription>
                </Alert>
              ) : null}
              <div className="flex justify-end">
                <Button type="button" onClick={() => save.mutate(result)} disabled={!name.trim() || save.isPending}>
                  {save.isPending ? "Saving…" : saved ? "Retry activation" : activate ? "Save and activate" : "Save site"}
                </Button>
              </div>
            </div>
          ) : null}
        </div>
      </DialogContent>
    </Dialog>
  );
}
