"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { useDialogs, useToast } from "@/components/feedback";
import { errorText } from "@/lib/api";
import { cx, fmtDateTime } from "@/lib/format";
import { channelName, currentChannel, usePanel } from "@/lib/panel";
import type { Controls, Session } from "@/lib/types";

function sessionLabel(s: Session): string {
  return s.label && s.label.trim() ? s.label : s.session_id;
}

// grey = not running anywhere in the fleet, red = running but its network
// (Telegram or WhatsApp; status.telegram_connected covers both) is not
// connected, green = running and connected.
function sessionDotClass(s: Session): string {
  if (!s.running_here) return "dot grey";
  if (s.status && s.status.telegram_connected) return "dot on";
  return "dot";
}

function SessionSwitcher() {
  const { state, selectSession, openAddAccount, can } = usePanel();
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const close = (ev: MouseEvent) => { if (!box.current?.contains(ev.target as Node)) setOpen(false); };
    document.addEventListener("click", close);
    return () => document.removeEventListener("click", close);
  }, [open]);

  const current = state.sessions.find((s) => s.session_id === state.sessionId);
  const label = current ? sessionLabel(current)
    : !state.sessionsLoaded ? "Loading sessions…" : state.sessions.length ? "Select session…" : "No sessions";

  return (
    <div id="session-switcher" ref={box}>
      <button id="session-btn" className="btn small" type="button" aria-expanded={open}
              onClick={() => setOpen((o) => !o)}>
        <span className={current ? sessionDotClass(current) : "dot grey"} />
        <span id="session-btn-label">{label}</span>
      </button>
      {open && (
        <div id="session-menu">
          {!state.sessions.length && <div className="s-empty">No accounts yet.</div>}
          {state.sessions.map((s) => (
            <button key={s.session_id} type="button"
                    className={cx("s-row", s.session_id === state.sessionId && "active")}
                    onClick={() => { setOpen(false); void selectSession(s.session_id); }}>
              <span className={sessionDotClass(s)} />
              <span className="s-name" title={`${channelName(s.channel)} · ${s.session_id}`}>{sessionLabel(s)}</span>
              <span className={cx("badge channel", s.channel === "whatsapp" ? "wa" : "tg")}>
                {s.channel === "whatsapp" ? "WA" : "TG"}
              </span>
            </button>
          ))}
          {can("accounts.connect") && (
            <button type="button" className="s-row s-add" onClick={() => { setOpen(false); void openAddAccount(); }}>
              <span className="s-name">+ Add account</span>
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function ConnectionStatus() {
  const { state } = usePanel();
  const { socket, status, sessionId } = state;
  // telegram_connected: the account's own network, WhatsApp included (the
  // field name is kept for compatibility).
  const network = channelName(currentChannel(state));
  let on = false;
  let text = "connecting…";
  if (!sessionId) text = state.sessionsLoaded ? "no account open" : "connecting…";
  else if (socket === "reconnecting") text = "reconnecting…";
  else if (socket === "live") {
    if (!status || status.telegram_connected === undefined) { on = true; text = "live"; }
    else if (status.telegram_connected) {
      on = true;
      text = status.me?.name ? `${network} connected as ${status.me.name}` : `${network} connected`;
    } else text = `${network} offline`;
  }
  return (
    <span className="status">
      <span className={cx("dot", on && "on")} />
      <span id="conn-text">{text}</span>
    </span>
  );
}

function modeText(panel: ReturnType<typeof usePanel>): string {
  const { status, tenantConfig } = panel.state;
  if (status?.persona_configured === false) return "no business details in the prompt yet — open Settings";
  if (!tenantConfig) return "";
  const bits = [tenantConfig.auto_send ? "auto-send ON" : "approval required"];
  const quiet = tenantConfig.quiet_hours;
  if (quiet && quiet.enabled) bits.push(`quiet ${quiet.start}–${quiet.end} ${tenantConfig.timezone}`);
  return bits.join(" · ");
}

export function Header() {
  const panel = usePanel();
  const { state, sApi, dispatch, setModal, safety, unansweredOpen, verifyCount, staffCount, logout, me, can, isAdmin } =
    panel;
  const toast = useToast();
  const { confirm } = useDialogs();
  const pathname = usePathname();
  const router = useRouter();
  const [menuOpen, setMenuOpen] = useState(false);

  const status = state.status;
  const paused = !!status?.global_pause;
  const offReason = status?.off_reason || "";
  const tenantId = status?.tenant_id;
  const hasSession = !!state.sessionId;
  const alertTotal = safety?.alerts?.total || 0;
  // What the role can't use is hidden (the server refuses it anyway); the
  // account-level buttons also need the conversations view.
  const accounts = can("view.conversations");
  const whatsapp = currentChannel(state) === "whatsapp";
  const staffMember = me && !me.admin ? me : null;

  const togglePause = async () => {
    try {
      dispatch({ type: "controls", controls: await sApi<Controls>("POST", "/global-pause", { global_pause: !paused }) });
    } catch (err) { toast(errorText(err)); }
  };

  const nav = (href: string, label: React.ReactNode, title?: string) => (
    <Link href={href} title={title} className={cx("btn", pathname === href.split("?")[0] && "current")}>{label}</Link>
  );

  const banner: string[] = [];
  if (can("view.safety") && safety?.global_stop?.on) {
    banner.push(`GLOBAL STOP: every client is soft-off (${safety.global_stop.reason}). Safety → All clients to resume.`);
  }
  if (can("view.safety") && safety?.scheduler?.stale) {
    banner.push("The scheduler is not running" +
      (safety.scheduler.at ? ` (last seen ${fmtDateTime(safety.scheduler.at)})` : "") +
      ": no reminders, no health alerts, no billing changes until it is.");
  }

  return (
    <>
      {/* Several instances can be open in separate tabs; name this one. */}
      <title>{status?.instance ? `${status.instance} — Telegram AI Assistant` : "Telegram AI Assistant"}</title>
      <header className={cx(menuOpen && "menu-open")}>
        <span className="brand">
          <Link href="/" style={{ color: "inherit", textDecoration: "none" }}>Telegram AI Assistant</Link>
          <span className="instance">{status?.instance || ""}</span>
        </span>
        {accounts && <SessionSwitcher />}
        {accounts && <ConnectionStatus />}
        <span className="spacer" />
        <span id="mode" className="status">{modeText(panel)}</span>
        {staffMember && (
          <span className="me-chip" title={`Signed in as ${staffMember.display_name || staffMember.username} ` +
            `(role: ${staffMember.role || "none"}). Some changes you make may wait for the admin's approval.`}>
            {staffMember.username} · {staffMember.role || "no role"}
          </span>
        )}
        {offReason && (
          <button type="button" className="off-chip" title={`${offReason} — see Safety`} onClick={() => {
            if (can("view.safety")) router.push(tenantId ? `/safety?tab=client&tenant=${tenantId}` : "/safety?tab=client");
          }}>
            Sending off: {offReason}
          </button>
        )}
        <button id="menu-toggle" className="btn" type="button" aria-expanded={menuOpen} aria-controls="hdr-actions"
                onClick={() => setMenuOpen((o) => !o)}>☰ Menu</button>
        <nav id="hdr-actions" onClick={(ev) => {
          if ((ev.target as HTMLElement).closest("button, a")) setMenuOpen(false);
        }}>
          {accounts && can("tenant.pause") && (
            <button type="button" className={cx("btn", paused && "on")} disabled={!hasSession} onClick={togglePause}
                    title="Soft-off for this client: messages are received, nothing is sent on its own">
              {paused ? "Paused — resume" : "Pause all"}
            </button>
          )}
          {/* Writing first to contacts is Telegram-only: on WhatsApp it is what gets numbers banned. */}
          {accounts && !whatsapp && (
            <button type="button" className="btn" disabled={!hasSession} onClick={() => setModal("outreach")}>Outreach</button>
          )}
          {accounts && can("view.bookings") &&
            nav("/bookings", "Bookings", "This client's bookings, opening hours, waitlist and calendar link")}
          {accounts && <button type="button" className="btn" disabled={!hasSession} onClick={() => setModal("media")}>Media</button>}
          {accounts && <button type="button" className="btn" disabled={!hasSession} onClick={() => setModal("style")}>Style</button>}
          {accounts && can("view.config") && (
            <Link href={tenantId ? `/clients?node=tenant-${tenantId}` : "/clients"} className="btn"
                  title="How this client's bot behaves: config, prompt, versions">Settings</Link>
          )}
          {can("view.safety") && nav("/safety", <>Safety <span className={cx("count-badge", !!safety?.alerts?.critical && "critical")}>
            {alertTotal ? String(alertTotal) : ""}</span></>, "Kill switches, billing, alerts and account health")}
          {can("view.unanswered") && nav("/unanswered",
            <>Unanswered <span className="count-badge">{unansweredOpen ? String(unansweredOpen) : ""}</span></>,
            "Customer messages the bot did not answer, across all clients")}
          {can("view.config") && nav("/clients", "Clients", "All clients, industries and the platform rules")}
          {can("view.config") && nav("/onboarding", "New client", "Set up a new client step by step, or continue one")}
          {can("view.training") && nav("/review", "Review", "Review the bot's replies and export them for training")}
          {isAdmin && nav("/finetune", "Finetune",
            "Build a client's prompt and the industry standard from screenshots of its chats")}
          {can("view.clients") && nav("/owners", "Client logins", "Logins for business owners to their own dashboard (/owner/)")}
          {can("view.verification") && nav("/verification",
            <>Verification <span className="count-badge">{verifyCount ? String(verifyCount) : ""}</span></>,
            "Identity videos and client photos that wait for your decision")}
          {can("view.terms") && nav("/terms-and-signup", "Terms",
            "Terms of service clients accept, and whether clients can sign up themselves")}
          {isAdmin && nav("/managers", "Managers", "Moderator logins (/manager/) who can step in when you can't")}
          {isAdmin && nav("/staff", <>Staff <span className="count-badge">{staffCount ? String(staffCount) : ""}</span></>,
            "Staff roles, what each may do, and moderator changes waiting for your approval")}
          <button type="button" className="btn" onClick={async () => {
            if (await confirm("Sign out of the admin panel?")) await logout();
          }}>Log out</button>
        </nav>
      </header>
      {banner.length > 0 && (
        <button type="button" className="safety-banner" onClick={() => router.push("/safety")}>
          {banner.join("  ·  ")}
        </button>
      )}
    </>
  );
}
