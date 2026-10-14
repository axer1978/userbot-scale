"use client";

// The open account's conversations: the list, the thread with the AI's
// drafts waiting for approval, linked context, and a box to write as
// yourself. Live through the panel's socket (lib/panel.tsx). The admin can
// flag a chat for finetuning from its header (FinetuneExampleDialog).

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useToast } from "@/components/feedback";
import { FinetuneExampleDialog, verdictLabel } from "@/components/FinetuneExampleDialog";
import { MediaPreview } from "@/components/MediaDialog";
import { api, errorText } from "@/lib/api";
import { contactLabel, cx, fmtTime } from "@/lib/format";
import { usePanel } from "@/lib/panel";
import type { ChatLink, Conversation, ExampleVerdict, LinkSuggestion, Message } from "@/lib/types";

// chat id → verdict, for the account's chats flagged for finetuning (admin only).
type Flags = Record<string, ExampleVerdict>;
const NO_FLAGS: Flags = {};

// Someone wrote in this chat by hand; the bot stays quiet until then.
function takeoverActive(conv?: Conversation | null): boolean {
  return !!(conv && conv.human_takeover_until && new Date(conv.human_takeover_until) > new Date());
}

/* ----------------------------------------------------------------- sidebar */

function Sidebar({ onOpen, flags }: { onOpen: () => void; flags: Flags }) {
  const { state, sApi, selectChat } = usePanel();
  const open = (chatId: number) => { onOpen(); void selectChat(chatId); };
  const toast = useToast();

  const togglePause = async (conv: Conversation) => {
    try { await sApi("POST", `/conversations/${conv.chat_id}/pause`, { paused: !conv.automation_paused }); }
    catch (err) { toast(errorText(err)); }
  };

  if (!state.sessionId) {
    return <aside id="sidebar"><div className="empty">Pick a session above to get started.</div></aside>;
  }
  if (!state.conversations.length) {
    return <aside id="sidebar"><div className="empty">No conversations yet.</div></aside>;
  }
  return (
    <aside id="sidebar">
      {state.conversations.map((conv) => {
        const escalated = (conv.paused_reason || "").startsWith("escalation");
        return (
          <div key={conv.chat_id} role="button" tabIndex={0}
               className={cx("conv", conv.chat_id === state.activeChatId && "active")}
               onClick={() => open(conv.chat_id)}
               onKeyDown={(ev) => { if (ev.key === "Enter") open(conv.chat_id); }}>
            <div className="conv-top">
              <span className="conv-name">{conv.display_name || String(conv.chat_id)}</span>
              <span className="conv-time">{fmtTime(conv.last_message_at)}</span>
            </div>
            <div className="conv-preview">{conv.last_message_preview || "—"}</div>
            <div className="conv-meta">
              {conv.is_bot && <span className="badge bot">bot</span>}
              {flags[String(conv.chat_id)] && (
                <span className={cx("badge", `ft-${flags[String(conv.chat_id)]}`)} title="Flagged for finetuning">
                  ★ {verdictLabel(flags[String(conv.chat_id)])}</span>
              )}
              {conv.automation_paused && (
                <span className={cx("badge", escalated ? "escalated" : "paused")} title={conv.paused_reason || "paused by hand"}>
                  {escalated ? "escalated" : "paused"}
                </span>
              )}
              {takeoverActive(conv) && <span className="badge takeover">you&apos;re handling</span>}
              {(conv.unread || 0) > 0 && <span className="unread">{conv.unread}</span>}
              <span style={{ flex: 1 }} />
              <button type="button" className={cx("btn small", conv.automation_paused && "on")}
                      onClick={(ev) => { ev.stopPropagation(); void togglePause(conv); }}>
                {conv.automation_paused ? "Resume" : "Pause"}
              </button>
            </div>
          </div>
        );
      })}
    </aside>
  );
}

/* ------------------------------------------------------------ thread header */

/* A chat this conversation draws on. The reason is on the chip's tooltip:
   a link the app made on its own should never be unexplained. */
function LinkChip({ link }: { link: ChatLink }) {
  const { sApi, dispatch } = usePanel();
  const toast = useToast();
  return (
    <span className="badge link" title={(link.origin === "auto" ? "Detected: " : "Linked by hand: ") + (link.reason || "same person")}>
      <span>context: {link.source_name}</span>
      <button type="button" title="Stop drawing on this chat" onClick={async () => {
        try {
          const res = await sApi<{ links: ChatLink[] }>("DELETE", `/conversations/${link.chat_id}/links/${link.source_id}`);
          dispatch({ type: "links", links: res.links });
          toast("Unlinked. This chat's replies won't use that one.", "info");
        } catch (err) { toast(errorText(err)); }
      }}>×</button>
    </span>
  );
}

