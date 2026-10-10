"use client";

import qrcode from "qrcode-generator";
import { useEffect, useMemo, useState } from "react";
import { errorMessage, MIN_PASSWORD, ownerApi } from "@/lib/owner/api";
import type { TotpSetup } from "@/lib/owner/types";
import { useMe, useOwner } from "./context";
import { s } from "./parts";

// UTF-8, as authenticator apps read it (the setup link is ASCII anyway).
qrcode.stringToBytes = (str: string) => Array.from(new TextEncoder().encode(str));

/** A QR code as inline SVG (never HTML): dark modules on white with the 4-module quiet zone scanners need. */
function QrCode({ text }: { text: string }) {
  const qr = useMemo(() => {
    try {
      const code = qrcode(0, "M");
      code.addData(text);
      code.make();
      const count = code.getModuleCount();
      const quiet = 4;
      let path = "";
      for (let r = 0; r < count; r += 1) {
        for (let c = 0; c < count; c += 1) {
          if (code.isDark(r, c)) path += `M${c + quiet} ${r + quiet}h1v1h-1z`;
        }
      }
      return { size: count + quiet * 2, path };
    } catch {
      return null;
    }
  }, [text]);
  if (!qr) return null;
  return (
    <svg className={s.qr} viewBox={`0 0 ${qr.size} ${qr.size}`} role="img" aria-label="Setup code to scan"
         shapeRendering="crispEdges">
      <rect width={qr.size} height={qr.size} fill="#fff" />
      <path d={qr.path} fill="#000" />
    </svg>
  );
}

function groupSecret(secret: string): string {
  return secret.replace(/(.{4})/g, "$1 ").trim();
}

function CodeField({ value, onChange, id }: { value: string; onChange: (v: string) => void; id: string }) {
  return (
    <label className={s.field} htmlFor={id}>Code from the app
      <input id={id} type="text" inputMode="numeric" autoComplete="one-time-code" maxLength={7} placeholder="6 digits"
             value={value} onChange={(e) => onChange(e.target.value)} />
    </label>
  );
}

function TotpBox() {
  const { me, setMe, toast, authProblem } = useOwner();
  const [setup, setSetup] = useState<TotpSetup | null>(null);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);

  async function run(fn: () => Promise<void>) {
    setBusy(true);
    try { await fn(); } catch (err) { if (!authProblem(err)) toast(errorMessage(err)); } finally { setBusy(false); }
  }

  if (!me) return null;
  if (me.totp) {
    return (
      <div>
        <p className={s.hint}>On. Signing in asks for a code from your authenticator app. To turn it off, enter a current code.</p>
        <CodeField id="totp-off-code" value={code} onChange={setCode} />
        <button type="button" className={`${s.btn} ${s.danger} ${s.block}`} disabled={busy}
                onClick={() => void run(async () => {
                  await ownerApi("DELETE", "/api/owner/totp", { code: code.trim() });
                  setMe({ ...me, totp: false });
                  setCode("");
                  toast("Two-step sign-in is off.", "info");
                })}>Turn off two-step sign-in</button>
      </div>
    );
  }
  if (!setup) {
    return (
      <div>
        <p className={s.hint}>Off. With it on, signing in also asks for a 6-digit code from an app such as
          Google Authenticator, Authy or 1Password, so a stolen password alone is not enough.</p>
        <button type="button" className={`${s.btn} ${s.block}`} disabled={busy}
                onClick={() => void run(async () => setSetup(await ownerApi<TotpSetup>("POST", "/api/owner/totp", {})))}>
          Set up two-step sign-in</button>
      </div>
    );
  }
  return (
    <div>
      <p className={s.hint}>1. In your authenticator app, scan this code, or add an account and type in the key below:</p>
      <QrCode text={setup.uri} />
      <div className={s.secret}>{groupSecret(setup.secret)}</div>
      <p className={s.hint}>On the phone that has the app, you can also use this setup link:</p>
      <div className={s.secret}>{setup.uri}</div>
      <p className={s.hint}>2. Enter the code the app shows now:</p>
      <CodeField id="totp-on-code" value={code} onChange={setCode} />
      <button type="button" className={`${s.btn} ${s.primary} ${s.block}`} disabled={busy}
              onClick={() => void run(async () => {
                await ownerApi("POST", "/api/owner/totp", { code: code.trim() });
                setMe({ ...me, totp: true });
                setSetup(null);
                setCode("");
                toast("Two-step sign-in is on.", "info");
              })}>Turn on</button>
      <button type="button" className={`${s.btn} ${s.ghost} ${s.block}`} disabled={busy}
              onClick={() => { setSetup(null); setCode(""); }}>Cancel</button>
    </div>
  );
}

export function SettingsSheet({ onClose }: { onClose: () => void }) {
  const me = useMe();
  const { toast, authProblem, signOut } = useOwner();
  const [f, setF] = useState({ current: "", next: "", repeat: "" });
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => { if (ev.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  async function changePassword(ev: React.FormEvent) {
    ev.preventDefault();
    if (f.next.length < MIN_PASSWORD) { toast(`At least ${MIN_PASSWORD} characters, please.`); return; }
    if (f.next !== f.repeat) { toast("The two new passwords are not the same."); return; }
    setBusy(true);
    try {
      await ownerApi("POST", "/api/owner/password", { current: f.current, new: f.next });
      setF({ current: "", next: "", repeat: "" });
      toast("Password changed.", "info");
    } catch (err) {
      if (!authProblem(err)) toast(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={s.sheetBackdrop} onClick={(ev) => { if (ev.target === ev.currentTarget) onClose(); }}>
      <div className={s.sheet} role="dialog" aria-modal="true" aria-labelledby="settings-title">
        <div className={s.sheetHead}>
          <h2 id="settings-title">Settings</h2>
          <button type="button" className={`${s.btn} ${s.ghost}`} onClick={onClose}>Close</button>
        </div>

        <section className={s.sheetBlock}>
          <h3>Change password</h3>
          <form onSubmit={changePassword} autoComplete="on">
            <input type="text" name="username" autoComplete="username" hidden readOnly value={me.username} />
            <label className={s.field}>Current password
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
            <button type="submit" className={`${s.btn} ${s.primary} ${s.block}`} disabled={busy}>Change password</button>
          </form>
          <p className={s.hint}>Changing it signs you out on your other devices.</p>
        </section>

        <section className={s.sheetBlock}>
          <h3>Two-step sign-in</h3>
          <TotpBox />
        </section>

        <section className={s.sheetBlock}>
          <a href="/terms/" target="_blank" rel="noopener" className={s.small}>Terms of service</a>
        </section>

        <section className={s.sheetBlock}>
          <button type="button" className={`${s.btn} ${s.danger} ${s.block}`} onClick={() => void signOut()}>Sign out</button>
        </section>
      </div>
    </div>
  );
}
