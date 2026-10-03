"use client";

// Logins for business owners to their own dashboard (/owner/), managed by
// the platform admin (owner_admin_api.py). One login can see several
// businesses. The password set here is temporary: the owner chooses their
// own at the first sign-in. The server never returns a stored password or
// authenticator secret, so nothing here can show one.

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useDialogs, useToast } from "@/components/feedback";
import { PageShell, Tabs } from "@/components/ui";
import { api, errorText } from "@/lib/api";
import { cx, fmtDateTime } from "@/lib/format";
import type { Owner, PlatformTree } from "@/lib/types";
import { useLoader } from "@/lib/useLoader";

type Tab = "logins" | "new";
type TenantRef = { id: number; name: string };

const TABS: [Tab, string][] = [["logins", "Logins"], ["new", "New login"]];
// No 0/O/1/l/I: temporary passwords get read out or typed from a message.
const ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";

function tempPassword(length = 14): string {
  const bytes = new Uint32Array(length);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => ALPHABET[b % ALPHABET.length]).join("");
}

// The address is only known in the browser; the server renders the path.
const noSubscription = () => () => {};
function useDashboardUrl(): string {
  return useSyncExternalStore(noSubscription, () => window.location.origin + "/owner/", () => "/owner/");
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

function OwnerCard({ o, tenants, call, showSecret }: {
  o: Owner; tenants: TenantRef[]; call: Call; showSecret: (label: string, password: string) => void;
}) {
  const { confirm, prompt } = useDialogs();
  const [name, setName] = useState(o.display_name);
  const [linked, setLinked] = useState(o.tenant_ids);

  return (
    <div className={cx("pf-section ow-card", o.disabled && "ow-disabled")}>
      <div className="title">
        <span>{o.username}</span>
        <span className="ow-badges">
          {o.disabled && <span className="badge paused">disabled</span>}
          {o.must_change_password && <span className="badge">temporary password</span>}
          {o.totp && <span className="badge link">2FA on</span>}
        </span>
      </div>
      <div className="pf-note ow-meta">
        {o.display_name || "No name"} · last sign-in {fmtDateTime(o.last_login_at, "never")} ·{" "}
        {o.sessions} active session{o.sessions === 1 ? "" : "s"} · created {fmtDateTime(o.created_at, "never")}
      </div>

      <div className="pf-actions">
        <input type="text" value={name} placeholder="Name shown on their dashboard" maxLength={200}
               onChange={(ev) => setName(ev.target.value)} />
        <button type="button" className="btn small"
                onClick={() => call("PATCH", `/api/owners/${o.id}`, { display_name: name }, "Name saved.")}>Save name</button>
      </div>

      <div className="sf-sub muted">Businesses this login can see</div>
      <TenantPicker tenants={tenants} selected={linked} onChange={setLinked} />

      <div className="pf-actions">
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
        <button type="button" className="btn small warn" onClick={async () => {
          if (!(await confirm(`Delete the login ${o.username}? The businesses and their data stay; only the login goes.`))) return;
          await call("DELETE", `/api/owners/${o.id}`, undefined, "Login deleted.");
        }}>Delete</button>
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

export function Owners() {
  const toast = useToast();
  const [tab, setTab] = useState<Tab>("logins");
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
        <Tabs tabs={TABS} value={tab} onChange={(key) => { setTab(key); setSecret(null); }} />
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
                       showSecret={(label, password) => setSecret({ label, password })} />
          ))}
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
