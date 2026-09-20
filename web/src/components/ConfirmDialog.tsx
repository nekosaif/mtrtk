import { useId, useState, type ReactNode } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { describeError } from "@/lib/api";

/**
 * A confirmation step in front of an action that touches the receiver. `onConfirm` runs when
 * the operator confirms; if it resolves the dialog closes, if it throws the server's detail
 * (`ApiError.detail`, verbatim) is shown in an alert and the dialog stays open. `requireText`
 * holds the confirm button until that exact word is typed — for the resets that cost data.
 * `children` render between the body and the footer (a reset-type picker, say).
 */
export function ConfirmDialog({
  trigger,
  title,
  body,
  confirmLabel,
  destructive,
  onConfirm,
  requireText,
  children,
}: {
  trigger: ReactNode;
  title: string;
  body?: ReactNode;
  confirmLabel: string;
  destructive?: boolean;
  onConfirm: () => Promise<unknown> | void;
  requireText?: string;
  children?: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputId = useId();
  const ok = !requireText || typed === requireText;

  const change = (next: boolean) => {
    setOpen(next);
    setTyped("");
    setError(null);
  };
  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      await onConfirm();
      change(false);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={change}>
      <DialogTrigger asChild>{trigger}</DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="text-[16px] leading-6 font-medium">{title}</DialogTitle>
          {body ? <DialogDescription className="text-[14px] leading-5 text-ink-2">{body}</DialogDescription> : null}
        </DialogHeader>
        {children}
        {requireText ? (
          <div className="flex flex-col gap-1 text-[14px] leading-5">
            <label htmlFor={inputId}>
              Type <span className="num font-medium">{requireText}</span> to continue
            </label>
            <Input id={inputId} value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" spellCheck={false} />
          </div>
        ) : null}
        {error ? (
          <Alert variant="destructive">
            <AlertDescription className="text-[14px] leading-5">{error}</AlertDescription>
          </Alert>
        ) : null}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => change(false)}>
            Cancel
          </Button>
          <Button type="button" variant={destructive ? "destructive" : "default"} disabled={!ok || busy} onClick={confirm}>
            {confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
