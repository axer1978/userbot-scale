"use client";

// Logins for business owners to their own dashboard (/owner/), managed by
// the platform admin (owner_admin_api.py). One login can see several
// businesses. The password set here is temporary: the owner chooses their
// own at the first sign-in. The server never returns a stored password or
// authenticator secret, so nothing here can show one.
//
// Logins people made themselves (sign-up at /owner/) wait as "pending" in the
// Waiting tab until they are approved or rejected here (or by a manager).

import { useCallback, useEffect, useRef, useState } from "react";
import { useDialogs, useToast } from "@/components/feedback";
import { PageShell, Tabs } from "@/components/ui";
import { api, errorText } from "@/lib/api";
import { cx, fmtDateTime, tempPassword } from "@/lib/format";
import type { Owner, PlatformTree, TermsAcceptance } from "@/lib/types";
import { useLoader } from "@/lib/useLoader";
import { useOrigin } from "@/lib/useOrigin";

type Tab = "waiting" | "logins" | "new";
type TenantRef = { id: number; name: string };
// The address is only known in the browser; the server renders the path.
function useDashboardUrl(): string {
  return useOrigin() + "/owner/";
}

function TenantPicker({ tenants, selected, onChange }: {
  tenants: TenantRef[]; selected: number[]; onChange: (ids: number[]) => void;
}) {
  return (
    <div className="picker ow-picker">
      {!tenants.length && <div className="row-item muted">No clients yet.</div>}
      {tenants.map((t) => (
        <label key={t.id} className="row-item">
          <input type="checkbox" checked={selected.includes(t.id)} onChange={(ev) =>
            onChange(ev.target.checked ? [...selected, t.id] : selected.filter((id) => id !== t.id))} />
          <span>{t.name}</span>
          <span className="handle">#{t.id}</span>
        </label>
      ))}
    </div>
  );
}

type Call = (method: string, path: string, body?: unknown, done?: string | null) => Promise<unknown>;

// "Terms accepted: vN" and a button listing every acceptance with its address.
function TermsLine({ o }: { o: Owner }) {
  const [open, setOpen] = useState(false);
  const [rows, setRows] = useState<TermsAcceptance[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  return (
    <>
      <div className="pf-actions ow-terms-line">
        <span className="ow-terms-state">{o.terms_accepted ? `Terms accepted: v${o.terms_accepted}` : "Terms: not accepted"}</span>
        <button type="button" className="btn small" onClick={async () => {
          if (open) { setOpen(false); return; }
          setOpen(true);
          setRows(null);
          setError(null);
          try { setRows(await api<TermsAcceptance[]>("GET", `/api/owners/${o.id}/terms`)); }
          catch (err) { setError(errorText(err)); }
        }}>Terms history</button>
      </div>
      {open && (
        <div className="ow-terms">
          {error ? <div className="pf-errors">{error}</div>
            : !rows ? "Loading…"
            : !rows.length ? <div className="muted">Has not accepted any version.</div>
            : rows.map((r, i) => (
              <div key={i} className="ow-terms-row">
                v{r.version} · accepted {fmtDateTime(r.accepted_at, "never")} · from {r.ip || "unknown address"}
              </div>
            ))}
        </div>
      )}
    </>
  );
}

// Reason (required, the client sees it) for a new identity video.
function VerifyAgain({ o, call, onClose }: { o: Owner; call: Call; onClose: () => void }) {
  const toast = useToast();
  const { confirm } = useDialogs();
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => { input.current?.focus(); }, []);
  const submit = async () => {
    const text = reason.trim();
    if (!text) { toast("Write a reason first: the client sees it."); input.current?.focus(); return; }
    if (!(await confirm(`Ask ${o.username} to send a new verification video? Every business of this login pauses ` +
      "until you approve it (Verification → Videos)."))) return;
    setBusy(true);
    const done = await call("POST", `/api/owners/${o.id}/request-verification`, { reason: text },
      `${o.username} has to verify again; their businesses are paused.`);
    if (!done) setBusy(false);
  };
  return (
    <div className="pf-actions ow-verify">
      <input ref={input} type="text" maxLength={500} placeholder="Why they must verify again (the client sees it)"
             value={reason} onChange={(ev) => setReason(ev.target.value)}
             onKeyDown={(ev) => { if (ev.key === "Enter") void submit(); }} />
      <button type="button" className="btn small warn" disabled={busy} onClick={submit}>Ask to verify</button>
      <button type="button" className="btn small" onClick={onClose}>Cancel</button>
    </div>
  );
}

