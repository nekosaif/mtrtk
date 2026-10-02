import { render, screen } from "@testing-library/react";
import { StatusBadge } from "./StatusBadge";

describe("StatusBadge", () => {
  // B3 — the border and the icon are the non-colour carrier's partners, so they take the mark
  // tokens (3:1 on every surface in both themes); the word itself stays in ink.
  it.each(["good", "warning", "serious", "critical"] as const)("draws a %s badge's border and icon in the mark colour, the word in ink", (level) => {
    render(<StatusBadge level={level} label="Word" />);
    const badge = screen.getByText("Word");
    expect(badge.style.borderColor).toBe(`var(--status-${level}-mark)`);
    expect(badge.style.color).toBe("var(--ink)");
    expect((badge.querySelector("svg") as SVGElement).style.color).toBe(`var(--status-${level}-mark)`);
  });
});
