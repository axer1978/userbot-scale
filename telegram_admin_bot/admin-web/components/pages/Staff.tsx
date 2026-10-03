"use client";

// Staff roles and the approval queue (staff_api.py, staff.py). A role sets
// every action of the catalogue to Off, Allowed or Needs my approval. A
// change that needs approval answers the moderator as if it was done, but
// waits here: approving runs it now, exactly as sent; rejecting drops it
// and the moderator is not told. Protective changes (pausing a bot or a
// chat, disabling a login…) run at once whatever the level. The server
// refuses all of /api/staff/ to staff, so this is the admin's alone.

import "@/app/staff-verification.css";
import { useRouter } from "next/navigation";
import { useCallback, useRef, useState } from "react";
import { useDialogs, useToast } from "@/components/feedback";
import { PageShell, Tabs } from "@/components/ui";
import { ApiError, api, errorText } from "@/lib/api";
import { cx, fmtDateTime } from "@/lib/format";
import { usePanel } from "@/lib/panel";
import type { Manager, PermissionLevel, StaffAction, StaffRequest, StaffRole } from "@/lib/types";
import { useLoader } from "@/lib/useLoader";
import { useOrigin } from "@/lib/useOrigin";

export type StaffTab = "waiting" | "activity" | "roles";
type Level = "off" | PermissionLevel;

const LEVELS: [Level, string][] = [["off", "Off"], ["allow", "Allowed"], ["approve", "Needs my approval"]];
const STATUS: Record<string, [string, string]> = {
  applied: ["done", "badge link"],
  pending: ["pending", "badge paused"],
  approved: ["approved", "badge link"],
  rejected: ["rejected", "badge"],
  failed: ["failed", "badge escalated"],
};
const FILTERS: [string, string][] = [["all", "Every status"], ["applied", "Done at once"], ["pending", "Waiting"],
                                     ["approved", "Approved"], ["rejected", "Rejected"], ["failed", "Failed"]];
const DROP = "Drop the unsaved changes to this role?";

type RequestList = { requests: StaffRequest[]; pending: number };
type View =
  | { tab: "waiting"; requests: StaffRequest[] }
  | { tab: "activity"; requests: StaffRequest[]; managers: Manager[] }
  | { tab: "roles"; roles: StaffRole[]; catalogue: StaffAction[] };
/** One answer, tagged with what was asked so a tab never shows another's data. */
type Loaded = { key: string; stamp: number; view: View | null; error: string | null };

/** The role being edited: a copy, saved only with Save. */
type Draft = {
  id: number | null;
  name: string;
  description: string;
  admin_panel: boolean;
  permissions: Record<string, PermissionLevel>;
  members: number;
};

// Every load gets a new stamp; the lists are keyed by it, so a reload
// draws them afresh (closed forms, cleared notes) as the old panel did.
let loads = 0;

function draftOf(role: StaffRole): Draft {
  return {
    id: role.id, name: role.name, description: role.description || "", admin_panel: !!role.admin_panel,
    permissions: { ...(role.permissions || {}) }, members: role.members || 0,
  };
}

const BLANK_ROLE: Draft = { id: null, name: "", description: "", admin_panel: false, permissions: {}, members: 0 };

function who(r: { username: string; role_name?: string | null }): string {
  return r.username + (r.role_name ? ` (${r.role_name})` : "");
}

function business(r: StaffRequest): string {
  if (r.tenant_name) return r.tenant_name;
  return r.tenant_id ? `Client ${r.tenant_id}` : "";
}

function StatusBadge({ status }: { status: string }) {
  const [label, cls] = STATUS[status] || [status, "badge"];
  return <span className={cls}>{label}</span>;
}

// The JSON that was sent, pretty-printed when it is JSON.
function pretty(body?: string | null): string {
  if (!body) return "";
  try { return JSON.stringify(JSON.parse(body), null, 2); } catch { return body; }
}

