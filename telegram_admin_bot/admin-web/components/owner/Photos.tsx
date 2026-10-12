"use client";

// Photos for a business whose photos we review: a new or replaced photo only
// reaches the bot once an admin approves it (owner_review_api.py). Removing
// one takes effect at once. Local previews are blob: URLs, revoked when done.

import { useCallback, useEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { cx } from "@/lib/format";
import { errorMessage, ownerApi, ownerUpload } from "@/lib/owner/api";
import { fmtWhen, MAX_DESCRIPTION, photoProblem, PHOTO_TYPES, SUBMISSION_STATES } from "@/lib/owner/format";
import type { LivePhoto, OwnerTenant, PhotosData, Submission } from "@/lib/owner/types";
import { useOwner } from "./context";
import { Section, s } from "./parts";
import { useOwnerData } from "./useOwnerData";

const STATE_STYLE: Record<string, string> = {
  pending: s.stPending, approved: s.stApproved, rejected: s.stRejected, withdrawn: s.stWithdrawn,
};

export function PhotosSection({ tenant }: { tenant: OwnerTenant; tz: string }) {
  const { toast, authProblem } = useOwner();
  const base = `/api/owner/tenants/${encodeURIComponent(tenant.id)}`;
  const load = useCallback(() => ownerApi<PhotosData>("GET", base + "/photos"), [base]);
  const { data, error, reload } = useOwnerData(load);
  const [form, setForm] = useState<{ replacing: LivePhoto | null } | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  // Opened inside the tap, so phones show the picker straight away.
  function openForm(replacing: LivePhoto | null) {
    flushSync(() => setForm({ replacing }));
    fileRef.current?.click();
  }

  async function removePhoto(item: LivePhoto) {
    const label = (item.kind === "video" ? "Video" : "Photo") + " #" + item.id;
    if (!window.confirm(`Remove ${label.toLowerCase()}? The bot stops sending it right away.`)) return;
    try {
      await ownerApi("DELETE", `${base}/photos/${encodeURIComponent(item.id)}`);
      toast("Removed.", "info");
      await reload();
    } catch (err) {
      if (!authProblem(err)) toast(errorMessage(err));
    }
  }

  async function withdraw(sub: Submission) {
    if (!window.confirm("Withdraw this photo? We will not review it.")) return;
    try {
      await ownerApi("DELETE", `${base}/submissions/${encodeURIComponent(sub.id)}`);
      toast("Withdrawn.", "info");
      await reload();
    } catch (err) {
      if (!authProblem(err)) toast(errorMessage(err));
    }
  }

  const subs = data ? data.submissions.slice().sort((a, b) => b.id - a.id) : [];
  return (
    <Section title="Photos"
             extra={<button type="button" className={`${s.btn} ${s.btnSmall}`} onClick={() => openForm(null)}>Add a photo</button>}>
      <p className={s.hint}>The photos the bot can send to your customers. New and replaced photos are checked by us
        before the bot can send them. Removing a photo takes effect right away.</p>
      {form && (
        <PhotoForm base={base} replacing={form.replacing} maxMb={data?.max_mb ?? null} fileRef={fileRef}
                   onCancel={() => setForm(null)}
                   onSent={() => { setForm(null); void reload(); }}
                   onGone={() => { setForm(null); void reload(); }} />
      )}
      {error ? <div className={s.errorBox}>Could not load the photos: {error.message}</div>
        : !data ? <div className={s.loading}>Loading…</div>
        : (
          <div>
            <h3 className={s.subHead}>What the bot can send now</h3>
            {!data.live.length ? (
              <div className={s.empty}>No photos yet. Tap “Add a photo” to send your first one for review.</div>
            ) : (
              <div className={s.photoGrid}>
                {data.live.map((item) => (
                  <PhotoTile key={item.id} base={base} item={item} onReplace={() => openForm(item)}
                             onRemove={() => void removePhoto(item)} />
                ))}
              </div>
            )}
            {subs.length > 0 && (
              <>
                <h3 className={s.subHead}>Sent for review</h3>
                <div className={s.list}>
                  {subs.map((sub) => <SubmissionRow key={sub.id} base={base} sub={sub} onWithdraw={() => void withdraw(sub)} />)}
                </div>
              </>
            )}
          </div>
        )}
    </Section>
  );
}

function PhotoTile({ base, item, onReplace, onRemove }: {
  base: string; item: LivePhoto; onReplace: () => void; onRemove: () => void;
}) {
  const [broken, setBroken] = useState(false);
  const isVideo = item.kind === "video";
  const label = (isVideo ? "Video" : "Photo") + " #" + item.id;
  return (
    <div className={s.photo}>
      {isVideo ? <div className={s.videoTile}>Video</div>
        : broken ? <div className={s.noThumb}>No preview</div>
        // eslint-disable-next-line @next/next/no-img-element -- served by the API with the client's cookie; nothing to optimise
        : <img className={s.thumb} alt={label} loading="lazy" decoding="async" onError={() => setBroken(true)}
               src={`${base}/photos/${encodeURIComponent(item.id)}/file`} />}
      <div className={s.photoText}>
        <div className={cx(s.muted, s.small)}>{label}</div>
        <div className={s.desc}>{item.description || "No description"}</div>
      </div>
      <div className={s.photoActions}>
        <button type="button" className={`${s.btn} ${s.btnSmall}`} onClick={onReplace}>Replace</button>
        <button type="button" className={`${s.btn} ${s.btnSmall} ${s.danger}`} onClick={onRemove}>Remove</button>
      </div>
    </div>
  );
}

function SubmissionRow({ base, sub, onWithdraw }: { base: string; sub: Submission; onWithdraw: () => void }) {
  const [thumbBroken, setThumbBroken] = useState(false);
  const own = sub.source === "owner";
  const pending = sub.status === "pending";
  const meta: string[] = [];
  if (!own) meta.push("One of your photos, checked again by us");
  if (sub.replaces_item) meta.push(`Replaces photo #${sub.replaces_item}`);
  if (sub.status === "approved" && sub.media_item) meta.push(`Now live as photo #${sub.media_item}`);
  const reason = String(sub.review_reason || "").trim();
  return (
    <div className={cx(s.subItem, (sub.status === "withdrawn" || sub.status === "rejected") && s.dimmed)}>
      {pending && own && sub.kind !== "video" && !thumbBroken && (
        // eslint-disable-next-line @next/next/no-img-element -- served by the API with the client's cookie
        <img className={s.subThumb} alt="The photo you sent" loading="lazy" decoding="async"
             onError={() => setThumbBroken(true)} src={`${base}/submissions/${encodeURIComponent(sub.id)}/file`} />
      )}
      <div className={s.body}>
        <div className={s.subTop}>
          <span className={cx(s.state, STATE_STYLE[sub.status])}>{SUBMISSION_STATES[sub.status] || sub.status}</span>
          <span className={cx(s.muted, s.small)}>{fmtWhen(sub.created_at)}</span>
        </div>
        <div className={s.desc}>{sub.description || "No description"}</div>
        {meta.length > 0 && <div className={s.meta}>{meta.join(" · ")}</div>}
        {sub.status === "rejected" && reason && <div className={s.reasonText}>Reason: {reason}</div>}
        {pending && own && (
          <div className={s.actions}>
            <button type="button" className={`${s.btn} ${s.btnSmall}`} onClick={onWithdraw}>Withdraw</button>
          </div>
        )}
      </div>
    </div>
  );
}

// `replacing`: the live item the new photo is meant to replace, or null.
function PhotoForm({ base, replacing, maxMb, fileRef, onCancel, onSent, onGone }: {
  base: string;
  replacing: LivePhoto | null;
  maxMb: number | null;
  fileRef: React.RefObject<HTMLInputElement | null>;
  onCancel: () => void;
  onSent: () => void;
  onGone: () => void;
}) {
  const { toast, authProblem } = useOwner();
  const [file, setFile] = useState<File | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [desc, setDesc] = useState(replacing?.description ?? "");
  const [busy, setBusy] = useState(false);
  const urlRef = useRef<string | null>(null);
  const kind = replacing?.kind === "video" ? "video" : "photo";

  useEffect(() => () => {
    if (urlRef.current) URL.revokeObjectURL(urlRef.current);
  }, []);

  function choose(ev: React.ChangeEvent<HTMLInputElement>) {
    if (urlRef.current) URL.revokeObjectURL(urlRef.current);
    urlRef.current = null;
    setPreviewUrl(null);
    const chosen = ev.target.files?.[0] ?? null;
    setFile(chosen);
    if (!chosen) return;
    const problem = photoProblem(chosen, maxMb);
    if (problem) { toast(problem); return; }
    urlRef.current = URL.createObjectURL(chosen);
    setPreviewUrl(urlRef.current);
  }

  async function submit(ev: React.FormEvent) {
    ev.preventDefault();
    if (busy) return;
    if (!file) { toast("Choose a photo first."); return; }
    const problem = photoProblem(file, maxMb);
    if (problem) { toast(problem); return; }
    const text = desc.trim();
    if (text.length > MAX_DESCRIPTION) { toast(`The description is limited to ${MAX_DESCRIPTION} characters.`); return; }
    let path = `${base}/photos?name=${encodeURIComponent(file.name || "photo.jpg")}&description=${encodeURIComponent(text)}`;
    if (replacing) path += `&replaces=${encodeURIComponent(replacing.id)}`;
    setBusy(true);
    try {
      await ownerUpload(path, file);
      toast("Sent. We'll check it before the bot can send it.", "info");
      onSent();
    } catch (err) {
      if (authProblem(err)) return;
      toast(errorMessage(err));
      // The photo to replace is gone (removed meanwhile): show what is there now.
      if (replacing && (err as { status?: number }).status === 404) onGone();
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className={s.photoForm} onSubmit={submit} noValidate>
      <h3>{replacing ? `Replace ${kind} #${replacing.id}` : "Add a photo"}</h3>
      <p className={s.hint}>{replacing
        ? "Once we approve the new photo it takes this one's place. Until then the bot keeps sending the old one."
        : "We check every new photo before the bot can send it."}</p>
      <label className={s.field}>Photo (JPG, PNG or WebP{maxMb ? `, up to ${maxMb} MB)` : ")"}
        <input ref={fileRef} type="file" accept={PHOTO_TYPES.join(",")} disabled={busy} onChange={choose} />
      </label>
      {previewUrl && (
        // eslint-disable-next-line @next/next/no-img-element -- a blob: preview of the chosen file
        <img className={s.photoPreview} src={previewUrl} alt="The photo you chose" onError={() => setPreviewUrl(null)} />
      )}
      <label className={s.field}>What the photo shows (the bot uses this to pick it)
        <textarea rows={3} maxLength={MAX_DESCRIPTION} disabled={busy} value={desc}
                  placeholder="For example: the entrance, seen from the street" onChange={(e) => setDesc(e.target.value)} />
      </label>
      <div className={cx(s.muted, s.small, s.counter)}>{desc.length}/{MAX_DESCRIPTION}</div>
      <button type="submit" className={`${s.btn} ${s.primary} ${s.block}`} disabled={busy}>
        {busy ? "Uploading…" : "Send for review"}</button>
      <button type="button" className={`${s.btn} ${s.ghost} ${s.block}`} disabled={busy} onClick={onCancel}>Cancel</button>
    </form>
  );
}
