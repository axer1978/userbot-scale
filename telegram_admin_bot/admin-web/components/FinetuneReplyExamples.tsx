"use client";

// Finetune → Examples: the reply examples of one client (examples_api.py).
// Real moments of its chats, a client's message and the reply a person
// typed or corrected, kept masked and turned into vectors; with retrieval
// on, every new message shows the model the closest few. The admin builds
// them, tries a message to see what the bot would get (on or off), and
// switches a bad one off. Kept apart from Finetune.tsx, which only mounts it.

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { useToast } from "@/components/feedback";
import { api, errorText } from "@/lib/api";
import { cx, fmtDateTime } from "@/lib/format";
import type {
  ReplyExample, ReplyExampleMatch, ReplyExamplePage, ReplyExamplePhase, ReplyExampleSource, ReplyExamplesRebuild,
  ReplyExamplesStatus, ReplyExamplesWorded, ReplyExampleTry,
} from "@/lib/types";
import { useLoader } from "@/lib/useLoader";
import "@/app/finetune.css";

const PATH = "/api/finetune/reply-examples";
const PAGE = 50;
// One model call words this many (examples.WORDINGS_PER_CALL).
const WORDINGS_LIMIT = 20;
const MAX_TRY_CHARS = 2000;

const SOURCE: Record<ReplyExampleSource, string> = {
  hand: "typed by hand", edited: "corrected draft", flagged: "flagged chat",
};
const PHASE: Record<ReplyExamplePhase, string> = {
  before_time: "before a time is agreed", booked: "booked", after: "after the visit",
};
const PHASES = Object.keys(PHASE) as ReplyExamplePhase[];
const SOURCES = Object.keys(SOURCE) as ReplyExampleSource[];
// Why a turn of a chat did not become an example (examples.build and
// ingest_account), in the admin's words.
const SKIPPED: Record<string, string> = {
  bot: "the bot's own replies (never learned from)",
  corrected: "replies with a correction on a flagged chat",
  time_pending: "the client's time was waiting for the owner",
  time_named: "the client named a time (only the owner answers times)",
  no_client_text: "the client sent only photos or media",
  media_only: "the reply was only photos or media",
  rates_only: "the reply was only the rates or extras message",
  address: "the reply had the address or the way in (the system sends those)",
  too_long: "the reply was too long",
};
const WORDING_ERROR: Record<string, string> = {
  no_key: "DEEPSEEK_PLATFORM_KEY is not set",
  "unusable answer": "the model's answer could not be used",
  embedder: "the embedder stopped answering",
};

function plural(n: number, one: string): string {
  return `${n} ${one}${n === 1 ? "" : "s"}`;
}

function percent(share: number): string {
  return `${Math.round(share * 100)}%`;
}

