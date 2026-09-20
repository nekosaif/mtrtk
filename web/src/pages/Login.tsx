/**
 * The one page served outside the shell. `POST /api/login` sets an httpOnly session cookie and
 * that cookie is the whole story: nothing is kept in `localStorage`, and the `?token=` fallback
 * on the WebSocket URL exists only for a bearer token somebody pasted by hand.
 *
 * On success the browser is sent to `?next` with a real navigation rather than a client-side
 * route change: the page's queries and its WebSocket were all started without a session, and a
 * fresh load is the shortest way to have every one of them carry the new cookie.
 */
import { useEffect, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { describeError, login } from "@/lib/api";

/**
 * Where to go after signing in. Only a path on this base station is allowed: `//evil.example`
 * and `https://evil.example` are absolute URLs to a browser, so a link with one in `?next`
 * would turn the login page into an open redirect.
 */
export function safeNext(next: string | null): string {
  if (!next || !next.startsWith("/") || next.startsWith("//")) return "/";
  return next;
}

export default function Login() {
  const [params] = useSearchParams();
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [noPassword, setNoPassword] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    document.title = "Sign in · mtrtk";
  }, []);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const { token } = await login(password);
      // The daemon answers 200 with an empty token when WEB_PASSWORD is unset: there is no
      // session to create, and saying so is more use than pretending the sign-in did something.
      if (token === "") {
        setNoPassword(true);
        return;
      }
      window.location.replace(safeNext(params.get("next")));
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="flex min-h-full items-center justify-center p-6">
      <div className="panel flex w-full max-w-[26rem] flex-col gap-5 p-6">
        <div>
          <span className="display text-[40px] leading-[44px]">mtrtk</span>
          <h1 className="text-[20px] leading-7">Sign in</h1>
        </div>
        {noPassword ? (
          <>
            <p className="text-ink-2">
              No password is configured on this base station, so there is nothing to sign in to. Anyone who can reach it over the network can use it — set{" "}
              <span className="num text-ink">WEB_PASSWORD</span> in Settings if that is not what you want.
            </p>
            <Link to="/" className="text-ink-2 underline-offset-4 hover:text-ink hover:underline">
              Go to the dashboard
            </Link>
          </>
        ) : (
          <form onSubmit={(e) => void submit(e)} className="flex flex-col gap-4">
            <p className="text-ink-2">This base station is password protected.</p>
            {/* A password manager (and Chrome's own audit) wants a username beside a password.
                This daemon has one account and no name for it, so the field is fixed and hidden
                rather than one more box to fill in. */}
            <input type="text" name="username" autoComplete="username" value="mtrtk" readOnly hidden />
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="login-password">Password</Label>
              <Input id="login-password" type="password" autoComplete="current-password" autoFocus value={password} onChange={(e) => setPassword(e.target.value)} />
            </div>
            {error ? (
              <p role="alert" className="text-[14px] leading-5" style={{ color: "var(--status-critical)" }}>
                {error}
              </p>
            ) : null}
            <Button type="submit" disabled={busy}>
              {busy ? "Signing in…" : "Sign in"}
            </Button>
          </form>
        )}
      </div>
    </main>
  );
}
