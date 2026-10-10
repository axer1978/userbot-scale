"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { errorMessage, OwnerApiError, ownerApi, ownerUpload } from "@/lib/owner/api";
import { MB, sizeMb, videoName, fmtWhen } from "@/lib/owner/format";
import type { Challenge, OwnerTerms, Verification } from "@/lib/owner/types";
import { cx } from "@/lib/format";
import { useOwner } from "./context";
import { Notice, s, TermsBox, termsMeta } from "./parts";

// ------------------------------------------------------ accept (new) terms

export function TermsGateScreen() {
  const { boot, toast, toLogin, signOut, authProblem } = useOwner();
  const [data, setData] = useState<OwnerTerms | null>(null);
  const [notice, setNotice] = useState<{ text: string; info?: boolean } | null>(null);
  const [checked, setChecked] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback((message?: string): Promise<void> => ownerApi<OwnerTerms>("GET", "/api/owner/terms").then(
    (d) => {
      setData(d);
      setChecked(false);
      setNotice(message ? { text: message, info: true } : null);
    },
    (err) => {
      if (!authProblem(err)) toLogin("Could not load the terms of service: " + errorMessage(err));
    },
  ), [authProblem, toLogin]);

  useEffect(() => {
    load().catch(() => undefined);
  }, [load]);

  async function accept() {
    const t = data?.terms;
    if (!t || !checked || busy) return;
    setBusy(true);
    try {
      await ownerApi("POST", "/api/owner/terms/accept", { version: t.version });
      toast("Thank you. The terms are accepted.", "info");
      await boot();
    } catch (err) {
      if (err instanceof OwnerApiError && err.status === 409) {
        await load(err.detail || "The terms changed in the meantime. Please read the new version.");
      } else if (!authProblem(err)) {
        setNotice({ text: errorMessage(err) });
      }
    } finally {
      setBusy(false);
    }
  }

  if (!data) return <div className={s.center}><div className={s.loading}>Loading…</div></div>;
  const t = data.terms;
  const note = (t?.change_note || "").trim();
  return (
    <div className={s.center}>
      <div className={`${s.card} ${s.wide}`}>
        <h1>{t?.title || "Terms of service"}</h1>
        <p className={s.hint}>{!t ? "" : data.accepted
          ? "Our terms of service changed. Please read the new version and accept it to keep using the dashboard."
          : "Please read our terms of service and accept them to use the dashboard."}</p>
        {t && <p className={cx(s.muted, s.small)}>{termsMeta(t)}</p>}
        {note && <p className={cx(s.notice, s.info)}>What changed: {note}</p>}
        {t ? <TermsBox terms={t} noMeta /> : <div className={s.empty}>No terms are published yet.</div>}
        <Notice text={notice?.text} info={notice?.info} />
        <div className={s.check}>
          <input id="terms-accept-check" type="checkbox" disabled={!t} checked={checked}
                 onChange={(e) => setChecked(e.target.checked)} />
          <label htmlFor="terms-accept-check">I am at least 18 years old, and I have read and accept these terms</label>
        </div>
        <button type="button" className={`${s.btn} ${s.primary} ${s.block}`} disabled={!t || !checked || busy}
                onClick={() => void accept()}>Accept</button>
        <button type="button" className={`${s.btn} ${s.ghost} ${s.block}`} onClick={() => void signOut()}>Sign out</button>
        <p className={s.hint}>If you do not accept, you can&apos;t use the dashboard. Contact us to close your account.</p>
      </div>
    </div>
  );
}

// ------------------------------------------------- identity check (video)

function gestureText(gesture: string): string {
  const g = String(gesture || "");
  return g.charAt(0).toUpperCase() + g.slice(1);
}