function LinkPicker({ conv }: { conv: Conversation }) {
  const { state, sApi, dispatch } = usePanel();
  const toast = useToast();
  const ref = useRef<HTMLSelectElement>(null);
  useEffect(() => { ref.current?.focus(); }, []);
  const linked = new Set(state.links.map((l) => l.source_id));
  const options = state.linkOptions || [];

  return (
    <select ref={ref} className="link-picker" defaultValue=""
            onBlur={() => dispatch({ type: "linkOptions", options: null })}
            onChange={async (ev) => {
              const sourceId = Number(ev.target.value);
              dispatch({ type: "linkOptions", options: null });
              if (!sourceId) return;
              try {
                const res = await sApi<{ links: ChatLink[] }>("POST", `/conversations/${conv.chat_id}/links`, { source_id: sourceId });
                dispatch({ type: "links", links: res.links });
                toast("Linked. Replies here will draw on that chat.", "info");
              } catch (err) { toast(errorText(err)); }
            }}>
      <option value="">Draw on which chat?</option>
      {/* What detection thought was the same person but would not act on by
          itself goes first; then every conversation, in case it is someone
          it had no way of recognising. */}
      {options.length > 0 && (
        <optgroup label="Looks like the same person">
          {options.map((s: LinkSuggestion) => (
            <option key={`s-${s.chat_id}`} value={s.chat_id}>{contactLabel(s)} — {s.reason}</option>
          ))}
        </optgroup>
      )}
      <optgroup label="All conversations">
        {state.conversations
          .filter((c) => c.chat_id !== conv.chat_id && !linked.has(c.chat_id))
          .map((c) => <option key={c.chat_id} value={c.chat_id}>{contactLabel(c)}</option>)}
      </optgroup>
    </select>
  );
}

