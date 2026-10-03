"use client";

// Kill switches, billing, alerts and health (safety_api.py). Platform-admin
// only. The top bar keeps a small summary current (lib/panel.tsx); this page
// refreshes it whenever it loads the full overview.

import { useCallback, useEffect, useState } from "react";
import { useDialogs, useToast } from "@/components/feedback";
import { Fact, PageShell, Tabs } from "@/components/ui";
import { api, errorText } from "@/lib/api";
import { cx, fmtDateTime } from "@/lib/format";
import { usePanel } from "@/lib/panel";
import type { Alert, SafetyOverview, TenantControls } from "@/lib/types";
import { useLoader } from "@/lib/useLoader";

export type SafetyTab = "clients" | "alerts" | "client" | "billing";

const TABS: [SafetyTab, string][] = [["clients", "All clients"], ["alerts", "Alerts"], ["client", "This client"],
                                     ["billing", "Billing notice"]];
const HEALTH: Record<string, string> = {
  ok: "connected", not_running: "not running", disconnected: "not connected", logged_out: "logged out",
  rate_limited: "rate-limited", revoked: "revoked (hard-off)", stopped: "stopped", unknown: "no report yet",
};
const BILLING: Record<string, string> = { active: "active", grace: "grace", suspended: "suspended" };

type Ctx = {
  overview: SafetyOverview;
  reload: () => Promise<void>;
  openClient: (tenantId: number) => void;
  /** POST/PUT, then reload this page and the open account's status. */
  call: (method: "POST" | "PUT", path: string, body?: unknown, done?: string) => Promise<unknown>;
};

/* ------------------------------------------------------------- all clients */

