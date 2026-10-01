import { bearingToBase, corrAgeLevel, fixLevel, levelForEvent } from "./status";
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

describe("corrAgeLevel", () => {
  it("is good under 5 s, warning under 10 s, critical from 10 s and with no corrections", () => {
    expect(corrAgeLevel(0)).toBe("good");
    expect(corrAgeLevel(4.9)).toBe("good");
    expect(corrAgeLevel(5)).toBe("warning");
    expect(corrAgeLevel(9.9)).toBe("warning");
    expect(corrAgeLevel(10)).toBe("critical");
    expect(corrAgeLevel(null)).toBe("critical");
  });
});

describe("bearingToBase", () => {
  it("reverses the base→rover vector's heading", () => {
    // rover 100 m north of the base: relPosN = 100, relPosE = 0, relPosHeading = 0°; the base is due south
    expect(bearingToBase(0)).toBe(180);
    expect(bearingToBase(91.2)).toBeCloseTo(271.2, 9);
    expect(bearingToBase(180)).toBe(0);
    expect(bearingToBase(270)).toBe(90);
    expect(bearingToBase(359.5)).toBeCloseTo(179.5, 9);
    expect(bearingToBase(-90)).toBe(90); // a negative heading still lands in 0–360
  });
});