function OwnerCard({ o, tenants, call, showSecret, toWaiting }: {
  o: Owner; tenants: TenantRef[]; call: Call; showSecret: (label: string, password: string) => void;
  toWaiting: () => void;
}) {
  const { confirm, prompt } = useDialogs();
  const [name, setName] = useState(o.display_name);
  const [linked, setLinked] = useState(o.tenant_ids);
  const [verifying, setVerifying] = useState(false);
  const contact = [o.company, o.email, o.phone].filter(Boolean);

  return (
    <div className={cx("pf-section ow-card", o.disabled && "ow-disabled")}>
      <div className="title">
        <span>{o.username}</span>
        <span className="ow-badges">
          {o.status === "pending" && <span className="badge paused">pending</span>}
          {o.status === "rejected" && <span className="badge escalated">rejected</span>}
          {o.disabled && <span className="badge paused">disabled</span>}
          {o.must_change_password && <span className="badge">temporary password</span>}
          {o.totp && <span className="badge link">2FA on</span>}
        </span>
      </div>
      <div className="pf-note ow-meta">
        {o.display_name || "No name"} · last sign-in {fmtDateTime(o.last_login_at, "never")} ·{" "}
        {o.sessions} active session{o.sessions === 1 ? "" : "s"} · created {fmtDateTime(o.created_at, "never")}
        {o.created_by ? ` by ${o.created_by}` : ""}
      </div>
      {contact.length > 0 && <div className="pf-note ow-meta">{contact.join(" · ")}</div>}
      {o.status === "active" && o.reviewed_by && (
        <div className="pf-note ow-meta">
          Sign-up approved by {o.reviewed_by} {fmtDateTime(o.reviewed_at, "never")}{o.review_reason ? `: ${o.review_reason}` : ""}
        </div>
      )}
      {o.status === "rejected" && (
        <div className="pf-note ow-reason">
          Rejected by {o.reviewed_by || "?"} {fmtDateTime(o.reviewed_at, "never")}: {o.review_reason || "no reason given"}
        </div>
      )}
      {o.status === "pending" && (
        <div className="pf-note ow-meta">Signed up and waiting for approval; sees no business yet.</div>
      )}
      <TermsLine o={o} />

      <div className="pf-actions">
        <input type="text" value={name} placeholder="Name shown on their dashboard" maxLength={200}
               onChange={(ev) => setName(ev.target.value)} />
        <button type="button" className="btn small"
                onClick={() => call("PATCH", `/api/owners/${o.id}`, { display_name: name }, "Name saved.")}>Save name</button>
      </div>

      <div className="sf-sub muted">Businesses this login can see</div>
      <TenantPicker tenants={tenants} selected={linked} onChange={setLinked} />

      <div className="pf-actions">
        {o.status === "pending" && (
          <button type="button" className="btn small primary" onClick={toWaiting}>Approve or reject…</button>
        )}
        {o.status === "rejected" && (
          <button type="button" className="btn small"
                  title="Make this login active after all; the businesses ticked above stay as saved"
                  onClick={async () => {
                    const reason = await prompt(`Approve ${o.username} after all? A note for the audit log (optional):`, "");
                    if (reason === null) return;
                    await call("POST", `/api/owners/${o.id}/approve`, { reason }, `${o.username} approved.`);
                  }}>Approve anyway</button>
        )}
        <button type="button" className="btn small"
                onClick={() => call("PATCH", `/api/owners/${o.id}`, { tenant_ids: linked }, "Businesses saved.")}>
          Save businesses</button>
        <button type="button" className={cx("btn small", !o.disabled && "warn")}
                title={o.disabled ? "Allow this login again" : "Block this login and end its sessions now"}
                onClick={async () => {
                  if (!o.disabled && !(await confirm(`Disable ${o.username}? Their open sessions end at once.`))) return;
                  await call("PATCH", `/api/owners/${o.id}`, { disabled: !o.disabled }, o.disabled ? "Enabled." : "Disabled.");
                }}>{o.disabled ? "Enable" : "Disable"}</button>
        <button type="button" className="btn small" onClick={async () => {
          const password = await prompt(`New temporary password for ${o.username} (at least 10 characters). ` +
            "Their sessions end and they choose their own at the next sign-in.", tempPassword());
          if (password === null) return;
          const done = await call("POST", `/api/owners/${o.id}/reset-password`, { password }, null);
          if (done) showSecret(`Temporary password for ${o.username}`, password);
        }}>Reset password…</button>
        {o.totp && (
          <button type="button" className="btn small"
                  title="For a lost phone: they sign in with the password alone and can set up a new app"
                  onClick={async () => {
                    if (!(await confirm(`Remove the authenticator of ${o.username}? Only do this when you are sure it is them asking.`))) return;
                    await call("DELETE", `/api/owners/${o.id}/totp`, undefined, "Authenticator removed.");
                  }}>Remove 2FA</button>
        )}
        <button type="button" className="btn small"
                title="They must send a new identity video; every business of this login pauses until you approve it"
                onClick={() => setVerifying((v) => !v)}>Ask to verify again…</button>
        <button type="button" className="btn small warn" onClick={async () => {
          if (!(await confirm(`Delete the login ${o.username}? The businesses and their data stay; only the login goes.`))) return;
          await call("DELETE", `/api/owners/${o.id}`, undefined, "Login deleted.");
        }}>Delete</button>
      </div>
      {verifying && <VerifyAgain o={o} call={call} onClose={() => setVerifying(false)} />}
    </div>
  );
}

