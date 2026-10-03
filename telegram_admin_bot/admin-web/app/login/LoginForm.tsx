"use client";

// The shared admin password, plus an authenticator code when the server
// has ADMIN_TOTP_SECRET set (it says so on /api/login-options).

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { api, errorText } from "@/lib/api";

export function LoginForm({ next }: { next: string }) {
  const router = useRouter();
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [totp, setTotp] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    input.current?.focus();
    api<{ totp: boolean }>("GET", "/api/login-options")
      .then((opts) => setTotp(!!opts.totp))
      .catch((err) => setNotice("Could not reach the admin API: " + errorText(err)));
  }, []);

  return (
    <div className="fullscreen">
      <div className="sheet w-380">
        <div className="logo">
          <svg width="24" height="24" viewBox="0 0 32 32" aria-hidden="true"><path d="M7 16.5l17-7-6 16-3.5-6z" fill="#fff" /></svg>
        </div>
        <h2>Admin sign-in</h2>
        <p className="hint">Enter the shared admin password to manage sessions.</p>
        {notice && <div className="notice">{notice}</div>}
        <form autoComplete="off" onSubmit={async (ev) => {
          ev.preventDefault();
          setBusy(true);
          try {
            await api("POST", "/api/login", { password, code: code.trim() });
            setPassword("");
            setCode("");
            router.replace(next);
            router.refresh();
          } catch (err) {
            setNotice(errorText(err) || "Wrong password");
            setBusy(false);
          }
        }}>
          <div className="field">
            <label htmlFor="ag-password">Password</label>
            <input id="ag-password" ref={input} type="password" autoComplete="current-password" required
                   value={password} onChange={(ev) => setPassword(ev.target.value)} />
          </div>
          {totp && (
            <div className="field">
              <label htmlFor="ag-code">Authenticator code</label>
              <input id="ag-code" type="text" inputMode="numeric" autoComplete="one-time-code" maxLength={7} required
                     placeholder="6 digits from your authenticator app"
                     value={code} onChange={(ev) => setCode(ev.target.value)} />
            </div>
          )}
          <div className="sheet-actions">
            <button type="submit" className="btn primary" disabled={busy}>Sign in</button>
          </div>
        </form>
      </div>
    </div>
  );
}
