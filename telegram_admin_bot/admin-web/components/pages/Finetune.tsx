"use client";

// Finetune from a client's real chats (finetune_api.py). Pick a client,
// add screenshots of its chats (read by the vision model) or paste them as
// text (read by DeepSeek), marked good or bad if you like, and the model
// proposes the client's prompt layer and an updated industry standard from
// the industry's finetune template. Or pick the client's chats flagged
// from Conversations (FinetuneExampleDialog), each with the admin's verdict,
// or let the server pick the chats where a person typed the replies (from
// this client and any others added), which the model learns the voice from.
// Nothing changes until a run is applied; applying saves ordinary prompt
// versions, which the Clients page can roll back. Screenshots and typed
// chats are never stored. The Quality tab counts how the bot did week by
// week, to see whether a run made it better.

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useDialogs, useToast } from "@/components/feedback";
import { CLIENT_KINDS, OUTCOMES, verdictLabel } from "@/components/FinetuneExampleDialog";
import { REASONS as UNANSWERED_REASONS } from "@/components/pages/Unanswered";
import { PageShell, Tabs } from "@/components/ui";
import { api, errorText } from "@/lib/api";
import { cx, fmtDateTime } from "@/lib/format";
import type {
  BusinessLayer, FinetuneExample, FinetuneIndustry, FinetuneRun, HandChats, PlatformTree, Quality, QualityWeek,
} from "@/lib/types";
import { useLoader } from "@/lib/useLoader";
import "@/app/finetune.css";

type Tab = "new" | "runs" | "template" | "quality";
type Mark = "" | "good" | "bad";
type Shot = { file: File; base: string; ext: string; mark: Mark };
type Chat = { id: number; name: string; mark: Mark; text: string };
type Source = "screenshots" | "text" | "examples" | "hand";
type Mode = "inherit" | "override" | "append";

const MAX_IMAGE_BYTES = 5 * 1024 * 1024;
const MAX_IMAGES = 40;
const STATUS: Record<FinetuneRun["status"], string> = {
  running: "reading the chats…", done: "ready to review", failed: "failed", applied: "applied", discarded: "discarded",
};
const POLL_MS = 3000;
const MAX_TRANSCRIPT_CHARS = 100_000;

function sourceCount(run: FinetuneRun): string {
  const n = run.files.length;
  const s = n === 1 ? "" : "s";
  if (run.source === "examples") return `${n} flagged chat${s}`;
  if (run.source === "hand") return `${n} hand-typed chat${s}`;
  return run.source === "text" ? `${n} typed conversation${s}` : `${n} screenshot${s}`;
}

const SOURCE_NAME: Record<FinetuneRun["source"], string> = {
  screenshots: "Screenshots", text: "Conversations", examples: "Flagged chats", hand: "Hand-typed chats",
};

/** "03a-good.png" → base "03a", mark "good", ext ".png". */
function splitName(name: string): Omit<Shot, "file"> {
  const dot = name.lastIndexOf(".");
  const ext = dot > 0 ? name.slice(dot) : "";
  let base = dot > 0 ? name.slice(0, dot) : name;
  const m = base.match(/^(.*?)[-_ ](good|bad)$/i);
  let mark: Mark = "";
  if (m) { base = m[1]; mark = m[2].toLowerCase() as Mark; }
  return { base, ext, mark };
}

function shotName(s: Shot): string {
  return `${s.base}${s.mark ? "-" + s.mark : ""}${s.ext}`;
}

/** Natural order, the same as the server's: 2 before 10, 03a before 03b. */
function naturalCompare(a: string, b: string): number {
  return a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
}

function readBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).replace(/^data:[^,]*,/, ""));
    reader.onerror = () => reject(reader.error ?? new Error(`Could not read ${file.name}`));
    reader.readAsDataURL(file);
  });
}

/* ---------------------------------------------------------------- new run */

type RunProps = { tenantId: number; info: FinetuneIndustry; onStarted: (run: FinetuneRun) => void };

function NewRun({ tree, ...props }: RunProps & { tree: PlatformTree }) {
  const { info } = props;
  // Screenshots when the vision model is set up, typed chats otherwise.
  const [source, setSource] = useState<Source>(info.screenshots_ready || !info.transcripts_ready ? "screenshots" : "text");

  if (!info.template.trim()) {
    return (
      <div className="empty">Write the finetune template for {info.industry.name} first (Template tab)
        {info.default_template ? ", or load the default there and save it" : ""}.</div>
    );
  }
  return (
    <>
      <div className="ft-source" role="radiogroup" aria-label="What the chats are">
        <label className="check"><input type="radio" name="ft-source" checked={source === "screenshots"}
                                        onChange={() => setSource("screenshots")} /> Screenshots</label>
        <label className="check"><input type="radio" name="ft-source" checked={source === "text"}
                                        onChange={() => setSource("text")} /> Typed chats</label>
        <label className="check"><input type="radio" name="ft-source" checked={source === "examples"}
                                        onChange={() => setSource("examples")} /> Flagged chats</label>
        <label className="check"><input type="radio" name="ft-source" checked={source === "hand"}
                                        onChange={() => setSource("hand")} /> Hand-typed chats</label>
      </div>
      {source === "screenshots" && !info.screenshots_ready && (
        <div className="pf-errors">No vision model yet: set VISION_API_URL and VISION_API_KEY in the server&apos;s .env,
          or use typed chats.</div>
      )}
      {source !== "screenshots" && !info.transcripts_ready && (
        <div className="pf-errors">{source === "text" ? "Typed" : source === "hand" ? "Hand-typed" : "Flagged"} chats
          need DEEPSEEK_PLATFORM_KEY in the server&apos;s .env.</div>
      )}
      <p className="pf-note">The {info.industry.name} standard is built from {info.businesses_so_far} business
        {info.businesses_so_far === 1 ? "" : "es"} so far{info.has_standard ? "" : " (none yet: this run writes the first)"}.
        {source === "examples" ? "Only your judgement of each chat is stored; the chat is read when the run starts."
          : source === "hand" ? "Nothing is stored; the chats are read when the run starts."
            : "The chats are sent to the model and not stored."}</p>
      {source === "screenshots" ? <ScreenshotRun {...props} />
        : source === "text" ? <TranscriptRun {...props} />
          : source === "examples" ? <ExampleRun {...props} /> : <HandRun {...props} tree={tree} />}
    </>
  );
}

