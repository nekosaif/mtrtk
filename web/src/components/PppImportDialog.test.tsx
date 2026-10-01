import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { PppResult, Site } from "@/lib/types";
import { PppImportDialog } from "./PppImportDialog";

const result: PppResult = { source: "csrs-ppp", format: "csrs-sum", frame: "ITRF20", epoch: "2026.7137", x: -26748.172, y: 5837156.6184, z: 2561801.2607, sigma_x: 0.0036, sigma_y: 0.0077, sigma_z: 0.0041, lat: 23.8373506, lon: 90.2625502, height_m: -36.268, notes: ["CSRS-PPP sigmas are 95 %; stored as 1σ"], suggested_name: "MTRK-csrs-ppp-2026.71" };
const site = (over: Partial<Site> = {}): Site => ({ id: 3, name: result.suggested_name, x: result.x, y: result.y, z: result.z, lat: result.lat, lon: result.lon, height_m: result.height_m, sigma_x: 0.0036, sigma_y: 0.0077, sigma_z: 0.0041, frame: "ITRF20", epoch: "2026.7137", source: "csrs-ppp", notes: null, created_utc: null, active: false, ...over });

let calls: [string, RequestInit | undefined][] = [];
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function mockFetch(importAnswer?: { status: number; detail: unknown }) {
  calls = [];
  globalThis.fetch = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const p = String(url);
    calls.push([p, init]);
    if (p.endsWith("/api/base/ppp/import")) return importAnswer ? json({ detail: importAnswer.detail }, importAnswer.status) : json(result);
    if (p.endsWith("/api/base/sites")) {
      const body = JSON.parse(String(init!.body)) as { name: string };
      return json({ site: site({ name: body.name }), applied: false });
    }
    if (p.endsWith("/activate")) return json({ site: site({ active: true }), applied: true });
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
  beforeEach(() => mockFetch());

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
    mockFetch({ status: 422, detail: { message: "not a PPP result this parser reads", hint: "Upload the .sum, .pos, .zip, .snx or OPUS e-mail text.", head: "RINEX VERSION / TYPE\nfirst lines" } });
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
    mockFetch({ status: 413, detail });
    renderDialog();
    await openAndUpload();
    expect(await screen.findByText(detail)).toBeInTheDocument();
  });
});