/* ----------------------------------------------------------------- waiting */

function WaitingCard({ o, tenants, call }: { o: Owner; tenants: TenantRef[]; call: Call }) {
  const toast = useToast();
  const { confirm } = useDialogs();
  const [linked, setLinked] = useState(o.tenant_ids);
  const [reason, setReason] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const facts: [string, string | null | undefined][] = [
    ["Name", o.display_name], ["Company", o.company], ["E-mail", o.email], ["Phone", o.phone],
    ["Signed up", fmtDateTime(o.created_at, "never")],
    ["Terms", o.terms_accepted ? `accepted v${o.terms_accepted}` : "not accepted"],
  ];
  return (
    <div className="pf-section ow-card">
      <div className="title">
        <span>{o.username}</span>
        <span className="ow-badges"><span className="badge paused">pending</span></span>
      </div>
      <div className="ow-facts">
        {facts.map(([label, value]) => (
          <div key={label}><span className="muted">{label}</span><span>{value || "—"}</span></div>
        ))}
      </div>
      <div className="sf-sub muted">Link to businesses (optional)</div>
      <TenantPicker tenants={tenants} selected={linked} onChange={setLinked} />
      <div className="pf-actions">
        <input ref={input} type="text" maxLength={500} value={reason} onChange={(ev) => setReason(ev.target.value)}
               placeholder="Reason: optional to approve, required to reject (the applicant sees it)" />
      </div>
      <div className="pf-actions">
        <button type="button" className="btn small primary" onClick={() => {
          const body: { reason: string; tenant_ids?: number[] } = { reason: reason.trim() };
          if (linked.length) body.tenant_ids = linked;
          void call("POST", `/api/owners/${o.id}/approve`, body,
            `${o.username} approved` + (linked.length ? "." : "; no business linked yet."));
        }}>Approve</button>
        <button type="button" className="btn small warn" onClick={async () => {
          const text = reason.trim();
          if (!text) { toast("Write a reason first: the applicant sees it."); input.current?.focus(); return; }
          if (!(await confirm(`Reject ${o.username}? They will see this reason:\n\n${text}`))) return;
          await call("POST", `/api/owners/${o.id}/reject`, { reason: text }, `${o.username} rejected.`);
        }}>Reject</button>
      </div>
    </div>
  );
}

function NewLogin({ tenants, onCreated }: { tenants: TenantRef[]; onCreated: (owner: Owner, password: string) => Promise<void> }) {
  const toast = useToast();
  const [username, setUsername] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState(tempPassword);
  const [linked, setLinked] = useState<number[]>([]);
  const [busy, setBusy] = useState(false);
  const first = useRef<HTMLInputElement>(null);
  useEffect(() => { first.current?.focus(); }, []);

  return (
    <div className="pf-section">
      <div className="field">
        <label htmlFor="ow-username">Username (3–64 letters, digits or . _ @ -)</label>
        <input id="ow-username" ref={first} type="text" placeholder="e.g. anna@salon.lv or anna.salon" autoComplete="off"
               maxLength={64} value={username} onChange={(ev) => setUsername(ev.target.value)} />
      </div>
      <div className="field">
        <label htmlFor="ow-name">Name</label>
        <input id="ow-name" type="text" placeholder="e.g. Anna Kalniņa" maxLength={200} value={name}
               onChange={(ev) => setName(ev.target.value)} />
      </div>
      <div className="field">
        <label htmlFor="ow-password">Temporary password (at least 10 characters; they change it at the first sign-in)</label>
        <div className="pf-actions ow-pw-row">
          <input id="ow-password" type="text" autoComplete="off" value={password} onChange={(ev) => setPassword(ev.target.value)} />
          <button type="button" className="btn small" onClick={() => setPassword(tempPassword())}>New suggestion</button>
        </div>
      </div>
      <div className="sf-sub muted">Businesses this login can see</div>
      <TenantPicker tenants={tenants} selected={linked} onChange={setLinked} />
      <div className="pf-actions">
        <button type="button" className="btn primary" disabled={busy} onClick={async () => {
          setBusy(true);
          try {
            const owner = await api<Owner>("POST", "/api/owners", {
              username: username.trim(), display_name: name.trim(), password, tenant_ids: linked,
            });
            toast(`Login ${owner.username} created.`, "info");
            await onCreated(owner, password);
          } catch (err) { toast(errorText(err)); }
          finally { setBusy(false); }
        }}>Create login</button>
      </div>
    </div>
  );
}