function ClientsTab({ ctx }: { ctx: Ctx }) {
  const { overview: o, call, openClient } = ctx;
  const { confirm, prompt } = useDialogs();
  return (
    <>
      <div className={cx("pf-section", o.global_stop.on && "danger")}>
        <div className="title">Global stop</div>
        {o.global_stop.on ? <>
          <p className="warn-note">On since {fmtDateTime(o.global_stop.at)}: {o.global_stop.reason}. Every client is
            soft-off: messages are received, nothing is sent on its own.</p>
          <button type="button" className="btn primary" onClick={async () => {
            if (!(await confirm("Lift the global stop? Clients with their own holds stay off."))) return;
            await call("POST", "/api/safety/global-stop", { on: false, reason: "" });
          }}>Resume everything</button>
        </> : <>
          <p className="pf-note">Soft-off for every client at once: messages keep arriving and are stored, nothing is
            sent on its own until you lift it. Resuming replays nothing. Also available on the server: python
            controls.py stop &quot;reason&quot;.</p>
          <button type="button" className="btn warn" onClick={async () => {
            const reason = await prompt("Why is everything being stopped? (goes into the audit log)", "");
            if (!reason || !reason.trim()) return;
            await call("POST", "/api/safety/global-stop", { on: true, reason });
          }}>Stop everything…</button>
        </>}
      </div>
      <p className="pf-note">Scheduler: {o.scheduler.at ? `last tick ${fmtDateTime(o.scheduler.at)}` : "never ran"}
        {o.scheduler.stale ? " — NOT RUNNING" : ""}</p>

      <table className="cfg-table sf-table">
        <tbody>
          <tr>{["Client", "Account", "Telegram", "Sending", "Billing", "Alerts"].map((h) => <td key={h} className="muted">{h}</td>)}</tr>
          {o.tenants.map((t) => {
            const health = t.health.status;
            const sending = o.global_stop.on ? "global stop"
              : t.holds.length ? t.holds.map((h) => h.label).join(", ") : "on";
            return (
              <tr key={t.id} className="sf-click" onClick={() => openClient(t.id)}
                  title={t.health.last_error ? "Last error: " + t.health.last_error : ""}>
                <td>{t.name}</td>
                <td className="muted">{t.label || t.session_id || "—"}</td>
                <td className={`sf-h-${health}`}>{HEALTH[health] || health}</td>
                <td className={sending === "on" ? "sf-ok" : "sf-bad"}>{sending}</td>
                <td className={`sf-b-${t.billing.status}`}>
                  {BILLING[t.billing.status]}{t.billing.next_due ? ` · due ${t.billing.next_due}` : ""}
                </td>
                <td className={t.open_alerts ? "sf-bad" : undefined}>{t.open_alerts ? String(t.open_alerts) : ""}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </>
  );
}

/* ------------------------------------------------------------------ alerts */

function AlertRow({ a, ctx }: { a: Alert; ctx: Ctx }) {
  const name = (id: number | null) => {
    const t = ctx.overview.tenants.find((x) => x.id === id);
    return t ? t.name : id ? `Client ${id}` : "Platform";
  };
  return (
    <div className={cx("bk-row sf-alert", `sev-${a.severity}`, a.acknowledged_at && "done")}>
      <div className="bk-head" onClick={() => { if (a.tenant_id) ctx.openClient(a.tenant_id); }}>
        <span className="bk-state">{a.severity}</span>
        <span className="bk-time">{fmtDateTime(a.last_at)}</span>
        <span className="bk-who">{name(a.tenant_id)}</span>
        <span className="muted">{a.kind}</span>
        {a.count > 1 && <span className="muted">×{a.count}</span>}
        <span className="spacer" />
        {a.acknowledged_at ? <span className="muted">closed by {a.acknowledged_by}</span> : (
          <button type="button" className="btn small" onClick={(ev) => {
            ev.stopPropagation();
            void ctx.call("POST", `/api/alerts/${a.id}/ack`);
          }}>Acknowledge</button>
        )}
      </div>
      <div className="bk-detail">{a.message}</div>
    </div>
  );
}

function AlertsTab({ ctx, reloadKey }: { ctx: Ctx; reloadKey: number }) {
  const [openOnly, setOpenOnly] = useState(true);
  const [list, setList] = useState<Alert[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api<Alert[]>("GET", "/api/alerts" + (openOnly ? "?open=true" : ""))
      .then((alerts) => { if (!cancelled) { setList(alerts); setError(null); } })
      .catch((err) => { if (!cancelled) setError(errorText(err)); });
    return () => { cancelled = true; };
  }, [openOnly, reloadKey]);

  return (
    <>
      <div className="bk-nav">
        <button type="button" className="btn small" onClick={() => setOpenOnly((v) => !v)}>
          {openOnly ? "Show acknowledged too" : "Only open ones"}
        </button>
        <button type="button" className="btn small" onClick={() => void ctx.call("POST", "/api/alerts/ack-all", {})}>
          Acknowledge all
        </button>
        <span className="muted">Also delivered by e-mail / webhook when ALERT_EMAIL / ALERT_WEBHOOK_URL are set in .env.</span>
      </div>
      {error && <div className="pf-errors">{error}</div>}
      {list && !list.length && <div className="empty">{openOnly ? "No open alerts." : "No alerts yet."}</div>}
      {(list || []).map((a) => <AlertRow key={a.id} a={a} ctx={ctx} />)}
    </>
  );
}

/* -------------------------------------------------------------- one client */

function ClientTab({ ctx, tenantId, reloadKey }: { ctx: Ctx; tenantId: number | null; reloadKey: number }) {
  const toast = useToast();
  const { confirm, prompt } = useDialogs();
  const [c, setC] = useState<TenantControls | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [due, setDue] = useState("");
  const [billingStatus, setBillingStatus] = useState("active");
  const [proxyUrl, setProxyUrl] = useState("");

  useEffect(() => {
    if (!tenantId) return;
    let cancelled = false;
    api<TenantControls>("GET", `/api/tenants/${tenantId}/controls`)
      .then((controls) => {
        if (cancelled) return;
        setC(controls);
        setError(null);
        setDue(controls.billing.next_due || "");
        setBillingStatus(controls.billing.status);
      })
      .catch((err) => { if (!cancelled) setError(errorText(err)); });
    return () => { cancelled = true; };
  }, [tenantId, reloadKey]);

  if (!tenantId) return <div className="empty">Pick a client under All clients.</div>;
  if (error) return <div className="pf-errors">{error}</div>;
  if (!c) return null;

  const id = tenantId;
  const b = c.billing;
  const h = c.health;

  const setProxy = async (url: string) => {
    const r = await ctx.call("PUT", `/api/tenants/${id}/proxy`, { proxy_url: url }) as { reconnected?: boolean } | null;
    if (r) {
      toast((url ? "Proxy saved. " : "Proxy removed. ") +
        (r.reconnected ? "The account reconnected." : "The account uses it when it next starts."), "info");
      setProxyUrl("");
    }
  };

  return (
    <>
      <h3 className="sf-name">{c.tenant.name} · {c.tenant.label || c.tenant.session_id || "no account"}</h3>

      <div className={cx("pf-section", c.off_reason && "danger")}>
        <div className="title">Sending</div>
        <p className={c.off_reason ? "warn-note" : "pf-note"}>{c.off_reason
          ? `Soft-off: ${c.off_reason}. Messages are received and stored; nothing is sent on its own. Resuming replays nothing.`
          : "On: replies, reminders and owner messages go out as configured."}</p>
        {c.holds.map((hold) => (
          <div key={hold.kind} className="sf-hold">
            <strong>{hold.label}</strong>
            <span>{hold.reason}</span>
            <span className="muted">{fmtDateTime(hold.created_at)} by {hold.created_by}</span>
            <span className="spacer" />
            {hold.kind === "billing" ? <span className="muted">lifted by recording a payment (below)</span> : (
              <button type="button" className="btn small primary" onClick={async () => {
                const reason = await prompt(`Lift "${hold.label}"? Say why (goes into the audit log).`, "");
                if (reason === null) return;
                await ctx.call("POST", `/api/tenants/${id}/resume`, { kind: hold.kind, reason });
              }}>Resume</button>
            )}
          </div>
        ))}
        {!c.holds.some((hold) => hold.kind === "manual") && (
          <button type="button" className="btn small warn" onClick={async () => {
            const reason = await prompt("Why? (e.g. payment, owner on holiday)", "");
            if (reason === null) return;
            await ctx.call("POST", `/api/tenants/${id}/soft-off`, { reason });
          }}>Soft-off (pause)…</button>
        )}
      </div>

      <div className="pf-section">
        <div className="title">Billing</div>
        <p className="pf-note">Status: {BILLING[b.status]}
          {b.status === "grace" ? ` until ${fmtDateTime(b.grace_until)} (owner told: ${b.notice_sent_at ? fmtDateTime(b.notice_sent_at) : "not yet"})` : ""}
          . The day after the due date without a payment it goes into grace (the owner gets a message from this account),
          then it is suspended.</p>
        <div className="pf-actions">
          <span className="muted">Next due</span>
          <input type="date" value={due} onChange={(ev) => setDue(ev.target.value)} style={{ flex: "0 1 180px", minWidth: 0 }} />
          <button type="button" className="btn small"
                  onClick={() => void ctx.call("PUT", `/api/tenants/${id}/billing/due`, { next_due: due || null }, "Saved.")}>
            Save due date</button>
          <button type="button" className="btn small primary" onClick={async () => {
            const next = await prompt("Payment recorded. Next due date (YYYY-MM-DD, empty = stop tracking):", "");
            if (next === null) return;
            if (next && !/^\d{4}-\d{2}-\d{2}$/.test(next.trim())) { toast("Use YYYY-MM-DD"); return; }
            await ctx.call("POST", `/api/tenants/${id}/billing/paid`, { next_due: next.trim() || null });
          }}>Record payment…</button>
          <select value={billingStatus} onChange={(ev) => setBillingStatus(ev.target.value)}>
            {Object.keys(BILLING).map((s) => <option key={s} value={s}>Set {BILLING[s]}</option>)}
          </select>
          <button type="button" className="btn small" onClick={async () => {
            const reason = await prompt(`Set the billing status to "${billingStatus}" by hand. Why?`, "");
            if (!reason || !reason.trim()) return;
            await ctx.call("POST", `/api/tenants/${id}/billing/status`, { status: billingStatus, reason });
          }}>Override</button>
        </div>
      </div>

      <div className="pf-section">
        <div className="title">Telegram</div>
        <Fact label="Status" value={(HEALTH[h.status] || h.status) + (h.status_since ? ` since ${fmtDateTime(h.status_since)}` : "")} />
        <Fact label="Last connected" value={fmtDateTime(h.last_seen_at)} />
        <Fact label="Last error" value={h.last_error ? `${h.last_error} (${fmtDateTime(h.last_error_at)})` : ""} />
        <Fact label="Rate-limited until" value={h.rate_limited_until ? fmtDateTime(h.rate_limited_until) : ""} />
        {h.logins && <>
          <div className="muted sf-sub">Logins on this Telegram account (checked {fmtDateTime(h.logins_checked_at)}).
            A new one switches the client off (anomaly.new_login_suspend).</div>
          {h.logins.map((l, i) => (
            <div key={i} className="bk-fact">
              {l.current ? "▶ this server · " : ""}{l.device || "?"} · {l.platform || ""} · {l.app || ""} · {l.country || ""}
              {l.created ? " · since " + fmtDateTime(l.created) : ""}
            </div>
          ))}
        </>}
      </div>

      <div className="pf-section">
        <div className="title">Telegram proxy</div>
        <p className="pf-note">{c.proxy
          ? `Connects through ${c.proxy.type}://${c.proxy.host}:${c.proxy.port}` +
            (c.proxy.username ? ` as ${c.proxy.username}` : "") + ". The password is stored encrypted and never shown."
          : "Connects directly from this server. A residential or mobile proxy in the account's country makes it " +
            "connect from there instead."}</p>
        {c.tenant.session_id && (
          <div className="pf-actions">
            <input type="password" autoComplete="off" placeholder="socks5://user:password@host:port"
                   value={proxyUrl} onChange={(ev) => setProxyUrl(ev.target.value)} />
            <button type="button" className="btn small primary" onClick={() => void setProxy(proxyUrl.trim())}>
              {c.proxy ? "Replace" : "Use this proxy"}</button>
            {c.proxy && (
              <button type="button" className="btn small warn" onClick={async () => {
                if (await confirm("Stop using the proxy and connect from this server's own address?")) await setProxy("");
              }}>Connect directly</button>
            )}
          </div>
        )}
      </div>

      <div className="pf-section danger">
        <div className="title">Hard-off</div>
        <p className="pf-note">For a hijacked or leaked session: logs this server&apos;s Telegram session out, deletes its
          key and deactivates the account. Signing it in again is a new login from the panel. It does not touch the
          owner&apos;s phone or other logins.</p>
        {c.tenant.session_id && c.tenant.state !== "revoked" ? (
          <button type="button" className="btn warn" onClick={async () => {
            const reason = await prompt("Why is the session being revoked? (audit log)", "");
            if (!reason || !reason.trim()) return;
            const confirmId = await prompt(`Type the account id (${c.tenant.session_id}) to confirm.`, "");
            if (confirmId === null) return;
            const r = await ctx.call("POST", `/api/tenants/${id}/hard-off`, { reason, confirm: confirmId }) as
              { logged_out?: boolean } | null;
            if (r) {
              toast(r.logged_out ? "Session logged out and deleted."
                : "Key deleted, but Telegram could not be told: end the session under Settings → Devices on the phone.",
                r.logged_out ? "info" : "error");
            }
          }}>Revoke the session…</button>
        ) : <p className="muted">{c.tenant.state === "revoked" ? "Already revoked." : "No account."}</p>}
      </div>

      {c.alerts.length > 0 && <>
        <h4 className="bk-day">Alerts for this client</h4>
        {c.alerts.map((a) => <AlertRow key={a.id} a={a} ctx={ctx} />)}
      </>}
    </>
  );
}

/* ---------------------------------------------------------- billing notice */

function BillingTab({ ctx }: { ctx: Ctx }) {
  const [settings, setSettings] = useState<{ grace_hours: number | string; notice: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<{ grace_hours: number; notice: string }>("GET", "/api/platform/billing")
      .then(setSettings).catch((err) => setError(errorText(err)));
  }, []);

  if (error) return <div className="pf-errors">{error}</div>;
  if (!settings) return null;
  return (
    <>
      <p className="pf-note">Sent to the owner (booking.provider) from the client&apos;s own account when a payment is
        missed, once, at the start of grace. You can use {"{business}"}, {"{due}"} and {"{until}"}.</p>
      <div className="field">
        <label htmlFor="sf-grace">Grace, in hours</label>
        <input id="sf-grace" type="number" min={1} value={settings.grace_hours}
               onChange={(ev) => setSettings({ ...settings, grace_hours: ev.target.value })} />
      </div>
      <div className="field">
        <label htmlFor="sf-notice">Message to the owner</label>
        <textarea id="sf-notice" rows={4} value={settings.notice}
                  onChange={(ev) => setSettings({ ...settings, notice: ev.target.value })} />
      </div>
      <button type="button" className="btn primary" onClick={() => void ctx.call("PUT", "/api/platform/billing",
        { grace_hours: Number(settings.grace_hours), notice: settings.notice }, "Saved.")}>Save</button>
    </>
  );
}

/* -------------------------------------------------------------------- page */

export function Safety({ initialTab, initialTenant }: { initialTab?: SafetyTab; initialTenant?: number | null }) {
  const { state, applySafety, refreshStatus } = usePanel();
  const toast = useToast();
  const [tab, setTab] = useState<SafetyTab>(initialTab || "clients");
  const [tenantId, setTenantId] = useState<number | null>(initialTenant ?? null);
  const [reloadKey, setReloadKey] = useState(0);

  const fetchOverview = useCallback(async () => {
    const o = await api<SafetyOverview>("GET", "/api/safety");
    applySafety(o);
    return o;
  }, [applySafety]);
  const { data: overview, error, reload: reloadOverview } = useLoader(fetchOverview);

  // The overview, and whatever the open tab shows (it watches reloadKey).
  const reload = useCallback(async () => {
    await reloadOverview();
    setReloadKey((n) => n + 1);
  }, [reloadOverview]);

  const showTab = (key: SafetyTab) => { setTab(key); void reload(); };

  const call = useCallback(async (method: "POST" | "PUT", path: string, body?: unknown, done?: string) => {
    let result: unknown = null;
    try {
      result = await api(method, path, body);
      if (done) toast(done, "info");
    } catch (err) { toast(errorText(err)); }
    await reload();
    await refreshStatus();
    return result;
  }, [reload, refreshStatus, toast]);

  const ctx: Ctx | null = overview && {
    overview, reload, call,
    openClient: (id) => { setTenantId(id); showTab("client"); },
  };

  return (
    <PageShell title="Safety" crumb="kill switches, billing, alerts, health" width="w-980">
      <div className="bk-scroll">
        <Tabs tabs={TABS} value={tab} onChange={showTab} />
        {error && <div className="pf-errors">{error}</div>}
        {ctx && tab === "clients" && <ClientsTab ctx={ctx} />}
        {ctx && tab === "alerts" && <AlertsTab ctx={ctx} reloadKey={reloadKey} />}
        {ctx && tab === "client" &&
          <ClientTab ctx={ctx} tenantId={tenantId ?? state.status?.tenant_id ?? null} reloadKey={reloadKey} />}
        {ctx && tab === "billing" && <BillingTab ctx={ctx} />}
      </div>
    </PageShell>
  );
}
