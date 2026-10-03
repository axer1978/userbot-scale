"use client";

// The media library: files the assistant may send, each with the short
// description it picks them by, and the entrance photo for arrival checks.

import { useEffect, useState } from "react";
import { useDialogs, useToast } from "@/components/feedback";
import { Overlay } from "@/components/ui";
import { errorText, sessionPath } from "@/lib/api";
import { cx } from "@/lib/format";
import { usePanel } from "@/lib/panel";
import type { MediaItem } from "@/lib/types";

export function MediaPreview({ item, sessionId, small }: { item: MediaItem; sessionId: string; small?: boolean }) {
  const src = sessionPath(sessionId, `/media/${item.id}/file`);
  if (item.kind === "video") {
    return <video src={src} controls preload="metadata" muted className={small ? "preview" : undefined} />;
  }
  // A plain <img>: the file is behind the admin cookie on panel.py, not something to optimise.
  // eslint-disable-next-line @next/next/no-img-element
  return <img src={src} alt={item.description || item.file} loading="lazy" className={small ? "preview" : undefined} />;
}

function MediaCard({ item }: { item: MediaItem }) {
  const { state, sApi } = usePanel();
  const toast = useToast();
  const { confirm } = useDialogs();
  const [description, setDescription] = useState(item.description);
  const [busy, setBusy] = useState<"save" | "send" | null>(null);
  // Ticked or unticked here, until the library (over the socket) says otherwise.
  const [pending, setPending] = useState<boolean | null>(null);
  const [roleSeen, setRoleSeen] = useState(item.role);
  if (roleSeen !== item.role) { setRoleSeen(item.role); setPending(null); }
  const entrance = pending ?? item.role === "arrival_reference";
  const chat = state.activeChatId;

  return (
    <div className="media-card">
      <MediaPreview item={item} sessionId={state.sessionId!} small />
      <div className="meta">{item.kind} #{item.id} · {item.file}</div>
      <textarea placeholder="What is in it? e.g. me at the beach, blue bikini" value={description}
                onChange={(ev) => setDescription(ev.target.value)} />
      {item.kind === "photo" && (
        // The entrance photo the arrival check compares customers' photos with.
        <label className="entrance">
          <input type="checkbox" checked={entrance} onChange={async (ev) => {
            const on = ev.target.checked;
            setPending(on);
            try { await sApi("PATCH", `/media/${item.id}/role`, { role: on ? "arrival_reference" : null }); }
            catch (err) { setPending(null); toast(errorText(err)); }
          }} />
          <span>Entrance (for the arrival photo check)</span>
        </label>
      )}
      <div className="actions">
        <button type="button" className="btn small primary" disabled={busy === "save"} onClick={async () => {
          setBusy("save");
          try { await sApi("PATCH", `/media/${item.id}`, { description }); toast("Saved.", "info"); }
          catch (err) { toast(errorText(err)); }
          setBusy(null);
        }}>Save</button>
        <button type="button" className="btn small" disabled={chat === null || busy === "send"}
                title={chat === null ? "Open a conversation first" : "Send this file now, as yourself"}
                onClick={async () => {
                  setBusy("send");
                  try {
                    await sApi("POST", `/conversations/${chat}/send-media`, { media_id: item.id });
                    toast("Sent.", "info");
                  } catch (err) { toast(errorText(err)); }
                  setBusy(null);
                }}>Send to open chat</button>
        <button type="button" className="btn small warn" onClick={async () => {
          if (!(await confirm(`Delete ${item.file}? The file is removed from the media folder.`))) return;
          try { await sApi("DELETE", `/media/${item.id}`); } catch (err) { toast(errorText(err)); }
        }}>Delete</button>
      </div>
    </div>
  );
}

export function MediaDialog({ onClose }: { onClose: () => void }) {
  const { state, sApi, dispatch } = usePanel();
  const toast = useToast();
  const [progress, setProgress] = useState("");
  const [over, setOver] = useState(false);

  useEffect(() => {
    sApi<MediaItem[]>("GET", "/media")
      .then((media) => dispatch({ type: "media", media }))
      .catch((err) => toast(errorText(err)));
  }, [sApi, dispatch, toast]);

  // One at a time, raw bytes: the panel streams each upload to disk. The
  // list refreshes itself over the socket ("media" event).
  const upload = async (files: File[]) => {
    const sessionId = state.sessionId;
    if (!sessionId) return;
    let n = 0;
    for (const file of files) {
      n += 1;
      setProgress(`Uploading ${file.name} (${n}/${files.length})…`);
      try {
        const res = await fetch(`${sessionPath(sessionId, "/media/upload")}?name=${encodeURIComponent(file.name)}`, {
          method: "PUT", body: file, headers: { "Content-Type": "application/octet-stream" },
        });
        if (!res.ok) {
          let detail = res.statusText;
          try { detail = (await res.json()).detail || detail; } catch { /* not JSON */ }
          toast(`${file.name}: ${detail}`);
        }
      } catch (err) { toast(`${file.name}: ${errorText(err)}`); }
    }
    setProgress("");
  };

  return (
    <Overlay onClose={onClose}>
      <div className="sheet w-860">
        <h2>Photos &amp; videos</h2>
        <p className="hint">Files the assistant may send when someone asks for a photo or video.
          Give each one a short description — that is how it picks the right one (&quot;the one from
          the beach&quot;). Files can also be dropped straight into the <code>media/</code> folder.</p>

        <div className={cx("drop", over && "over")}
             onDragOver={(ev) => { ev.preventDefault(); setOver(true); }}
             onDragLeave={() => setOver(false)}
             onDrop={(ev) => { ev.preventDefault(); setOver(false); void upload(Array.from(ev.dataTransfer.files || [])); }}>
          <label htmlFor="m-files" style={{ cursor: "pointer" }}>Drop photos or videos here, or <u>choose files</u></label>
          <input id="m-files" type="file" multiple
                 accept="image/*,video/*,.jpg,.jpeg,.png,.webp,.gif,.mp4,.mov,.m4v,.mkv,.webm"
                 onChange={async (ev) => {
                   const input = ev.currentTarget;
                   await upload(Array.from(input.files || []));
                   input.value = "";
                 }} />
          <div style={{ marginTop: 6 }}>{progress}</div>
        </div>

        <div className="media-grid">
          {!state.media.length ? <div className="empty">Nothing loaded yet.</div>
            : state.media.map((item) => <MediaCard key={item.id} item={item} />)}
        </div>

        <p className="hint" style={{ marginTop: 16 }}>Whether the AI may send these, and the video rules, are in{" "}
          <b>Settings → Config</b> under <code>media</code>. A photo marked <b>Entrance</b> is what a
          customer&apos;s arrival photo is compared with (<code>booking.arrival_photo_check</code>); the owner can
          also send one to this account captioned &quot;door&quot;.</p>

        <div className="sheet-actions">
          <button type="button" className="btn" onClick={onClose}>Close</button>
        </div>
      </div>
    </Overlay>
  );
}
