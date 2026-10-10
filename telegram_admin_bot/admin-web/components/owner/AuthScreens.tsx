"use client";

import { useEffect, useRef, useState } from "react";
import { CODE_REQUIRED, errorMessage, MIN_PASSWORD, OwnerApiError, ownerApi } from "@/lib/owner/api";
import type { Account, SignupOptions, TermsVersion } from "@/lib/owner/types";
import { useOwner } from "./context";
import { Logo, Notice, s, TermsBox } from "./parts";

// ------------------------------------------------------------------ sign in

export function LoginScreen({ message, onSignup, signup }: {
  message?: string;
  onSignup: () => void;
  signup: SignupOptions | null;
}) {
  const { boot } = useOwner();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [needsCode, setNeedsCode] = useState(false);
  const [notice, setNotice] = useState<{ text: string; info?: boolean } | null>(message ? { text: message } : null);
  const [busy, setBusy] = useState(false);
  const codeRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (needsCode) codeRef.current?.focus();
  }, [needsCode]);

  async function submit(ev: React.FormEvent) {
    ev.preventDefault();
    setBusy(true);
    try {
      await ownerApi("POST", "/api/owner/login", { username: username.trim(), password, code: code.trim() });
      setPassword("");
      setCode("");
      setNotice(null);
      // The server says what comes next: the dashboard, or a password to choose.
      await boot();
    } catch (err) {
      if (err instanceof OwnerApiError && err.detail === CODE_REQUIRED) {
        setNeedsCode(true);
        setNotice({ text: "Enter the 6-digit code from your authenticator app.", info: true });
      } else {
        setNotice({ text: errorMessage(err) });
      }
    } finally {
      setBusy(false);
    }
  }

  const canSignup = Boolean(signup && signup.open && signup.terms);
  return (
    <div className={s.center}>
      <div className={`${s.card} ${s.narrow}`}>
        <Logo />
        <h1>Sign in</h1>
        <p className={s.hint}>Your bookings, messages and weekly numbers.</p>
        <Notice text={notice?.text} info={notice?.info} />
        <form onSubmit={submit} autoComplete="on">
          <label className={s.field}>Username
            <input name="username" type="text" autoComplete="username" autoCapitalize="none" spellCheck={false}
                   required value={username} onChange={(e) => setUsername(e.target.value)} autoFocus />
          </label>
          <label className={s.field}>Password
            <input name="password" type="password" autoComplete="current-password" required value={password}
                   onChange={(e) => setPassword(e.target.value)} />
          </label>
          {needsCode && (
            <label className={s.field}>Code from your authenticator app
              <input ref={codeRef} name="code" type="text" inputMode="numeric" autoComplete="one-time-code"
                     maxLength={7} placeholder="6 digits" value={code} onChange={(e) => setCode(e.target.value)} />
            </label>
          )}
          <button type="submit" className={`${s.btn} ${s.primary} ${s.block}`} disabled={busy}>Sign in</button>
        </form>
        {canSignup && (
          <div className={s.alt}>
            <p className={s.hint}>New here?</p>
            <button type="button" className={`${s.btn} ${s.ghost} ${s.block}`} onClick={onSignup}>Create an account</button>
          </div>
        )}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ sign up

export function SignupScreen({ options, reloadOptions, onBack }: {
  options: SignupOptions | null;
  reloadOptions: () => Promise<SignupOptions | null>;
  onBack: () => void;
}) {
  const { boot } = useOwner();
  const [f, setF] = useState({ name: "", company: "", email: "", phone: "", password: "", repeat: "" });
  const [accept, setAccept] = useState(false);
  const [notice, setNotice] = useState<string>(
    options && options.open && options.terms ? "" : "Sign-up is not open right now. Please contact us for an account.");
  const [busy, setBusy] = useState(false);
  const [terms, setTerms] = useState<{ open: boolean; loading: boolean; data: TermsVersion | null; error: string }>(
    { open: false, loading: false, data: null, error: "" });
  const [termsVersion, setTermsVersion] = useState<{ version: number; title: string } | null>(options?.terms ?? null);
  const [closed, setClosed] = useState(!(options && options.open && options.terms));

  const set = (key: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setF((prev) => ({ ...prev, [key]: e.target.value }));

  // The terms the person reads here are the ones they accept: if they are
  // newer than what the sign-up options said, the tick is cleared.
  async function loadTerms() {
    setTerms((t) => ({ ...t, open: true, loading: true, error: "" }));
    try {
      const data = await ownerApi<TermsVersion>("GET", "/api/terms");
      if (!termsVersion || termsVersion.version !== data.version) {
        setTermsVersion({ version: data.version, title: data.title });
        setAccept(false);
      }
      setTerms({ open: true, loading: false, data, error: "" });
    } catch (err) {
      const status = err instanceof OwnerApiError ? err.status : 0;
      setTerms({ open: true, loading: false, data: null,
                error: status === 404 ? "No terms are published yet." : "Could not load the terms: " + errorMessage(err) });
    }
  }

  function toggleTerms() {
    if (terms.open) setTerms((t) => ({ ...t, open: false }));
    else void loadTerms();
  }

  async function submit(ev: React.FormEvent) {
    ev.preventDefault();
    if (closed || !termsVersion) { setNotice("Sign-up is not open right now. Please contact us for an account."); return; }
    if (f.password.length < MIN_PASSWORD) { setNotice(`Password: at least ${MIN_PASSWORD} characters, please.`); return; }
    if (f.password !== f.repeat) { setNotice("The two passwords are not the same."); return; }
    if (!accept) { setNotice("Please tick the box to accept the terms."); return; }
    setBusy(true);
    try {
      await ownerApi("POST", "/api/owner/signup", {
        email: f.email.trim(), password: f.password, display_name: f.name.trim(), company: f.company.trim(),
        phone: f.phone.trim(), terms_version: termsVersion.version, accept_terms: true,
      });
      setNotice("");
      await boot();
    } catch (err) {
      setNotice(errorMessage(err));
      const status = err instanceof OwnerApiError ? err.status : 0;
      if (status === 403) setClosed(true);
      if (status === 409) {
        // Either the e-mail is taken or the terms changed: re-read the terms to tell.
        const before = termsVersion.version;
        const o = await reloadOptions();
        if (o && o.terms && o.terms.version !== before) {
          setTermsVersion(o.terms);
          setAccept(false);
          if (terms.open) void loadTerms();
        }
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={s.center}>
      <div className={`${s.card} ${s.narrow}`}>
        <h1>Create an account</h1>
        <p className={s.hint}>Tell us about you and your business. We check every new account before it opens.</p>
        <Notice text={notice} />
        <form onSubmit={submit} autoComplete="on">
          <label className={s.field}>Your name
            <input type="text" autoComplete="name" maxLength={200} required value={f.name} onChange={set("name")} autoFocus />
          </label>
          <label className={s.field}>Business name
            <input type="text" autoComplete="organization" maxLength={200} required value={f.company} onChange={set("company")} />
          </label>
          <label className={s.field}>E-mail
            <input type="email" autoComplete="email" autoCapitalize="none" spellCheck={false} maxLength={64} required
                   value={f.email} onChange={set("email")} />
          </label>
          <label className={s.field}>Phone (optional)
            <input type="tel" autoComplete="tel" maxLength={50} value={f.phone} onChange={set("phone")} />
          </label>
          <label className={s.field}>Password (at least 10 characters)
            <input type="password" autoComplete="new-password" minLength={MIN_PASSWORD} required value={f.password}
                   onChange={set("password")} />
          </label>
          <label className={s.field}>Password again
            <input type="password" autoComplete="new-password" minLength={MIN_PASSWORD} required value={f.repeat}
                   onChange={set("repeat")} />
          </label>
          <div className={s.check}>
            <input id="signup-accept" type="checkbox" checked={accept} onChange={(e) => setAccept(e.target.checked)} />
            <label htmlFor="signup-accept">I am at least 18 years old, and I have read and accept the{" "}
              <a href="/terms/" target="_blank" rel="noopener">{termsVersion?.title || "terms of service"}</a>
            </label>
          </div>
          <button type="button" className={`${s.btn} ${s.btnSmall}`} onClick={toggleTerms}>
            {terms.open ? "Hide the terms" : "Read the terms"}
          </button>
          {terms.open && (
            terms.loading ? <div className={s.loading}>Loading…</div>
              : terms.data ? <TermsBox terms={terms.data} className={s.signupTerms} />
              : <div className={s.errorBox}>{terms.error}</div>
          )}
          <button type="submit" className={`${s.btn} ${s.primary} ${s.block}`} disabled={busy}>Create my account</button>
        </form>
        <button type="button" className={`${s.btn} ${s.ghost} ${s.block}`} onClick={onBack}>Back to sign in</button>
      </div>
    </div>
  );
}

// --------------------------------------------------- forced password change

export function ChangePasswordScreen({ message }: { message?: string }) {
  const { boot, toast, signOut } = useOwner();
  const [f, setF] = useState({ current: "", next: "", repeat: "" });
  const [notice, setNotice] = useState(message ?? "");
  const [busy, setBusy] = useState(false);
  const { authProblem } = useOwner();

  async function submit(ev: React.FormEvent) {
    ev.preventDefault();
    if (f.next.length < MIN_PASSWORD) { setNotice(`At least ${MIN_PASSWORD} characters, please.`); return; }
    if (f.next !== f.repeat) { setNotice("The two new passwords are not the same."); return; }
    setBusy(true);
    try {
      await ownerApi("POST", "/api/owner/password", { current: f.current, new: f.next });
      setF({ current: "", next: "", repeat: "" });
      toast("Password saved.", "info");
      await boot();
    } catch (err) {
      if (!authProblem(err)) setNotice(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={s.center}>
      <div className={`${s.card} ${s.narrow}`}>
        <h1>Choose your password</h1>
        <p className={s.hint}>You signed in with a temporary password. Choose your own to continue
          (at least 10 characters).</p>
        <Notice text={notice} />
        <form onSubmit={submit} autoComplete="on">
          <input type="text" name="username" autoComplete="username" hidden readOnly />
          <label className={s.field}>Temporary password
            <input type="password" autoComplete="current-password" required value={f.current}
                   onChange={(e) => setF({ ...f, current: e.target.value })} autoFocus />
          </label>
          <label className={s.field}>New password
            <input type="password" autoComplete="new-password" minLength={MIN_PASSWORD} required value={f.next}
                   onChange={(e) => setF({ ...f, next: e.target.value })} />
          </label>
          <label className={s.field}>New password again
            <input type="password" autoComplete="new-password" minLength={MIN_PASSWORD} required value={f.repeat}
                   onChange={(e) => setF({ ...f, repeat: e.target.value })} />
          </label>
          <button type="submit" className={`${s.btn} ${s.primary} ${s.block}`} disabled={busy}>Save and continue</button>
        </form>
        <button type="button" className={`${s.btn} ${s.ghost} ${s.block}`} onClick={() => void signOut()}>Sign out</button>
      </div>
    </div>
  );
}

// ---------------------------------------------------- waiting, not approved

export function PendingScreen({ account }: { account: Account }) {
  const { boot, signOut, authProblem } = useOwner();
  const [notice, setNotice] = useState<{ text: string; info?: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const name = account.display_name || account.username || "";

  async function refresh() {
    setBusy(true);
    try {
      const fresh = await ownerApi<Account>("GET", "/api/owner/account");
      if (fresh.gate === "pending_approval") {
        setNotice({ text: "Still waiting for approval. Please check again later.", info: true });
        return;
      }
      await boot();
    } catch (err) {
      if (!authProblem(err)) setNotice({ text: "Could not check: " + errorMessage(err) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={s.center}>
      <div className={`${s.card} ${s.narrow}`}>
        <h1>Almost there</h1>
        <p>{(name ? `Thanks, ${name}. ` : "Thanks. ") +
          "Your account is waiting for approval. We check every new account before it opens. " +
          "Please come back later, or tap Refresh status."}</p>
        <Notice text={notice?.text} info={notice?.info} />
        <button type="button" className={`${s.btn} ${s.primary} ${s.block}`} disabled={busy} onClick={() => void refresh()}>
          Refresh status</button>
        <button type="button" className={`${s.btn} ${s.ghost} ${s.block}`} onClick={() => void signOut()}>Sign out</button>
      </div>
    </div>
  );
}

export function RejectedScreen({ account }: { account: Account }) {
  const { signOut } = useOwner();
  const reason = (account.review_reason || "").trim();
  return (
    <div className={s.center}>
      <div className={`${s.card} ${s.narrow}`}>
        <h1>Account not approved</h1>
        <p>Sorry, your account request was not approved.</p>
        {reason && <p className={s.reason}>Reason: {reason}</p>}
        <p className={s.hint}>If you think this is a mistake, please contact us.</p>
        <button type="button" className={`${s.btn} ${s.ghost} ${s.block}`} onClick={() => void signOut()}>Sign out</button>
      </div>
    </div>
  );
}
