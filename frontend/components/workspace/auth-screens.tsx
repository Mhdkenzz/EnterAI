"use client";
import { useEffect, useState, type ReactNode } from "react";
import { Loader2 } from "lucide-react";
import { API_BASE, api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { field } from "@/components/workspace/shared";

export function AuthShell({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle: string;
  children: ReactNode;
}) {
  return (
    <main className="grid min-h-screen place-items-center bg-canvas p-4 sm:p-6">
      <Card className="w-full max-w-md p-6 shadow-glow sm:p-7">
        <div className="mb-6 flex gap-3">
          <div className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-accent font-black text-slate-950">
            E
          </div>
          <div className="min-w-0">
            <h1 className="font-semibold">{title}</h1>
            <p className="text-xs text-zinc-500">{subtitle}</p>
          </div>
        </div>
        {children}
      </Card>
    </main>
  );
}
export function Notice({ tone, children }: { tone: "error" | "ok"; children: ReactNode }) {
  return (
    <p
      role={tone === "error" ? "alert" : "status"}
      className={cn(
        "rounded-lg border p-3 text-sm",
        tone === "error"
          ? "border-red-500/30 bg-red-500/10 text-red-200"
          : "border-emerald-500/30 bg-emerald-500/10 text-emerald-200",
      )}
    >
      {children}
    </p>
  );
}
export function Submit({ busy, children }: { busy: boolean; children: ReactNode }) {
  return (
    <Button className="w-full" disabled={busy}>
      {busy && <Loader2 className="animate-spin" size={15} />}
      {children}
    </Button>
  );
}
/** Unauthenticated routing. The links in transactional email land back on this page
 *  carrying a single-use token, so the screen is chosen from the query string. */
export function AuthScreens({ login, busy, error, signedIn }: any) {
  const [screen, setScreen] = useState("login"),
    [emailToken, setEmailToken] = useState(""),
    [ssoError, setSsoError] = useState("");
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const ssoParams = new URLSearchParams(window.location.hash.slice(1));
    // A successful SSO sign-in arrives with its session cookie already set, so the
    // workspace opens on its own; only a failure is reported through the fragment.
    if (ssoParams.get("sso_error")) {
      window.history.replaceState({}, "", window.location.pathname + window.location.search);
      setSsoError("SSO sign-in failed. Please try again.");
      return;
    }
    for (const [key, next] of [
      ["reset", "reset"],
      ["verify", "verify"],
      ["invite", "invite"],
    ] as const) {
      const value = params.get(key);
      if (value) {
        setEmailToken(value);
        setScreen(next);
        // Drop the token from the address bar so it does not linger in history or in a
        // referrer header once the page starts making requests.
        window.history.replaceState({}, "", window.location.pathname);
        return;
      }
    }
  }, []);
  if (screen === "forgot") return <ForgotPassword back={() => setScreen("login")} />;
  if (screen === "reset") return <ResetPassword token={emailToken} done={() => setScreen("login")} />;
  if (screen === "verify") return <VerifyEmail token={emailToken} done={() => setScreen("login")} />;
  if (screen === "invite")
    return <AcceptInvite token={emailToken} signedIn={signedIn} cancel={() => setScreen("login")} />;
  if (screen === "signup") return <Signup signedIn={signedIn} cancel={() => setScreen("login")} />;
  return (
    <Login
      login={login}
      busy={busy}
      error={error || ssoError}
      forgot={() => setScreen("forgot")}
      signup={() => setScreen("signup")}
    />
  );
}
// `next dev` only: the demo login is public, and a production build must not
// advertise it (production refuses to seed it anyway).
const DEMO_LOGIN =
  process.env.NODE_ENV === "development"
    ? { email: "admin@demo.enterai.com", password: "enterai-demo" }
    : { email: "", password: "" };

