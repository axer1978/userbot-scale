"use client";

// Logins for moderators who can step in when the platform admin can't
// (manager_admin_api.py). They sign in at /manager/ with a temporary
// password set here, choose their own and must set up an authenticator app
// before anything opens. Nothing here ever shows a stored password or
// authenticator secret; the server never returns them. What each manager
// may do is their role's (Staff → Roles); a role with the admin panel signs
// in at / as well, the rest only at /manager/.

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useDialogs, useToast } from "@/components/feedback";
import { PageShell, Tabs } from "@/components/ui";
import { api, errorText } from "@/lib/api";
import { cx, fmtDateTime, tempPassword } from "@/lib/format";
import type { Manager, StaffRole } from "@/lib/types";
import { useLoader } from "@/lib/useLoader";
import { useOrigin } from "@/lib/useOrigin";
import "@/app/terms-managers.css";

export type ManagersTab = "list" | "new";

const TABS: [ManagersTab, string][] = [["list", "Managers"], ["new", "New manager"]];

// The addresses are only known in the browser; the server renders the paths.
function useUrls() {
  const origin = useOrigin();
  const manager = origin + "/manager/";
  return {
    origin,
    manager,
    // Where a manager with this role signs in.
    signIn: (roles: StaffRole[], roleId: number | null) => {
      const role = roles.find((r) => r.id === roleId);
      return role && role.admin_panel ? origin + "/" : manager;
    },
  };
}

// A role picker; "" stands for the default (the server's "Moderator").
function RoleSelect({ roles, value, withDefault, onChange, id }: {
  roles: StaffRole[]; value: string; withDefault: boolean; onChange: (value: string) => void; id?: string;
}) {
  return (
    <select id={id} className="mg-role" value={value} onChange={(ev) => onChange(ev.target.value)}>
      {withDefault && <option value="">Moderator (default)</option>}
      {roles.map((r) => (
        <option key={r.id} value={String(r.id)}>{r.name + (r.admin_panel ? " · admin panel" : "")}</option>
      ))}
    </select>
  );
}

// What a manager can do is their role's; the roles there are, and the rules
// every manager login has.
function Rules({ roles }: { roles: StaffRole[] }) {
  const router = useRouter();
  const { manager } = useUrls();
  return (
    <div className="pf-note mg-rules">
      <div className="mg-rules-head">What a manager can do comes from their role</div>
      {roles.length > 0 && (
        <ul>
          {roles.map((r) => (
            <li key={r.id}>
              <b>{r.name}</b>
              {` (${r.admin_panel ? "admin panel and " : ""}/manager/)` + (r.description ? ": " + r.description : "")}
            </li>
          ))}
        </ul>
      )}
      <button type="button" className="btn small" onClick={() => router.push("/staff?tab=roles")}>Edit roles</button>
      <p>An authenticator app is required: they set one up at their first sign-in at {manager}, before anything
        opens (also for the admin panel). Every change they make is in Staff → Activity and the audit log as
        &quot;manager:&lt;username&gt;&quot;.</p>
    </div>
  );
}

type Call = (method: string, path: string, body?: unknown, done?: string | null) => Promise<unknown>;