function ScreenshotRun({ tenantId, onStarted }: RunProps) {
  const toast = useToast();
  const [shots, setShots] = useState<Shot[]>([]);
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);

  const add = (files: FileList | null) => {
    if (!files) return;
    const fresh: Shot[] = [];
    for (const file of Array.from(files)) {
      if (!file.type.startsWith("image/")) { toast(`${file.name} is not an image.`); continue; }
      if (file.size > MAX_IMAGE_BYTES) { toast(`${file.name} is larger than 5 MB.`); continue; }
      fresh.push({ file, ...splitName(file.name) });
    }
    setShots((list) => {
      const known = new Set(list.map((s) => s.file.name));
      return [...list, ...fresh.filter((s) => !known.has(s.file.name))]
        .sort((a, b) => naturalCompare(a.file.name, b.file.name));
    });
  };

  const start = async () => {
    if (!shots.length) { toast("Add the screenshots first."); return; }
    if (shots.length > MAX_IMAGES) { toast(`At most ${MAX_IMAGES} screenshots per run.`); return; }
    const names = shots.map(shotName);
    if (new Set(names).size !== names.length) { toast("Two screenshots have the same name; rename one."); return; }
    setBusy(true);
    try {
      const images = await Promise.all(shots.map(async (s) => ({ name: shotName(s), data: await readBase64(s.file) })));
      const run = await api<FinetuneRun>("POST", "/api/finetune/runs", { tenant_id: tenantId, images });
      setShots([]);
      onStarted(run);
    } catch (err) { toast(errorText(err)); } finally { setBusy(false); }
  };

  return (
    <>
      <p className="pf-note">
        Screenshots of one business&apos;s real chats. Name them so they sort in order: the same number is one
        conversation (03a, 03b, 03c). Mark a screenshot <b>good</b> (handled the way the business wants) or <b>bad</b> (a
        reply not to repeat); the mark is added to its file name for the model.
      </p>
      <label className={cx("ft-drop", dragging && "on")}
             onDragOver={(ev) => { ev.preventDefault(); setDragging(true); }}
             onDragLeave={() => setDragging(false)}
             onDrop={(ev) => { ev.preventDefault(); setDragging(false); add(ev.dataTransfer.files); }}>
        <input type="file" accept="image/png,image/jpeg,image/webp,image/gif" multiple hidden
               onChange={(ev) => { add(ev.target.files); ev.target.value = ""; }} />
        Drop screenshots here, or click to choose
      </label>
      {shots.length > 0 && (
        <div className="ft-shots">
          {shots.map((s, i) => (
            <div key={s.file.name} className={cx("ft-shot", s.mark && `mark-${s.mark}`)}>
              <span className="grow">{shotName(s)}</span>
              <span className="muted">{Math.ceil(s.file.size / 1024)} KB</span>
              <select value={s.mark} aria-label={`Mark ${s.file.name}`}
                      onChange={(ev) => setShots((list) => list.map((x, n) => (n === i ? { ...x, mark: ev.target.value as Mark } : x)))}>
                <option value="">no mark</option>
                <option value="good">good</option>
                <option value="bad">bad</option>
              </select>
              <button type="button" className="btn small" onClick={() => setShots((list) => list.filter((_, n) => n !== i))}>
                Remove</button>
            </div>
          ))}
        </div>
      )}
      <div className="pf-actions">
        <span className="muted grow">{shots.length} screenshot{shots.length === 1 ? "" : "s"}</span>
        <button type="button" className="btn" disabled={!shots.length || busy} onClick={() => setShots([])}>Clear</button>
        <button type="button" className="btn primary" disabled={!shots.length || busy} onClick={start}>
          {busy ? "Sending…" : "Start the run"}</button>
      </div>
    </>
  );
}

function nextChatName(chats: Chat[]): string {
  const numbers = chats.map((c) => parseInt(c.name, 10)).filter((n) => !Number.isNaN(n));
  return String((numbers.length ? Math.max(...numbers) : 0) + 1).padStart(2, "0");
}

let chatSeq = 0;
const newChat = (name: string): Chat => ({ id: ++chatSeq, name, mark: "", text: "" });

