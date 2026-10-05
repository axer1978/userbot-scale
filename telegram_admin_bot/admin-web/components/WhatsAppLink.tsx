"use client";

// Adding a WhatsApp account: the number is linked as a device of the phone,
// like WhatsApp Web. The wa-gateway service does the linking; the panel
// follows it, and this polls the panel every 2 s for the latest QR code,
// pairing code or outcome.

import qrcode from "qrcode-generator";
import { useEffect, useRef, useState } from "react";
import { api, ApiError, errorText } from "@/lib/api";
import { usePanel } from "@/lib/panel";
import type { WaPairing } from "@/lib/types";

const FINISHED: WaPairing["status"][] = ["paired", "failed", "cancelled", "expired"];
const POLL_MS = 2000;

const QR_HELP = "On the phone: WhatsApp → Settings (on Android: ⋮ menu) → Linked devices → " +
                "Link a device, then point the camera at this code. It changes about every 20 seconds.";
const CODE_HELP = "On the phone: WhatsApp → Settings (on Android: ⋮ menu) → Linked devices → " +
                  "Link a device → Link with phone number instead, then type this code.";

// The pairing this tab follows. Kept outside the dialog, so closing and
// opening it again picks the pairing up where it is.
const following: { pairId: string | null; status: WaPairing["status"] | null } = { pairId: null, status: null };

export function waPairingInProgress(): boolean {
  return !!following.pairId && !FINISHED.includes(following.status!);
}

// Dark modules on white, with the 4-module quiet zone scanners need, whole
// pixels per module so it stays sharp.
function drawQr(canvas: HTMLCanvasElement, text: string) {
  // UTF-8, as the gateway's QR library encodes it (the string is ASCII anyway).
  qrcode.stringToBytes = (str: string) => Array.from(new TextEncoder().encode(str));
  const qr = qrcode(0, "M");
  qr.addData(text);
  qr.make();
  const count = qr.getModuleCount();
  const quiet = 4;
  const scale = Math.max(2, Math.floor(264 / (count + quiet * 2)));
  const size = (count + quiet * 2) * scale;
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  ctx.fillStyle = "#fff";
  ctx.fillRect(0, 0, size, size);
  ctx.fillStyle = "#000";
  for (let r = 0; r < count; r += 1) {
    for (let c = 0; c < count; c += 1) {
      if (qr.isDark(r, c)) ctx.fillRect((c + quiet) * scale, (r + quiet) * scale, scale, scale);
    }
  }
}