function ManagerCard({ m, roles, call, showSecret }: {
  m: Manager; roles: StaffRole[]; call: Call; showSecret: (label: string, password: string) => void;
}) {
  const { confirm, prompt } = useDialogs();
  const urls = useUrls();
  const [name, setName] = useState(m.display_name);
  const [role, setRole] = useState(m.role_id === null ? "" : String(m.role_id));

  return (
    <div className={cx("pf-section ow-card", m.disabled && "ow-disabled")}>
      <div className="title">
        <span>{m.username}</span>
        <span className="ow-badges">
          {m.disabled && <span className="badge paused">disabled</span>}
          {m.must_change_password && <span className="badge">temporary password</span>}
          {m.totp ? <span className="badge link">2FA on</span> : <span className="badge paused">no authenticator yet</span>}
          <span className="badge takeover">{m.role_name || "no role"}</span>
        </span>
      </div>
      <div className="pf-note ow-meta">
        {m.display_name || "No name"} · last sign-in {fmtDateTime(m.last_login_at, "never")} ·{" "}
        {m.sessions} active session{m.sessions === 1 ? "" : "s"} · created {fmtDateTime(m.created_at, "never")}
        {m.created_by ? ` by ${m.created_by}` : ""} · signs in at {urls.signIn(roles, m.role_id)}
      </div>

      {roles.length > 0 && (
        <div className="pf-actions">
          <span className="muted">Role</span>
          <RoleSelect roles={roles} value={role} withDefault={m.role_id === null} onChange={setRole} />
          <button type="button" className="btn small" onClick={() => {
            if (!role || Number(role) === m.role_id) return;
            call("PATCH", `/api/managers/${m.id}`, { role_id: Number(role) }, "Role changed.");
          }}>Change role</button>
        </div>
      )}

      <div className="pf-actions">
        <input type="text" value={name} placeholder="Name" maxLength={200} onChange={(ev) => setName(ev.target.value)} />
        <button type="button" className="btn small"
                onClick={() => call("PATCH", `/api/managers/${m.id}`, { display_name: name }, "Name saved.")}>Save name</button>
      </div>

      <div className="pf-actions">
        <button type="button" className={cx("btn small", !m.disabled && "warn")}
                title={m.disabled ? "Allow this manager to sign in again" : "Block this manager and end their sessions now"}
                onClick={async () => {
                  if (!m.disabled && !(await confirm(`Disable ${m.username}? Their open sessions end at once.`))) return;
                  await call("PATCH", `/api/managers/${m.id}`, { disabled: !m.disabled }, m.disabled ? "Enabled." : "Disabled.");
                }}>{m.disabled ? "Enable" : "Disable"}</button>
        <button type="button" className="btn small" onClick={async () => {
          const password = await prompt(`New temporary password for ${m.username} (at least 10 characters). ` +
            "Their sessions end and they choose their own at the next sign-in.", tempPassword());
          if (password === null) return;
          const done = await call("POST", `/api/managers/${m.id}/reset-password`, { password }, null);
          if (done) showSecret(`Temporary password for ${m.username}`, password);
        }}>Reset password…</button>
        {m.totp && (
          <button type="button" className="btn small"
                  title="For a lost phone: their sessions end and they must set up a new app at the next sign-in"
                  onClick={async () => {
                    if (!(await confirm(`Remove the authenticator of ${m.username}? Their sessions end now and they must set up ` +
                      "a new app at the next sign-in. Only do this when you are sure it is them asking."))) return;
                    await call("DELETE", `/api/managers/${m.id}/totp`, undefined, "Authenticator removed.");
                  }}>Remove 2FA</button>
        )}
        <button type="button" className="btn small warn" onClick={async () => {
          if (!(await confirm(`Delete the manager ${m.username}? What they did stays in the audit log.`))) return;
          await call("DELETE", `/api/managers/${m.id}`, undefined, "Manager deleted.");
        }}>Delete</button>
      </div>
    </div>
  );
}

function NewManager({ roles, onCreated }: {
  roles: StaffRole[]; onCreated: (manager: Manager, password: string) => Promise<void>;
}) {
  const toast = useToast();
  const urls = useUrls();
  const fallback = roles.find((r) => r.name === "Moderator") ?? null;
  const [username, setUsername] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState(tempPassword);
  const [role, setRole] = useState(fallback ? String(fallback.id) : "");
  const [busy, setBusy] = useState(false);
  const first = useRef<HTMLInputElement>(null);
  useEffect(() => { first.current?.focus(); }, []);

  const where = urls.signIn(roles, role ? Number(role) : (fallback ? fallback.id : null));

  return <>
    <div className="pf-section">
      <div className="field">
        <label htmlFor="mg-username">Username (3–64 letters, digits or . _ @ + -)</label>
        <input id="mg-username" ref={first} type="text" placeholder="e.g. maris@example.com or maris" autoComplete="off"
               maxLength={64} value={username} onChange={(ev) => setUsername(ev.target.value)} />
      </div>
      <div className="field">
        <label htmlFor="mg-name">Name</label>
        <input id="mg-name" type="text" placeholder="e.g. Māris Ozols" maxLength={200} value={name}
               onChange={(ev) => setName(ev.target.value)} />
      </div>
      <div className="field">
        <label htmlFor="mg-password">Temporary password (at least 10 characters; they change it at the first sign-in)</label>
        <div className="pf-actions ow-pw-row">
          <input id="mg-password" type="text" autoComplete="off" value={password} onChange={(ev) => setPassword(ev.target.value)} />
          <button type="button" className="btn small" onClick={() => setPassword(tempPassword())}>New suggestion</button>
        </div>
      </div>
      <div className="field">
        <label htmlFor="mg-role">Role (what they may do; Staff → Roles)</label>
        <RoleSelect id="mg-role" roles={roles} value={role} withDefault onChange={setRole} />
      </div>
      <p className="pf-note">
        {where === urls.manager
          ? `They sign in at ${urls.manager}.`
          : `This role includes the admin panel: they set up their login at ${urls.manager} first, then sign in at ` +
            `${where} with their username.`}
      </p>
      <div className="pf-actions">
        <button type="button" className="btn primary" disabled={busy} onClick={async () => {
          setBusy(true);
          try {
            const body: { username: string; display_name: string; password: string; role_id?: number } = {
              username: username.trim(), display_name: name.trim(), password,
            };
            if (role) body.role_id = Number(role);
            const manager = await api<Manager>("POST", "/api/managers", body);
            toast(`Manager ${manager.username} created.`, "info");
            await onCreated(manager, password);
          } catch (err) { toast(errorText(err)); }
          finally { setBusy(false); }
        }}>Create manager</button>
      </div>
    </div>
    <Rules roles={roles} />
  </>;
}

