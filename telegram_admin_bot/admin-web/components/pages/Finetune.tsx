"use client";

// Finetune from chat screenshots (finetune_api.py). Pick a client, add
// screenshots of its real chats (marked good or bad if you like), and the
// vision model proposes the client's prompt layer and an updated industry
// standard from the industry's finetune template. Nothing changes until a
// run is applied; applying saves ordinary prompt versions, which the
// Clients page can roll back. The screenshots are never stored.

import { useCallback, useEffect, useMemo, useState } from "react";
import { useDialogs, useToast } from "@/components/feedback";
import { PageShell, Tabs } from "@/components/ui";
import { api, errorText } from "@/lib/api";
import { cx, fmtDateTime } from "@/lib/format";
import type { BusinessLayer, FinetuneIndustry, FinetuneRun, PlatformTree } from "@/lib/types";
import { useLoader } from "@/lib/useLoader";
import "@/app/finetune.css";

type Tab = "new" | "runs" | "template";
type Mark = "" | "good" | "bad";
type Shot = { file: File; base: string; ext: string; mark: Mark };
type Mode = "inherit" | "override" | "append";

const MAX_IMAGE_BYTES = 5 * 1024 * 1024;
const MAX_IMAGES = 40;
const STATUS: Record<FinetuneRun["status"], string> = {
  running: "reading the screenshots…", done: "ready to review", failed: "failed", applied: "applied", discarded: "discarded",
};
const POLL_MS = 3000;

/** "03a-good.png" → base "03a", mark "good", ext ".png". */
function splitName(name: string): Omit<Shot, "file"> {
  const dot = name.lastIndexOf(".");
  const ext = dot > 0 ? name.slice(dot) : "";
  let base = dot > 0 ? name.slice(0, dot) : name;
  const m = base.match(/^(.*?)[-_ ](good|bad)$/i);
  let mark: Mark = "";
  if (m) { base = m[1]; mark = m[2].toLowerCase() as Mark; }
  return { base, ext, mark };
}

function shotName(s: Shot): string {
  return `${s.base}${s.mark ? "-" + s.mark : ""}${s.ext}`;
}

/** Natural order, the same as the server's: 2 before 10, 03a before 03b. */
function naturalCompare(a: string, b: string): number {
  return a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
}

function readBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).replace(/^data:[^,]*,/, ""));
    reader.onerror = () => reject(reader.error ?? new Error(`Could not read ${file.name}`));
    reader.readAsDataURL(file);
  });
}

/* ---------------------------------------------------------------- new run */