function ThreadHeader({ onBack, flags, onFlagged }: {
  onBack: () => void;
  flags: Flags;
  onFlagged: (chatId: number, verdict: ExampleVerdict | null) => void;
}) {
  const { state, sApi, dispatch, isAdmin } = usePanel();
  const toast = useToast();
  const [flagging, setFlagging] = useState(false);
  const conv = state.conversations.find((c) => c.chat_id === state.activeChatId) || null;
  const closeFlag = useCallback(() => setFlagging(false), []);

  if (!conv) {
    return <div id="thread-header"><span className="muted">No conversation selected</span></div>;
  }

  const until = conv.human_takeover_until ? new Date(conv.human_takeover_until) : null;

  return (
    <div id="thread-header">
      {/* Phones: the list and the chat take turns; this goes back to the list. */}
      <button type="button" className="btn small back-btn" onClick={onBack}>‹ Chats</button>
      <strong>{conv.display_name || String(conv.chat_id)}</strong>
      {conv.username && <span className="muted">@{conv.username}</span>}
      {conv.is_bot && <span className="badge bot">bot</span>}
      {state.links.map((link) => <LinkChip key={link.source_id} link={link} />)}
      <span className="spacer" />
      {state.linkOptions ? <LinkPicker conv={conv} /> : (
        <button type="button" className="btn small" title="Answer this chat with what another chat already knows"
                onClick={async () => {
                  try {
                    const data = await sApi<{ links: ChatLink[]; suggestions: LinkSuggestion[] }>("GET", `/conversations/${conv.chat_id}/links`);
                    dispatch({ type: "links", links: data.links });
                    dispatch({ type: "linkOptions", options: data.suggestions });
                  } catch (err) { toast(errorText(err)); }
                }}>Link chat…</button>
      )}
      {takeoverActive(conv) && until && (
        <>
          <span className="badge takeover">
            bot quiet until {until.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
          </span>
          <button type="button" className="btn small"
                  title={"Someone wrote here by hand, so the bot is quiet until " +
                    until.toLocaleString([], { dateStyle: "short", timeStyle: "short" }) + ". It then carries on by itself."}
                  onClick={async () => {
                    try {
                      dispatch({ type: "conversation",
                        conversation: await sApi<Conversation>("POST", `/conversations/${conv.chat_id}/takeover`, { active: false }) });
                    } catch (err) { toast(errorText(err)); }
                  }}>Hand back to the bot</button>
        </>
      )}
      {isAdmin && state.sessionId && (
        <button type="button" className={cx("btn small", flags[String(conv.chat_id)] && `ft-flagged`)}
                title="Mark this chat as a good or bad example for finetuning, and say how to read it"
                onClick={() => setFlagging(true)}>
          {flags[String(conv.chat_id)] ? `★ ${verdictLabel(flags[String(conv.chat_id)])}` : "Flag for finetune"}
        </button>
      )}
      <button type="button" className={cx("btn small", conv.automation_paused && "on")} title={conv.paused_reason || undefined}
              onClick={async () => {
                try { await sApi("POST", `/conversations/${conv.chat_id}/pause`, { paused: !conv.automation_paused }); }
                catch (err) { toast(errorText(err)); }
              }}>
        {conv.automation_paused ? "Automation paused" : "Pause automation"}
      </button>
      {flagging && state.sessionId && (
        <FinetuneExampleDialog key={`${state.sessionId}-${conv.chat_id}`} sessionId={state.sessionId}
                               chatId={conv.chat_id} name={contactLabel(conv)} onClose={closeFlag}
                               onChanged={(verdict) => onFlagged(conv.chat_id, verdict)} />
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ thread */

// Thumbnails for the files a row carries: a sent file, or what a draft will
// send with it. A file gone from the library is named rather than shown.
function Attachments({ ids }: { ids?: number[] }) {
  const { state } = usePanel();
  if (!ids || !ids.length || !state.sessionId) return null;
  return (
    <div className="attachments">
      {ids.map((id) => {
        const item = state.media.find((m) => m.id === id);
        return item ? <MediaPreview key={id} item={item} sessionId={state.sessionId!} />
          : <span key={id} className="chip">file #{id} (removed)</span>;
      })}
    </div>
  );
}

function MessageRow({ msg }: { msg: Message }) {
  if (msg.status === "note" || msg.status === "error") {
    return (
      <div className={`msg ${msg.status}`}>
        <div className="label">{msg.status}</div>
        <div>{msg.text}</div>
        <div className="time">{fmtTime(msg.created_at)}</div>
      </div>
    );
  }
  const isIn = msg.direction === "in";
  const rejected = msg.status === "rejected";
  return (
    <div className={cx("msg", rejected ? "rejected" : isIn ? "in" : "out")}>
      <div className="label">{isIn ? "them" : rejected ? "rejected draft" : "me"}</div>
      {msg.text && <div>{msg.text}</div>}
      <Attachments ids={msg.attachments} />
      <div className="time">{fmtTime(msg.created_at)}</div>
    </div>
  );
}

function Draft({ msg }: { msg: Message }) {
  const { state, sApi, draftEdits } = usePanel();
  const toast = useToast();
  const id = msg.id!;
  const [editing, setEditing] = useState(() => draftEdits.current.has(id));
  const [text, setText] = useState(() => draftEdits.current.get(id) ?? msg.text);
  const [busy, setBusy] = useState(false);
  const box = useRef<HTMLTextAreaElement>(null);

  const kinds = (msg.attachments || []).map((a) => state.media.find((m) => m.id === a)?.kind || "file");

  const send = async (edited: string | null) => {
    setBusy(true);
    try {
      await sApi("POST", `/drafts/${id}/approve`, edited === null ? {} : { text: edited });
      draftEdits.current.delete(id);
    } catch (err) { toast(errorText(err)); setBusy(false); }
  };

  return (
    <div className="draft">
      <div className="label">AI draft — awaiting approval</div>
      {editing ? (
        <textarea ref={box} rows={4} value={text} onChange={(ev) => {
          setText(ev.target.value);
          draftEdits.current.set(id, ev.target.value);
        }} />
      ) : <div>{msg.text}</div>}
      {kinds.length > 0 && <div className="label">will send {kinds.join(" + ")} with it</div>}
      <Attachments ids={msg.attachments} />
      <div className="draft-actions">
        {!editing && (
          <button type="button" className="btn small primary" disabled={busy} onClick={() => send(null)}>Approve &amp; Send</button>
        )}
        <button type="button" className={cx("btn small", editing && "primary")} disabled={busy} onClick={() => {
          if (!editing) {
            setEditing(true);
            draftEdits.current.set(id, text);
            setTimeout(() => box.current?.focus(), 0);
            return;
          }
          void send(text);
        }}>{editing ? "Send edited" : "Edit then Send"}</button>
        <button type="button" className="btn small warn" disabled={busy} onClick={async () => {
          setBusy(true);
          draftEdits.current.delete(id);
          try { await sApi("POST", `/drafts/${id}/reject`); }
          catch (err) { toast(errorText(err)); setBusy(false); }
        }}>Reject</button>
      </div>
      <div className="time">{fmtTime(msg.created_at)}</div>
    </div>
  );
}

function Thread() {
  const { state } = usePanel();
  const ref = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const drafting = state.activeChatId !== null && state.drafting.includes(state.activeChatId);

  // Follow new messages only while already at the bottom.
  useLayoutEffect(() => {
    const node = ref.current;
    if (node && stick.current) node.scrollTop = node.scrollHeight;
  }, [state.messages, drafting]);

  useLayoutEffect(() => { stick.current = true; }, [state.activeChatId]);

  return (
    <div id="thread" ref={ref} onScroll={() => {
      const node = ref.current;
      if (node) stick.current = node.scrollTop + node.clientHeight >= node.scrollHeight - 60;
    }}>
      {state.activeChatId === null ? <div className="empty">Incoming DMs appear here as they arrive.</div> : (
        <>
          {state.messages.map((msg, i) =>
            msg.status === "pending_approval" && msg.id !== null
              ? <Draft key={`d-${msg.id}`} msg={msg} />
              : <MessageRow key={msg.id ?? `n-${i}`} msg={msg} />)}
          {drafting && <div className="typing">AI is preparing a reply…</div>}
        </>
      )}
    </div>
  );
}

function Composer() {
  const { state, sApi } = usePanel();
  const toast = useToast();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const box = useRef<HTMLTextAreaElement>(null);

  if (state.activeChatId === null) return null;
  const chatId = state.activeChatId;

  const submit = async () => {
    const value = text.trim();
    if (!value) return;
    setBusy(true);
    try {
      await sApi("POST", `/conversations/${chatId}/send`, { text: value });
      setText("");
    } catch (err) { toast(errorText(err)); }
    setBusy(false);
    box.current?.focus();
  };

  return (
    <div id="composer">
      <textarea ref={box} rows={1} placeholder="Write a message as yourself…" value={text}
                onChange={(ev) => setText(ev.target.value)}
                onKeyDown={(ev) => { if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); void submit(); } }} />
      <button type="button" className="btn primary" disabled={busy} onClick={submit}>Send</button>
    </div>
  );
}

export function Conversations() {
  const { state, me, can, isAdmin } = usePanel();
  const [flagged, setFlagged] = useState<{ sessionId: string | null; flags: Flags }>({ sessionId: null, flags: NO_FLAGS });
  // Phones: the list and the open chat take turns filling the screen.
  const [showChat, setShowChat] = useState(state.activeChatId !== null);
  const [shownFor, setShownFor] = useState(state.activeChatId);
  if (shownFor !== state.activeChatId) {
    setShownFor(state.activeChatId);
    setShowChat(state.activeChatId !== null);
  }

  // Finetune is the admin's alone; a moderator never asks. Kept with the
  // account they belong to, so a switch never shows the last one's.
  const sessionId = state.sessionId;
  const flags = flagged.sessionId === sessionId ? flagged.flags : NO_FLAGS;
  useEffect(() => {
    if (!isAdmin || !sessionId) return;
    let cancelled = false;
    api<Flags>("GET", `/api/finetune/sessions/${encodeURIComponent(sessionId)}/flags`)
      .then((data) => { if (!cancelled) setFlagged({ sessionId, flags: data }); })
      .catch(() => { /* the badges are a convenience */ });
    return () => { cancelled = true; };
  }, [isAdmin, sessionId]);
  const onFlagged = useCallback((chatId: number, verdict: ExampleVerdict | null) => setFlagged((s) => {
    const next = { ...s.flags };
    if (verdict) next[String(chatId)] = verdict; else delete next[String(chatId)];
    return { ...s, flags: next };
  }), []);

  if (me && !can("view.conversations")) {
    return (
      <main className="chat">
        <div className="empty">Your role does not include accounts and conversations. Use the buttons at the top for
          what it does include.</div>
      </main>
    );
  }

  return (
    <main className={cx("chat", showChat && "chat-open")}>
      <Sidebar onOpen={() => setShowChat(true)} flags={flags} />
      <section id="panel">
        <ThreadHeader onBack={() => setShowChat(false)} flags={flags} onFlagged={onFlagged} />
        <Thread />
        <Composer />
      </section>
    </main>
  );
}