function TranscriptRun({ tenantId, onStarted }: RunProps) {
  const toast = useToast();
  const [chats, setChats] = useState<Chat[]>(() => [newChat("01")]);
  const [busy, setBusy] = useState(false);
  const total = chats.reduce((n, c) => n + c.text.trim().length, 0);
  const filled = chats.filter((c) => c.text.trim());

  const edit = (id: number, patch: Partial<Chat>) =>
    setChats((list) => list.map((c) => (c.id === id ? { ...c, ...patch } : c)));
  const fullName = (c: Chat) => `${c.name.trim()}${c.mark ? "-" + c.mark : ""}`;

  const start = async () => {
    if (!filled.length) { toast("Type at least one conversation."); return; }
    if (filled.some((c) => !c.name.trim())) { toast("Every conversation needs a name, e.g. 03."); return; }
    const names = filled.map(fullName);
    if (new Set(names).size !== names.length) { toast("Two conversations have the same name; rename one."); return; }
    if (total > MAX_TRANSCRIPT_CHARS) { toast(`At most ${MAX_TRANSCRIPT_CHARS.toLocaleString()} characters per run.`); return; }
    setBusy(true);
    try {
      const run = await api<FinetuneRun>("POST", "/api/finetune/text-runs", {
        tenant_id: tenantId, chats: filled.map((c) => ({ name: fullName(c), text: c.text })),
      });
      setChats([newChat("01")]);
      onStarted(run);
    } catch (err) { toast(errorText(err)); } finally { setBusy(false); }
  };

  return (
    <>
      <p className="pf-note">
        One box per conversation, numbered in order. Start each line with <b>Client:</b> or <b>Business:</b>, one
        message per line; add times or [voice note] / [photo] where they matter. Replace the client&apos;s name and
        number with [client]. Mark a conversation <b>good</b> (handled the way the business wants) or <b>bad</b>{" "}
        (replies not to repeat).
      </p>
      {chats.map((c) => (
        <div key={c.id} className={cx("pf-section ft-chat", c.mark && `mark-${c.mark}`)}>
          <div className="title">
            <input className="ft-chat-name" value={c.name} aria-label={`Name of conversation ${c.name}`}
                   onChange={(ev) => edit(c.id, { name: ev.target.value })} />
            <select value={c.mark} aria-label={`Mark conversation ${c.name}`}
                    onChange={(ev) => edit(c.id, { mark: ev.target.value as Mark })}>
              <option value="">no mark</option>
              <option value="good">good</option>
              <option value="bad">bad</option>
            </select>
            <button type="button" className="btn small" disabled={chats.length === 1}
                    onClick={() => setChats((list) => list.filter((x) => x.id !== c.id))}>Remove</button>
          </div>
          <textarea rows={8} value={c.text} aria-label={`Conversation ${c.name}`}
                    placeholder={"Client: Hi, are you available tonight?\nBusiness: Hi! What time were you thinking?"}
                    onChange={(ev) => edit(c.id, { text: ev.target.value })} />
        </div>
      ))}
      <div className={cx("pf-note ft-count", total > MAX_TRANSCRIPT_CHARS && "warn-note")}>
        {filled.length} conversation{filled.length === 1 ? "" : "s"} · {total.toLocaleString()} /{" "}
        {MAX_TRANSCRIPT_CHARS.toLocaleString()} characters</div>
      <div className="pf-actions">
        <button type="button" className="btn" onClick={() => setChats((list) => [...list, newChat(nextChatName(list))])}>
          + Add conversation</button>
        <span className="grow" />
        <button type="button" className="btn primary" disabled={!filled.length || busy} onClick={start}>
          {busy ? "Sending…" : "Start the run"}</button>
      </div>
    </>
  );
}

function ExampleRun({ tenantId, onStarted }: RunProps) {
  const toast = useToast();
  const { confirm } = useDialogs();
  const fetchExamples = useCallback(
    () => api<FinetuneExample[]>("GET", `/api/finetune/examples?tenant_id=${tenantId}`), [tenantId]);
  const { data: examples, error, reload } = useLoader(fetchExamples);
  // null: the default pick, every chat no run has read yet.
  const [picked, setPicked] = useState<Set<number> | null>(null);
  const [previews, setPreviews] = useState<Record<number, string>>({});
  const [open, setOpen] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const list = examples ?? [];
  const chosen = picked ?? new Set(list.filter((e) => e.last_run_id === null).map((e) => e.id));

  const toggle = (id: number) => {
    const next = new Set(chosen);
    if (next.has(id)) next.delete(id); else next.add(id);
    setPicked(next);
  };

  const preview = async (id: number) => {
    if (open === id) { setOpen(null); return; }
    setOpen(id);
    if (previews[id] !== undefined) return;
    try {
      const full = await api<FinetuneExample>("GET", `/api/finetune/examples/${id}`);
      setPreviews((all) => ({ ...all, [id]: full.preview ?? "" }));
    } catch (err) { toast(errorText(err)); }
  };

  const remove = async (e: FinetuneExample) => {
    if (!(await confirm(`Remove the flag from ${e.display_name || "this chat"}? The chat itself stays as it is.`))) return;
    try {
      await api("DELETE", `/api/finetune/examples/${e.id}`);
      if (picked) { const next = new Set(picked); next.delete(e.id); setPicked(next); }
      void reload();
    } catch (err) { toast(errorText(err)); }
  };

  const start = async () => {
    const ids = list.filter((e) => chosen.has(e.id)).map((e) => e.id);
    if (!ids.length) { toast("Pick at least one flagged chat."); return; }
    setBusy(true);
    try {
      const run = await api<FinetuneRun>("POST", "/api/finetune/example-runs", { tenant_id: tenantId, example_ids: ids });
      setPicked(null);
      onStarted(run);
    } catch (err) { toast(errorText(err)); } finally { setBusy(false); }
  };

  const kind = (key: string) => CLIENT_KINDS.find(([k]) => k === key)?.[1];
  const outcome = (key: string) => OUTCOMES.find(([k]) => k === key)?.[1];
  const corrections = (e: FinetuneExample) => Object.keys(e.corrections).length;

  return (
    <>
      <p className="pf-note">
        Real chats of this client, flagged from <Link href="/">Conversations</Link> (open a finished chat, press{" "}
        <b>Flag for finetune</b>): good or bad, what kind of client, how it ended, how to read it, and corrections on
        single messages. The model gets each chat with that judgement on top; names, numbers and the address are masked.
      </p>
      {error && <div className="pf-errors">{error}</div>}
      {examples && !list.length && (
        <div className="empty">No flagged chats for this client yet. Open one of its finished chats under Conversations
          and press Flag for finetune.</div>
      )}
      {list.length > 0 && (
        <div className="ft-shots">
          {list.map((e) => (
            <div key={e.id}>
              <div className="ft-example">
                <input type="checkbox" checked={chosen.has(e.id)} aria-label={`Use ${e.name}`} onChange={() => toggle(e.id)} />
                <div className="grow">
                  <div>
                    <b>{e.display_name || `Chat ${e.chat_id}`}</b>{" "}
                    <span className={cx("badge", `ft-${e.verdict}`)}>{verdictLabel(e.verdict)}</span>{" "}
                    {e.client_kind && <span className="badge">{kind(e.client_kind)}</span>}{" "}
                    {e.outcome && <span className="badge">{outcome(e.outcome)}</span>}{" "}
                    <span className="muted">
                      {e.name} · {corrections(e)} correction{corrections(e) === 1 ? "" : "s"}
                      {e.last_run_id !== null ? ` · read by run ${e.last_run_id}` : ""} · {fmtDateTime(e.updated_at)}
                    </span>
                  </div>
                  {e.note && <div className="ft-example-note">{e.note}</div>}
                </div>
                <button type="button" className="btn small" onClick={() => preview(e.id)}>
                  {open === e.id ? "Hide" : "What the model reads"}</button>
                <button type="button" className="btn small" onClick={() => remove(e)}>Remove</button>
              </div>
              {open === e.id && <pre className="pf-rendered">{previews[e.id] ?? "Loading…"}</pre>}
            </div>
          ))}
        </div>
      )}
      <div className="pf-actions">
        <span className="muted grow">{chosen.size} of {list.length} picked
          {picked === null && list.length > 0 ? " (the ones no run has read yet)" : ""}</span>
        {list.length > 0 && (
          <button type="button" className="btn" onClick={() => setPicked(chosen.size === list.length
            ? new Set() : new Set(list.map((e) => e.id)))}>{chosen.size === list.length ? "Pick none" : "Pick all"}</button>
        )}
        <button type="button" className="btn primary" disabled={!chosen.size || busy} onClick={start}>
          {busy ? "Sending…" : "Start the run"}</button>
      </div>
    </>
  );
}