function NewRun({ tenantId, info, onStarted }: {
  tenantId: number; info: FinetuneIndustry; onStarted: (run: FinetuneRun) => void;
}) {
  const toast = useToast();
  const [shots, setShots] = useState<Shot[]>([]);
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);

  const add = (files: FileList | null) => {
    if (!files) return;
    const fresh: Shot[] = [];
    for (const file of Array.from(files)) {
      if (!file.type.startsWith("image/")) { toast(`${file.name} is not an image.`); continue; }
      if (file.size > MAX_IMAGE_BYTES) { toast(`${file.name} is larger than 5 MB.`); continue; }
      fresh.push({ file, ...splitName(file.name) });
    }
    setShots((list) => {
      const known = new Set(list.map((s) => s.file.name));
      return [...list, ...fresh.filter((s) => !known.has(s.file.name))]
        .sort((a, b) => naturalCompare(a.file.name, b.file.name));
    });
  };

  const start = async () => {
    if (!shots.length) { toast("Add the screenshots first."); return; }
    if (shots.length > MAX_IMAGES) { toast(`At most ${MAX_IMAGES} screenshots per run.`); return; }
    const names = shots.map(shotName);
    if (new Set(names).size !== names.length) { toast("Two screenshots have the same name; rename one."); return; }
    setBusy(true);
    try {
      const images = await Promise.all(shots.map(async (s) => ({ name: shotName(s), data: await readBase64(s.file) })));
      const run = await api<FinetuneRun>("POST", "/api/finetune/runs", { tenant_id: tenantId, images });
      setShots([]);
      onStarted(run);
    } catch (err) { toast(errorText(err)); } finally { setBusy(false); }
  };

  if (!info.template.trim()) {
    return (
      <div className="empty">Write the finetune template for {info.industry.name} first (Template tab)
        {info.default_template ? ", or load the default there and save it" : ""}.</div>
    );
  }
  return (
    <>
      <p className="pf-note">
        Screenshots of one business&apos;s real chats. Name them so they sort in order: the same number is one
        conversation (03a, 03b, 03c). Mark a screenshot <b>good</b> (handled the way the business wants) or <b>bad</b> (a
        reply not to repeat); the mark is added to its file name for the model. The screenshots are sent to the vision
        model and not stored. The {info.industry.name} standard is built from {info.businesses_so_far} business
        {info.businesses_so_far === 1 ? "" : "es"} so far{info.has_standard ? "" : " (none yet: this run writes the first)"}.
      </p>
      <label className={cx("ft-drop", dragging && "on")}
             onDragOver={(ev) => { ev.preventDefault(); setDragging(true); }}
             onDragLeave={() => setDragging(false)}
             onDrop={(ev) => { ev.preventDefault(); setDragging(false); add(ev.dataTransfer.files); }}>
        <input type="file" accept="image/png,image/jpeg,image/webp,image/gif" multiple hidden
               onChange={(ev) => { add(ev.target.files); ev.target.value = ""; }} />
        Drop screenshots here, or click to choose
      </label>
      {shots.length > 0 && (
        <div className="ft-shots">
          {shots.map((s, i) => (
            <div key={s.file.name} className={cx("ft-shot", s.mark && `mark-${s.mark}`)}>
              <span className="grow">{shotName(s)}</span>
              <span className="muted">{Math.ceil(s.file.size / 1024)} KB</span>
              <select value={s.mark} aria-label={`Mark ${s.file.name}`}
                      onChange={(ev) => setShots((list) => list.map((x, n) => (n === i ? { ...x, mark: ev.target.value as Mark } : x)))}>
                <option value="">no mark</option>
                <option value="good">good</option>
                <option value="bad">bad</option>
              </select>
              <button type="button" className="btn small" onClick={() => setShots((list) => list.filter((_, n) => n !== i))}>
                Remove</button>
            </div>
          ))}
        </div>
      )}
      <div className="pf-actions">
        <span className="muted grow">{shots.length} screenshot{shots.length === 1 ? "" : "s"}</span>
        <button type="button" className="btn" disabled={!shots.length || busy} onClick={() => setShots([])}>Clear</button>
        <button type="button" className="btn primary" disabled={!shots.length || busy} onClick={start}>
          {busy ? "Sending…" : "Start the run"}</button>
      </div>
    </>
  );
}

/* ------------------------------------------------------------- review run */

type Editor = { key: string; heading: string; appendOnly: boolean; industry: string; mode: Mode; text: string };

function editorsFor(run: FinetuneRun): Editor[] {
  const proposedIndustry = run.result?.industry_sections ?? null;
  const proposedLayer = run.result?.business_layer?.overrides ?? {};
  return (run.sections ?? []).map((s) => {
    const o = proposedLayer[s.key];
    // A boundaries override is refused on save; offer it as the append it can be.
    const mode: Mode = !o ? "inherit" : s.append_only && o.mode === "override" ? "append" : o.mode;
    return {
      key: s.key, heading: s.heading, appendOnly: s.append_only,
      industry: proposedIndustry ? (proposedIndustry[s.key] ?? "") : (run.current?.industry_sections[s.key] ?? ""),
      mode, text: o?.text ?? "",
    };
  });
}

