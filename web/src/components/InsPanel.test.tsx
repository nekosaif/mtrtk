import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { InsBlock, InsConfigReport, InsStatus } from "@/lib/types";
import { InsPanel, needsApply } from "./InsPanel";

function report(overrides: Partial<InsConfigReport> = {}): InsConfigReport {
  return {
    items: [
      { name: "motion_profile", state: "mismatched", current: 2, wanted: 7 },
      { name: "output:EKF_NAV", state: "unchanged", current: 20, wanted: null },
      { name: "output:EVENT_E", state: "unsupported", current: null, wanted: null },
    ],
    applied: [],
    unchanged: ["output:EKF_NAV"],
    pending: [],
    mismatched: ["motion_profile"],
    unsupported: ["output:EVENT_E"],
    errors: [],
    notes: [],
    saved: false,
    current: {},
    wanted: {},
    ...overrides,
  };
}

const status: InsStatus = {
  vendor: "sbg",
  mode: 1,
  mode_name: "Vertical gyro",
  general_ok: { main_power: true, imu_power: true },
  aiding: { gps1_pos: true, gps1_hdt: true, mag: false },
  errors: {},
  uptime_s: 3600,
  cpu_pct: 42,
  com_status: 0,
  gnss_fix: 3,
  gnss_fix_name: "DGNSS",
  gnss_vel: null,
};

function block(overrides: Partial<InsBlock> = {}): InsBlock {
  return {
    vendor: "sbg",
    driver: "sbg_ellipse",
    connected: true,
    port: "/dev/ttyUSB0",
    apply_config: false,
    info: { model: "ELLIPSE-D-G4A3-B1", serial: "12345", firmware: "3.1.0.0", hardware: "2.0.0.0", details: {} },
    config_report: report(),
    lever_arms: [{ name: "gnss1", configured: [0.1, 0.2, -0.3], read_back: [0, 0, 0] }],
    status,
    rtcm_unverified: true,
    dropped_rtcm_bytes: 0,
    raw_gnss_format: "ubx",
    stats: {},
    ...overrides,
  };
}

const noop = () => Promise.resolve();

describe("InsPanel", () => {
  it("shows the vendor, the unit's identity and the filter state", () => {
    render(<InsPanel ins={block()} status={status} imu={null} attitude={{ roll_deg: 0.5, pitch_deg: -1.25, heading_deg: 91.25, acc_roll_deg: 0.1, acc_pitch_deg: 0.1, acc_heading_deg: 0.4, source: "sbg-gnss-hdt" }} actionable onApply={noop} onReread={noop} />);
    const unit = screen.getByRole("region", { name: "INS unit" });
    expect(unit).toHaveTextContent("SBG Systems");
    expect(unit).toHaveTextContent("ELLIPSE-D-G4A3-B1");
    expect(unit).toHaveTextContent("12345");
    const filter = screen.getByRole("region", { name: "INS filter" });
    expect(filter).toHaveTextContent("Vertical gyro");
    expect(filter).toHaveTextContent("91.25°");
    expect(filter).toHaveTextContent("±0.40° 1σ");
    expect(filter).toHaveTextContent("gps1 pos, gps1 hdt");
    const arms = screen.getByRole("table", { name: "Lever arms" });
    expect(arms).toHaveTextContent("0.100, 0.200, -0.300");
  });

  it("colours a mismatched item serious and offers the apply behind a confirmation", async () => {
    const onApply = vi.fn(async () => {});
    render(<InsPanel ins={block()} status={status} imu={null} attitude={null} actionable onApply={onApply} onReread={noop} />);
    const row = within(screen.getByRole("table", { name: "INS configuration" })).getByText("motion_profile").closest("tr");
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByText("read back wrong")).toHaveStyle({ color: "var(--status-serious-text)" });
    const apply = screen.getByRole("button", { name: "Apply INS configuration" });
    expect(apply).toBeEnabled();
    await userEvent.click(apply);
    const dialog = screen.getByRole("dialog", { name: "Apply the INS configuration?" });
    expect(dialog).toHaveTextContent("not saved to flash");
    await userEvent.click(within(dialog).getByRole("button", { name: "Apply" }));
    expect(onApply).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("disables the apply when nothing is pending or mismatched", () => {
    const clean = report({ items: [{ name: "output:EKF_NAV", state: "unchanged", current: 20, wanted: null }], mismatched: [] });
    expect(needsApply(clean)).toBe(false);
    render(<InsPanel ins={block({ config_report: clean })} status={status} imu={null} attitude={null} actionable onApply={noop} onReread={noop} />);
    expect(screen.getByRole("button", { name: "Apply INS configuration" })).toBeDisabled();
    expect(screen.getByText("The unit matches the profile.")).toBeInTheDocument();
  });

  it("prefers the live report over the block's own", () => {
    const live = report({ items: [{ name: "init_position", state: "pending", current: null, wanted: null }], pending: ["init_position"], mismatched: [] });
    render(<InsPanel ins={block()} report={live} status={status} imu={null} attitude={null} actionable onApply={noop} onReread={noop} />);
    const table = screen.getByRole("table", { name: "INS configuration" });
    expect(table).toHaveTextContent("init_position");
    expect(table).not.toHaveTextContent("motion_profile");
  });

  it("hides the IMU block without a sample and shows it with one", () => {
    const { rerender } = render(<InsPanel ins={block()} status={status} imu={null} attitude={null} actionable onApply={noop} onReread={noop} />);
    expect(screen.queryByRole("region", { name: "IMU" })).toBeNull();
    rerender(<InsPanel ins={block()} status={status} imu={{ accel_mps2: [0, 0, -9.81], gyro_radps: [0, 0, Math.PI], temperature_c: 38.04, timestamp_us: 1 }} attitude={null} actionable onApply={noop} onReread={noop} />);
    const imu = screen.getByRole("region", { name: "IMU" });
    expect(imu).toHaveTextContent("0.00, 0.00, -9.81");
    expect(imu).toHaveTextContent("0.00, 0.00, 180.00");
    expect(imu).toHaveTextContent("38.0 °C");
  });

  it("holds both actions while the unit cannot be reached", async () => {
    const onReread = vi.fn(async () => {});
    render(<InsPanel ins={block({ connected: false })} status={null} imu={null} attitude={null} actionable={false} onApply={noop} onReread={onReread} />);
    expect(screen.getByRole("button", { name: "Apply INS configuration" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Re-read configuration" })).toBeDisabled();
    expect(screen.getByRole("region", { name: "INS filter" })).toHaveTextContent("has not reported");
  });
});