export function WhatsAppLink({ onBack }: { onBack: () => void }) {
  const { finishAddAccount } = usePanel();
  const [form, setForm] = useState({ label: "", phone: "", deepseek: "", method: "qr" as "qr" | "code" });
  const [pairing, setPairing] = useState<WaPairing | null>(
    () => (waPairingInProgress() ? { pair_id: following.pairId!, status: following.status! } : null));
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const canvas = useRef<HTMLCanvasElement>(null);
  const first = useRef<HTMLInputElement>(null);

  const pairId = pairing?.pair_id ?? null;
  const status = pairing?.status ?? null;
  const live = !!pairId && !!status && !FINISHED.includes(status);

  // Every answer goes through here: it is also what the next open picks up.
  const apply = (p: WaPairing) => {
    following.status = p.status;
    if (p.status === "paired") {
      following.pairId = null;
      following.status = null;
      finishAddAccount(p.session_id || null, {
        keepTelegram: true,
        message: "WhatsApp linked — it starts within about 15 seconds. Replies wait for your approval until you " +
                 "turn on auto-send in Settings.",
      });
      return;
    }
    if (p.status === "cancelled") {
      following.pairId = null;
      setPairing(null);
      setNotice(null);
      return;
    }
    if (p.status === "failed" || p.status === "expired") setNotice(p.error || "WhatsApp did not link the number.");
    setPairing((current) => ({ ...current, ...p }));
  };

  useEffect(() => {
    if (!live || !pairId) return;
    const timer = setInterval(() => {
      api<WaPairing>("GET", `/api/wa/pair/${encodeURIComponent(pairId)}`).then((p) => {
        if (following.pairId === pairId) apply(p);
      }, (err) => {
        if (err instanceof ApiError && err.status === 404 && following.pairId === pairId) {
          apply({ status: "expired", error: err.message });
        }
        // Anything else (a network blip): the next poll tries again.
      });
    }, POLL_MS);
    return () => clearInterval(timer);
    // `apply` only reads refs and setters; restarting the timer on its identity would skip polls.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live, pairId]);

  const qr = status === "qr" ? pairing?.qr || "" : null;
  useEffect(() => {
    if (qr !== null && canvas.current) drawQr(canvas.current, qr);
  }, [qr]);

  useEffect(() => { if (!pairing) first.current?.focus(); }, [pairing]);

  const cancel = async () => {
    const id = following.pairId;
    if (id && waPairingInProgress()) {
      try { await api("POST", `/api/wa/pair/${encodeURIComponent(id)}/cancel`); } catch { /* closing anyway */ }
    }
    following.pairId = null;
    following.status = null;
    finishAddAccount(null, { keepTelegram: true });
  };

  if (!pairing) {
    return (
      <form autoComplete="off" onSubmit={async (ev) => {
        ev.preventDefault();
        setBusy(true);
        setNotice(null);
        try {
          const started = await api<WaPairing>("POST", "/api/wa/pair/start", {
            label: form.label.trim(), phone: form.phone.trim(), deepseek_api_key: form.deepseek.trim(), method: form.method,
          });
          setForm((f) => ({ ...f, deepseek: "" }));
          following.pairId = started.pair_id || null;
          setPairing(started);
          apply(started);
        } catch (err) {
          setNotice(errorText(err));
        } finally {
          setBusy(false);
        }
      }}>
        {notice && <div className="notice">{notice}</div>}
        <h2>Add a WhatsApp account</h2>
        <p className="hint">The number is linked as a device of the phone, like WhatsApp Web, and the assistant runs
          on the server. Keep the phone itself online now and then: WhatsApp unlinks devices of a phone that stays
          offline for about two weeks.</p>
        <div className="field"><label htmlFor="wa-label">Name <span className="muted">(optional)</span></label>
          <input id="wa-label" ref={first} type="text" placeholder="Shown in the account picker — defaults to the phone number"
                 value={form.label} onChange={(ev) => setForm({ ...form, label: ev.target.value })} /></div>
        <div className="field"><label htmlFor="wa-phone">Phone number</label>
          <input id="wa-phone" type="tel" placeholder="+37120000001 — international format" required
                 value={form.phone} onChange={(ev) => setForm({ ...form, phone: ev.target.value })} /></div>
        <div className="field"><label htmlFor="wa-deepseek">DeepSeek API key</label>
          <input id="wa-deepseek" type="password" placeholder="sk-…" autoComplete="off"
                 value={form.deepseek} onChange={(ev) => setForm({ ...form, deepseek: ev.target.value })} /></div>
        <p className="hint" style={{ marginTop: -6 }}>From{" "}
          <a href="https://platform.deepseek.com" target="_blank" rel="noopener noreferrer">platform.deepseek.com</a>.
          Leave empty when linking a number again that already has one.</p>
        <div className="field"><label>How to link</label>
          <div className="check">
            <input id="wa-method-qr" type="radio" name="wa-method" checked={form.method === "qr"}
                   onChange={() => setForm({ ...form, method: "qr" })} />
            <label htmlFor="wa-method-qr">Scan a QR code with the phone</label>
          </div>
          <div className="check">
            <input id="wa-method-code" type="radio" name="wa-method" checked={form.method === "code"}
                   onChange={() => setForm({ ...form, method: "code" })} />
            <label htmlFor="wa-method-code">Type a pairing code on the phone (when the phone can&apos;t scan this screen)</label>
          </div>
        </div>
        <div className="sheet-actions">
          <button type="button" className="btn" onClick={() => { setNotice(null); onBack(); }}>Back</button>
          <button type="submit" className="btn primary" disabled={busy}>{busy ? "…" : "Link WhatsApp"}</button>
        </div>
      </form>
    );
  }

  const code = String(pairing.code || "");
  const finished = !!status && FINISHED.includes(status);
  return (
    <div>
      {notice && <div className="notice">{notice}</div>}
      <h2>Link WhatsApp</h2>
      {(status === "qr" || status === "code") && <div className="where">{status === "qr" ? QR_HELP : CODE_HELP}</div>}
      {status === "qr" && <div className="wa-qr"><canvas ref={canvas} width={264} height={264} /></div>}
      {status === "code" && <div className="wa-code">{code.length === 8 ? `${code.slice(0, 4)}-${code.slice(4)}` : code}</div>}
      <p className="hint">
        {status === "qr" ? "Waiting for the phone to scan…"
          : status === "code" ? "Waiting for the code to be typed on the phone…"
          : status === "waiting" ? (pairing.method === "code" ? "Asking WhatsApp for a pairing code…" : "Getting a QR code from WhatsApp…")
          : ""}
      </p>
      <div className="sheet-actions">
        <button type="button" className="btn" onClick={cancel}>{finished ? "Close" : "Cancel"}</button>
        {(status === "failed" || status === "expired") && (
          <button type="button" className="btn primary" onClick={() => {
            following.pairId = null;
            following.status = null;
            setPairing(null);
            setNotice(null);
          }}>Try again</button>
        )}
      </div>
    </div>
  );
}