export function VerifyScreen() {
  const { boot, toast, toLogin, signOut, authProblem } = useOwner();
  const [data, setData] = useState<Verification | null>(null);
  const [challenge, setChallenge] = useState<Challenge | null>(null);
  const [notice, setNotice] = useState<{ text: string; info?: boolean } | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [fileKey, setFileKey] = useState(0);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewFailed, setPreviewFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const urlRef = useRef<string | null>(null);
  const challengeRef = useRef<HTMLDivElement>(null);

  // A local preview of the chosen video is a blob: URL, revoked when it is replaced or the screen closes.
  const dropFile = useCallback(() => {
    if (urlRef.current) URL.revokeObjectURL(urlRef.current);
    urlRef.current = null;
    setPreviewUrl(null);
    setPreviewFailed(false);
    setFile(null);
    setFileKey((k) => k + 1);
  }, []);

  useEffect(() => () => {
    if (urlRef.current) URL.revokeObjectURL(urlRef.current);
  }, []);

  const adopt = useCallback((c: Challenge | null) => {
    setChallenge(c);
    setNow(Date.now());
    dropFile();
  }, [dropFile]);

  const load = useCallback((message?: string): Promise<Verification | null> =>
    ownerApi<Verification>("GET", "/api/owner/verification").then(
      async (d) => {
        if (d.ok) { await boot(); return null; }
        setData(d);
        adopt(d.latest?.challenge ?? null);
        setNotice(message ? { text: message } : null);
        return d;
      },
      (err) => {
        if (!authProblem(err)) toLogin("Could not load the identity check: " + errorMessage(err));
        return null;
      },
    ), [adopt, authProblem, boot, toLogin]);

  useEffect(() => {
    load().catch(() => undefined);
  }, [load]);

  // The code is good for a few minutes: count down.
  useEffect(() => {
    if (!challenge) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [challenge]);

  const expiresAt = challenge ? Date.parse(challenge.expires_at) : NaN;
  const left = isNaN(expiresAt) ? null : Math.max(0, Math.floor((expiresAt - now) / 1000));
  // An upload under way: the server has the last word on the code.
  const expired = challenge !== null && left === 0 && !busy;

  async function getCode() {
    setNotice(null);
    setBusy(true);
    try {
      const row = await ownerApi<{ challenge?: Challenge }>("POST", "/api/owner/verification/challenge");
      if (!row.challenge) { setNotice({ text: "Could not get a code. Please try again." }); return; }
      adopt(row.challenge);
      challengeRef.current?.scrollIntoView({ block: "start", behavior: "smooth" });
    } catch (err) {
      if (authProblem(err)) return;
      // Already sent, or no longer needed: the screen shows which.
      if (err instanceof OwnerApiError && err.status === 409) await load(err.message);
      else setNotice({ text: "Could not get a code: " + errorMessage(err) });
    } finally {
      setBusy(false);
    }
  }

  function chooseFile(ev: React.ChangeEvent<HTMLInputElement>) {
    const chosen = ev.target.files?.[0] ?? null;
    if (urlRef.current) URL.revokeObjectURL(urlRef.current);
    urlRef.current = null;
    setPreviewUrl(null);
    setPreviewFailed(false);
    setFile(chosen);
    setNotice(null);
    if (!chosen || !data) return;
    if (!chosen.size) { setNotice({ text: "That file is empty. Please record the video again." }); return; }
    if (chosen.type && !chosen.type.toLowerCase().startsWith("video/")) {
      setNotice({ text: "That file is not a video. Please record or choose a video." });
      return;
    }
    if (chosen.size > data.max_mb * MB) {
      setNotice({ text: `The video is larger than ${data.max_mb} MB. Please record a shorter one: 5 to 20 seconds is enough.` });
      return;
    }
    urlRef.current = URL.createObjectURL(chosen);
    setPreviewUrl(urlRef.current);
  }

  async function send() {
    if (!file || !data || busy || file.size > data.max_mb * MB) return;
    setBusy(true);
    setNotice({ text: `Uploading ${sizeMb(file.size)}… Please keep this page open. ` +
      "On a phone connection this can take a minute or two.", info: true });
    try {
      await ownerUpload("/api/owner/verification/video?name=" + encodeURIComponent(videoName(file)), file);
      toast("Thank you. Your video was sent for review.", "info");
      await boot();
    } catch (err) {
      if (authProblem(err)) return;
      // 409: no code yet, the code expired, or the check changed meanwhile.
      if (err instanceof OwnerApiError && err.status === 409) await load(err.message);
      else setNotice({ text: "The upload failed: " + errorMessage(err) });
    } finally {
      setBusy(false);
    }
  }

  async function refresh() {
    const d = await load();
    if (d?.latest?.status === "submitted") {
      setNotice({ text: "Still waiting for review. Please check again later.", info: true });
    }
  }

  if (!data) return <div className={s.center}><div className={s.loading}>Loading…</div></div>;

  const latest = data.latest;
  const status = latest ? latest.status : null;
  const waiting = status === "submitted";
  const rejected = status === "rejected";
  // Why we ask, when the admin asked again (a code the client asked for carries no reason worth showing).
  const reason = latest && !String(latest.requested_by || "").startsWith("owner:") ? String(latest.reason || "").trim() : "";
  const why = rejected && latest ? String(latest.review_reason || "").trim() : "";
  const showChallenge = challenge !== null && !expired;

  return (
    <div className={s.center}>
      <div className={`${s.card} ${s.narrow}`}>
        <h1>Confirm it&apos;s you</h1>
        <p className={s.hint}>{rejected
          ? "Please record a new video so we can confirm who you are."
          : "To keep everyone safe, we check who runs each account and that they are an adult. " +
            "It takes a minute: you record a short video with a code we give you. Only our team sees it."}</p>
        {reason && <p className={s.reason}>Why we ask: {reason}</p>}
        {rejected && <p className={s.notice}>Your last video was not accepted{why ? ": " + why : "."}</p>}
        <Notice text={notice?.text} info={notice?.info} />

        {waiting ? (
          <div>
            <p className={cx(s.notice, s.info)}>Your video is waiting for review. We&apos;ll unlock your dashboard when it is approved.</p>
            <p className={cx(s.muted, s.small)}>{latest?.submitted_at ? "Sent " + fmtWhen(latest.submitted_at) + "." : ""}</p>
            <button type="button" className={`${s.btn} ${s.primary} ${s.block}`} disabled={busy}
                    onClick={() => { setBusy(true); void refresh().finally(() => setBusy(false)); }}>Refresh status</button>
          </div>
        ) : (
          <div>
            <ol className={s.steps}>{(data.instructions || []).map((line, i) => <li key={i}>{line}</li>)}</ol>
            {showChallenge && challenge && (
              <div className={s.challenge} ref={challengeRef}>
                <p className={s.label}>Your code</p>
                <div className={s.code}>{challenge.code}</div>
                <p className={s.label}>Your gesture</p>
                <p className={s.gesture}>{gestureText(challenge.gesture)}</p>
                {left !== null && (
                  <p className={s.countdown}>Time left to upload: {Math.floor(left / 60)}:{String(left % 60).padStart(2, "0")}</p>
                )}
              </div>
            )}
            {expired && <p className={s.notice}>This code expired. Get a new code, then record the video again.</p>}
            <button type="button" className={cx(s.btn, s.block, (!challenge || expired) && s.primary)} disabled={busy}
                    onClick={() => void getCode()}>{challenge ? "Get a new code" : "Get my code"}</button>
            {showChallenge && (
              <div className={s.upload}>
                <label className={s.field}>Record or choose the video
                  <input key={fileKey} type="file" accept="video/*" capture="user" disabled={busy} onChange={chooseFile} />
                </label>
                {file && <p className={cx(s.muted, s.small)}>{(file.name || "Video") + " · " + sizeMb(file.size)}</p>}
                {previewUrl && !previewFailed && (
                  <video className={s.preview} src={previewUrl} controls playsInline muted preload="metadata"
                         onError={() => setPreviewFailed(true)} />
                )}
                <button type="button" className={`${s.btn} ${s.primary} ${s.block}`}
                        disabled={busy || !previewUrl} onClick={() => void send()}>
                  {busy ? "Uploading…" : "Upload the video"}</button>
                <p className={s.hint}>Up to {data.max_mb} MB. A clip of 5 to 20 seconds is enough.</p>
              </div>
            )}
          </div>
        )}
        <button type="button" className={`${s.btn} ${s.ghost} ${s.block}`} disabled={busy} onClick={() => void signOut()}>
          Sign out</button>
      </div>
    </div>
  );
}