function Change({ r }: { r: StaffRequest }) {
  return (
    <div className="sr-change">
      <pre className="pf-rendered sr-sent">{pretty(r.body) || "(nothing was sent with it)"}</pre>
      <div className="sr-route">{`${r.method} ${r.path}${r.query ? "?" + r.query : ""}`}</div>
    </div>
  );
}

// What the app answered when an approved change was run: its "detail"
// when it is one of our JSON errors, else the text as it came.
function resultText(r: StaffRequest): string {
  const body = r.result_body || "";
  try {
    const data = JSON.parse(body);
    if (data && typeof data.detail === "string") return data.detail;
    if (data && data.detail) return JSON.stringify(data.detail);
  } catch { /* not JSON */ }
  return body || `error ${r.result_status}`;
}

/** The top-bar count, asked again right after a decision. */
function useRecount() {
  const { setStaffCount } = usePanel();
  return useCallback(() => {
    api<RequestList>("GET", "/api/staff/requests?status=pending&limit=1")
      .then((data) => setStaffCount(data.pending || 0), () => {});
  }, [setStaffCount]);
}

/* --------------------------------------------------------- waiting for you */

function RequestCard({ r, onStale }: { r: StaffRequest; onStale: () => void }) {
  const toast = useToast();
  const { confirm } = useDialogs();
  const recount = useRecount();
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [decided, setDecided] = useState<{ verb: "approve" | "reject"; row: StaffRequest } | null>(null);

  const decide = async (verb: "approve" | "reject") => {
    if (verb === "reject" && !(await confirm(`Reject this change by ${r.username}? It is never done, and they are not told.`))) {
      return;
    }
    setBusy(true);
    try {
      const row = await api<StaffRequest>("POST", `/api/staff/requests/${r.id}/${verb}`, { note: note.trim() });
      setDecided({ verb, row });
      recount();
    } catch (err) {
      toast(errorText(err));
      setBusy(false);
      // Decided elsewhere (another tab) or gone: show the queue as it is.
      if (err instanceof ApiError && (err.status === 409 || err.status === 404)) onStale();
    }
  };

  const meta = [who(r), fmtDateTime(r.created_at)];
  if (business(r)) meta.push("business: " + business(r));

  return (
    <div className={cx("pf-section sr-request", decided && "sr-decided")}>
      <div className="title">
        <span>{r.action_label}</span>
        <StatusBadge status={decided ? decided.row.status : r.status} />
      </div>
      <div className="pf-note sr-meta">{meta.join(" · ")}</div>
      <Change r={r} />
      {!decided && (
        <div className="pf-actions">
          <input type="text" maxLength={500} placeholder="Note (optional)" value={note}
                 onChange={(ev) => setNote(ev.target.value)} />
          <button type="button" className="btn small primary" disabled={busy} onClick={() => void decide("approve")}>
            Approve</button>
          <button type="button" className="btn small warn" disabled={busy} onClick={() => void decide("reject")}>
            Reject</button>
        </div>
      )}
      <div className="sr-result">
        {decided?.verb === "reject" && <div className="pf-note">Rejected: it was not done.</div>}
        {decided?.verb === "approve" && ((decided.row.result_status ?? 0) >= 400
          ? <div className="pf-errors">{"It could not be done: " + resultText(decided.row)}</div>
          : <div className="sr-ok">Approved and done.</div>)}
      </div>
    </div>
  );
}

function Waiting({ requests, stamp, onStale }: { requests: StaffRequest[]; stamp: number; onStale: () => void }) {
  return (
    <>
      <p className="pf-note">{"Moderators see these as done. Approving runs them now, exactly as sent; " +
        "rejecting drops them and the moderator is not told."}</p>
      {!requests.length && <div className="pf-note">Nothing waits for you.</div>}
      {requests.map((r) => <RequestCard key={`${stamp}-${r.id}`} r={r} onStale={onStale} />)}
    </>
  );
}

/* ---------------------------------------------------------------- activity */

