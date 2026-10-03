"use client";

// Industry folders with their clients, plus the platform rules. How a
// client's bot behaves (config), what it knows (prompt), and the version
// history of both are edited here, against platform_api.py. The account's
// pause switch and per-contact styles stay in the top bar and Style dialog.

import { useCallback, useEffect, useState } from "react";
import { ConfigEditor } from "@/components/ConfigEditor";
import { useDialogs, useToast } from "@/components/feedback";
import { PageShell, Tabs } from "@/components/ui";
import { api, errorText } from "@/lib/api";
import { cx, fmtTime } from "@/lib/format";
import type {
  AuditEvent, BaseView, ConfigProposal, IndustryView, PlatformTree, TenantView, Version,
} from "@/lib/types";
import { useLoader } from "@/lib/useLoader";

export type NodeKind = "tenant" | "industry" | "base";
export type TreeNode = { kind: NodeKind; id: number };

const TABS = {
  tenant: [["config", "Config"], ["prompt", "Prompt"], ["versions", "Versions"],
           ["preview", "Rendered prompt"], ["assist", "Ask AI"], ["audit", "Audit log"]],
  industry: [["template", "Template"], ["config", "Default config"], ["versions", "Versions"], ["clients", "Clients"]],
  base: [["rules", "Rules"], ["versions", "Versions"], ["prices", "AI prices"]],
} as const satisfies Record<NodeKind, readonly (readonly [string, string])[]>;

type AnyView = TenantView | IndustryView | BaseView;

/** The tab asked for, if this kind of node has it; else its first tab. */
function tabFor(kind: NodeKind, wanted?: string): string {
  const tabs = TABS[kind] as readonly (readonly [string, string])[];
  return tabs.some(([key]) => key === wanted) ? wanted! : tabs[0][0];
}

function sameNode(a: TreeNode | null, b: TreeNode | null) {
  return !!a && !!b && a.kind === b.kind && a.id === b.id;
}

/** "Reason for … (goes into the audit log)"; null when cancelled. */
function useAskReason() {
  const { prompt } = useDialogs();
  return async (what: string) => {
    const reason = await prompt(`Reason for ${what} (goes into the audit log):`, "");
    return reason === null ? null : reason.trim();
  };
}

/* --------------------------------------------------------------- versions */

function VersionList({ versions, current, actionLabel, onPick }: {
  versions: Version[]; current: number; actionLabel: string; onPick?: (version: number) => void;
}) {
  return (
    <div className="pf-list">
      {!versions.length && <p className="pf-note">No versions yet.</p>}
      {versions.map((ver) => (
        <div key={ver.version} className={cx("v", ver.version === current && "current")}>
          <strong>v{ver.version}</strong>
          <div className="grow">
            <div>{ver.note || "(no note)"}</div>
            <div className="muted">{ver.created_by} · {fmtTime(ver.created_at)}</div>
            {ver.content ? (
              <details><summary>content</summary><pre>{JSON.stringify(ver.content, null, 2)}</pre></details>
            ) : null}
          </div>
          {ver.version === current ? <span className="tag sent">in use</span>
            : onPick && <button type="button" className="btn small" onClick={() => onPick(ver.version)}>{actionLabel}</button>}
        </div>
      ))}
    </div>
  );
}