function HandRun({ tenantId, tree, onStarted }: RunProps & { tree: PlatformTree }) {
  const toast = useToast();
  const [also, setAlso] = useState<number[]>([]);
  const fetchChats = useCallback(() => api<HandChats>("GET", `/api/finetune/hand-chats?tenant_id=${tenantId}`
    + also.map((id) => `&also=${id}`).join("")), [tenantId, also]);
  const { data, error } = useLoader(fetchChats);
  // null: the default pick, the best chats that fit one run.
  const [picked, setPicked] = useState<Set<string> | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const chats = data?.chats ?? [];
  const limits = data?.limits ?? { chats: 100, chars: MAX_TRANSCRIPT_CHARS };
  const chosen = picked ?? new Set(chats.filter((c) => c.picked).map((c) => c.key));
  const chars = chats.reduce((n, c) => n + (chosen.has(c.key) ? c.chars : 0), 0);
  const over = chosen.size > limits.chats || chars > limits.chars;
  const others = tree.tenants.filter((t) => t.id !== tenantId);
  const industryName = (id: number) => tree.industries.find((i) => i.id === id)?.name ?? "";

  const toggleAccount = (id: number) => {
    setAlso((list) => (list.includes(id) ? list.filter((x) => x !== id) : [...list, id]));
    setPicked(null);
  };
  const toggle = (key: string) => {
    const next = new Set(chosen);
    if (next.has(key)) next.delete(key); else next.add(key);
    setPicked(next);
  };

  const start = async () => {
    if (!chosen.size) { toast("Pick at least one chat."); return; }
    if (over) { toast(`At most ${limits.chats} chats and ${limits.chars.toLocaleString()} characters per run.`); return; }
    setBusy(true);
    try {
      const run = await api<FinetuneRun>("POST", "/api/finetune/hand-runs", {
        tenant_id: tenantId, also_tenant_ids: also,
        chats: picked === null ? [] : chats.filter((c) => chosen.has(c.key)).map((c) => c.key),
      });
      setPicked(null);
      onStarted(run);
    } catch (err) { toast(errorText(err)); } finally { setBusy(false); }
  };

  return (
    <>
      <p className="pf-note">
        The chats where a person typed the replies (on the phone, or by hand from the panel), found by the server, the
        most typed replies first. The model learns the voice and the way of working from the typed replies, and they win
        over the current standard; the bot&apos;s own replies in the same chats are marked as the assistant&apos;s. Names,
        numbers and the address are masked.
      </p>
      {others.length > 0 && (
        <div className="ft-accounts" role="group" aria-label="Also learn from">
          <span className="muted">Also learn the shared voice from:</span>
          {others.map((t) => (
            <label key={t.id} className="check">
              <input type="checkbox" checked={also.includes(t.id)} onChange={() => toggleAccount(t.id)} /> {t.name}
              {industryName(t.industry_id) && <span className="muted"> ({industryName(t.industry_id)})</span>}
            </label>
          ))}
        </div>
      )}
      {error && <div className="pf-errors">{error}</div>}
      {data && data.accounts.length > 0 && (
        <p className="pf-note">{data.accounts.map((a) => `${a.name}: ${a.chats} chat${a.chats === 1 ? "" : "s"}, `
          + `${a.typed} typed repl${a.typed === 1 ? "y" : "ies"}`).join(" · ")}</p>
      )}
      {data && !chats.length && (
        <div className="empty">No chats with replies typed by hand yet. Answer some clients yourself, on the phone or
          from Conversations, and they show up here.</div>
      )}
      {chats.length > 0 && (
        <div className="ft-shots">
          {chats.map((c) => (
            <div key={c.key}>
              <div className="ft-example">
                <input type="checkbox" checked={chosen.has(c.key)} aria-label={`Use ${c.name}`} onChange={() => toggle(c.key)} />
                <div className="grow">
                  <b>{c.display_name || `Chat ${c.chat_id}`}</b>{" "}
                  <span className="badge">{c.account}</span>{" "}
                  <span className="muted">
                    {c.name} · {c.typed} typed · {c.assistant} by the bot · {c.client} from the client ·{" "}
                    {c.chars.toLocaleString()} characters · {fmtDateTime(c.last_at)}
                  </span>
                </div>
                <button type="button" className="btn small" onClick={() => setOpen(open === c.key ? null : c.key)}>
                  {open === c.key ? "Hide" : "What the model reads"}</button>
              </div>
              {open === c.key && <pre className="pf-rendered">{c.text}</pre>}
            </div>
          ))}
        </div>
      )}
      <div className={cx("pf-note ft-count", over && "warn-note")}>
        {chosen.size} of {chats.length} picked · {chars.toLocaleString()} / {limits.chars.toLocaleString()} characters
        {picked === null && chats.length > 0 ? " (the best that fit one run)" : ""}</div>
      <div className="pf-actions">
        <span className="grow" />
        {picked !== null && <button type="button" className="btn" onClick={() => setPicked(null)}>Back to the best pick</button>}
        <button type="button" className="btn primary" disabled={!chosen.size || busy} onClick={start}>
          {busy ? "Sending…" : "Start the run"}</button>
      </div>
    </>
  );
}