export function Login({ login, busy, error, forgot, signup }: any) {
  const [email, setEmail] = useState(DEMO_LOGIN.email),
    [password, setPassword] = useState(DEMO_LOGIN.password),
    [ssoEmail, setSsoEmail] = useState(""),
    [sso, setSso] = useState(false);
  const startSso = () => {
    if (!ssoEmail.trim()) return;
    window.location.assign(`${API_BASE}/auth/sso/start?email=${encodeURIComponent(ssoEmail.trim())}`);
  };
  return (
    <AuthShell title="Enter AI" subtitle="Project execution, with AI assistance">
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          login(email, password);
        }}
      >
        <label className="block text-xs text-zinc-400">
          Email
          <input
            type="email"
            autoComplete="email"
            required
            className={field}
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </label>
        <label className="block text-xs text-zinc-400">
          Password
          <input
            type="password"
            autoComplete="current-password"
            required
            className={field}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        {error && <Notice tone="error">{error}</Notice>}
        <Submit busy={busy}>Sign in</Submit>
      </form>
      <div className="mt-5 border-t border-line pt-4">
        <button
          type="button"
          className="w-full text-sm text-sky-300 underline-offset-2 hover:underline"
          onClick={() => setSso((v) => !v)}
        >
          {sso ? "Use password instead" : "Continue with SSO"}
        </button>
        {sso && (
          <form
            className="mt-3 space-y-3"
            onSubmit={(e) => {
              e.preventDefault();
              startSso();
            }}
          >
            <label className="block text-xs text-zinc-400">
              Work email
              <input
                type="email"
                autoComplete="email"
                required
                className={field}
                value={ssoEmail}
                onChange={(e) => setSsoEmail(e.target.value)}
                placeholder="you@company.com"
              />
            </label>
            <Button type="submit" className="w-full">
              Continue with SSO
            </Button>
          </form>
        )}
      </div>
      <div className="mt-5 flex flex-wrap items-center justify-between gap-2 text-xs">
        <button type="button" onClick={forgot} className="text-sky-300 underline-offset-2 hover:underline">
          Forgot your password?
        </button>
        <button type="button" onClick={signup} className="text-zinc-400 underline-offset-2 hover:underline">
          Create a workspace
        </button>
      </div>
    </AuthShell>
  );
}
export function ForgotPassword({ back }: { back: () => void }) {
  const [email, setEmail] = useState(""),
    [busy, setBusy] = useState(false),
    [sent, setSent] = useState(false),
    [error, setError] = useState("");
  return (
    <AuthShell title="Reset your password" subtitle="We'll email you a link to choose a new one">
      {sent ? (
        <>
          <Notice tone="ok">
            If that address has an account, we've sent it a reset link. It expires in 60 minutes.
          </Notice>
          <Button variant="ghost" className="mt-5 w-full" onClick={back}>
            Back to sign in
          </Button>
        </>
      ) : (
        <form
          className="space-y-4"
          onSubmit={async (e) => {
            e.preventDefault();
            setBusy(true);
            setError("");
            try {
              await api("/auth/forgot-password", { method: "POST", body: JSON.stringify({ email }) });
              setSent(true);
            } catch (err: any) {
              setError(err.message);
            } finally {
              setBusy(false);
            }
          }}
        >
          <label className="block text-xs text-zinc-400">
            Email
            <input
              type="email"
              autoComplete="email"
              required
              className={field}
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </label>
          {error && <Notice tone="error">{error}</Notice>}
          <Submit busy={busy}>Email me a link</Submit>
          <Button type="button" variant="ghost" className="w-full" onClick={back}>
            Back to sign in
          </Button>
        </form>
      )}
    </AuthShell>
  );
}
export function ResetPassword({ token, done }: { token: string; done: () => void }) {
  const [password, setPassword] = useState(""),
    [confirm, setConfirm] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [ok, setOk] = useState(false);
  return (
    <AuthShell title="Choose a new password" subtitle="This link can only be used once">
      {ok ? (
        <>
          <Notice tone="ok">Your password has been changed. Any other sessions have been signed out.</Notice>
          <Button className="mt-5 w-full" onClick={done}>
            Sign in
          </Button>
        </>
      ) : (
        <form
          className="space-y-4"
          onSubmit={async (e) => {
            e.preventDefault();
            if (password !== confirm) {
              setError("Those passwords do not match.");
              return;
            }
            setBusy(true);
            setError("");
            try {
              await api("/auth/reset-password", {
                method: "POST",
                body: JSON.stringify({ token, password }),
              });
              setOk(true);
            } catch (err: any) {
              setError(err.message);
            } finally {
              setBusy(false);
            }
          }}
        >
          <label className="block text-xs text-zinc-400">
            New password
            <input
              type="password"
              autoComplete="new-password"
              required
              minLength={8}
              className={field}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>
          <label className="block text-xs text-zinc-400">
            Confirm new password
            <input
              type="password"
              autoComplete="new-password"
              required
              minLength={8}
              className={field}
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
            />
          </label>
          <p className="text-xs text-zinc-500">At least 8 characters.</p>
          {error && <Notice tone="error">{error}</Notice>}
          <Submit busy={busy}>Change password</Submit>
          <Button type="button" variant="ghost" className="w-full" onClick={done}>
            Back to sign in
          </Button>
        </form>
      )}
    </AuthShell>
  );
}
export function VerifyEmail({ token, done }: { token: string; done: () => void }) {
  const [state, setState] = useState<"working" | "ok" | "failed">("working"),
    [error, setError] = useState("");
  useEffect(() => {
    let live = true;
    api("/auth/verify-email", { method: "POST", body: JSON.stringify({ token }) })
      .then(() => live && setState("ok"))
      .catch((err: any) => {
        if (live) {
          setError(err.message);
          setState("failed");
        }
      });
    return () => {
      live = false;
    };
  }, [token]);
  return (
    <AuthShell title="Confirming your email" subtitle="One moment">
      {state === "working" && (
        <p role="status" className="flex items-center gap-2 text-sm text-zinc-400">
          <Loader2 className="animate-spin" size={15} />
          Checking your link...
        </p>
      )}
      {state === "ok" && <Notice tone="ok">Your email address is confirmed.</Notice>}
      {state === "failed" && <Notice tone="error">{error}</Notice>}
      <Button className="mt-5 w-full" onClick={done}>
        Continue to sign in
      </Button>
    </AuthShell>
  );
}
export function AcceptInvite({
  token,
  signedIn,
  cancel,
}: {
  token: string;
  signedIn: () => void;
  cancel: () => void;
}) {
  const [invite, setInvite] = useState<any>(null),
    [name, setName] = useState(""),
    [password, setPassword] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [loadError, setLoadError] = useState("");
  useEffect(() => {
    let live = true;
    api<any>("/auth/invites/" + encodeURIComponent(token) + "/preview")
      .then((r) => live && setInvite(r))
      .catch((err: any) => live && setLoadError(err.message));
    return () => {
      live = false;
    };
  }, [token]);
  if (loadError)
    return (
      <AuthShell title="Invitation" subtitle="We couldn't open this invitation">
        <Notice tone="error">{loadError}</Notice>
        <Button className="mt-5 w-full" onClick={cancel}>
          Back to sign in
        </Button>
      </AuthShell>
    );
  return (
    <AuthShell
      title={invite ? `Join ${invite.organization}` : "Invitation"}
      subtitle={invite ? `Invited as ${invite.email}` : "Loading your invitation"}
    >
      {!invite ? (
        <p role="status" className="flex items-center gap-2 text-sm text-zinc-400">
          <Loader2 className="animate-spin" size={15} />
          Loading...
        </p>
      ) : (
        <form
          className="space-y-4"
          onSubmit={async (e) => {
            e.preventDefault();
            setBusy(true);
            setError("");
            try {
              await api("/auth/accept-invite", {
                method: "POST",
                body: JSON.stringify({ token, name, password }),
              });
              signedIn();
            } catch (err: any) {
              setError(err.message);
            } finally {
              setBusy(false);
            }
          }}
        >
          <p className="text-sm text-zinc-400">
            You'll join as <b className="text-zinc-200">{invite.role}</b>.
          </p>
          <label className="block text-xs text-zinc-400">
            Your name
            <input
              required
              autoComplete="name"
              className={field}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <label className="block text-xs text-zinc-400">
            Choose a password
            <input
              type="password"
              autoComplete="new-password"
              required
              minLength={8}
              className={field}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>
          {error && <Notice tone="error">{error}</Notice>}
          <Submit busy={busy}>Accept invitation</Submit>
          <Button type="button" variant="ghost" className="w-full" onClick={cancel}>
            Back to sign in
          </Button>
        </form>
      )}
    </AuthShell>
  );
}
export function Signup({ signedIn, cancel }: { signedIn: () => void; cancel: () => void }) {
  const [form, setForm] = useState({ organization_name: "", name: "", email: "", password: "" }),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const set = (key: string) => (e: any) => setForm((f) => ({ ...f, [key]: e.target.value }));
  return (
    <AuthShell title="Create your workspace" subtitle="You'll be its first administrator">
      <form
        className="space-y-4"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setError("");
          try {
            await api("/auth/register", { method: "POST", body: JSON.stringify(form) });
            signedIn();
          } catch (err: any) {
            setError(err.message);
          } finally {
            setBusy(false);
          }
        }}
      >
        <label className="block text-xs text-zinc-400">
          Organization name
          <input
            required
            minLength={2}
            className={field}
            value={form.organization_name}
            onChange={set("organization_name")}
          />
        </label>
        <label className="block text-xs text-zinc-400">
          Your name
          <input required autoComplete="name" className={field} value={form.name} onChange={set("name")} />
        </label>
        <label className="block text-xs text-zinc-400">
          Work email
          <input
            type="email"
            autoComplete="email"
            required
            className={field}
            value={form.email}
            onChange={set("email")}
          />
        </label>
        <label className="block text-xs text-zinc-400">
          Password
          <input
            type="password"
            autoComplete="new-password"
            required
            minLength={8}
            className={field}
            value={form.password}
            onChange={set("password")}
          />
        </label>
        <p className="text-xs text-zinc-500">
          At least 8 characters. We'll email you a link to confirm your address.
        </p>
        {error && <Notice tone="error">{error}</Notice>}
        <Submit busy={busy}>Create workspace</Submit>
        <Button type="button" variant="ghost" className="w-full" onClick={cancel}>
          I already have an account
        </Button>
      </form>
    </AuthShell>
  );
}