function capital(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function sourceLabel(source: ReplyExampleSource, verdict: "good" | "plain"): string {
  return source === "flagged" ? `flagged ${verdict === "good" ? "good" : "ordinary"} chat` : SOURCE[source];
}

/** One moment: what the business had just said, the client, her reply. */
function Moment({ context, client, reply }: { context: string; client: string; reply: string }) {
  return (
    <div className="ft-moment">
      {context && <div className="ft-line-head muted"><span className="ft-who">Before</span><span className="ft-text">{context}</span></div>}
      <div className="ft-line-head"><span className="ft-who">Client</span><span className="ft-text">{client}</span></div>
      <div className="ft-line-head ft-business"><span className="ft-who">Reply</span><span className="ft-text">{reply}</span></div>
    </div>
  );
}

/** Switch one example off (never shown again) or back on. */
async function setActive(id: number, active: boolean): Promise<ReplyExample> {
  return api<ReplyExample>("PATCH", `${PATH}/${id}`, { active });
}

/* ------------------------------------------------------------------ try */

type PhaseChoice = ReplyExamplePhase | "any";

// What the bot would get for a client message, whether retrieval is on or
// not: the best ten, which of them the note would hold, and the note.
function TryMessage({ tenantId, embedderUp, onChanged }: {
  tenantId: number; embedderUp: boolean; onChanged: () => void;
}) {
  const toast = useToast();
  const [text, setText] = useState("");
  // The chat's phase: the bot only compares a message with moments of the
  // same one. Most messages come before a time is agreed.
  const [phase, setPhase] = useState<PhaseChoice>("before_time");
  const [asked, setAsked] = useState<{ text: string; phase: PhaseChoice } | null>(null);
  const [found, setFound] = useState<ReplyExampleTry | null>(null);
  const [busy, setBusy] = useState(false);

  const run = async (question: { text: string; phase: PhaseChoice }) => {
    setBusy(true);
    try {
      setFound(await api<ReplyExampleTry>("POST", `${PATH}/try`, {
        tenant_id: tenantId, text: question.text, phase: question.phase === "any" ? null : question.phase,
      }));
      setAsked(question);
    } catch (err) { toast(errorText(err)); } finally { setBusy(false); }
  };

  const switchOff = async (match: ReplyExampleMatch) => {
    try {
      await setActive(match.id, false);
      toast(`Switched off: the bot never gets it again.${match.own ? " The list below can switch it back on."
        : ` It is ${match.account}'s; its own list can switch it back on.`}`, "info");
      onChanged();
      if (asked) await run(asked);
    } catch (err) { toast(errorText(err)); }
  };

  const whyNot = (m: ReplyExampleMatch, f: ReplyExampleTry) => {
    if (f.time_named) return "a time was named";
    if (m.similarity < f.retrieval.min_similarity) return `less than ${percent(f.retrieval.min_similarity)} alike`;
    return `not among the best ${f.retrieval.k} that fit in ${f.retrieval.max_chars.toLocaleString()} characters`;
  };

  return (
    <div className="pf-section">
      <div className="title">Try a message</div>
      <p className="pf-note ft-try-note">Type what a client might write and see which moments the bot would get, whether
        retrieval is on or not. Hide names and numbers the way the examples do, e.g. [client].</p>
      <textarea rows={2} maxLength={MAX_TRY_CHARS} value={text} aria-label="A client's message"
                placeholder="hi, how much for an hour?" onChange={(ev) => setText(ev.target.value)} />
      <div className="pf-actions">
        <select value={phase} aria-label="Where the chat is" onChange={(ev) => setPhase(ev.target.value as PhaseChoice)}>
          <option value="any">Any time in the chat</option>
          {PHASES.map((p) => <option key={p} value={p}>{capital(PHASE[p])}</option>)}
        </select>
        <span className="grow" />
        <button type="button" className="btn primary" disabled={busy || !text.trim() || !embedderUp}
                onClick={() => run({ text, phase })}>{busy ? "Looking…" : "Try"}</button>
      </div>

      {found && <>
        <p className="pf-note ft-try-meta">{found.results.length
          ? `The best ${found.results.length}, best first`
          : "Nothing found: there are no examples for this phase yet"}{" "}
          · {found.vectors.toLocaleString()} vectors searched in {found.ms} ms · {found.model}</p>
        {found.results.length > 0 && (
          <div className="ft-shots">
            {found.results.map((m) => (
              <div key={m.id} className="ft-example">
                <div className="grow">
                  <div className="ft-example-badges">
                    <b>{percent(m.similarity)}</b>
                    <span className={cx("badge", m.shown && "ft-good")}>{m.shown ? "the bot gets it" : "not shown"}</span>
                    <span className="badge">{sourceLabel(m.source, m.verdict)}</span>
                    <span className="badge">{PHASE[m.phase] ?? m.phase}</span>
                    {m.has_figures && <span className="badge">numbers</span>}
                    {!m.own && <span className="badge ft-plain">another account</span>}
                    <span className="muted">{m.chat}{m.shown ? "" : ` · ${whyNot(m, found)}`}</span>
                  </div>
                  <Moment context={m.context_text} client={m.client_text} reply={m.reply_text} />
                  <div className="muted ft-matched">Matched {m.generated ? "a reworded version" : "the client's own words"}:
                    {" "}&ldquo;{m.matched}&rdquo;</div>
                </div>
                <button type="button" className="btn small" onClick={() => switchOff(m)}>Switch off</button>
              </div>
            ))}
          </div>
        )}
        {found.time_named ? (
          <div className="pf-errors">You named a time: only the owner answers times, so the bot gets no examples here.</div>
        ) : found.note ? <>
          <div className="rv-label">What the bot would be told</div>
          <pre className="pf-rendered">{found.note}</pre>
        </> : (
          <p className="pf-note">Nothing alike enough (at least {percent(found.retrieval.min_similarity)}): the bot would
            get no examples for this message.</p>
        )}
      </>}
    </div>
  );
}

/* ----------------------------------------------------------------- list */

type Filters = { source: "" | ReplyExampleSource; phase: "" | ReplyExamplePhase; active: "" | "true" | "false" };

function ExampleList({ tenantId, version, onChanged }: { tenantId: number; version: number; onChanged: () => void }) {
  const toast = useToast();
  const [filters, setFilters] = useState<Filters>({ source: "", phase: "", active: "" });
  const [offset, setOffset] = useState(0);
  const [busy, setBusy] = useState<number | null>(null);
  const fetchPage = useCallback(() => {
    const query = new URLSearchParams({ tenant_id: String(tenantId), limit: String(PAGE), offset: String(offset) });
    for (const [key, value] of Object.entries(filters)) if (value) query.set(key, value);
    return api<ReplyExamplePage>("GET", `${PATH}/items?${query}`);
  }, [tenantId, filters, offset]);
  const { data: page, error, reload } = useLoader(fetchPage);

  // A rebuild or wording run above changed the examples: read the page again.
  useEffect(() => { if (version) void reload(); }, [version, reload]);

  const filter = (patch: Partial<Filters>) => { setFilters((f) => ({ ...f, ...patch })); setOffset(0); };

  const toggle = async (item: ReplyExample) => {
    setBusy(item.id);
    try {
      await setActive(item.id, !item.active);
      await reload();
      onChanged();
    } catch (err) { toast(errorText(err)); } finally { setBusy(null); }
  };

  const total = page?.total ?? 0;
  return (
    <>
      <div className="sub">The examples ({total})</div>
      <div className="bk-nav">
        <select value={filters.source} aria-label="Where the reply came from"
                onChange={(ev) => filter({ source: ev.target.value as Filters["source"] })}>
          <option value="">Every source</option>
          {SOURCES.map((s) => <option key={s} value={s}>{capital(SOURCE[s])}</option>)}
        </select>
        <select value={filters.phase} aria-label="Where the chat was"
                onChange={(ev) => filter({ phase: ev.target.value as Filters["phase"] })}>
          <option value="">Any time in the chat</option>
          {PHASES.map((p) => <option key={p} value={p}>{capital(PHASE[p])}</option>)}
        </select>
        <select value={filters.active} aria-label="Switched on or off"
                onChange={(ev) => filter({ active: ev.target.value as Filters["active"] })}>
          <option value="">On and off</option>
          <option value="true">Switched on</option>
          <option value="false">Switched off</option>
        </select>
      </div>
      {error && <div className="pf-errors">{error}</div>}
      {page && !page.items.length && <div className="empty">No examples{filters.source || filters.phase || filters.active
        ? " with these filters" : " yet: rebuild them from the chats above"}.</div>}
      {page && page.items.length > 0 && (
        <div className="ft-shots">
          {page.items.map((item) => (
            <div key={item.id} className={cx("ft-example", !item.active && "ft-off")}>
              <div className="grow">
                <div className="ft-example-badges">
                  <span className="badge">{sourceLabel(item.source, item.verdict)}</span>
                  <span className="badge">{PHASE[item.phase] ?? item.phase}</span>
                  {item.has_figures && <span className="badge">numbers</span>}
                  {!item.embedded && <span className="badge">no vector yet</span>}
                  <span className="muted">#{item.id} · chat {item.chat_id} · {fmtDateTime(item.created_at)}</span>
                </div>
                <Moment context={item.context_text} client={item.client_text} reply={item.reply_text} />
                {item.wordings.length > 0 && (
                  <details className="ft-wordings">
                    <summary className="muted">The client side reworded {item.wordings.length} ways</summary>
                    <ul className="ft-list">{item.wordings.map((w, i) => <li key={i}>{w}</li>)}</ul>
                  </details>
                )}
              </div>
              <label className="check ft-active">
                <input type="checkbox" checked={item.active} disabled={busy === item.id} onChange={() => toggle(item)} />
                Active</label>
            </div>
          ))}
        </div>
      )}
      {total > PAGE && (
        <div className="pf-actions">
          <span className="muted grow">{offset + 1}–{Math.min(offset + PAGE, total)} of {total}, newest first</span>
          <button type="button" className="btn small" disabled={offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - PAGE))}>← Newer</button>
          <button type="button" className="btn small" disabled={offset + PAGE >= total}
                  onClick={() => setOffset(offset + PAGE)}>Older →</button>
        </div>
      )}
    </>
  );
}