/* ------------------------------------------------------------- review run */

type Editor = { key: string; heading: string; appendOnly: boolean; industry: string; mode: Mode; text: string };

function editorsFor(run: FinetuneRun): Editor[] {
  const proposedIndustry = run.result?.industry_sections ?? null;
  const proposedLayer = run.result?.business_layer?.overrides ?? {};
  return (run.sections ?? []).map((s) => {
    const o = proposedLayer[s.key];
    // A boundaries override is refused on save; offer it as the append it can be.
    const mode: Mode = !o ? "inherit" : s.append_only && o.mode === "override" ? "append" : o.mode;
    return {
      key: s.key, heading: s.heading, appendOnly: s.append_only,
      industry: proposedIndustry ? (proposedIndustry[s.key] ?? "") : (run.current?.industry_sections[s.key] ?? ""),
      mode, text: o?.text ?? "",
    };
  });
}

function RunReview({ run, onChanged }: { run: FinetuneRun; onChanged: (run: FinetuneRun) => void }) {
  const toast = useToast();
  const { confirm } = useDialogs();
  const [editors, setEditors] = useState<Editor[]>(() => editorsFor(run));
  const [addendum, setAddendum] = useState(run.result?.business_layer?.addendum ?? "");
  const [applyIndustry, setApplyIndustry] = useState(!!run.result?.industry_sections);
  const [applyBusiness, setApplyBusiness] = useState(!!run.result?.business_layer);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const current = run.current;
  const reviewable = run.status === "done" && !run.stale;

  const edit = (i: number, patch: Partial<Editor>) =>
    setEditors((list) => list.map((e, n) => (n === i ? { ...e, ...patch } : e)));

  const apply = async () => {
    const industry_sections: Record<string, string> = {};
    const overrides: BusinessLayer["overrides"] = {};
    for (const e of editors) {
      if (e.industry.trim()) industry_sections[e.key] = e.industry;
      if (e.mode !== "inherit" && e.text.trim()) overrides[e.key] = { mode: e.mode, text: e.text };
    }
    const what = [applyIndustry && "the industry standard (every client in the industry)", applyBusiness && "this client's layer"]
      .filter(Boolean).join(" and ");
    if (!what) { toast("Choose what to apply."); return; }
    if (!(await confirm(`Apply ${what} as new prompt versions? They go live at once and can be rolled back under Clients.`))) return;
    setBusy(true);
    try {
      onChanged(await api<FinetuneRun>("POST", `/api/finetune/runs/${run.id}/apply`, {
        industry_sections: applyIndustry ? industry_sections : null,
        business_layer: applyBusiness ? { overrides, addendum } : null,
        note: note.trim(),
      }));
      toast("Applied.", "info");
    } catch (err) { toast(errorText(err)); } finally { setBusy(false); }
  };

  const discard = async () => {
    if (!(await confirm("Discard this run? Nothing it proposed is saved."))) return;
    try { onChanged(await api<FinetuneRun>("POST", `/api/finetune/runs/${run.id}/discard`)); }
    catch (err) { toast(errorText(err)); }
  };

  return (
    <>
      <div className="rv-top">
        <span className={cx("bk-state", `ft-${run.status}`)}>{STATUS[run.status]}</span>
        <span>Run {run.id}</span>
        <span className="muted">{fmtDateTime(run.created_at)} · {run.model} · {sourceCount(run)}</span>
      </div>
      <details className="ft-files"><summary className="muted">{SOURCE_NAME[run.source] ?? "Screenshots"}</summary>{run.files.join(", ")}</details>
      {run.status === "running" && <div className="empty">The model is reading the chats. This can take a few minutes.</div>}
      {run.error && <div className="pf-errors">{run.error}</div>}
      {run.status === "applied" && run.applied && (
        <p className="pf-note">Saved{run.applied.industry_version ? ` industry template v${run.applied.industry_version}` : ""}
          {run.applied.industry_version && run.applied.client_version ? " and" : ""}
          {run.applied.client_version ? ` client version ${run.applied.client_version}` : ""}, {fmtDateTime(run.applied_at)}.{" "}
          <Link href={`/clients?node=tenant-${run.tenant_id}&tab=prompt`}>Edit prices and other sections by hand</Link>
          {" "}(no rerun needed; saves a new client version).</p>
      )}
      {run.stale && run.status === "done" && <div className="pf-errors">{run.stale}</div>}

      {run.result && <>
        {run.result.errors.length > 0 && (
          <div className="pf-section danger">
            <div className="title">Problems in the answer</div>
            <ul className="ft-list">{run.result.errors.map((e) => <li key={e}>{e}</li>)}</ul>
            <p className="pf-note">Fix them below before applying, or start a new run.</p>
          </div>
        )}
        {run.result.notes && (
          <div className="pf-section">
            <div className="title">Notes for the operator</div>
            <div className="inherited-text">{run.result.notes}</div>
          </div>
        )}

        {reviewable && <>
          <div className="sub">Proposal, section by section</div>
          <p className="pf-note">Left: the industry standard, applied to every client in the industry. Right: this
            client&apos;s own layer on top of it. Grey text is what is live now.</p>
          {editors.map((e, i) => {
            const live = current?.industry_sections[e.key] ?? "";
            const liveOverride = current?.business_layer?.overrides?.[e.key];
            return (
              <div key={e.key} className="pf-section ft-pair">
                <div className="title">{e.heading}</div>
                <div className="ft-cols">
                  <div>
                    <div className="rv-label">Industry standard</div>
                    <div className="inherited-text">{live || "(empty now)"}</div>
                    <textarea rows={4} value={e.industry} disabled={!applyIndustry}
                              onChange={(ev) => edit(i, { industry: ev.target.value })} />
                  </div>
                  <div>
                    <div className="rv-label ft-mode">This client
                      <select value={e.mode} disabled={!applyBusiness}
                              onChange={(ev) => edit(i, { mode: ev.target.value as Mode })}>
                        <option value="inherit">Inherit</option>
                        {!e.appendOnly && <option value="override">Override</option>}
                        <option value="append">Append</option>
                      </select>
                    </div>
                    <div className="inherited-text">
                      {liveOverride ? `${liveOverride.mode}: ${liveOverride.text}` : "(inherits now)"}</div>
                    {e.mode !== "inherit" && (
                      <textarea rows={4} value={e.text} disabled={!applyBusiness}
                                onChange={(ev) => edit(i, { text: ev.target.value })} />
                    )}
                  </div>
                </div>
              </div>
            );
          })}
          <div className="pf-section">
            <div className="title">Additional notes from the business (this client)</div>
            {current?.business_layer?.addendum && <div className="inherited-text">{current.business_layer.addendum}</div>}
            <textarea rows={3} maxLength={run.addendum_limit} value={addendum} disabled={!applyBusiness}
                      onChange={(ev) => setAddendum(ev.target.value)} />
            <div className="pf-note">{addendum.length} / {run.addendum_limit} characters</div>
          </div>

          <div className="pf-actions">
            <label className="check"><input type="checkbox" checked={applyIndustry}
                                            onChange={(ev) => setApplyIndustry(ev.target.checked)} /> Apply the industry standard</label>
            <label className="check"><input type="checkbox" checked={applyBusiness}
                                            onChange={(ev) => setApplyBusiness(ev.target.checked)} /> Apply this client&apos;s layer</label>
          </div>
          <div className="pf-actions">
            <input placeholder="Note saved with the new versions (optional)" value={note} onChange={(ev) => setNote(ev.target.value)} />
            <button type="button" className="btn" disabled={busy} onClick={discard}>Discard</button>
            <button type="button" className="btn primary" disabled={busy || (!applyIndustry && !applyBusiness)} onClick={apply}>
              Apply</button>
          </div>
        </>}
      </>}
      {(run.status === "failed" || (run.status === "done" && run.stale)) && (
        <div className="pf-actions"><button type="button" className="btn" onClick={discard}>Discard</button></div>
      )}
      {run.raw_output && (
        <details className="ft-raw"><summary className="muted">The model&apos;s full answer</summary>
          <pre className="pf-rendered">{run.raw_output}</pre></details>
      )}
    </>
  );
}

