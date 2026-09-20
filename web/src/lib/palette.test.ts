import { GNSS_ID_TO_NAME, STATUS, SYSTEM_ORDER, systemColor } from "./palette";

describe("palette", () => {
  it("keeps the constellation order fixed: GPS, GLONASS, Galileo, BeiDou, QZSS, SBAS", () => {
    expect([...SYSTEM_ORDER]).toEqual(["GPS", "GLONASS", "Galileo", "BeiDou", "QZSS", "SBAS"]);
  });

  it("maps a system name or a UBX gnssId to the same token; other systems fall back to muted ink", () => {
    expect(systemColor("GPS")).toBe("var(--sys-gps)");
    expect(systemColor(0)).toBe(systemColor("GPS"));
    expect(systemColor(6)).toBe("var(--sys-glonass)");
    expect(systemColor(2)).toBe("var(--sys-galileo)");
    expect(systemColor(3)).toBe("var(--sys-beidou)");
    expect(systemColor(5)).toBe("var(--sys-qzss)");
    expect(systemColor(1)).toBe("var(--sys-sbas)");
    expect(GNSS_ID_TO_NAME[4]).toBe("IMES");
    expect(systemColor("IMES")).toBe("var(--ink-3)");
    expect(systemColor(7)).toBe("var(--ink-3)"); // NavIC
    expect(systemColor(99)).toBe("var(--ink-3)"); // unknown
  });

  it("exposes the four fixed status levels in severity order", () => {
    expect(Object.keys(STATUS)).toEqual(["good", "warning", "serious", "critical"]);
  });
});