export function Owners({ initialTab }: { initialTab?: Tab }) {
  const toast = useToast();
  // Opened without a tab: Waiting when someone waits, else Logins.
  const [chosen, setChosen] = useState<Tab | null>(initialTab ?? null);
  // A password is shown once, right after it was set, to pass on to the owner.
  const [secret, setSecret] = useState<{ label: string; password: string } | null>(null);
  const dashboard = useDashboardUrl();

  const fetchAll = useCallback(async () => {
    const [owners, tree] = await Promise.all([api<Owner[]>("GET", "/api/owners"), api<PlatformTree>("GET", "/api/platform/tree")]);
    const tenants: TenantRef[] = tree.tenants.map((t) => ({ id: t.id, name: t.name }))
      .sort((a, b) => a.name.localeCompare(b.name) || a.id - b.id);
    return { owners, tenants };
  }, []);
  const { data, error, reload: load } = useLoader(fetchAll);
  const owners = data?.owners ?? null;
  const tenants = data?.tenants ?? [];
  const pending = (owners || []).filter((o) => o.status === "pending");
  const tab: Tab = chosen ?? (pending.length ? "waiting" : "logins");
  const setTab = (key: Tab) => setChosen(key);
  const tabs: [Tab, React.ReactNode][] = [
    ["waiting", <>Waiting <span className="count-badge">{pending.length ? String(pending.length) : ""}</span></>],
    ["logins", "Logins"], ["new", "New login"],
  ];

  const call: Call = async (method, path, body, done) => {
    try {
      const result = await api(method, path, body);
      if (done) toast(done, "info");
      await load();
      return result ?? true;
    } catch (err) {
      toast(errorText(err));
      return null;
    }
  };

  return (
    <PageShell title="Client logins" crumb="dashboard access for business owners" width="w-860">
      <div className="bk-scroll">
        <Tabs tabs={tabs} value={tab} onChange={(key) => { setTab(key); setSecret(null); }} />
        {error && <div className="pf-errors">{error}</div>}
        {secret && (
          <div className="pf-section ow-secret">
            <div className="title">{secret.label}</div>
            <p className="pf-note">Shown only now. Send it to them together with {dashboard}; they choose their own
              password when they first sign in.</p>
            <code className="ow-password">{secret.password}</code>
          </div>
        )}
        {owners && tab === "logins" && <>
          <p className="pf-note">Each login opens the dashboard at {dashboard} for the businesses ticked below:
            bookings, weekly numbers and unanswered messages, read-only apart from marking a message reviewed. It
            cannot change any bot setting or pause anything.</p>
          {!owners.length ? <>
            <div className="pf-note">No client logins yet.</div>
            <button type="button" className="btn primary" onClick={() => setTab("new")}>Create the first one</button>
          </> : owners.map((o) => (
            <OwnerCard key={`${o.id}-${o.tenant_ids.join(",")}-${o.display_name}`} o={o} tenants={tenants} call={call}
                       showSecret={(label, password) => setSecret({ label, password })} toWaiting={() => setTab("waiting")} />
          ))}
        </>}
        {owners && tab === "waiting" && <>
          <p className="pf-note">People who asked for a login themselves at {dashboard}. Until approved they see only a
            waiting page. Approving can link them to businesses at once (leave all unticked to link later under Logins).
            Rejecting needs a reason: they see it when they sign in, and get it by e-mail when e-mail is set up. Managers
            can approve and reject too, but not link businesses.</p>
          {!pending.length ? <div className="pf-note">Nobody is waiting for approval.</div>
            : pending.map((o) => <WaitingCard key={`${o.id}-${o.tenant_ids.join(",")}`} o={o} tenants={tenants} call={call} />)}
        </>}
        {owners && tab === "new" && (
          <NewLogin tenants={tenants} onCreated={async (owner, password) => {
            setTab("logins");
            await load();
            setSecret({ label: `Temporary password for ${owner.username}`, password });
          }} />
        )}
      </div>
    </PageShell>
  );
}