function RunView({ runId, onBack, onChanged }: { runId: number; onBack: () => void; onChanged: () => void }) {
  const fetchRun = useCallback(() => api<FinetuneRun>("GET", `/api/finetune/runs/${runId}`), [runId]);
  const { data: run, error, reload } = useLoader(fetchRun);
  const running = run?.status === "running";

  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => { void reload(); }, POLL_MS);
    // When it stops running, the list behind "All runs" needs the new status.
    return () => { clearInterval(timer); onChanged(); };
  }, [running, reload, onChanged]);

  return (
    <>
      <div className="pf-actions ft-back"><button type="button" className="btn small" onClick={onBack}>← All runs</button></div>
      {error && <div className="pf-errors">{error}</div>}
      {run && <RunReview key={`${run.id}-${run.status}`} run={run}
                         onChanged={() => { void reload(); onChanged(); }} />}
    </>
  );
}

/* ----------------------------------------------------------------- runs */

function RunList({ runs, onOpen }: { runs: FinetuneRun[]; onOpen: (id: number) => void }) {
  if (!runs.length) return <div className="empty">No runs for this client yet.</div>;
  return (
    <>
      {runs.map((r) => (
        <div key={r.id} className={cx("bk-row", `ft-row-${r.status}`)}>
          <div className="bk-head" role="button" tabIndex={0} onClick={() => onOpen(r.id)}
               onKeyDown={(ev) => { if (ev.key === "Enter") onOpen(r.id); }}>
            <span className={cx("bk-state", `ft-${r.status}`)}>{STATUS[r.status]}</span>
            <span className="bk-time">{fmtDateTime(r.created_at)}</span>
            <span>Run {r.id}</span>
            <span className="muted">{sourceCount(r)} · {r.model}</span>
          </div>
        </div>
      ))}
    </>
  );
}

