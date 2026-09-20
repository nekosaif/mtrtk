import { fixLevel, levelForEvent } from "./status";
import type { FixInfo } from "./types";

const fix = (over: Partial<FixInfo>): FixInfo => ({
  fix_type: 3, fix_type_name: "3D", gnss_fix_ok: true, diff_soln: false, carr_soln: 0, carr_soln_name: "None",
  num_sv: 12, last_correction_age: 0, psm_state: 0, spoof_det_state: 0, ttff_ms: null, uptime_ms: null, ...over,
});

describe("fixLevel", () => {
  it("puts the receiver link first, then staleness, then the fix", () => {
    expect(fixLevel(fix({ carr_soln: 2 }), false, false)).toEqual({ level: "critical", label: "Receiver disconnected" });
    expect(fixLevel(fix({ carr_soln: 2 }), true, true)).toEqual({ level: "warning", label: "Waiting for data" });
    expect(fixLevel(undefined, null, false)).toEqual({ level: "warning", label: "Waiting for data" });
  });
  it("names every fix class", () => {
    expect(fixLevel(fix({ carr_soln: 2 }), true, false)).toEqual({ level: "good", label: "RTK fixed" });
    expect(fixLevel(fix({ carr_soln: 1 }), true, false)).toEqual({ level: "warning", label: "RTK float" });
    expect(fixLevel(fix({ fix_type: 5 }), true, false)).toEqual({ level: "good", label: "Fixed position" });
    expect(fixLevel(fix({ fix_type: 3, diff_soln: true }), true, false)).toEqual({ level: "good", label: "3D DGNSS" });
    expect(fixLevel(fix({ fix_type: 3 }), true, false)).toEqual({ level: "good", label: "3D fix" });
    expect(fixLevel(fix({ fix_type: 4 }), true, false)).toEqual({ level: "good", label: "3D fix" });
    expect(fixLevel(fix({ fix_type: 2 }), true, false)).toEqual({ level: "serious", label: "2D fix" });
    expect(fixLevel(fix({ fix_type: 0 }), true, false)).toEqual({ level: "critical", label: "No fix" });
    expect(fixLevel(fix({ fix_type: 1 }), true, false)).toEqual({ level: "critical", label: "No fix" });
  });
  it("maps event levels onto status levels", () => {
    expect(levelForEvent("error")).toBe("critical");
    expect(levelForEvent("warning")).toBe("warning");
    expect(levelForEvent("info")).toBe("good");
  });
});
