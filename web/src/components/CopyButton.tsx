import { Check, Copy, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";

/**
 * Put `text` on the clipboard. The async Clipboard API needs a secure context, and the daemon
 * is often reached over plain HTTP on a LAN (`WEB_ALLOW_INSECURE`), so the legacy
 * `execCommand("copy")` path is the fallback. Returns whether anything was copied.
 */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // permission denied or insecure context: try the legacy path
  }
  try {
    if (typeof document.execCommand !== "function") return false;
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  } catch {
    return false;
  }
}

/** A small outline button that says "Copied" (or "Not copied") for a moment afterwards. */
export function CopyButton({ text, label = "Copy", className }: { text: string; label?: string; className?: string }) {
  const [state, setState] = useState<"idle" | "done" | "failed">("idle");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );
  const flash = (next: "done" | "failed") => {
    setState(next);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setState("idle"), 1500);
  };
  const Icon = state === "done" ? Check : state === "failed" ? X : Copy;
  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      className={className}
      onClick={async () => flash((await copyText(text)) ? "done" : "failed")}
    >
      <Icon className="size-3.5" aria-hidden />
      <span aria-live="polite">{state === "done" ? "Copied" : state === "failed" ? "Not copied" : label}</span>
    </Button>
  );
}
