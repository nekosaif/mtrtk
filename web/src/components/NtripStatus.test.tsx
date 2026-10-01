import { render, screen } from "@testing-library/react";
import { NtripStatus } from "./NtripStatus";

describe("NtripStatus", () => {
  it("says no caster is configured when NTRIP_URL is unset", () => {
    render(<NtripStatus ntrip={null} configuredUrl={null} />);
    expect(screen.getByText(/No caster configured/)).toBeInTheDocument();
  });

  it("says a set NTRIP_URL is unusable instead of 'not configured' when no client runs", () => {
    render(<NtripStatus ntrip={null} configuredUrl="ntrip://user:***@host-only" />);
    expect(screen.getByText(/NTRIP_URL is set but is not usable/)).toBeInTheDocument();
    expect(screen.queryByText(/No caster configured/)).toBeNull();
  });
});
