import { render, screen } from "@testing-library/react";
import { StaleScope } from "./StaleScope";
import { Stat } from "./Stat";

describe("Stat", () => {
  it("colours its value with the status text form", () => {
    render(<Stat label="Antenna" value="OK" level="good" />);
    expect(screen.getByText("OK").style.color).toBe("var(--status-good-text)");
  });

  // D3 — the page's stale rule greys `.num` with a class, and an inline colour beats any class:
  // on a stale page every figure greyed except the status-coloured ones, which kept a confident
  // green. Inside a stale scope a Stat drops its level and greys with the rest.
  it("drops its status colour inside a stale scope, and keeps it in a live one", () => {
    const { rerender } = render(
      <StaleScope stale data-testid="scope" className="grid">
        <Stat label="Antenna" value="OK" level="good" />
      </StaleScope>,
    );
    const scope = screen.getByTestId("scope");
    expect(scope).toHaveAttribute("data-stale", "true");
    expect(scope.className).toContain("[&_.num]:text-ink-3");
    expect(scope.className).toContain("grid");
    expect(screen.getByText("OK").style.color).toBe("");
    rerender(
      <StaleScope stale={false} data-testid="scope" className="grid">
        <Stat label="Antenna" value="OK" level="good" />
      </StaleScope>,
    );
    expect(screen.getByTestId("scope")).toHaveAttribute("data-stale", "false");
    expect(screen.getByTestId("scope").className).not.toContain("text-ink-3");
    expect(screen.getByText("OK").style.color).toBe("var(--status-good-text)");
  });
});
