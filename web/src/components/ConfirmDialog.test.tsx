import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "./ConfirmDialog";

describe("ConfirmDialog", () => {
  it("opens from its trigger, runs onConfirm and closes", async () => {
    const onConfirm = vi.fn(async () => {});
    render(<ConfirmDialog trigger={<Button>Re-apply profile</Button>} title="Re-apply the receiver profile?" body="Rewrites every key." confirmLabel="Re-apply" onConfirm={onConfirm} />);
    expect(screen.queryByRole("dialog")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Re-apply profile" }));
    const dialog = screen.getByRole("dialog", { name: "Re-apply the receiver profile?" });
    expect(dialog).toHaveTextContent("Rewrites every key.");
    await userEvent.click(screen.getByRole("button", { name: "Re-apply" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("holds the confirm button until the required text is typed exactly", async () => {
    const onConfirm = vi.fn(async () => {});
    render(<ConfirmDialog trigger={<Button>Reset…</Button>} title="Reset the receiver" confirmLabel="Confirm reset" destructive requireText="factory" onConfirm={onConfirm} />);
    await userEvent.click(screen.getByRole("button", { name: "Reset…" }));
    const confirm = screen.getByRole("button", { name: "Confirm reset" });
    expect(confirm).toBeDisabled();
    expect(confirm).toHaveAttribute("data-variant", "destructive");
    const input = screen.getByRole("textbox", { name: /type factory to continue/i });
    await userEvent.type(input, "Factory");
    expect(confirm).toBeDisabled();
    await userEvent.clear(input);
    await userEvent.type(input, "factory");
    expect(confirm).toBeEnabled();
    await userEvent.click(confirm);
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("shows the server's detail verbatim when onConfirm fails, and stays open", async () => {
    const onConfirm = vi.fn(async () => {
      throw new ApiError(504, "receiver did not answer: no reply within 2.0 s");
    });
    render(<ConfirmDialog trigger={<Button>Reset…</Button>} title="Reset the receiver" confirmLabel="Confirm reset" onConfirm={onConfirm} />);
    await userEvent.click(screen.getByRole("button", { name: "Reset…" }));
    await userEvent.click(screen.getByRole("button", { name: "Confirm reset" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("receiver did not answer: no reply within 2.0 s");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    // cancelling clears the error for the next opening
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await userEvent.click(screen.getByRole("button", { name: "Reset…" }));
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