function RunReview({ run, onChanged }: { run: FinetuneRun; onChanged: (run: FinetuneRun) => void }) {
  const toast = useToast();
  const { confirm } = useDialogs();
  const [editors, setEditors] = useState<Editor[]>(() => editorsFor(run));
  const [addendum, setAddendum] = useState(run.result?.business_layer?.addendum ?? "");
  const [applyIndustry, setApplyIndustry] = useState(!!run.result?.industry_sections);
  const [applyBusiness, setApplyBusiness] = useState(!!run.result?.business_layer);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const current = run.current;
  const reviewable = run.status === "done" && !run.stale;

  const edit = (i: number, patch: Partial<Editor>) =>
    setEditors((list) => list.map((e, n) => (n === i ? { ...e, ...patch } : e)));

  const apply = async () => {
    const industry_sections: Record<string, string> = {};
    const overrides: BusinessLayer["overrides"] = {};
    for (const e of editors) {
      if (e.industry.trim()) industry_sections[e.key] = e.industry;
      if (e.mode !== "inherit" && e.text.trim()) overrides[e.key] = { mode: e.mode, text: e.text };
    }
    const what = [applyIndustry && "the industry standard (every client in the industry)", applyBusiness && "this client's layer"]
      .filter(Boolean).join(" and ");
    if (!what) { toast("Choose what to apply."); return; }
    if (!(await confirm(`Apply ${what} as new prompt versions? They go live at once and can be rolled back under Clients.`))) return;
    setBusy(true);
    try {
      onChanged(await api<FinetuneRun>("POST", `/api/finetune/runs/${run.id}/apply`, {
        industry_sections: applyIndustry ? industry_sections : null,
        business_layer: applyBusiness ? { overrides, addendum } : null,
        note: note.trim(),
      }));
      toast("Applied.", "info");
    } catch (err) { toast(errorText(err)); } finally { setBusy(false); }
  };

  const discard = async () => {
    if (!(await confirm("Discard this run? Nothing it proposed is saved."))) return;
    try { onChanged(await api<FinetuneRun>("POST", `/api/finetune/runs/${run.id}/discard`)); }
    catch (err) { toast(errorText(err)); }
  };

  return (
    <>
      <div className="rv-top">
        <span className={cx("bk-state", `ft-${run.status}`)}>{STATUS[run.status]}</span>
        <span>Run {run.id}</span>
        <span className="muted">{fmtDateTime(run.created_at)} · {run.model} · {run.files.length} screenshots</span>
      </div>
      <details className="ft-files"><summary className="muted">Screenshots</summary>{run.files.join(", ")}</details>
      {run.status === "running" && <div className="empty">The model is reading the screenshots. This can take a few minutes.</div>}
      {run.error && <div className="pf-errors">{run.error}</div>}
      {run.status === "applied" && run.applied && (
        <p className="pf-note">Saved{run.applied.industry_version ? ` industry template v${run.applied.industry_version}` : ""}
          {run.applied.industry_version && run.applied.client_version ? " and" : ""}
          {run.applied.client_version ? ` client version ${run.applied.client_version}` : ""}, {fmtDateTime(run.applied_at)}.</p>
      )}
      {run.stale && run.status === "done" && <div className="pf-errors">{run.stale}</div>}

      {run.result && <>
        {run.result.errors.length > 0 && (
          <div className="pf-section danger">
            <div className="title">Problems in the answer</div>
            <ul className="ft-list">{run.result.errors.map((e) => <li key={e}>{e}</li>)}</ul>
            <p className="pf-note">Fix them below before applying, or start a new run.</p>
          </div>
        )}
        {run.result.notes && (
          <div className="pf-section">
            <div className="title">Notes for the operator</div>
            <div className="inherited-text">{run.result.notes}</div>
          </div>
        )}

        {reviewable && <>
          <div className="sub">Proposal, section by section</div>
          <p className="pf-note">Left: the industry standard, applied to every client in the industry. Right: this
            client&apos;s own layer on top of it. Grey text is what is live now.</p>
          {editors.map((e, i) => {
            const live = current?.industry_sections[e.key] ?? "";
            const liveOverride = current?.business_layer?.overrides?.[e.key];
            return (
              <div key={e.key} className="pf-section ft-pair">
                <div className="title">{e.heading}</div>
                <div className="ft-cols">
                  <div>
                    <div className="rv-label">Industry standard</div>
                    <div className="inherited-text">{live || "(empty now)"}</div>
                    <textarea rows={4} value={e.industry} disabled={!applyIndustry}
                              onChange={(ev) => edit(i, { industry: ev.target.value })} />
                  </div>
                  <div>
                    <div className="rv-label ft-mode">This client
                      <select value={e.mode} disabled={!applyBusiness}
                              onChange={(ev) => edit(i, { mode: ev.target.value as Mode })}>
                        <option value="inherit">Inherit</option>
                        {!e.appendOnly && <option value="override">Override</option>}
                        <option value="append">Append</option>
                      </select>
                    </div>
                    <div className="inherited-text">
                      {liveOverride ? `${liveOverride.mode}: ${liveOverride.text}` : "(inherits now)"}</div>
                    {e.mode !== "inherit" && (
                      <textarea rows={4} value={e.text} disabled={!applyBusiness}
                                onChange={(ev) => edit(i, { text: ev.target.value })} />
                    )}
                  </div>
                </div>
              </div>
            );
          })}
          <div className="pf-section">
            <div className="title">Additional notes from the business (this client)</div>
            {current?.business_layer?.addendum && <div className="inherited-text">{current.business_layer.addendum}</div>}
            <textarea rows={3} maxLength={run.addendum_limit} value={addendum} disabled={!applyBusiness}
                      onChange={(ev) => setAddendum(ev.target.value)} />
            <div className="pf-note">{addendum.length} / {run.addendum_limit} characters</div>
          </div>

          <div className="pf-actions">
            <label className="check"><input type="checkbox" checked={applyIndustry}
                                            onChange={(ev) => setApplyIndustry(ev.target.checked)} /> Apply the industry standard</label>
            <label className="check"><input type="checkbox" checked={applyBusiness}
                                            onChange={(ev) => setApplyBusiness(ev.target.checked)} /> Apply this client&apos;s layer</label>
          </div>
          <div className="pf-actions">
            <input placeholder="Note saved with the new versions (optional)" value={note} onChange={(ev) => setNote(ev.target.value)} />
            <button type="button" className="btn" disabled={busy} onClick={discard}>Discard</button>
            <button type="button" className="btn primary" disabled={busy || (!applyIndustry && !applyBusiness)} onClick={apply}>
              Apply</button>
          </div>
        </>}
      </>}
      {(run.status === "failed" || (run.status === "done" && run.stale)) && (
        <div className="pf-actions"><button type="button" className="btn" onClick={discard}>Discard</button></div>
      )}
      {run.raw_output && (
        <details className="ft-raw"><summary className="muted">The model&apos;s full answer</summary>
          <pre className="pf-rendered">{run.raw_output}</pre></details>
      )}
    </>
  );
}

