import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { toast } from "sonner";
import type { PppResult, Site } from "@/lib/types";
import { PppImportDialog } from "./PppImportDialog";

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const result: PppResult = { source: "csrs-ppp", format: "csrs-sum", frame: "ITRF20", epoch: "2026.7137", x: -26748.172, y: 5837156.6184, z: 2561801.2607, sigma_x: 0.0036, sigma_y: 0.0077, sigma_z: 0.0041, lat: 23.8373506, lon: 90.2625502, height_m: -36.268, notes: ["CSRS-PPP sigmas are 95 %; stored as 1σ"], suggested_name: "MTRK-csrs-ppp-2026.71" };
const site = (over: Partial<Site> = {}): Site => ({ id: 3, name: result.suggested_name, x: result.x, y: result.y, z: result.z, lat: result.lat, lon: result.lon, height_m: result.height_m, sigma_x: 0.0036, sigma_y: 0.0077, sigma_z: 0.0041, frame: "ITRF20", epoch: "2026.7137", source: "csrs-ppp", notes: null, created_utc: null, active: false, ...over });

let calls: [string, RequestInit | undefined][] = [];
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

interface Answers {
  /** Status + detail for the import; default 200 with `result`. */
  importAnswer?: { status: number; detail: unknown };
  /** The import's answer per uploaded file name, and an optional promise to wait on first. */
  byFile?: Record<string, { result: PppResult; wait?: Promise<void> }>;
  /** Status + detail for /activate; default 200 applied. Mutable between calls. */
  activate?: { status: number; detail: unknown } | { applied: boolean };
}

function mockFetch(a: Answers = {}) {
  calls = [];
  globalThis.fetch = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const p = String(url);
    calls.push([p, init]);
    if (p.endsWith("/api/base/ppp/import")) {
      const f = (init!.body as FormData).get("file") as File;
      const own = a.byFile?.[f.name];
      if (own) {
        await own.wait;
        return json(own.result);
      }
      return a.importAnswer ? json({ detail: a.importAnswer.detail }, a.importAnswer.status) : json(result);
    }
    if (p.endsWith("/api/base/sites")) {
      const body = JSON.parse(String(init!.body)) as { name: string };
      return json({ site: site({ name: body.name }), applied: false });
    }
    if (p.endsWith("/activate")) {
      const act = a.activate ?? { applied: true };
      if ("status" in act) return json({ detail: act.detail }, act.status);
      return json({ site: site({ active: true }), applied: act.applied });
    }
    return json({ detail: "not found" }, 404);
  }) as typeof fetch;
}

function renderDialog(onSaved = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <PppImportDialog onSaved={onSaved} />
    </QueryClientProvider>,
  );
  return onSaved;
}

async function openAndUpload(name = "MTRK.sum") {
  await userEvent.click(screen.getByRole("button", { name: /import ppp result/i }));
  const file = new File(["fake"], name, { type: "text/plain" });
  await userEvent.upload(screen.getByLabelText(/result file/i), file);
  return file;
}

const callsTo = (suffix: string) => calls.filter(([u]) => u.endsWith(suffix));

