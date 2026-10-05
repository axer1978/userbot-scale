"use client";

// "Add an account": first which network, then
// - Telegram: credentials, the login code, the cloud password if the
//   account has one. The server holds one sign-in in progress at a time;
// - WhatsApp: the number is linked as a device of the phone (WhatsAppLink).
// Opening the dialog picks either up at whatever step it is on.

import { useEffect, useRef, useState } from "react";
import { WhatsAppLink, waPairingInProgress } from "@/components/WhatsAppLink";
import { api, errorText } from "@/lib/api";
import { usePanel } from "@/lib/panel";
import type { AuthStep } from "@/lib/types";

function Logo() {
  return (
    <div className="logo">
      <svg width="24" height="24" viewBox="0 0 32 32" aria-hidden="true"><path d="M7 16.5l17-7-6 16-3.5-6z" fill="#fff" /></svg>
    </div>
  );
}

// Telegram names the one channel a resend may use, and how long it must be
// asked to wait; the button shows both rather than promising an SMS.
function ResendButton({ auth, onResent, onError }: {
  auth: AuthStep; onResent: (auth: AuthStep) => void; onError: (message: string) => void;
}) {
  const [left, setLeft] = useState(auth.resend_in || 0);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (left <= 0) return;
    const timer = setTimeout(() => setLeft((n) => n - 1), 1000);
    return () => clearTimeout(timer);
  }, [left]);

  if (!auth.next_label) return null;
  return (
    <button type="button" disabled={busy || left > 0} onClick={async () => {
      setBusy(true);
      try { onResent(await api<AuthStep>("POST", "/api/auth/resend")); }
      catch (err) { onError(errorText(err)); }
      finally { setBusy(false); }
    }}>
      {left > 0 ? `${auth.next_label} (${left}s)` : auth.next_label}
    </button>
  );
}

function TelegramSignIn({ auth }: { auth: AuthStep }) {
  const { setAddAccount, finishAddAccount } = usePanel();
  // A notice belongs to the step it was raised on; otherwise the server's own shows.
  const [raised, setRaised] = useState<{ text: string; info?: boolean; on: AuthStep | null } | null>(null);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({ label: "", api_id: "", api_hash: "", phone: "", proxy_url: "", deepseek_api_key: "" });
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const focus = useRef<HTMLInputElement>(null);
  const step = auth.step;

  useEffect(() => { focus.current?.focus(); }, [step]);

  const notice = raised && raised.on === auth ? raised : auth.notice ? { text: auth.notice, info: false } : null;

  // Busy while the request runs, so a double click can't ask Telegram for two codes.
  const run = async (path: string, body: unknown) => {
    setBusy(true);
    try {
      const next = await api<AuthStep>("POST", path, body);
      setAddAccount(next);
      if (next.step === "done") finishAddAccount(next.session_id || null);
      return true;
    } catch (err) {
      // Some failures (an expired code) send the flow back to the start.
      let now: AuthStep = auth;
      try { now = await api<AuthStep>("GET", "/api/auth"); setAddAccount(now); } catch { /* keep the step */ }
      setRaised({ text: errorText(err), on: now });
      return false;
    } finally {
      setBusy(false);
    }
  };

  const restart = async () => {
    try { setAddAccount(await api<AuthStep>("POST", "/api/auth/cancel")); }
    catch (err) { setRaised({ text: errorText(err), on: auth }); }
  };

  const set = (key: keyof typeof form) => (ev: React.ChangeEvent<HTMLInputElement>) =>
    setForm((f) => ({ ...f, [key]: ev.target.value }));

  return (
    <>
        {notice && <div className={notice.info ? "notice info" : "notice"}>{notice.text}</div>}

        {(step === "credentials" || !["code", "password"].includes(step || "")) && (
          <form autoComplete="off" onSubmit={async (ev) => {
            ev.preventDefault();
            const ok = await run("/api/auth/start", {
              api_id: form.api_id.trim(), api_hash: form.api_hash.trim(), phone: form.phone.trim(),
              deepseek_api_key: form.deepseek_api_key.trim(), label: form.label.trim(), proxy_url: form.proxy_url.trim(),
            });
            if (ok) {
              setForm((f) => ({ ...f, api_hash: "", deepseek_api_key: "", proxy_url: "" }));
              setCode("");
            }
          }}>
            <h2>Add a Telegram account</h2>
            <p className="hint">The assistant runs on the server, not in this browser, so it keeps answering while this
              computer is off. Credentials are stored encrypted on the server; you do this once per account.</p>
            <div className="field"><label htmlFor="l-label">Name <span className="muted">(optional)</span></label>
              <input id="l-label" type="text" value={form.label} onChange={set("label")}
                     placeholder="Shown in the account picker — defaults to the phone number" /></div>
            <div className="row">
              <div className="field"><label htmlFor="l-api-id">API ID</label>
                <input id="l-api-id" ref={focus} type="text" inputMode="numeric" placeholder="1234567" required
                       value={form.api_id} onChange={set("api_id")} /></div>
              <div className="field"><label htmlFor="l-api-hash">API hash</label>
                <input id="l-api-hash" type="password" placeholder="32-character hex string" autoComplete="off" required
                       value={form.api_hash} onChange={set("api_hash")} /></div>
            </div>
            <p className="hint" style={{ marginTop: -6 }}>Both come from{" "}
              <a href="https://my.telegram.org" target="_blank" rel="noopener noreferrer">my.telegram.org</a> → API development tools.</p>
            <div className="field"><label htmlFor="l-phone">Phone number</label>
              <input id="l-phone" type="tel" placeholder="+34600123456 — international format" required
                     value={form.phone} onChange={set("phone")} /></div>
            <div className="field"><label htmlFor="l-proxy">Proxy <span className="muted">(optional)</span></label>
              <input id="l-proxy" type="password" placeholder="socks5://user:password@host:port" autoComplete="off"
                     value={form.proxy_url} onChange={set("proxy_url")} /></div>
            <p className="hint" style={{ marginTop: -6 }}>A residential or mobile proxy in the account&apos;s own country, so it
              signs in and runs from there instead of from this server. Leave empty to connect directly.</p>
            <div className="field"><label htmlFor="l-deepseek">DeepSeek API key</label>
              <input id="l-deepseek" type="password" placeholder="sk-…" autoComplete="off"
                     value={form.deepseek_api_key} onChange={set("deepseek_api_key")} /></div>
            <p className="hint" style={{ marginTop: -6 }}>From{" "}
              <a href="https://platform.deepseek.com" target="_blank" rel="noopener noreferrer">platform.deepseek.com</a>.
              Used to write the replies.</p>
            <div className="sheet-actions">
              <button type="button" className="btn" onClick={() => finishAddAccount(null)}>Cancel</button>
              <button type="submit" className="btn primary" disabled={busy}>{busy ? "…" : "Send code"}</button>
            </div>
          </form>
        )}

        {step === "code" && (
          <form autoComplete="off" onSubmit={(ev) => { ev.preventDefault(); void run("/api/auth/code", { code: code.trim() }); }}>
            <h2>Enter the code</h2>
            <div className="where">
              <b>Telegram sent the code for {auth.phone || "your number"}:</b>
              {auth.delivery || "in the Telegram app itself."}
            </div>
            <div className="field">
              <input ref={focus} className="code-input" type="text" inputMode="numeric" autoComplete="one-time-code"
                     placeholder="•••••" maxLength={auth.code_length || undefined}
                     value={code} onChange={(ev) => setCode(ev.target.value)} />
            </div>
            <div className="sheet-actions">
              <button type="button" className="btn" onClick={() => finishAddAccount(null)}>Cancel</button>
              <button type="submit" className="btn primary" disabled={busy}>{busy ? "…" : "Sign in"}</button>
            </div>
            <div className="links">
              <ResendButton key={`${auth.next_label}-${auth.resend_in}`} auth={auth}
                            onResent={(next) => { setAddAccount(next); setRaised({ text: "Code sent again.", info: true, on: next }); }}
                            onError={(text) => setRaised({ text, on: auth })} />
              <button type="button" onClick={restart}>Start over</button>
            </div>
          </form>
        )}

        {step === "password" && (
          <form autoComplete="off" onSubmit={async (ev) => {
            ev.preventDefault();
            await run("/api/auth/password", { password });
            setPassword("");
          }}>
            <h2>Two-step verification</h2>
            <p className="hint">This account has a cloud password set in Telegram. Enter it to finish signing in.
              It is used once and never stored.</p>
            <div className="field"><label htmlFor="l-password">Password</label>
              <input id="l-password" ref={focus} type="password" autoComplete="current-password"
                     value={password} onChange={(ev) => setPassword(ev.target.value)} /></div>
            <div className="sheet-actions">
              <button type="button" className="btn" onClick={() => finishAddAccount(null)}>Cancel</button>
              <button type="submit" className="btn primary" disabled={busy}>{busy ? "…" : "Sign in"}</button>
            </div>
            <div className="links">
              <button type="button" onClick={restart}>Start over</button>
            </div>
          </form>
        )}
    </>
  );
}

