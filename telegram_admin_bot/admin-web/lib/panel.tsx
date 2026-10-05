"use client";

// The state of "whichever account is open": its conversations, the open
// chat, its config and status, its media and outreach queue, and the live
// socket (/ws/{session_id}) that keeps all of it current. Lives in the
// panel layout, so moving between pages keeps the chat where it was and
// the socket connected. Also the top bar's counters (safety, unanswered).

import { createContext, useCallback, useContext, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { useToast } from "@/components/feedback";
import { api, errorText, sessionPath, UNAUTHORIZED_EVENT } from "@/lib/api";
import type {
  AuthStep, Channel, ChatLink, Controls, Conversation, LinkSuggestion, MediaItem, Me, Message, OutreachItem,
  ReviewSummary, SafetySummary, Session, SessionConfig, Status, TenantConfig,
} from "@/lib/types";

/* ---------------------------------------------------------------- the role */
// The admin may do everything. A moderator only what their role lists:
// "allow" (done at once) or "approve" (it waits for the admin, but looks
// done). Views are only ever "allow". The server checks all of this again;
// the page only hides what would be refused.

export function permits(me: Me | null, key: string): boolean {
  if (!me) return false;
  if (me.admin) return true;
  const level = me.permissions[key];
  return level === "allow" || level === "approve";
}

/** Done at once, not put up for approval: for changes the page makes on its
 *  own (marking a chat read), which must not fill the admin's queue. */
export function permitsNow(me: Me | null, key: string): boolean {
  return !!me && (me.admin || me.permissions[key] === "allow");
}

/** The network the open account is on: its live status, else the session list. */
export function currentChannel(state: { status: Status | null; sessions: Session[]; sessionId: string | null }): Channel {
  if (state.status?.channel) return state.status.channel;
  const s = state.sessions.find((x) => x.session_id === state.sessionId);
  return s?.channel || "telegram";
}

export function channelName(channel?: Channel | null): string {
  return channel === "whatsapp" ? "WhatsApp" : "Telegram";
}

/* ------------------------------------------------------------------- state */

export type SocketState = "idle" | "connecting" | "live" | "reconnecting";

type State = {
  sessions: Session[];
  sessionsLoaded: boolean;
  sessionId: string | null;
  conversations: Conversation[];
  messages: Message[];
  activeChatId: number | null;
  drafting: number[];
  links: ChatLink[];
  linkOptions: LinkSuggestion[] | null;
  config: SessionConfig | null;
  tenantConfig: TenantConfig | null;
  status: Status | null;
  media: MediaItem[];
  outreachItems: OutreachItem[];
  socket: SocketState;
};

const initialState: State = {
  sessions: [], sessionsLoaded: false, sessionId: null,
  conversations: [], messages: [], activeChatId: null, drafting: [], links: [], linkOptions: null,
  config: null, tenantConfig: null, status: null, media: [], outreachItems: [], socket: "idle",
};

type Action =
  | { type: "sessions"; sessions: Session[] }
  | { type: "selectSession"; sessionId: string | null }
  | { type: "hello"; conversations: Conversation[]; media: MediaItem[]; config: SessionConfig | null;
      tenantConfig: TenantConfig | null; status: Status | null }
  | { type: "conversations"; conversations: Conversation[] }
  | { type: "conversation"; conversation: Conversation | null | undefined }
  | { type: "message"; message: Message }
  | { type: "selectChat"; chatId: number | null }
  | { type: "messages"; chatId: number; messages: Message[]; links: ChatLink[] }
  | { type: "drafting"; chatId: number; on: boolean }
  | { type: "links"; links: ChatLink[] }
  | { type: "linkAdded"; link: ChatLink }
  | { type: "linkRemoved"; chatId: number; sourceId: number }
  | { type: "linkOptions"; options: LinkSuggestion[] | null }
  | { type: "config"; config: SessionConfig | null }
  | { type: "tenantConfig"; config: TenantConfig | null | undefined }
  | { type: "status"; status: Status | null | undefined }
  | { type: "controls"; controls: Controls | null | undefined }
  | { type: "media"; media: MediaItem[] }
  | { type: "outreach"; items: OutreachItem[] }
  | { type: "socket"; socket: SocketState };

function byLastMessage(a: Conversation, b: Conversation) {
  return String(b.last_message_at || "").localeCompare(String(a.last_message_at || ""));
}

function withControls(status: Status | null, controls: Controls): Status {
  const next: Status = { ...(status || {}) };
  if (controls.holds) {
    next.holds = controls.holds;
    next.global_pause = controls.holds.some((h) => h.kind === "manual");
  }
  if ("off_reason" in controls) next.off_reason = controls.off_reason;
  return next;
}

function reducer(state: State, action: Action): State {
  switch (action.type) {
    case "sessions":
      return { ...state, sessions: action.sessions, sessionsLoaded: true };
    case "selectSession":
      // Everything that belongs to "whichever account is open" starts over.
      return { ...initialState, sessions: state.sessions, sessionsLoaded: state.sessionsLoaded,
               sessionId: action.sessionId, socket: action.sessionId ? "connecting" : "idle" };
    case "hello":
      return {
        ...state,
        conversations: action.conversations,
        media: action.media,
        config: action.config,
        tenantConfig: action.tenantConfig ?? state.tenantConfig,
        status: action.status ? withControls({ ...(state.status || {}), ...action.status }, action.status) : state.status,
      };
    case "conversations":
      return { ...state, conversations: action.conversations };
    case "conversation": {
      const conv = action.conversation;
      if (!conv) return state;
      const list = state.conversations.filter((c) => c.chat_id !== conv.chat_id);
      list.push(conv);
      list.sort(byLastMessage);
      return { ...state, conversations: list };
    }
    case "message": {
      const msg = action.message;
      if (msg.chat_id !== state.activeChatId) return state;
      const idx = msg.id === null ? -1 : state.messages.findIndex((m) => m.id === msg.id);
      const messages = state.messages.slice();
      if (idx === -1) messages.push(msg);
      else messages[idx] = msg;
      return { ...state, messages };
    }
    case "selectChat":
      return { ...state, activeChatId: action.chatId, messages: [], links: [], linkOptions: null };
    case "messages":
      if (action.chatId !== state.activeChatId) return state;
      return { ...state, messages: action.messages, links: action.links };
    case "drafting": {
      const rest = state.drafting.filter((id) => id !== action.chatId);
      return { ...state, drafting: action.on ? [...rest, action.chatId] : rest };
    }
    case "links":
      return { ...state, links: action.links };
    case "linkAdded":
      if (action.link.chat_id !== state.activeChatId) return state;
      if (state.links.some((l) => l.source_id === action.link.source_id)) return state;
      return { ...state, links: [...state.links, action.link] };
    case "linkRemoved":
      if (action.chatId !== state.activeChatId) return state;
      return { ...state, links: state.links.filter((l) => l.source_id !== action.sourceId) };
    case "linkOptions":
      return { ...state, linkOptions: action.options };
    case "config":
      return { ...state, config: action.config };
    case "tenantConfig":
      return action.config ? { ...state, tenantConfig: action.config } : state;
    case "status":
      if (!action.status) return state;
      return { ...state, status: withControls({ ...(state.status || {}), ...action.status }, action.status) };
    case "controls":
      if (!action.controls) return state;
      return { ...state, status: withControls(state.status, action.controls) };
    case "media":
      return { ...state, media: action.media };
    case "outreach":
      return { ...state, outreachItems: action.items };
    case "socket":
      return { ...state, socket: action.socket };
  }
}

/* ----------------------------------------------------------------- context */

export type SocketEvent = { type: string; [key: string]: unknown };
type Listener = (event: SocketEvent) => void;
export type Modal = "outreach" | "style" | "media" | null;

type Panel = {
  state: State;
  /** Who is signed in (GET /api/me); null until known. */
  me: Me | null;
  isAdmin: boolean;
  /** The role may see / do this (allowed, or allowed with the admin's approval). */
  can: (key: string) => boolean;
  dispatch: React.Dispatch<Action>;
  /** A request under the open account's /api/sessions/{id}. */
  sApi: <T = unknown>(method: string, subpath: string, body?: unknown, sessionId?: string) => Promise<T>;
  refreshSessions: () => Promise<Session[] | null>;
  selectSession: (sessionId: string) => Promise<void>;
  selectChat: (chatId: number | null) => Promise<void>;
  refreshStatus: () => Promise<void>;
  logout: () => Promise<void>;
  /** Text of drafts being edited, by draft id, so a re-render keeps an edit. */
  draftEdits: React.RefObject<Map<number, string>>;
  modal: Modal;
  setModal: (modal: Modal) => void;
  /** The "add a Telegram account" dialog; resolves with the new session id, or null. */
  addAccount: AuthStep | null;
  setAddAccount: (auth: AuthStep | null) => void;
  openAddAccount: () => Promise<string | null>;
  /** Closes the dialog: with the new account (and what to say about it), or without one. */
  finishAddAccount: (sessionId: string | null, options?: { message?: string; keepTelegram?: boolean }) => void;
  safety: SafetySummary | null;
  applySafety: (summary: SafetySummary) => void;
  refreshSafety: () => Promise<void>;
  unansweredOpen: number;
  setUnansweredOpen: (count: number) => void;
  /** Identity videos and photos waiting (Verification), and moderator changes waiting (Staff). */
  verifyCount: number;
  setVerifyCount: (count: number) => void;
  staffCount: number;
  setStaffCount: (count: number) => void;
  /** Raw socket events, for pages that refresh themselves on one. */
  subscribe: (listener: Listener) => () => void;
};

const PanelContext = createContext<Panel | null>(null);

export function usePanel(): Panel {
  const panel = useContext(PanelContext);
  if (!panel) throw new Error("usePanel() outside <PanelProvider>");
  return panel;
}

/** Runs `handler` for every socket event of one of the given types. */
export function useSocketEvent(types: string[], handler: (event: SocketEvent) => void) {
  const { subscribe } = usePanel();
  const ref = useRef(handler);
  useEffect(() => { ref.current = handler; });
  const key = types.join(",");
  useEffect(() => subscribe((event) => {
    if (key.split(",").includes(event.type)) ref.current(event);
  }), [subscribe, key]);
}

/* ---------------------------------------------------------------- provider */

const PREFERRED_SESSION = "panelSessionId";

function readPreferred(): string | null {
  try { return localStorage.getItem(PREFERRED_SESSION); } catch { return null; }
}

function writePreferred(sessionId: string) {
  try { localStorage.setItem(PREFERRED_SESSION, sessionId); } catch { /* private window */ }
}

const BOOKING_STATE: Record<string, string> = {
  requested: "requested — not sent to the owner yet", pending: "waiting for the owner", confirmed: "confirmed",
  cancelled: "cancelled", no_show: "marked as missed", completed: "done",
};

export function PanelProvider({ children }: { children: React.ReactNode }) {
  const [state, dispatch] = useReducer(reducer, initialState);
  const [modal, setModal] = useState<Modal>(null);
  const [addAccount, setAddAccount] = useState<AuthStep | null>(null);
  const [safety, setSafety] = useState<SafetySummary | null>(null);
  const [unansweredOpen, setUnansweredOpen] = useState(0);
  const [verifyCount, setVerifyCount] = useState(0);
  const [staffCount, setStaffCount] = useState(0);
  const [me, setMe] = useState<Me | null>(null);
  const toast = useToast();
  const router = useRouter();
  const pathname = usePathname();

  // Set synchronously on a switch, so a request that comes back for the
  // account that was open before can tell it is stale.
  const sessionIdRef = useRef<string | null>(null);
  const activeChatRef = useRef<number | null>(null);
  const draftEdits = useRef(new Map<number, string>());
  const listeners = useRef(new Set<Listener>());
  const addAccountResolve = useRef<((sessionId: string | null) => void) | null>(null);
  const signedOut = useRef(false);
  // For callbacks that must not change identity when these do.
  const meRef = useRef<Me | null>(null);
  const stateRef = useRef(state);
  useEffect(() => { stateRef.current = state; });

  const sApi = useCallback(<T,>(method: string, subpath: string, body?: unknown, sessionId?: string) => {
    const id = sessionId || sessionIdRef.current;
    if (!id) return Promise.reject(new Error("Pick an account first."));
    return api<T>(method, sessionPath(id, subpath), body);
  }, []);

  const subscribe = useCallback((listener: Listener) => {
    listeners.current.add(listener);
    return () => { listeners.current.delete(listener); };
  }, []);

  const refreshSessions = useCallback(async () => {
    if (!permits(meRef.current, "view.conversations")) return null;
    try {
      const sessions = await api<Session[]>("GET", "/api/sessions");
      dispatch({ type: "sessions", sessions });
      return sessions;
    } catch {
      return null;
    }
  }, []);

  const applySafety = useCallback((summary: SafetySummary) => setSafety(summary), []);

  // The top bar's counters, each only when the role may see it.
  const refreshSafety = useCallback(async () => {
    if (!permits(meRef.current, "view.safety")) return;
    try { setSafety(await api<SafetySummary>("GET", "/api/safety/summary")); } catch { /* next poll */ }
  }, []);

  const refreshUnanswered = useCallback(async () => {
    if (!permits(meRef.current, "view.unanswered")) return;
    try { setUnansweredOpen((await api<{ open: number }>("GET", "/api/unanswered/count")).open); } catch { /* next poll */ }
  }, []);

  const refreshVerify = useCallback(async () => {
    if (!permits(meRef.current, "view.verification")) return;
    try {
      const pending = (await api<ReviewSummary>("GET", "/api/review/summary")).pending || {};
      setVerifyCount((pending.verifications || 0) + (pending.photos || 0));
    } catch { /* next poll */ }
  }, []);

  const refreshStaff = useCallback(async () => {
    if (!meRef.current?.admin) return;
    try {
      setStaffCount((await api<{ pending: number }>("GET", "/api/staff/requests?status=pending&limit=1")).pending || 0);
    } catch { /* next poll */ }
  }, []);

  const refreshStatus = useCallback(async () => {
    const id = sessionIdRef.current;
    if (!id) return;
    try {
      const status = await api<Status>("GET", sessionPath(id, "/status"));
      if (sessionIdRef.current === id) dispatch({ type: "status", status });
    } catch { /* the socket keeps it current */ }
  }, []);

  // A fast first paint over REST; the socket's "hello" repeats the same
  // data a moment later, which is harmless.
  const loadSessionData = useCallback(async (id: string) => {
    const stale = () => sessionIdRef.current !== id;
    try {
      const conversations = await api<Conversation[]>("GET", sessionPath(id, "/conversations"));
      if (stale()) return;
      dispatch({ type: "conversations", conversations: conversations.slice().sort(byLastMessage) });
    } catch (err) { if (!stale()) toast("Could not load conversations: " + errorText(err)); }
    if (permits(meRef.current, "view.config")) {
      try {
        const config = await api<SessionConfig>("GET", sessionPath(id, "/config"));
        if (!stale()) dispatch({ type: "config", config });
      } catch (err) { if (!stale()) toast("Could not load config: " + errorText(err)); }
    }
    try {
      const media = await api<MediaItem[]>("GET", sessionPath(id, "/media"));
      if (!stale()) dispatch({ type: "media", media });
    } catch { /* the socket's hello brings it */ }
  }, [toast]);

  const selectSession = useCallback(async (id: string) => {
    if (!id || id === sessionIdRef.current) return;
    sessionIdRef.current = id;
    activeChatRef.current = null;
    writePreferred(id);
    draftEdits.current.clear();
    dispatch({ type: "selectSession", sessionId: id });
    await loadSessionData(id);
  }, [loadSessionData]);

  const selectChat = useCallback(async (chatId: number | null) => {
    activeChatRef.current = chatId;
    dispatch({ type: "selectChat", chatId });
    if (chatId === null) return;
    const id = sessionIdRef.current;
    const stale = () => sessionIdRef.current !== id || activeChatRef.current !== chatId;
    try {
      const data = await sApi<{ messages: Message[]; links?: ChatLink[] }>("GET", `/conversations/${chatId}/messages`);
      if (stale()) return;
      dispatch({ type: "messages", chatId, messages: data.messages, links: data.links || [] });
      // A role that may not mark chats read, or only with the admin's
      // approval: opening a chat must not queue a request each time.
      if (permitsNow(meRef.current, "chat.manage")) {
        const conversation = await sApi<Conversation>("POST", `/conversations/${chatId}/read`);
        if (!stale()) dispatch({ type: "conversation", conversation });
      }
    } catch (err) { toast(errorText(err)); }
  }, [sApi, toast]);

  const openAddAccount = useCallback(async () => {
    let auth: AuthStep;
    try { auth = await api<AuthStep>("GET", "/api/auth"); }
    catch (err) { toast(errorText(err)); return null; }
    addAccountResolve.current?.(null);
    setAddAccount(auth);
    return new Promise<string | null>((resolve) => { addAccountResolve.current = resolve; });
  }, [toast]);

  const finishAddAccount = useCallback((sessionId: string | null,
                                         options?: { message?: string; keepTelegram?: boolean }) => {
    setAddAccount(null);
    const resolve = addAccountResolve.current;
    addAccountResolve.current = null;
    if (sessionId) {
      toast(options?.message || "Signed in. The server starts it within about 15 seconds. Replies wait for your " +
            "approval until you turn on auto-send in Settings.", "info");
      void refreshSessions().then(() => selectSession(sessionId));
    } else if (!options?.keepTelegram) {
      // The server holds one Telegram sign-in in progress; closing the dialog drops it.
      api("POST", "/api/auth/cancel").catch(() => {});
    }
    resolve?.(sessionId);
  }, [refreshSessions, selectSession, toast]);

  const logout = useCallback(async () => {
    signedOut.current = true;
    sessionIdRef.current = null;
    meRef.current = null;
    setMe(null);
    dispatch({ type: "selectSession", sessionId: null });
    try { await api("POST", "/api/logout"); } catch (err) { toast(errorText(err)); }
    router.replace("/login");
  }, [router, toast]);

  /* --------------------------------------------------------- the socket */

  const handleEvent = useCallback((data: SocketEvent) => {
    const d = data as Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
    switch (data.type) {
      case "hello":
        dispatch({ type: "hello", conversations: (d.conversations || []).slice().sort(byLastMessage),
                   media: d.media || [], config: d.config ?? null, tenantConfig: d.tenant_config ?? null,
                   status: d.status ?? null });
        break;
      case "message":
        dispatch({ type: "drafting", chatId: d.message.chat_id, on: false });
        dispatch({ type: "conversation", conversation: d.conversation });
        dispatch({ type: "message", message: d.message });
        break;
      case "conversation":
        dispatch({ type: "conversation", conversation: d.conversation });
        break;
      case "media":
        dispatch({ type: "media", media: d.media || [] });
        break;
      case "config":
        dispatch({ type: "config", config: d.config });
        break;
      case "tenant_config":
        dispatch({ type: "tenantConfig", config: d.config });
        break;
      case "status":
        dispatch({ type: "status", status: d.status });
        if (d.status && !d.status.telegram_connected && d.status.telegram_error) toast(d.status.telegram_error);
        break;
      case "controls":
        dispatch({ type: "controls", controls: d as Controls });
        void refreshSafety();
        break;
      case "halted":
        toast(`Stopped after a ${channelName(currentChannel(stateRef.current))} error: ` + d.reason);
        void refreshSafety();
        break;
      case "escalation":
        toast(`${d.name || "A customer"} wrote “${d.keyword}”: the chat is paused and the owner was pinged.`);
        break;
      case "drafting":
        dispatch({ type: "drafting", chatId: d.chat_id, on: true });
        break;
      case "chat_link":
        toast(`${d.link.source_name} looks like the same person as another chat (${d.link.reason}) — ` +
              "their context is now shared.", "info");
        dispatch({ type: "linkAdded", link: d.link });
        break;
      case "chat_unlink":
        dispatch({ type: "linkRemoved", chatId: d.chat_id, sourceId: d.source_id });
        break;
      case "outreach":
        dispatch({ type: "outreach", items: d.items || [] });
        break;
      case "outreach_paused":
        toast(d.reason, "info");
        break;
      case "booking": {
        const b = d.booking;
        const when = new Date(b.starts_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short", timeZone: b.tz });
        toast(`Booking #${b.number} for ${b.customer_name || "a customer"} on ${when}: ` +
              `${BOOKING_STATE[b.state] || b.state}`, "info");
        break;
      }
      case "unanswered":
        void refreshUnanswered();
        break;
      case "error":
        if (d.chat_id !== undefined) dispatch({ type: "drafting", chatId: d.chat_id, on: false });
        toast(d.text);
        if (d.message) dispatch({ type: "message", message: d.message });
        break;
    }
    for (const listener of listeners.current) listener(data);
  }, [refreshSafety, refreshUnanswered, toast]);

  useEffect(() => {
    const id = state.sessionId;
    if (!id) return;
    let socket: WebSocket | null = null;
    let retryDelay = 1000;
    let reconnect: ReturnType<typeof setTimeout> | null = null;
    let closed = false;

    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const ws = new WebSocket(`${proto}://${location.host}/ws/${encodeURIComponent(id)}`);
      socket = ws;
      ws.addEventListener("open", () => {
        if (closed) return;
        retryDelay = 1000;
        dispatch({ type: "socket", socket: "live" });
      });
      ws.addEventListener("close", (ev) => {
        if (closed) return;
        if (ev.code === 4401) { window.dispatchEvent(new Event(UNAUTHORIZED_EVENT)); return; }
        dispatch({ type: "socket", socket: "reconnecting" });
        reconnect = setTimeout(connect, retryDelay);
        retryDelay = Math.min(retryDelay * 2, 15000);
      });
      ws.addEventListener("message", (ev) => {
        if (closed) return;
        let data: SocketEvent;
        try { data = JSON.parse(ev.data); } catch { return; }
        handleEvent(data);
      });
    };
    connect();
    const ping = setInterval(() => { if (socket?.readyState === WebSocket.OPEN) socket.send("ping"); }, 25000);

    return () => {
      // A switch to another account: this socket must never deliver into it.
      closed = true;
      clearInterval(ping);
      if (reconnect) clearTimeout(reconnect);
      socket?.close();
    };
  }, [state.sessionId, handleEvent]);

  /* ------------------------------------------------- boot, polls, auth */

  useEffect(() => {
    const onUnauthorized = () => {
      if (signedOut.current) return;
      signedOut.current = true;
      sessionIdRef.current = null;
      dispatch({ type: "selectSession", sessionId: null });
      router.replace(pathname && pathname !== "/" ? `/login?next=${encodeURIComponent(pathname)}` : "/login");
    };
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, [router, pathname]);

  // Signed in (or already were): who it is, then what their role allows.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      let who: Me;
      try { who = await api<Me>("GET", "/api/me"); }
      catch (err) {
        // A 401 goes to /login by itself (UNAUTHORIZED_EVENT).
        if (!cancelled && !(err && (err as { status?: number }).status === 401)) {
          toast("Could not reach the admin API: " + errorText(err));
        }
        return;
      }
      if (cancelled) return;
      meRef.current = who;
      setMe(who);
      void refreshSafety();
      void refreshUnanswered();
      void refreshVerify();
      void refreshStaff();
      // A role without the conversations view: no accounts, no chats.
      if (!permits(who, "view.conversations")) {
        dispatch({ type: "sessions", sessions: [] });
        return;
      }
      let sessions: Session[];
      try { sessions = await api<Session[]>("GET", "/api/sessions"); }
      catch (err) { if (!cancelled) toast("Could not load the accounts: " + errorText(err)); return; }
      if (cancelled) return;
      dispatch({ type: "sessions", sessions });
      const preferred = readPreferred();
      const wanted = sessions.find((s) => s.session_id === preferred) || sessions[0];
      if (wanted) await selectSession(wanted.session_id);
      else if (permits(who, "accounts.connect")) await openAddAccount();
    })();
    // Running/connected can change from outside this tab; so can alerts.
    const timers = [
      setInterval(refreshSessions, 20000),
      setInterval(refreshSafety, 30000),
      setInterval(refreshUnanswered, 60000),
      setInterval(refreshVerify, 60000),
      setInterval(refreshStaff, 60000),
    ];
    return () => { cancelled = true; timers.forEach(clearInterval); };
    // Once per mount of the panel layout.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const can = useCallback((key: string) => permits(me, key), [me]);

  const value = useMemo<Panel>(() => ({
    state, me, isAdmin: !!me?.admin, can, dispatch, sApi, refreshSessions, selectSession, selectChat, refreshStatus, logout,
    draftEdits, modal, setModal, addAccount, setAddAccount, openAddAccount, finishAddAccount,
    safety, applySafety, refreshSafety, unansweredOpen, setUnansweredOpen, verifyCount, setVerifyCount,
    staffCount, setStaffCount, subscribe,
  }), [state, me, can, sApi, refreshSessions, selectSession, selectChat, refreshStatus, logout, modal, addAccount,
       openAddAccount, finishAddAccount, safety, applySafety, refreshSafety, unansweredOpen, verifyCount, staffCount,
       subscribe]);

  return <PanelContext.Provider value={value}>{children}</PanelContext.Provider>;
}