function RunView({ runId, onBack, onChanged }: { runId: number; onBack: () => void; onChanged: () => void }) {
  const fetchRun = useCallback(() => api<FinetuneRun>("GET", `/api/finetune/runs/${runId}`), [runId]);
  const { data: run, error, reload } = useLoader(fetchRun);
  const running = run?.status === "running";

  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => { void reload(); }, POLL_MS);
    return () => clearInterval(timer);
  }, [running, reload]);

  return (
    <>
      <div className="pf-actions ft-back"><button type="button" className="btn small" onClick={onBack}>← All runs</button></div>
      {error && <div className="pf-errors">{error}</div>}
      {run && <RunReview key={`${run.id}-${run.status}`} run={run}
                         onChanged={() => { void reload(); onChanged(); }} />}
    </>
  );
}

/* ----------------------------------------------------------------- runs */

function RunList({ runs, onOpen }: { runs: FinetuneRun[]; onOpen: (id: number) => void }) {
  if (!runs.length) return <div className="empty">No runs for this client yet.</div>;
  return (
    <>
      {runs.map((r) => (
        <div key={r.id} className={cx("bk-row", `ft-row-${r.status}`)}>
          <div className="bk-head" role="button" tabIndex={0} onClick={() => onOpen(r.id)}
               onKeyDown={(ev) => { if (ev.key === "Enter") onOpen(r.id); }}>
            <span className={cx("bk-state", `ft-${r.status}`)}>{STATUS[r.status]}</span>
            <span className="bk-time">{fmtDateTime(r.created_at)}</span>
            <span>Run {r.id}</span>
            <span className="muted">{r.files.length} screenshots · {r.model}</span>
          </div>
        </div>
      ))}
    </>
  );
}

/* -------------------------------------------------------------- template */