/* ------------------------------------------------------------------ tab */

type Rebuilt = { chats: number; turns: number; added: number; removed: number; embedded: number;
  skipped: Record<string, number>; more: boolean };

function rebuiltText(r: Rebuilt, embedderUp: boolean): string {
  const skipped = Object.entries(r.skipped).filter(([, n]) => n).sort((a, b) => b[1] - a[1])
    .map(([why, n]) => `${SKIPPED[why] ?? why}: ${n.toLocaleString()}`);
  return `Read ${plural(r.chats, "chat")} (${plural(r.turns, "turn")}): ${plural(r.added, "new example")}`
    + `${r.removed ? `, ${r.removed} removed (their chat was flagged bad or unflagged)` : ""}, `
    + `${r.embedded} turned into vectors${!embedderUp && r.added > r.embedded ? " (the rest wait for the embedder)" : ""}.`
    + (skipped.length ? ` Left out: ${skipped.join("; ")}.` : "")
    + (r.more ? " Stopped with chats left to read: Rebuild from the chats goes on from here." : "");
}

export function ReplyExamplesTab({ tenantId, tenantName, industryId, industryName }: {
  tenantId: number; tenantName: string; industryId: number; industryName: string;
}) {
  const toast = useToast();
  const fetchStatus = useCallback(() => api<ReplyExamplesStatus>("GET", `${PATH}?tenant_id=${tenantId}`), [tenantId]);
  const { data: st, error, reload } = useLoader(fetchStatus);
  // Progress of a rebuild ("" when none runs); set to stop it between calls.
  const [rebuilding, setRebuilding] = useState("");
  const stop = useRef(false);
  const [rebuilt, setRebuilt] = useState("");
  const [wording, setWording] = useState(false);
  // Bumped when the examples change, so the list reads them again.
  const [version, setVersion] = useState(0);
  const changed = useCallback(() => { void reload(); setVersion((v) => v + 1); }, [reload]);

  if (!st) return error ? <div className="pf-errors">{error}</div> : <div className="empty">Loading…</div>;
  const c = st.counts;
  const r = st.retrieval;

  // Each call reads the next chats and says whether more are left; keep
  // going until none are, or Stop. Only the first call starts over.
  const rebuild = async (fromStart: boolean) => {
    stop.current = false;
    const sum: Rebuilt = { chats: 0, turns: 0, added: 0, removed: 0, embedded: 0, skipped: {}, more: true };
    let embedderUp = st.embedder.up;
    setRebuilding("Reading the chats…");
    try {
      for (let first = true; sum.more && !stop.current; first = false) {
        const out = await api<ReplyExamplesRebuild>("POST", `${PATH}/rebuild`,
          { tenant_id: tenantId, from_start: fromStart && first });
        const i = out.ingest;
        sum.chats += i.chats; sum.turns += i.turns; sum.added += i.added; sum.removed += i.removed;
        sum.embedded += out.embedded; sum.more = i.more;
        for (const [why, n] of Object.entries(i.skipped)) sum.skipped[why] = (sum.skipped[why] ?? 0) + n;
        embedderUp = out.embedder.up;
        setRebuilding(`Reading the chats… ${sum.chats.toLocaleString()} so far`);
      }
      toast(`Rebuilt: ${plural(sum.added, "new example")}.`, "info");
    } catch (err) { toast(errorText(err)); } finally {
      if (sum.chats) setRebuilt(rebuiltText(sum, embedderUp));
      setRebuilding("");
      changed();
    }
  };

  const word = async () => {
    setWording(true);
    try {
      const { result } = await api<ReplyExamplesWorded>("POST", `${PATH}/wordings`, { tenant_id: tenantId, limit: WORDINGS_LIMIT });
      if (result.error) {
        toast(`Wording stopped: ${WORDING_ERROR[result.error] ?? result.error}`
          + `${result.examples ? ` (after ${plural(result.examples, "example")})` : ""}.`);
      } else {
        toast(result.examples ? `Reworded the client side of ${plural(result.examples, "example")}: `
          + `${plural(result.wordings, "new wording")}.` : "Every example has its wordings already.", "info");
      }
    } catch (err) { toast(errorText(err)); } finally { setWording(false); changed(); }
  };

  const rows: [string, string][] = [
    ["Examples", `${c.total} (${c.active} switched on)`],
    ["Where the reply came from", SOURCES.map((s) => `${SOURCE[s]} ${c.by_source[s] ?? 0}`).join(" · ")],
    ["Where the chat was", PHASES.map((p) => `${PHASE[p]} ${c.by_phase[p] ?? 0}`).join(" · ")],
    ["With numbers in the reply", `${c.with_figures} (shown to this account only)`],
    ["Turned into vectors", `${c.embedded} of ${c.total}${c.not_embedded ? ` (${c.not_embedded} wait for the embedder)` : ""}`],
    ["Client side reworded", `${c.worded} of ${c.total} examples, ${plural(c.wordings, "wording")} in all`],
    [`From the other accounts of ${industryName}`, `${c.shared_in_industry} without numbers`
      + (r.share_in_industry ? "" : " (not used: share_in_industry is off)")],
  ];

  return (
    <>
      {error && <div className="pf-errors">{error}</div>}
      <p className="pf-note">Real moments from the chats: a client&apos;s message and the reply a person typed by hand,
        or a draft an operator corrected before approving it (or the bot&apos;s replies in a chat flagged good). Names,
        numbers, the address and the way in are hidden. With retrieval on, every new message shows the bot the closest
        few, as examples of how she handles a moment like it.</p>
      <ul className="pf-note ft-rules-of">
        <li>Only the client&apos;s side is reworded (about six ways, by the model), so other phrasings still find it.
          Her replies are always what she really sent.</li>
        <li>Times are never used: a client naming a time is never an example and never gets any, since only the owner
          answers times.</li>
        <li>An example with numbers in the reply (a price, a time) stays in its own account; the other accounts of the
          industry only share the ones without.</li>
        <li>The copy guard ({r.copy_guard ? "on" : "off"} here) writes a reply again when it nearly copies an example:
          the same text to many clients gets a number flagged.</li>
      </ul>

      <p className="pf-note"><b>Retrieval {r.enabled ? "on" : "off"}</b> for {tenantName}: at most {r.k} example
        {r.k === 1 ? "" : "s"} a reply, at least {percent(r.min_similarity)} alike, in up to{" "}
        {r.max_chars.toLocaleString()} characters, {r.share_in_industry ? `with ${industryName}'s shared examples`
          : "from this account only"}.{" "}
        {r.enabled ? "" : "Try a message below first. "}To switch it: <Link href={`/clients?node=tenant-${tenantId}&tab=config`}>
          Clients → {tenantName} → Config</Link>, press Override on <code>retrieval.enabled</code>, tick it and save; or
        for every client of {industryName}, the same under{" "}
        <Link href={`/clients?node=industry-${industryId}&tab=config`}>Clients → {industryName} → Default config</Link>.</p>
      {st.embedder.up
        ? <p className="pf-note">Embedder: running ({st.embedder.model}).</p>
        : <div className="pf-errors">The embedder is not answering: trying a message, wording and new vectors wait for
          it. Rebuilding still stores the examples.</div>}
      {!st.wordings_ready && (
        <div className="pf-errors">Rewording the client side needs DEEPSEEK_PLATFORM_KEY in the server&apos;s .env.</div>
      )}
      <table className="cfg-table ft-style">
        <tbody>{rows.map(([label, value]) => <tr key={label}><td>{label}</td><td>{value}</td></tr>)}</tbody>
      </table>
      <p className="pf-note">Chats last read: {fmtDateTime(st.last_ingest_at, "never")}.{" "}
        {st.in_hourly_round ? "The hourly round keeps them up to date."
          : `The hourly round skips ${industryName} until one of its accounts has retrieval on: rebuild by hand to try it first.`}</p>

      <div className="pf-actions">
        <span className="muted grow">{rebuilding || "Reads the chats not read yet, stores what is new and turns it "
          + "into vectors."}</span>
        {rebuilding ? (
          <button type="button" className="btn" onClick={() => { stop.current = true; }}>Stop</button>
        ) : (
          <button type="button" className="btn" disabled={wording} onClick={() => rebuild(true)}
                  title="Read every chat again; examples already stored are kept">From the start</button>
        )}
        <button type="button" className="btn primary" disabled={!!rebuilding || wording} onClick={() => rebuild(false)}>
          {rebuilding ? "Rebuilding…" : "Rebuild from the chats"}</button>
      </div>
      {rebuilt && <p className="pf-note ft-rebuilt">{rebuilt}</p>}
      <div className="pf-actions">
        <span className="muted grow">{wording ? "The model is rewording the client side… (up to a minute or two)"
          : `Rewords the client side of the next ${WORDINGS_LIMIT} examples that have no wordings yet `
            + `(${c.total - c.worded} have none).`}</span>
        <button type="button" className="btn" onClick={word}
                disabled={wording || !!rebuilding || !st.wordings_ready || !st.embedder.up || c.total === c.worded}>
          {wording ? "Rewording…" : "Word the client side again"}</button>
      </div>

      <TryMessage tenantId={tenantId} embedderUp={st.embedder.up} onChanged={changed} />
      <ExampleList tenantId={tenantId} version={version} onChanged={() => { void reload(); }} />
    </>
  );
}