describe("PppImportDialog", () => {
  beforeEach(() => {
    mockFetch();
    vi.mocked(toast.success).mockClear();
  });

  it("uploads, previews and saves an active site", async () => {
    const onSaved = renderDialog();
    const file = await openAndUpload();
    expect(await screen.findByText("ITRF20 @ 2026.7137")).toBeInTheDocument();
    // the upload is a multipart form with the file and the OPUS frame choice, as form fields
    const upload = callsTo("/api/base/ppp/import")[0];
    expect(upload[1]?.method).toBe("POST");
    const form = upload[1]!.body as FormData;
    expect(form).toBeInstanceOf(FormData);
    expect(form.get("file")).toBe(file);
    expect(form.get("prefer_frame")).toBe("itrf");
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("-26748.1720 m ± 3.6 mm")).toBeInTheDocument();
    expect(within(dialog).getByText("5837156.6184 m ± 7.7 mm")).toBeInTheDocument();
    expect(within(dialog).getByText(/CSRS-PPP sigmas are 95 %/)).toBeInTheDocument();
    expect(within(dialog).getByText(/23°50'14\.4622"N 90°15'45\.1807"E · -36\.268 m/)).toBeInTheDocument();
    expect(screen.getByDisplayValue("MTRK-csrs-ppp-2026.71")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /save site/i }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    const post = callsTo("/api/base/sites")[0];
    expect(post[1]?.method).toBe("POST");
    expect(JSON.parse(String(post[1]!.body))).toMatchObject({ name: "MTRK-csrs-ppp-2026.71", x: -26748.172, y: 5837156.6184, z: 2561801.2607, sigma_x: 0.0036, sigma_y: 0.0077, sigma_z: 0.0041, frame: "ITRF20", epoch: "2026.7137", source: "csrs-ppp" });
    expect(callsTo("/api/base/sites/MTRK-csrs-ppp-2026.71/activate")).toHaveLength(1);
    expect(onSaved).toHaveBeenCalledWith(expect.objectContaining({ applied: true, site: expect.objectContaining({ active: true }) }), true);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("saves without activating when the box is cleared, under the name typed", async () => {
    const onSaved = renderDialog();
    await openAndUpload();
    await screen.findByText("ITRF20 @ 2026.7137");
    await userEvent.click(screen.getByRole("checkbox", { name: /activate/i }));
    const name = screen.getByLabelText(/site name/i);
    await userEvent.clear(name);
    await userEvent.type(name, "roof-ppp");
    await userEvent.click(screen.getByRole("button", { name: /save site/i }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(expect.objectContaining({ applied: false }), false));
    expect(JSON.parse(String(callsTo("/api/base/sites")[0][1]!.body))).toMatchObject({ name: "roof-ppp" });
    expect(calls.some(([u]) => u.endsWith("/activate"))).toBe(false);
  });

  it("sends the NAD83 choice as a form field for an OPUS report", async () => {
    renderDialog();
    await userEvent.click(screen.getByRole("button", { name: /import ppp result/i }));
    await userEvent.selectOptions(screen.getByLabelText(/opus frame/i), "nad83");
    await userEvent.upload(screen.getByLabelText(/result file/i), new File(["x"], "opus.txt"));
    await waitFor(() => expect(callsTo("/api/base/ppp/import")).toHaveLength(1));
    const [url, init] = callsTo("/api/base/ppp/import")[0];
    expect(url).not.toContain("prefer_frame");
    expect((init!.body as FormData).get("prefer_frame")).toBe("nad83");
  });

  it("shows a file it cannot read as message, hint and the file's head, never raw JSON", async () => {
    mockFetch({ importAnswer: { status: 422, detail: { message: "not a PPP result this parser reads", hint: "Upload the .sum, .pos, .zip, .snx or OPUS e-mail text.", head: "RINEX VERSION / TYPE\nfirst lines" } } });
    renderDialog();
    await openAndUpload("not-a-report.txt");
    const dialog = screen.getByRole("dialog");
    expect(await within(dialog).findByText("not a PPP result this parser reads")).toBeInTheDocument();
    expect(within(dialog).getByText("Upload the .sum, .pos, .zip, .snx or OPUS e-mail text.")).toBeInTheDocument();
    expect(within(dialog).getByText(/RINEX VERSION \/ TYPE/)).toBeInTheDocument();
    expect(dialog.textContent).not.toContain('{"message"');
    expect(within(dialog).queryByRole("button", { name: /save site/i })).not.toBeInTheDocument();
  });

  it("shows any other refusal verbatim", async () => {
    const detail = "request body too large: /api accepts at most 262144 bytes";
    mockFetch({ importAnswer: { status: 413, detail } });
    renderDialog();
    await openAndUpload();
    expect(await screen.findByText(detail)).toBeInTheDocument();
  });

  it("re-reads a chosen file when the OPUS frame changes", async () => {
    const nad83: PppResult = { ...result, source: "opus", format: "opus", frame: "NAD83(2011)", epoch: "2010.0", x: -26747.5, suggested_name: "MTRK-opus-nad83" };
    renderDialog();
    await openAndUpload("opus.txt");
    await screen.findByText("ITRF20 @ 2026.7137");
    mockFetch({ byFile: { "opus.txt": { result: nad83 } } });
    await userEvent.selectOptions(screen.getByLabelText(/opus frame/i), "nad83");
    await waitFor(() => expect(callsTo("/api/base/ppp/import")).toHaveLength(1));
    expect((callsTo("/api/base/ppp/import")[0][1]!.body as FormData).get("prefer_frame")).toBe("nad83");
    expect(await screen.findByText("NAD83(2011) @ 2010.0")).toBeInTheDocument();
    expect(screen.getByText(/-26747\.5000 m/)).toBeInTheDocument();
    expect(screen.getByDisplayValue("MTRK-opus-nad83")).toBeInTheDocument();
  });

  it("shows a missing sigma as a dash and saves it as null", async () => {
    const noSigma: PppResult = { ...result, sigma_x: null, sigma_y: null, sigma_z: null };
    mockFetch({ byFile: { "MTRK.sum": { result: noSigma } } });
    renderDialog();
    await openAndUpload();
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("-26748.1720 m ± —")).toBeInTheDocument();
    expect(within(dialog).getByText("5837156.6184 m ± —")).toBeInTheDocument();
    expect(dialog.textContent).not.toContain("NaN");
    await userEvent.click(screen.getByRole("button", { name: /save site/i }));
    await waitFor(() => expect(callsTo("/api/base/sites")).toHaveLength(1));
    expect(JSON.parse(String(callsTo("/api/base/sites")[0][1]!.body))).toMatchObject({ sigma_x: null, sigma_y: null, sigma_z: null });
  });

  it("keeps the name of the latest file when an earlier upload answers last", async () => {
    let release!: () => void;
    const slow = new Promise<void>((r) => (release = r));
    mockFetch({ byFile: { "a.sum": { result: { ...result, suggested_name: "from-a" }, wait: slow }, "b.sum": { result: { ...result, frame: "ITRF14", suggested_name: "from-b" } } } });
    renderDialog();
    await userEvent.click(screen.getByRole("button", { name: /import ppp result/i }));
    await userEvent.upload(screen.getByLabelText(/result file/i), new File(["a"], "a.sum"));
    await userEvent.upload(screen.getByLabelText(/result file/i), new File(["b"], "b.sum"));
    expect(await screen.findByText("ITRF14 @ 2026.7137")).toBeInTheDocument();
    expect(screen.getByDisplayValue("from-b")).toBeInTheDocument();
    release();
    await waitFor(() => expect(callsTo("/api/base/ppp/import")).toHaveLength(2));
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.getByLabelText(/site name/i)).toHaveValue("from-b");
  });

  it("says when an activation was saved but not applied to the receiver", async () => {
    mockFetch({ activate: { applied: false } });
    const onSaved = renderDialog();
    await openAndUpload();
    await screen.findByText("ITRF20 @ 2026.7137");
    await userEvent.click(screen.getByRole("button", { name: /save site/i }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(expect.objectContaining({ applied: false }), true));
    const [, opts] = vi.mocked(toast.success).mock.calls.at(-1)!;
    expect(String((opts as { description?: unknown }).description)).not.toMatch(/the base switches to it/i);
    expect(String((opts as { description?: unknown }).description)).toMatch(/receiver was not changed/i);
  });

  it("after a failed activation keeps the saved row, offers a retry and never adds it twice", async () => {
    const answers: Answers = { activate: { status: 409, detail: "the base is surveying in; wait for it to finish" } };
    mockFetch(answers);
    const onSaved = renderDialog();
    await openAndUpload();
    await screen.findByText("ITRF20 @ 2026.7137");
    await userEvent.click(screen.getByRole("button", { name: /save site/i }));
    const dialog = screen.getByRole("dialog");
    expect(await within(dialog).findByText("Saved MTRK-csrs-ppp-2026.71, but activating it failed: the base is surveying in; wait for it to finish")).toBeInTheDocument();
    expect(onSaved).toHaveBeenCalledWith(expect.objectContaining({ applied: false }), false);
    expect(within(dialog).getByRole("button", { name: /retry activation/i })).toBeEnabled();
    // the row exists: nothing that would start a different site stays editable
    expect(within(dialog).getByLabelText(/site name/i)).toBeDisabled();
    expect(within(dialog).getByLabelText(/result file/i)).toBeDisabled();
    expect(within(dialog).getByLabelText(/opus frame/i)).toBeDisabled();

    answers.activate = { applied: true };
    await userEvent.click(within(dialog).getByRole("button", { name: /retry activation/i }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(expect.objectContaining({ applied: true }), true));
    expect(callsTo("/api/base/sites")).toHaveLength(1);
    expect(callsTo("/activate")).toHaveLength(2);
  });
});