function TemplateEditor({ info, onSaved }: { info: FinetuneIndustry; onSaved: (info: FinetuneIndustry) => void }) {
  const toast = useToast();
  const { prompt, confirm } = useDialogs();
  const [text, setText] = useState(info.template);
  const [busy, setBusy] = useState(false);
  const missing = info.placeholders.filter((p) => !text.includes(p));

  const loadDefault = async () => {
    if (text.trim() && !(await confirm(`Replace the text in the editor with the default ${info.industry.name} template? `
      + "Nothing is saved until you press Save template."))) return;
    setText(info.default_template);
  };

  return (
    <>
      <p className="pf-note">The instructions sent with the screenshots, for every client in {info.industry.name}. These
        are filled in for each run: {info.placeholders.map((p) => <code key={p}>{p} </code>)}— the business name, how
        many businesses the standard is built from, and the current industry standard (&quot;none&quot; when there is
        none). The answer must be the three blocks === BUSINESS LAYER ===, === INDUSTRY STANDARD === and === NOTES FOR
        OPERATOR ===.</p>
      <textarea className="mono" rows={24} value={text} onChange={(ev) => setText(ev.target.value)} />
      {missing.length > 0 && text.trim() && (
        <div className="pf-errors">Not in the template: {missing.join(", ")}. The model won&apos;t get that information.</div>
      )}
      <div className="pf-actions">
        <span className="muted grow">{text.length.toLocaleString()} characters</span>
        {info.default_template && (
          <button type="button" className="btn" disabled={busy || text === info.default_template} onClick={loadDefault}>
            Load the default</button>
        )}
        <button type="button" className="btn primary" disabled={busy || text === info.template} onClick={async () => {
          const reason = await prompt("Reason for changing the finetune template (goes into the audit log):", "");
          if (reason === null) return;
          setBusy(true);
          try {
            onSaved(await api<FinetuneIndustry>("PUT", `/api/finetune/industries/${info.industry.id}/template`,
              { template: text, reason: reason.trim() }));
            toast("Template saved.", "info");
          } catch (err) { toast(errorText(err)); } finally { setBusy(false); }
        }}>Save template</button>
      </div>
    </>
  );
}

/* ------------------------------------------------------------------ page */

export function Finetune() {
  const fetchTree = useCallback(() => api<PlatformTree>("GET", "/api/platform/tree"), []);
  const { data: tree, error: treeError } = useLoader(fetchTree);
  const [tenantId, setTenantId] = useState<number | null>(null);
  const [tab, setTab] = useState<Tab>("new");
  const [openRun, setOpenRun] = useState<number | null>(null);

  const tenant = useMemo(() => {
    if (!tree?.tenants.length) return null;
    return tree.tenants.find((t) => t.id === tenantId) ?? tree.tenants[0];
  }, [tree, tenantId]);
  const industryName = tree?.industries.find((i) => i.id === tenant?.industry_id)?.name ?? "";

  const fetchInfo = useCallback(async () => (tenant
    ? api<FinetuneIndustry>("GET", `/api/finetune/industries/${tenant.industry_id}`) : null), [tenant]);
  const { data: info, error: infoError, reload: reloadInfo } = useLoader(fetchInfo);
  const fetchRuns = useCallback(async () => (tenant
    ? api<FinetuneRun[]>("GET", `/api/finetune/runs?tenant_id=${tenant.id}`) : []), [tenant]);
  const { data: runs, error: runsError, reload: reloadRuns } = useLoader(fetchRuns);

  const tabs: [Tab, string][] = [["new", "New run"], ["runs", `Runs${runs?.length ? ` (${runs.length})` : ""}`],
    ["template", `Template${industryName ? ` — ${industryName}` : ""}`]];
  const error = treeError || infoError || runsError;

  return (
    <PageShell title="Finetune" crumb="a client's prompt from screenshots of its chats" width="w-980">
      <div className="bk-scroll">
        {error && <div className="pf-errors">{error}</div>}
        {tree && !tree.tenants.length && <div className="empty">No clients yet.</div>}
        {tenant && <>
          <div className="bk-nav">
            <label className="muted" htmlFor="ft-client">Client</label>
            <select id="ft-client" value={String(tenant.id)}
                    onChange={(ev) => { setTenantId(Number(ev.target.value)); setOpenRun(null); }}>
              {tree?.tenants.map((t) => <option key={t.id} value={String(t.id)}>{t.name}</option>)}
            </select>
            <span className="muted">{industryName}</span>
          </div>
          <Tabs tabs={tabs} value={tab} onChange={(key) => { setTab(key); setOpenRun(null); }} />
          {info && tab === "new" && (
            <NewRun key={tenant.id} tenantId={tenant.id} info={info} onStarted={(run) => {
              void reloadRuns();
              setTab("runs");
              setOpenRun(run.id);
            }} />
          )}
          {tab === "runs" && (openRun !== null
            ? <RunView runId={openRun} onBack={() => setOpenRun(null)}
                       onChanged={() => { void reloadRuns(); void reloadInfo(); }} />
            : <RunList runs={runs ?? []} onOpen={setOpenRun} />)}
          {info && tab === "template" && (
            <TemplateEditor key={`${info.industry.id}-${info.template.length}`} info={info}
                            onSaved={() => { void reloadInfo(); }} />
          )}
        </>}
      </div>
    </PageShell>
  );
}