export function Managers({ initialTab = "list" }: { initialTab?: ManagersTab }) {
  const toast = useToast();
  const urls = useUrls();
  const [tab, setTab] = useState<ManagersTab>(initialTab);
  // A password is shown once, right after it was set, to pass on to the manager.
  const [secret, setSecret] = useState<{ label: string; password: string } | null>(null);

  // Without the roles (no permission, or an older server) the page still
  // works: no role pickers, and the server's default role for new ones.
  const fetchAll = useCallback(async () => {
    const [managers, roles] = await Promise.all([
      api<Manager[]>("GET", "/api/managers"),
      api<{ roles: StaffRole[] }>("GET", "/api/staff/roles").catch(() => null),
    ]);
    return { managers, roles: roles ? roles.roles : [] };
  }, []);
  const { data, error, reload: load } = useLoader(fetchAll);
  const managers = data?.managers ?? null;
  const roles = data?.roles ?? [];

  // Every change and every tab switch fetches again and starts the page
  // over, so a password shown earlier goes away.
  const switchTab = (key: ManagersTab) => { setTab(key); setSecret(null); void load(); };

  const call: Call = async (method, path, body, done) => {
    try {
      const result = await api(method, path, body);
      if (done) toast(done, "info");
      setSecret(null);
      await load();
      return result ?? true;
    } catch (err) {
      toast(errorText(err));
      return null;
    }
  };

  return (
    <PageShell title="Managers" crumb="moderators who can step in when you can't" width="w-860">
      <div className="bk-scroll">
        <Tabs tabs={TABS} value={tab} onChange={switchTab} />
        {error && <div className="pf-errors">{error}</div>}
        {secret && (
          <div className="pf-section ow-secret">
            <div className="title">{secret.label}</div>
            <p className="pf-note">Shown only now. Send it to them together with {urls.manager}; they choose their own
              password and set up an authenticator app when they first sign in.</p>
            <code className="ow-password">{secret.password}</code>
          </div>
        )}
        {managers && tab === "list" && <>
          <p className="pf-note">Managers sign in at {urls.manager}. Those whose role includes the admin panel can
            also sign in at {urls.origin}/ with their username.</p>
          <Rules roles={roles} />
          {!managers.length ? <>
            <div className="pf-note">No managers yet.</div>
            <button type="button" className="btn primary" onClick={() => switchTab("new")}>Create the first one</button>
          </> : managers.map((m) => (
            <ManagerCard key={`${m.id}-${m.role_id}-${m.display_name}`} m={m} roles={roles} call={call}
                         showSecret={(label, password) => setSecret({ label, password })} />
          ))}
        </>}
        {managers && tab === "new" && (
          <NewManager roles={roles} onCreated={async (manager, password) => {
            setTab("list");
            setSecret(null);
            await load();
            setSecret({ label: `Temporary password for ${manager.username}`, password });
          }} />
        )}
      </div>
    </PageShell>
  );
}