function Activity({ requests, managers, status, managerId, onStatus, onManager }: {
  requests: StaffRequest[]; managers: Manager[]; status: string; managerId: string;
  onStatus: (status: string) => void; onManager: (managerId: string) => void;
}) {
  return (
    <>
      <div className="bk-nav">
        <select value={status} onChange={(ev) => onStatus(ev.target.value)}>
          {FILTERS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
        <select value={managerId} onChange={(ev) => onManager(ev.target.value)}>
          <option value="">Every manager</option>
          {managers.map((m) => <option key={m.id} value={String(m.id)}>{who(m)}</option>)}
        </select>
      </div>
      <p className="pf-note">{"Every change a moderator made, newest first (the last 200). " +
        "\"done\" ran at once: the role allows it, or it only protects (a pause, say)."}</p>
      {!requests.length ? <div className="pf-note">Nothing here.</div> : (
        <table className="cfg-table sr-table">
          <thead>
            <tr>{["Time", "Who", "Change", "Business", "Status", "Note"].map((h) => <th key={h}>{h}</th>)}</tr>
          </thead>
          <tbody>
            {requests.map((r) => (
              <tr key={r.id}>
                <td className="sr-when">{fmtDateTime(r.created_at)}</td>
                <td>{who(r)}</td>
                <td>
                  <div>{r.action_label}</div>
                  <details className="sr-details">
                    <summary>what was sent</summary>
                    <Change r={r} />
                  </details>
                </td>
                <td>{business(r) || "—"}</td>
                <td><StatusBadge status={r.status} /></td>
                <td className="sr-note">
                  {r.note && <div>{r.note}</div>}
                  {r.decided_at && (
                    <div className="muted">{`${r.status === "rejected" ? "Rejected" : "Approved"}` +
                      `${r.decided_by ? " by " + r.decided_by : ""}, ${fmtDateTime(r.decided_at)}`}</div>
                  )}
                  {r.decision_note && <div>{"“" + r.decision_note + "”"}</div>}
                  {r.status === "failed" && <div className="warn-note">{"It could not be done: " + resultText(r)}</div>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

/* ------------------------------------------------------------------- roles */

// Only what is on; reads are never "approve" (the server drops that too).
function cleanPermissions(perms: Record<string, PermissionLevel>, catalogue: StaffAction[]) {
  const out: Record<string, PermissionLevel> = {};
  for (const action of catalogue) {
    const level = perms[action.key];
    if (level === "allow" || (level === "approve" && !action.view)) out[action.key] = level;
    else if (level === "approve" && action.view) out[action.key] = "allow";
  }
  return out;
}

function withLevel(perms: Record<string, PermissionLevel>, key: string, level: Level) {
  const next = { ...perms };
  if (level === "off") delete next[key];
  else next[key] = level;
  return next;
}

// Off / Allowed / Needs my approval (reads: Off / Allowed).
function Seg({ action, current, onPick }: { action: StaffAction; current: Level; onPick: (level: Level) => void }) {
  return (
    <div className="sr-seg" role="group" aria-label={action.label}>
      {LEVELS.filter(([level]) => !(action.view && level === "approve")).map(([level, label]) => {
        const on = current === level || (!!action.view && level === "allow" && current === "approve");
        return (
          <button key={level} type="button" className={cx(`sr-level sr-${level}`, on && "on")} aria-pressed={on}
                  onClick={() => onPick(level)}>{label}</button>
        );
      })}
    </div>
  );
}

function Group({ name, actions, permissions, onChange }: {
  name: string; actions: StaffAction[]; permissions: Record<string, PermissionLevel>;
  onChange: (permissions: Record<string, PermissionLevel>) => void;
}) {
  const quick: [Level, string][] = [["off", "All off"], ["allow", "All allowed"]];
  if (!actions.every((a) => a.view)) quick.push(["approve", "All need approval"]);
  return (
    <div className="pf-section sr-group">
      <div className="title">
        <span>{name}</span>
        <span className="sr-quick">
          {quick.map(([level, label]) => (
            <button key={level} type="button" className="btn small" onClick={() => {
              let next = permissions;
              for (const action of actions) {
                next = withLevel(next, action.key, level === "approve" && action.view ? "allow" : level);
              }
              onChange(next);
            }}>{label}</button>
          ))}
        </span>
      </div>
      {actions.map((action) => (
        <div key={action.key} className="sr-action">
          <div className="sr-action-text">
            <div>{action.label}</div>
            <div className="sr-action-key">{action.key}</div>
            {action.protective && (
              <div className="sr-protective">protective: runs at once even when it needs approval</div>
            )}
          </div>
          <Seg action={action} current={permissions[action.key] || "off"}
               onPick={(level) => onChange(withLevel(permissions, action.key, level))} />
        </div>
      ))}
    </div>
  );
}

function RoleEditor({ d, roles, catalogue, edit, setDraft, reload }: {
  d: Draft | null; roles: StaffRole[]; catalogue: StaffAction[];
  /** Change the draft and mark it unsaved. */
  edit: (patch: Partial<Draft>) => void;
  /** Replace the draft and mark it saved. */
  setDraft: (draft: Draft | null) => void;
  reload: () => Promise<unknown>;
}) {
  const toast = useToast();
  const { confirm } = useDialogs();
  const origin = useOrigin();
  const nameInput = useRef<HTMLInputElement>(null);
  const [saving, setSaving] = useState(false);

  if (!d) return <div className="pf-note">No roles yet. Make one with New role.</div>;

  // The matrix, one section per group, in the catalogue's order.
  const groups: { name: string; actions: StaffAction[] }[] = [];
  for (const action of catalogue) {
    let group = groups.find((g) => g.name === action.group);
    if (!group) { group = { name: action.group, actions: [] }; groups.push(group); }
    group.actions.push(action);
  }

  const save = async () => {
    const body = {
      name: d.name.trim(), description: d.description.trim(), admin_panel: d.admin_panel,
      permissions: cleanPermissions(d.permissions, catalogue),
    };
    if (!body.name) { toast("Give the role a name."); nameInput.current?.focus(); return; }
    setSaving(true);
    try {
      const role = d.id === null
        ? await api<StaffRole>("POST", "/api/staff/roles", body)
        : await api<StaffRole>("PUT", `/api/staff/roles/${d.id}`, body);
      toast(`Role ${role.name} saved.`, "info");
      setDraft(draftOf(role));
      await reload();
    } catch (err) {
      toast(errorText(err));
    } finally {
      setSaving(false);
    }
  };

  const remove = async () => {
    if (d.members) {
      toast(`${d.members} manager${d.members === 1 ? " has" : "s have"} this role. Give them another role first ` +
        "(Managers).");
      return;
    }
    if (!(await confirm(`Delete the role ${d.name}?`))) return;
    try {
      await api("DELETE", `/api/staff/roles/${d.id}`);
      toast("Role deleted.", "info");
      setDraft(null);
      await reload();
    } catch (err) { toast(errorText(err)); }
  };

  return (
    <>
      <div className="pf-section">
        <div className="title">
          <span>{d.id === null ? "New role" : d.name}</span>
          {d.id !== null && (
            <span className="muted">{`${d.members} manager${d.members === 1 ? "" : "s"} have this role`}</span>
          )}
        </div>
        <div className="field sr-field">
          <label htmlFor="sr-name">Name</label>
          <input id="sr-name" ref={nameInput} type="text" maxLength={80} placeholder="e.g. Night moderator"
                 value={d.name} onChange={(ev) => edit({ name: ev.target.value })} />
        </div>
        <div className="field">
          <label htmlFor="sr-description">Description</label>
          <textarea id="sr-description" rows={2} maxLength={500} placeholder="What this role is for (only you see it)"
                    value={d.description} onChange={(ev) => edit({ description: ev.target.value })} />
        </div>
        <div className="field check">
          <input id="sr-admin-panel" type="checkbox" checked={d.admin_panel}
                 onChange={(ev) => edit({ admin_panel: ev.target.checked })} />
          <label htmlFor="sr-admin-panel">Can sign in to the admin panel</label>
        </div>
        <p className="pf-note sr-hint">{"With it, members sign in at " + origin + "/ with their " +
          "moderator username, password and authenticator code, and see only what the role allows. " +
          "Without it, only the moderator panel at " + origin + "/manager/."}</p>
      </div>

      {groups.map((g) => (
        <Group key={g.name} name={g.name} actions={g.actions} permissions={d.permissions}
               onChange={(permissions) => edit({ permissions })} />
      ))}

      <div className="pf-actions sr-save">
        <button type="button" className="btn primary" disabled={saving} onClick={() => void save()}>
          {d.id === null ? "Create role" : "Save role"}</button>
        <button type="button" className="btn" onClick={() => {
          const saved = d.id === null ? null : roles.find((r) => r.id === d.id);
          setDraft(saved ? draftOf(saved) : null);
        }}>Discard changes</button>
        {d.id !== null && <button type="button" className="btn warn" onClick={() => void remove()}>Delete role</button>}
      </div>
    </>
  );
}

function Roles({ roles, catalogue, draft, pick, edit, setDraft, reload }: {
  roles: StaffRole[]; catalogue: StaffAction[]; draft: Draft | null;
  /** Switch to another role (or a new one), after asking when there are unsaved edits. */
  pick: (draft: Draft) => void;
  edit: (patch: Partial<Draft>) => void;
  setDraft: (draft: Draft | null) => void;
  reload: () => Promise<unknown>;
}) {
  return (
    <>
      <p className="pf-note">{"Each action is Off (refused, and that part of the panel is hidden), " +
        "Allowed (done at once) or Needs my approval: the change looks done to the moderator, but it waits under " +
        "\"Waiting for you\" until you approve it. Reads can only be Off or Allowed. Staff, roles and this queue " +
        "are always yours alone. A role's changes apply to its members from their next click."}</p>
      <div className="sr-roles">
        <div className="sr-role-list">
          {roles.map((r) => (
            <button key={r.id} type="button" className={cx("sr-role", draft?.id === r.id && "on")}
                    onClick={() => { if (!draft || draft.id !== r.id) pick(draftOf(r)); }}>
              <span className="sr-role-name">{r.name}</span>
              <span className="muted">{`${r.members} member${r.members === 1 ? "" : "s"}` +
                (r.admin_panel ? " · admin panel" : "")}</span>
            </button>
          ))}
          <button type="button" className={cx("btn small", draft?.id === null && "primary")}
                  onClick={() => { if (!draft || draft.id !== null) pick(BLANK_ROLE); }}>New role</button>
        </div>
        <div className="sr-editor">
          <RoleEditor d={draft} roles={roles} catalogue={catalogue} edit={edit} setDraft={setDraft} reload={reload} />
        </div>
      </div>
    </>
  );
}

/* -------------------------------------------------------------------- page */

const TAB_LABELS: [StaffTab, string][] = [["waiting", "Waiting for you"], ["activity", "Activity"], ["roles", "Roles"]];

// Staff, roles and the queue are the admin's alone (the server refuses all
// of /api/staff/ to moderators); a moderator who lands here is told so.
export function Staff({ initialTab }: { initialTab?: StaffTab }) {
  const { me, isAdmin } = usePanel();
  if (me && !isAdmin) {
    return (
      <PageShell title="Staff">
        <div className="bk-scroll"><div className="empty" style={{ padding: 40 }}>Only the admin can manage staff.</div></div>
      </PageShell>
    );
  }
  return <StaffPage initialTab={initialTab} />;
}

function StaffPage({ initialTab }: { initialTab?: StaffTab }) {
  const { staffCount, setStaffCount } = usePanel();
  const { confirm } = useDialogs();
  const router = useRouter();
  const [tab, setTab] = useState<StaffTab>(initialTab || "waiting");
  const [status, setStatus] = useState("all");      // Activity filters
  const [managerId, setManagerId] = useState("");
  const [draft, setDraftState] = useState<Draft | null>(null);
  const [dirty, setDirty] = useState(false);

  const key = tab === "activity" ? `activity:${status}:${managerId}` : tab;
  const fetchView = useCallback(async (): Promise<Loaded> => {
    const stamp = ++loads;
    try {
      if (tab === "roles") {
        const data = await api<{ roles: StaffRole[]; catalogue: StaffAction[] }>("GET", "/api/staff/roles");
        return { key, stamp, error: null, view: { tab, roles: data.roles, catalogue: data.catalogue } };
      }
      if (tab === "activity") {
        const query = new URLSearchParams({ status });
        if (managerId) query.set("manager_id", managerId);
        const [data, managers] = await Promise.all([
          api<RequestList>("GET", "/api/staff/requests?" + query.toString()),
          api<Manager[]>("GET", "/api/managers").catch(() => [] as Manager[]),
        ]);
        setStaffCount(data.pending || 0);
        return { key, stamp, error: null, view: { tab, requests: data.requests, managers } };
      }
      const data = await api<RequestList>("GET", "/api/staff/requests?status=pending");
      setStaffCount(data.pending || 0);
      return { key, stamp, error: null, view: { tab, requests: data.requests } };
    } catch (err) {
      return { key, stamp, error: errorText(err), view: null };
    }
  }, [key, tab, status, managerId, setStaffCount]);
  const { data: loaded, reload } = useLoader(fetchView);
  const current = loaded && loaded.key === key ? loaded : null;
  const view = current?.view ?? null;

  // The draft as the roles list now stands: a role deleted elsewhere drops
  // it, and with none picked the first role is shown.
  const roles = view?.tab === "roles" ? view.roles : null;
  const stale = !!(roles && draft && draft.id !== null && !roles.some((r) => r.id === draft.id));
  const shown = roles ? (draft && !stale ? draft : roles.length ? draftOf(roles[0]) : null) : draft;
  const unsaved = dirty && !stale;

  const setDraft = (next: Draft | null) => { setDraftState(next); setDirty(false); };
  const edit = (patch: Partial<Draft>) => {
    if (!shown) return;
    setDraftState({ ...shown, ...patch });
    setDirty(true);
  };
  const pick = async (next: Draft) => {
    if (unsaved && !(await confirm(DROP))) return;
    setDraft(next);
  };

  const showTab = async (next: StaffTab) => {
    if (tab === "roles" && next !== "roles" && unsaved) {
      if (!(await confirm(DROP))) return;
      setDraft(null);
    }
    if (next === tab) void reload();
    else setTab(next);
  };

  // Close (and any other link on the page) asks before dropping a role's edits.
  const guardLeave = (ev: React.MouseEvent) => {
    const link = (ev.target as Element).closest?.("a[href]");
    if (!link || !unsaved) return;
    ev.preventDefault();
    ev.stopPropagation();
    const href = link.getAttribute("href") || "/";
    void confirm(DROP).then((ok) => {
      if (!ok) return;
      setDraft(null);
      router.push(href);
    });
  };

  const tabs = TAB_LABELS.map(([k, label]) => [k, k === "waiting"
    ? <>{label}<span className="count-badge">{staffCount ? String(staffCount) : ""}</span></>
    : label] as const);

  return (
    <div className="sv-w1080" onClickCapture={guardLeave}>
      <PageShell title="Staff" crumb="roles, permissions and changes waiting for you">
        <div className="bk-scroll">
          <Tabs tabs={tabs} value={tab} onChange={(k) => void showTab(k)} />
          {!current && <div className="pf-note">Loading…</div>}
          {current?.error && <div className="pf-errors">{current.error}</div>}
          {view?.tab === "waiting" && current &&
            <Waiting requests={view.requests} stamp={current.stamp} onStale={() => void reload()} />}
          {view?.tab === "activity" && (
            <Activity requests={view.requests} managers={view.managers} status={status} managerId={managerId}
                      onStatus={setStatus} onManager={setManagerId} />
          )}
          {view?.tab === "roles" && (
            <Roles roles={view.roles} catalogue={view.catalogue} draft={shown} pick={(d) => void pick(d)}
                   edit={edit} setDraft={setDraft} reload={reload} />
          )}
        </div>
      </PageShell>
    </div>
  );
}