/* -------------------------------------------------------------- template */

function TemplateEditor({ info, onSaved }: { info: FinetuneIndustry; onSaved: (info: FinetuneIndustry) => void }) {
  const toast = useToast();
  const { prompt, confirm } = useDialogs();
  const [text, setText] = useState(info.template);
  const [busy, setBusy] = useState(false);
  const missing = info.placeholders.filter((p) => !text.includes(p));

  const loadDefault = async () => {
    if (text.trim() && !(await confirm(`Replace the text in the editor with the default ${info.industry.name} template? `
      + "Nothing is saved until you press Save template."))) return;
    setText(info.default_template);
  };

  return (
    <>
      <p className="pf-note">The instructions sent with the chats, for every client in {info.industry.name}. These
        are filled in for each run: {info.placeholders.map((p) => <code key={p}>{p} </code>)}— the business name, how
        many businesses the standard is built from, and the current industry standard (&quot;none&quot; when there is
        none). The answer must be the three blocks === BUSINESS LAYER ===, === INDUSTRY STANDARD === and === NOTES FOR
        OPERATOR ===.</p>
      <textarea className="mono" rows={24} value={text} onChange={(ev) => setText(ev.target.value)} />
      {missing.length > 0 && text.trim() && (
        <div className="pf-errors">Not in the template: {missing.join(", ")}. The model won&apos;t get that information.</div>
      )}
      <div className="pf-actions">
        <span className="muted grow">{text.length.toLocaleString()} characters</span>
        {info.default_template && (
          <button type="button" className="btn" disabled={busy || text === info.default_template} onClick={loadDefault}>
            Load the default</button>
        )}
        <button type="button" className="btn primary" disabled={busy || text === info.template} onClick={async () => {
          const reason = await prompt("Reason for changing the finetune template (goes into the audit log):", "");
          if (reason === null) return;
          setBusy(true);
          try {
            onSaved(await api<FinetuneIndustry>("PUT", `/api/finetune/industries/${info.industry.id}/template`,
              { template: text, reason: reason.trim() }));
            toast("Template saved.", "info");
          } catch (err) { toast(errorText(err)); } finally { setBusy(false); }
        }}>Save template</button>
      </div>
    </>
  );
}

/* --------------------------------------------------------------- quality */

function weekLabel(w: QualityWeek, isLast: boolean): string {
  if (isLast) return "This week";
  const [y, m, d] = w.since.slice(0, 10).split("-").map(Number);
  const sunday = new Date(Date.UTC(y, m - 1, d + 6));
  const two = (n: number) => String(n).padStart(2, "0");
  return `${two(d)}.${two(m)}–${two(sunday.getUTCDate())}.${two(sunday.getUTCMonth() + 1)}`;
}

function percent(part: number, whole: number): string {
  return whole ? `${Math.round((part / whole) * 100)}%` : "—";
}

type QualityRow = [label: string, value: (w: QualityWeek) => string | number, hint?: string];