type Network = "choose" | "telegram" | "whatsapp";

// Where the dialog opens: a Telegram sign-in half done, a WhatsApp pairing
// this tab is following, or the choice of network.
function firstStep(auth: AuthStep): Network {
  if (auth.step === "code" || auth.step === "password") return "telegram";
  if (waPairingInProgress()) return "whatsapp";
  return "choose";
}

export function AddAccountDialog() {
  const { addAccount: auth, setAddAccount, finishAddAccount } = usePanel();
  const [network, setNetwork] = useState<Network>("choose");
  const [error, setError] = useState<string | null>(null);
  // Each time the dialog opens, start from where things stand.
  const [wasOpen, setWasOpen] = useState(false);
  if (!!auth !== wasOpen) {
    setWasOpen(!!auth);
    if (auth) { setNetwork(firstStep(auth)); setError(null); }
  }

  if (!auth) return null;
  return (
    <div className="fullscreen" role="dialog" aria-modal="true">
      <div className="sheet w-440">
        <Logo />
        {network === "choose" && <>
          {error && <div className="notice">{error}</div>}
          <h2>Add an account</h2>
          <p className="hint">Which network is the account on?</p>
          <div className="channel-choice">
            <button type="button" className="btn channel-option" onClick={async () => {
              try { setAddAccount(await api<AuthStep>("GET", "/api/auth")); setNetwork("telegram"); }
              catch (err) { setError(errorText(err)); }
            }}>
              <b>Telegram</b><span>Sign in with the API ID and hash from my.telegram.org and a login code.</span>
            </button>
            <button type="button" className="btn channel-option" onClick={() => setNetwork("whatsapp")}>
              <b>WhatsApp</b><span>Link the number as a device of the phone: scan a QR code or type a pairing code.</span>
            </button>
          </div>
          <div className="sheet-actions">
            <button type="button" className="btn" onClick={() => finishAddAccount(null, { keepTelegram: true })}>Cancel</button>
          </div>
        </>}
        {network === "telegram" && <TelegramSignIn auth={auth} />}
        {network === "whatsapp" && <WhatsAppLink onBack={() => setNetwork("choose")} />}
      </div>
    </div>
  );
}