function AuditList({ events }: { events: AuditEvent[] }) {
  return (
    <div className="pf-list">
      {!events.length && <p className="pf-note">Nothing recorded yet.</p>}
      {events.map((e, i) => (
        <div key={i} className="v">
          <span className="muted">{fmtTime(e.created_at)}</span>
          <div className="grow">
            <div>{e.event} · {e.actor}{e.reason ? " — " + e.reason : ""}</div>
            {e.payload && Object.keys(e.payload).length > 0 && (
              <details><summary>details</summary><pre>{JSON.stringify(e.payload, null, 2)}</pre></details>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ tenant */

type TenantProps = { v: TenantView; id: number; setView: (v: TenantView) => void; reload: () => Promise<void> };

function TenantPrompt({ v, id, setView }: TenantProps) {
  const toast = useToast();
  const [editors, setEditors] = useState(() => v.prompt.sections.map((s) => ({
    key: s.key, mode: (s.override ? s.override.mode : "inherit") as "inherit" | "override" | "append",
    text: s.override ? s.override.text : "",
  })));
  const [addendum, setAddendum] = useState(v.prompt.addendum);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  return (
    <>
      <p className="pf-note">Each section comes from the industry template. Override replaces it for this client, append
        adds to it. The platform rules always come first and cannot be changed from here; see the Rendered prompt tab.</p>
      {v.prompt.sections.map((section, i) => {
        const ed = editors[i];
        return (
          <div key={section.key} className={cx("pf-section", ed.mode !== "inherit" && "overridden")}>
            <div className="title">
              {section.heading}
              <select value={ed.mode} onChange={(ev) => setEditors((list) => list.map((e, n) =>
                n === i ? { ...e, mode: ev.target.value as typeof e.mode } : e))}>
                <option value="inherit">Inherit from industry</option>
                <option value="override">Override</option>
                <option value="append">Append</option>
              </select>
            </div>
            <div className="inherited-text">{section.inherited || "(the industry template leaves this empty)"}</div>
            {ed.mode !== "inherit" && (
              <textarea rows={3} placeholder="This client's text" value={ed.text}
                        onChange={(ev) => setEditors((list) => list.map((e, n) => (n === i ? { ...e, text: ev.target.value } : e)))} />
            )}
          </div>
        );
      })}
      <div className="pf-section">
        <div className="title">Additional notes from the business</div>
        <textarea rows={3} maxLength={v.prompt.addendum_limit} value={addendum} onChange={(ev) => setAddendum(ev.target.value)} />
        <div className="pf-note">{addendum.length} / {v.prompt.addendum_limit} characters</div>
      </div>
      <div className="pf-actions">
        <input placeholder="What changed (saved with the new version)" value={note} onChange={(ev) => setNote(ev.target.value)} />
        <button type="button" className="btn primary" disabled={busy} onClick={async () => {
          const overrides: Record<string, { mode: string; text: string }> = {};
          for (const e of editors) if (e.mode !== "inherit" && e.text.trim()) overrides[e.key] = { mode: e.mode, text: e.text };
          setBusy(true);
          try {
            const saved = await api<TenantView>("PUT", `/api/tenants/${id}/prompt`, { overrides, addendum, note: note.trim() });
            toast(`Saved as client version ${saved.prompt.client_version}.`, "info");
            setView(saved);
          } catch (err) { toast(errorText(err)); } finally { setBusy(false); }
        }}>Save as new version</button>
      </div>
    </>
  );
}

function TenantVersions({ v, id, reload }: TenantProps) {
  const toast = useToast();
  const askReason = useAskReason();
  const [pin, setPin] = useState(v.prompt.pinned ? String(v.prompt.pinned) : "");

  return (
    <>
      <div className="sub">Industry template</div>
      <p className="pf-note">{v.prompt.pinned
        ? `Pinned to ${v.industry.name} template v${v.prompt.pinned}: industry edits do not reach this client until unpinned.`
        : `Follows the ${v.industry.name} template's live version (now v${v.industry.template_version}).`}</p>
      <div className="pf-actions">
        <select value={pin} onChange={(ev) => setPin(ev.target.value)}>
          <option value="">Follow the live version (v{v.industry.template_version})</option>
          {v.prompt.industry_versions.map((iv) => (
            <option key={iv.version} value={String(iv.version)}>Pin to v{iv.version}{iv.note ? " — " + iv.note : ""}</option>
          ))}
        </select>
        <button type="button" className="btn" onClick={async () => {
          const reason = await askReason("changing the pin");
          if (reason === null) return;
          try {
            await api("POST", `/api/tenants/${id}/pin`, { version: pin ? Number(pin) : null, reason });
            await reload();
          } catch (err) { toast(errorText(err)); }
        }}>Apply</button>
      </div>

      <div className="sub">This client&apos;s prompt versions</div>
      <VersionList versions={v.prompt.client_versions} current={v.prompt.client_version} actionLabel="Roll back to this"
                   onPick={async (version) => {
                     const reason = await askReason(`rolling back to v${version}`);
                     if (reason === null) return;
                     try {
                       await api("POST", `/api/tenants/${id}/prompt/rollback`, { version, reason });
                       await reload();
                       toast(`Now using client version ${version}.`, "info");
                     } catch (err) { toast(errorText(err)); }
                   }} />
    </>
  );
}

function TenantAssist({ id, setView, onApplied }: TenantProps & { onApplied: () => void }) {
  const toast = useToast();
  const [intent, setIntent] = useState("");
  const [proposal, setProposal] = useState<ConfigProposal | null>(null);
  const [asking, setAsking] = useState(false);
  const [applying, setApplying] = useState(false);

  return (
    <>
      <p className="pf-note">Describe a change in plain words. The AI proposes it as config, the schema checks it, and
        nothing changes until you press Apply. Uses DEEPSEEK_PLATFORM_KEY.</p>
      <textarea rows={3} value={intent} onChange={(ev) => setIntent(ev.target.value)}
                placeholder="e.g. Don't answer between 10 pm and 8 am, and never talk about prices of colouring" />
      <div className="pf-actions">
        <button type="button" className="btn primary" disabled={asking} onClick={async () => {
          if (!intent.trim()) return;
          setAsking(true);
          try { setProposal(await api<ConfigProposal>("POST", `/api/tenants/${id}/config/propose`, { intent })); }
          catch (err) { toast(errorText(err)); } finally { setAsking(false); }
        }}>{asking ? "Asking…" : "Propose"}</button>
      </div>
      {proposal && <div>
        <div className="sub">{proposal.valid ? "Proposed change" : "The proposal does not validate"}</div>
        {proposal.errors.map((e, i) => <div key={i} className="pf-errors">{e.path}: {e.message}</div>)}
        {proposal.valid && !proposal.changes.length && <p className="pf-note">It would change nothing.</p>}
        {proposal.changes.length > 0 && (
          <table className="cfg-table"><tbody>
            {proposal.changes.map((c) => (
              <tr key={c.path}>
                <td className="path">{c.path}</td><td>{JSON.stringify(c.from)}</td><td>→</td><td>{JSON.stringify(c.to)}</td>
              </tr>
            ))}
          </tbody></table>
        )}
        <details>
          <summary className="muted">raw proposal</summary>
          <pre className="pf-rendered">{JSON.stringify(proposal.proposal, null, 2)}</pre>
        </details>
        {proposal.valid && proposal.changes.length > 0 && (
          <div className="pf-actions">
            <button type="button" className="btn primary" disabled={applying} onClick={async () => {
              setApplying(true);
              try {
                const saved = await api<TenantView>("PUT", `/api/tenants/${id}/config`, {
                  overrides: proposal.overrides, reason: "AI proposal: " + proposal.intent, expected_revision: proposal.revision,
                });
                setProposal(null);
                setView(saved);
                toast("Applied.", "info");
                onApplied();
              } catch (err) { toast(errorText(err)); setApplying(false); }
            }}>Apply this change</button>
          </div>
        )}
      </div>}
    </>
  );
}

function TenantAudit({ id }: { id: number }) {
  const toast = useToast();
  const [events, setEvents] = useState<AuditEvent[] | null>(null);
  useEffect(() => {
    api<AuditEvent[]>("GET", `/api/audit?tenant_id=${id}`).then(setEvents).catch((err) => toast(errorText(err)));
  }, [id, toast]);
  return <AuditList events={events || []} />;
}

/* ---------------------------------------------------------------- industry */

function IndustryTemplate({ v, id, reload }: { v: IndustryView; id: number; reload: () => Promise<void> }) {
  const toast = useToast();
  const [texts, setTexts] = useState(() => v.sections.map((s) => s.text));
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <>
      <p className="pf-note">Saving makes a new version and puts it live for every {v.industry.name} client that is not pinned.</p>
      {v.sections.map((section, i) => (
        <div key={section.key} className="pf-section">
          <div className="title">{section.heading}</div>
          <textarea rows={3} value={texts[i]} onChange={(ev) => setTexts((list) => list.map((t, n) => (n === i ? ev.target.value : t)))} />
        </div>
      ))}
      <div className="pf-actions">
        <input placeholder="What changed (saved with the new version)" value={note} onChange={(ev) => setNote(ev.target.value)} />
        <button type="button" className="btn primary" disabled={busy} onClick={async () => {
          const sections: Record<string, string> = {};
          v.sections.forEach((s, i) => { if (texts[i].trim()) sections[s.key] = texts[i]; });
          setBusy(true);
          try {
            const saved = await api<{ template_version: number }>("PUT", `/api/industries/${id}/template`, { sections, note: note.trim() });
            toast(`Template v${saved.template_version} is live.`, "info");
            await reload();
          } catch (err) { toast(errorText(err)); } finally { setBusy(false); }
        }}>Save as new version</button>
      </div>
    </>
  );
}

/* -------------------------------------------------------------------- base */

function BaseRules({ v, reload }: { v: BaseView; reload: () => Promise<void> }) {
  const toast = useToast();
  const { confirm } = useDialogs();
  const [rules, setRules] = useState(v.current.content.rules);
  const [note, setNote] = useState("");
  return (
    <>
      <p className="pf-note">Rendered first in every client&apos;s prompt and restated as taking precedence at the end.
        Saving makes a new version for all clients at once. The policy checks on outgoing replies run in code whatever
        this says.</p>
      <textarea rows={16} value={rules} onChange={(ev) => setRules(ev.target.value)} />
      <div className="pf-actions">
        <input placeholder="What changed (saved with the new version)" value={note} onChange={(ev) => setNote(ev.target.value)} />
        <button type="button" className="btn primary" onClick={async () => {
          if (!(await confirm("This changes the prompt of every client. Save?"))) return;
          try {
            await api("PUT", "/api/platform/base", { rules, note: note.trim() });
            await reload();
            toast("Platform rules saved.", "info");
          } catch (err) { toast(errorText(err)); }
        }}>Save as new version</button>
      </div>
    </>
  );
}

// llm_prices: what each model costs per 1M tokens, used for every client's
// spend limits. A model missing here (the vision model, say) is costed at
// the highest listed rate.
function BasePrices() {
  const toast = useToast();
  const [text, setText] = useState("Loading…");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    api("GET", "/api/platform/prices")
      .then((prices) => setText(JSON.stringify(prices, null, 2)))
      .catch((err) => setError(errorText(err)));
  }, []);
  return (
    <>
      <p className="pf-note">Per 1M tokens, in the currency below. Every client&apos;s AI spend and its limits are worked
        out from these. Add a row for the vision model; one that is missing is costed at the highest listed rate.</p>
      <textarea className="mono" rows={16} value={text} onChange={(ev) => setText(ev.target.value)} />
      <div className="pf-errors">{error}</div>
      <div className="pf-actions">
        <button type="button" className="btn primary" disabled={busy} onClick={async () => {
          setError("");
          let body: unknown;
          try { body = JSON.parse(text); } catch { setError("That is not valid JSON."); return; }
          setBusy(true);
          try {
            setText(JSON.stringify(await api("PUT", "/api/platform/prices", body), null, 2));
            toast("Prices saved.", "info");
          } catch (err) { setError(errorText(err)); } finally { setBusy(false); }
        }}>Save prices</button>
      </div>
    </>
  );
}

/* -------------------------------------------------------------------- page */

export function Clients({ initialNode, initialTab }: { initialNode: TreeNode | null; initialTab?: string }) {
  const toast = useToast();
  const { confirm, prompt } = useDialogs();
  const askReason = useAskReason();
  const [node, setNode] = useState<TreeNode | null>(initialNode);
  const [tab, setTab] = useState<string>(initialNode ? tabFor(initialNode.kind, initialTab) : "");
  const [view, setView] = useState<AnyView | null>(null);
  // Bumped on every load, so editors start over from what the server has now.
  const [generation, setGeneration] = useState(0);

  const fetchTree = useCallback(() => api<PlatformTree>("GET", "/api/platform/tree"), []);
  const { data: tree, error: treeError, reload: loadTree } = useLoader(fetchTree);

  const fetchDetail = useCallback((target: TreeNode) => api<AnyView>("GET",
    target.kind === "tenant" ? `/api/tenants/${target.id}`
      : target.kind === "industry" ? `/api/industries/${target.id}` : "/api/platform/base"), []);

  const showView = useCallback((v: AnyView) => { setView(v); setGeneration((g) => g + 1); }, []);

  // After a change: what the server has now, for the node that is open.
  const loadDetail = useCallback(async () => {
    if (!node) return;
    try { showView(await fetchDetail(node)); } catch (err) { toast(errorText(err)); }
  }, [node, fetchDetail, showView, toast]);

  useEffect(() => {
    if (!node) return;
    let cancelled = false;
    fetchDetail(node).then((v) => { if (!cancelled) showView(v); }, (err) => toast(errorText(err)));
    return () => { cancelled = true; };
  }, [node, fetchDetail, showView, toast]);

  // The address says what is open, so a reload or a shared link lands there.
  useEffect(() => {
    if (!node) return;
    const key = node.kind === "base" ? "base" : `${node.kind}-${node.id}`;
    window.history.replaceState(null, "", `/clients?node=${key}&tab=${tab}`);
  }, [node, tab]);

  const select = (target: TreeNode, targetTab?: string) => {
    setView(null);
    setNode(target);
    setTab(tabFor(target.kind, targetTab));
  };

  const reload = async () => { await loadTree(); await loadDetail(); };
  const setTenantView = (v: TenantView) => showView(v);

  const patchTenant = async (id: number, body: Record<string, unknown>) => {
    try {
      await api("PATCH", `/api/tenants/${id}`, body);
      await reload();
      toast("Saved.", "info");
    } catch (err) { toast(errorText(err)); }
  };

  /* ---------------------------------------------------------- the tree */

  const nav = tree && (
    <nav id="pf-tree">
      <button type="button" className={cx("pf-node folder", sameNode(node, { kind: "base", id: 0 }) && "active")}
              onClick={() => select({ kind: "base", id: 0 })}>
        Platform rules <span className="count">v{tree.base_version}</span>
      </button>
      {tree.industries.map((industry) => {
        const clients = tree.tenants.filter((c) => c.industry_id === industry.id);
        return (
          <div key={industry.id}>
            <button type="button" className={cx("pf-node folder", sameNode(node, { kind: "industry", id: industry.id }) && "active")}
                    onClick={() => select({ kind: "industry", id: industry.id })}>
              📁 {industry.name} <span className="count">{clients.length}</span>
            </button>
            {clients.map((client) => (
              <button key={client.id} type="button"
                      className={cx("pf-node client", sameNode(node, { kind: "tenant", id: client.id }) && "active")}
                      onClick={() => select({ kind: "tenant", id: client.id })}>
                {client.name}
                {client.status !== "active" && <span className="count">{client.status}</span>}
              </button>
            ))}
          </div>
        );
      })}
      <div className="pf-tree-actions">
        <button type="button" className="btn small" onClick={async () => {
          const name = ((await prompt("Name of the new industry:")) || "").trim();
          if (!name) return;
          try {
            const created = await api<{ id: number }>("POST", "/api/industries", { name });
            await loadTree();
            select({ kind: "industry", id: created.id });
          } catch (err) { toast(errorText(err)); }
        }}>+ New industry</button>
      </div>
    </nav>
  );

  /* -------------------------------------------------------- the detail */

  let crumb = "";
  let detail: React.ReactNode = <div className="empty">Pick a client, an industry, or the platform rules on the left.</div>;

  if (node && view && tree) {
    const tabs = TABS[node.kind] as readonly (readonly [string, string])[];
    let head: React.ReactNode = null;
    let body: React.ReactNode = null;

    if (node.kind === "tenant" && "tenant" in view) {
      const v = view;
      crumb = `${v.industry.name} › ${v.tenant.name}`;
      const props: TenantProps = { v, id: node.id, setView: setTenantView, reload: () => loadDetail() };
      head = (
        <div className="pf-head">
          <h3>{v.tenant.name}</h3>
          <span className="tag">{v.tenant.status}</span>
          {v.tenant.session_id && <span className="muted">{v.tenant.session_id}</span>}
          <span className="spacer" />
          <button type="button" className="btn small" onClick={async () => {
            const name = ((await prompt("New name for this client:", v.tenant.name)) || "").trim();
            if (!name || name === v.tenant.name) return;
            await patchTenant(node.id, { name, reason: "renamed" });
          }}>Rename</button>
          <select value={String(v.tenant.industry_id)} onChange={async (ev) => {
            const target = tree.industries.find((i) => String(i.id) === ev.target.value);
            if (!target) return;
            if (!(await confirm(`Move ${v.tenant.name} to ${target.name}? It will inherit that industry's template and ` +
                                "defaults, and any pinned template version is released."))) return;
            await patchTenant(node.id, { industry_id: target.id, reason: "moved to " + target.name });
          }}>
            {tree.industries.map((i) => <option key={i.id} value={String(i.id)}>Industry: {i.name}</option>)}
          </select>
        </div>
      );
      body = {
        config: <ConfigEditor key={generation} fields={v.config.fields} overrides={v.config.overrides} layer="client"
                              inheritedLabel={`the ${v.industry.name} industry or the platform defaults`}
                              revision={v.config.revision}
                              save={async (b) => setTenantView(await api<TenantView>("PUT", `/api/tenants/${node.id}/config`, b))} />,
        prompt: <TenantPrompt key={generation} {...props} />,
        versions: <TenantVersions key={generation} {...props} />,
        preview: <>
          <p className="pf-note">Exactly what the model is given before the conversation, as {v.prompt.version_tag} (platform
            rules / industry template / client version). Per-reply notes (appointments, media, style) are added at runtime.</p>
          <pre className="pf-rendered">{v.prompt.rendered}</pre>
        </>,
        assist: <TenantAssist {...props} onApplied={() => setTab("config")} />,
        audit: <TenantAudit id={node.id} />,
      }[tab];
    } else if (node.kind === "industry" && "sections" in view) {
      const v = view;
      crumb = v.industry.name;
      head = (
        <div className="pf-head">
          <h3>{v.industry.name}</h3>
          <span className="muted">live template v{v.industry.template_version} · {v.tenants.length} client(s)</span>
        </div>
      );
      body = {
        template: <IndustryTemplate key={generation} v={v} id={node.id} reload={() => loadDetail()} />,
        config: <ConfigEditor key={generation} fields={v.config.fields} overrides={v.config.overrides} layer="industry"
                              inheritedLabel="the platform defaults" revision={v.config.revision}
                              save={async (b) => { await api("PUT", `/api/industries/${node.id}/config`, b); await loadDetail(); }} />,
        versions: <VersionList versions={v.versions} current={v.industry.template_version} actionLabel="Make live"
                               onPick={async (version) => {
                                 const reason = await askReason(`making v${version} live`);
                                 if (reason === null) return;
                                 try {
                                   await api("POST", `/api/industries/${node.id}/template/rollback`, { version, reason });
                                   await loadDetail();
                                 } catch (err) { toast(errorText(err)); }
                               }} />,
        clients: (
          <div className="pf-list">
            {!v.tenants.length && <p className="pf-note">No clients in this industry.</p>}
            {v.tenants.map((t) => (
              <div key={t.id} className="v">
                <div className="grow">{t.name}</div>
                <span className="muted">{t.prompt_pin_version ? `pinned to v${t.prompt_pin_version}` : "follows live"}</span>
                <button type="button" className="btn small" onClick={() => select({ kind: "tenant", id: t.id })}>Open</button>
              </div>
            ))}
          </div>
        ),
      }[tab];
    } else if (node.kind === "base" && "current" in view) {
      const v = view;
      crumb = "Platform rules";
      head = (
        <div className="pf-head">
          <h3>Platform rules</h3>
          <span className="muted">v{v.current.version} · first in every client&apos;s prompt; nothing below can override them</span>
        </div>
      );
      body = {
        rules: <BaseRules key={generation} v={v} reload={reload} />,
        versions: <VersionList versions={v.versions} current={v.current.version} actionLabel="Use this version"
                               onPick={async (version) => {
                                 const reason = await askReason(`switching every client to platform rules v${version}`);
                                 if (reason === null) return;
                                 try {
                                   await api("POST", "/api/platform/base/rollback", { version, reason });
                                   await reload();
                                 } catch (err) { toast(errorText(err)); }
                               }} />,
        prices: <BasePrices />,
      }[tab];
    }

    detail = <>
      {head}
      <Tabs tabs={tabs} value={tab} onChange={setTab} />
      <div>{body}</div>
    </>;
  }

  return (
    <PageShell title="Clients" crumb={crumb}>
      <div className="pf-body">
        {nav}
        <section id="pf-detail">{treeError && <div className="pf-errors">{treeError}</div>}{detail}</section>
      </div>
    </PageShell>
  );
}
