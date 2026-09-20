/**
 * Sign out, and the question of whether there is anything to sign out of.
 *
 * The daemon only asks for a password when `WEB_PASSWORD` is set, so the control has to be
 * absent — not disabled — on the many bases that sit behind Tailscale with no password at all.
 * Two independent signals say there is one: `GET /api/config` reports `web_password` as the
 * mask `***` (a password this session can already read, so the query is the reactive source),
 * and `auth.unauthorizedSeen()` remembers any 401 the client has met (true even before the
 * config query lands, and true on a session whose cookie has just expired).
 */
import { LogOut } from "lucide-react";
import { Button } from "@/components/ui/button";
import { auth, logout } from "@/lib/api";
import { useConfig } from "@/lib/queries";
import { cn } from "@/lib/utils";

/** True when this base station asks for a password. */
export function usePasswordConfigured(): boolean {
  const masked = useConfig().data?.values?.web_password;
  return auth.unauthorizedSeen() || (typeof masked === "string" && masked.length > 0);
}

/**
 * `POST /api/logout` clears the cookie, then the browser is sent to /login as a fresh load:
 * every query cache and the live socket start again with no session, which is the whole point.
 */
export function SignOutButton({ variant = "ghost", className }: { variant?: "ghost" | "outline"; className?: string }) {
  const signOut = async () => {
    try {
      await logout();
    } catch {
      // The cookie may already be gone (expired, cleared, daemon restarted); /login either way.
    }
    window.location.replace("/login");
  };
  return (
    <Button
      type="button"
      variant={variant}
      size="sm"
      onClick={() => void signOut()}
      className={cn(variant === "ghost" && "w-full justify-start gap-3 px-3 font-normal text-ink-2 hover:text-ink max-lg:justify-center max-lg:px-0", className)}
    >
      <LogOut className="size-4 shrink-0" aria-hidden />
      <span className={cn(variant === "ghost" && "rail-label max-lg:sr-only")}>Sign out</span>
    </Button>
  );
}