// How the bot did, week by week: the baseline that shows whether an applied
// run (or any prompt change) made the replies better (stats.quality).
function QualityView({ tenantId }: { tenantId: number }) {
  const fetchQuality = useCallback(() => api<Quality>("GET", `/api/finetune/quality?tenant_id=${tenantId}`), [tenantId]);
  const { data: q, error } = useLoader(fetchQuality);
  if (error) return <div className="pf-errors">{error}</div>;
  if (!q) return <div className="empty">Loading…</div>;

  const reasons = [...new Set(q.weeks.flatMap((w) => Object.keys(w.unanswered_by_reason)))].sort();
  const decided = (w: QualityWeek) => w.drafts.approved + w.drafts.rejected;
  const groups: [string, QualityRow[]][] = [
    ["Conversations", [
      ["Chats with a message from a client", (w) => w.messages.conversations],
      ["Messages from clients", (w) => w.messages.received],
    ]],
    ["The bot", [
      ["Replies the bot wrote", (w) => w.replies_written, "One per reply, however many messages it was sent as."],
      ["Messages it sent", (w) => w.messages.sent_by_bot],
    ]],
    ["Drafts waiting for approval", [
      ["Approved as written", (w) => w.drafts.approved - w.drafts.edited],
      ["Edited, then approved", (w) => w.drafts.edited, "What the bot wrote is kept beside the edit."],
      ["Rejected", (w) => w.drafts.rejected],
      ["Changed or rejected", (w) => percent(w.drafts.edited + w.drafts.rejected, decided(w)),
        "Of the drafts approved or rejected that week."],
    ]],
    ["People", [
      ["Chats taken over", (w) => w.takeovers, `Someone wrote by hand, so the bot kept quiet there for ${q.takeover_hours} h.`],
      ["Messages typed by hand", (w) => w.messages.sent_by_hand, "On the phone or in the panel."],
      ["Chats with hand-typed replies", (w) => w.messages.chats_by_hand],
    ]],
    ["Unanswered", [
      ["All", (w) => w.unanswered],
      ...reasons.map((r): QualityRow => [UNANSWERED_REASONS[r] || r, (w) => w.unanswered_by_reason[r] ?? 0]),
    ]],
    ["Bookings (by the day booked for)", [
      ["Booked", (w) => w.bookings.booked],
      ["Came", (w) => w.bookings.completed],
      ["No-show", (w) => w.bookings.no_show],
    ]],
  ];
  const t = q.totals;

  return (
    <>
      <p className="pf-note">Week by week, Monday to Sunday in {q.timezone}. Note these numbers before you apply a run or
        change the prompt, and compare the weeks after.</p>
      {q.auto_send && (
        <p className="pf-note">Replies go out without waiting for approval here (auto-send is on), so there are no
          drafts to edit or reject: the bot&apos;s mistakes show as chats taken over and messages typed by hand.</p>
      )}
      <table className="cfg-table ft-quality">
        <tbody>
          <tr>
            <td />
            {q.weeks.map((w, i) => <td key={w.since} className="muted num">{weekLabel(w, i === q.weeks.length - 1)}</td>)}
          </tr>
          {groups.map(([group, rows]) => [
            <tr key={group} className="cfg-group"><td colSpan={q.weeks.length + 1}>{group}</td></tr>,
            ...rows.map(([label, value, hint]) => (
              <tr key={`${group}-${label}`}>
                <td title={hint}>{label}{hint && <span className="muted"> ⓘ</span>}</td>
                {q.weeks.map((w) => <td key={w.since} className="num">{value(w)}</td>)}
              </tr>
            )),
          ])}
        </tbody>
      </table>
      <p className="pf-note">All time: {t.typed_by_hand} replies typed by hand, in {t.chats_by_hand} chats
        (the &quot;Hand-typed chats&quot; runs learn from these) · {t.drafts_edited} drafts edited before approval
        (kept since this was added) · {t.chats_flagged} chats flagged for finetuning.</p>
    </>
  );
}

/* ------------------------------------------------------------------ page */

export function Finetune() {
  const fetchTree = useCallback(() => api<PlatformTree>("GET", "/api/platform/tree"), []);
  const { data: tree, error: treeError } = useLoader(fetchTree);
  const [tenantId, setTenantId] = useState<number | null>(null);
  const [tab, setTab] = useState<Tab>("new");
  const [openRun, setOpenRun] = useState<number | null>(null);

  const tenant = useMemo(() => {
    if (!tree?.tenants.length) return null;
    return tree.tenants.find((t) => t.id === tenantId) ?? tree.tenants[0];
  }, [tree, tenantId]);
  const industryName = tree?.industries.find((i) => i.id === tenant?.industry_id)?.name ?? "";

  const fetchInfo = useCallback(async () => (tenant
    ? api<FinetuneIndustry>("GET", `/api/finetune/industries/${tenant.industry_id}`) : null), [tenant]);
  const { data: info, error: infoError, reload: reloadInfo } = useLoader(fetchInfo);
  const fetchRuns = useCallback(async () => (tenant
    ? api<FinetuneRun[]>("GET", `/api/finetune/runs?tenant_id=${tenant.id}`) : []), [tenant]);
  const { data: runs, error: runsError, reload: reloadRuns } = useLoader(fetchRuns);
  // Stable, so RunView can call it when a run stops running.
  const refresh = useCallback(() => { void reloadRuns(); void reloadInfo(); }, [reloadRuns, reloadInfo]);

  const tabs: [Tab, string][] = [["new", "New run"], ["runs", `Runs${runs?.length ? ` (${runs.length})` : ""}`],
    ["template", `Template${industryName ? ` — ${industryName}` : ""}`], ["quality", "Quality"]];
  const error = treeError || infoError || runsError;

  return (
    <PageShell title="Finetune" crumb="a client's prompt from its real chats" width="w-980">
      <div className="bk-scroll">
        {error && <div className="pf-errors">{error}</div>}
        {tree && !tree.tenants.length && <div className="empty">No clients yet.</div>}
        {tenant && <>
          <div className="bk-nav">
            <label className="muted" htmlFor="ft-client">Client</label>
            <select id="ft-client" value={String(tenant.id)}
                    onChange={(ev) => { setTenantId(Number(ev.target.value)); setOpenRun(null); }}>
              {tree?.tenants.map((t) => <option key={t.id} value={String(t.id)}>{t.name}</option>)}
            </select>
            <span className="muted">{industryName}</span>
          </div>
          <Tabs tabs={tabs} value={tab} onChange={(key) => { setTab(key); setOpenRun(null); }} />
          {info && tree && tab === "new" && (
            <NewRun key={tenant.id} tenantId={tenant.id} info={info} tree={tree} onStarted={(run) => {
              void reloadRuns();
              setTab("runs");
              setOpenRun(run.id);
            }} />
          )}
          {tab === "runs" && (openRun !== null
            ? <RunView runId={openRun} onBack={() => setOpenRun(null)}
                       onChanged={refresh} />
            : <RunList runs={runs ?? []} onOpen={setOpenRun} />)}
          {info && tab === "template" && (
            <TemplateEditor key={`${info.industry.id}-${info.template.length}`} info={info}
                            onSaved={() => { void reloadInfo(); }} />
          )}
          {tab === "quality" && <QualityView key={tenant.id} tenantId={tenant.id} />}
        </>}
      </div>
    </PageShell>
  );
}
