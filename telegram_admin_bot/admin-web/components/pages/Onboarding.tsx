"use client";

// "New client": a step-by-step setup over routes the panel already has:
// the account sign-in, PATCH /api/tenants/{id} for the name and industry,
// PUT /api/tenants/{id}/config for the settings (every save validated and
// audited there). Its only route of its own is the read-only status summary
// (/api/onboarding), which is what makes the wizard resumable: every step's
// "done" is derived from what is stored, so leaving the wizard, or changing
// a setting under Clients, loses nothing.
//
// The wizard writes no prompt text: the business sections are the platform
// owner's to fill in under Clients → Prompt.

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useId, useState } from "react";
import { useDialogs, useToast } from "@/components/feedback";
import { PageShell } from "@/components/ui";
import { api, ApiError, errorText } from "@/lib/api";
import { cx } from "@/lib/format";
import { usePanel } from "@/lib/panel";
import type { OnboardingStatus, PlatformTree, TenantView, TreeIndustry } from "@/lib/types";

type Step = "account" | "business" | "settings" | "staging" | "prompt" | "live" | "done";

const STEPS: [Exclude<Step, "done">, string][] = [["account", "Telegram account"], ["business", "Business"],
  ["settings", "Key settings"], ["staging", "Staging"], ["prompt", "Prompt check"], ["live", "Go live"]];

// The wizard keeps its place while you look at something else in the panel.
const memory: { tenantId: number | null; step: Step; promptSeen: Set<number> } =
  { tenantId: null, step: "account", promptSeen: new Set() };

type Effective = {
  timezone: string;
  auto_send: boolean;
  daily_message_cap: number | null;
  quiet_hours: { enabled: boolean; start: string; end: string };
  booking: { enabled: boolean; provider: string };
  staging: { enabled: boolean; test_chats: string[] };
};

function isObject(v: unknown): v is Record<string, unknown> {
  return !!v && typeof v === "object" && !Array.isArray(v);
}

// Nested objects merge; anything else (lists included) replaces.
function deepMerge(base: unknown, patch: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = isObject(base) ? structuredClone(base) : {};
  for (const [key, value] of Object.entries(patch)) {
    out[key] = isObject(value) && isObject(out[key]) ? deepMerge(out[key], value) : value;
  }
  return out;
}

/** Validation errors ({path, message}) by the field they are about; the rest as one line. */
function splitErrors(err: unknown, paths: string[]): { fields: Record<string, string>; loose: string } {
  const fields: Record<string, string> = {};
  if (!(err instanceof ApiError) || !err.errors) return { fields, loose: errorText(err) };
  const loose: string[] = [];
  for (const e of err.errors) {
    const hit = paths.find((p) => p === e.path) || paths.find((p) => p.startsWith(e.path + "."))
      || paths.find((p) => e.path.startsWith(p + "."));
    if (hit) fields[hit] = e.message; else loose.push(`${e.path}: ${e.message}`);
  }
  return { fields, loose: loose.length ? loose.join(" · ") : "Please fix the fields marked below." };
}

function Field({ label, hint, error, children }: { label: string; hint?: string; error?: string; children: React.ReactNode }) {
  return (
    <div className="field">
      <label>{label}{children}</label>
      {hint && <div className="ob-hint">{hint}</div>}
      <div className="ob-err">{error || ""}</div>
    </div>
  );
}

function Check({ label, checked, onChange, error }: { label: string; checked: boolean; onChange: (v: boolean) => void; error?: string }) {
  const id = useId();
  return (
    <div className="field check">
      <input type="checkbox" checked={checked} onChange={(ev) => onChange(ev.target.checked)} id={id} />
      <label htmlFor={id}>{label}</label>
      <div className="ob-err">{error || ""}</div>
    </div>
  );
}

function zones(): string[] {
  try { return Intl.supportedValuesOf("timeZone"); } catch { return []; }
}

export function Onboarding() {
  const { selectSession, openAddAccount } = usePanel();
  const toast = useToast();
  const { confirm } = useDialogs();
  const router = useRouter();
  const [tenantId, setTenantIdState] = useState<number | null>(memory.tenantId);
  const [step, setStepState] = useState<Step>(memory.tenantId ? memory.step : "account");
  const [status, setStatus] = useState<OnboardingStatus | null>(null);
  const [view, setView] = useState<TenantView | null>(null);
  const [waiting, setWaiting] = useState(false);

  const setStep = (next: Step) => { memory.step = next; setStepState(next); };
  const setTenantId = (id: number | null) => { memory.tenantId = id; setTenantIdState(id); };

  const fetchTenant = useCallback((id: number) => Promise.all([
    api<OnboardingStatus>("GET", `/api/onboarding/${id}`),
    api<TenantView>("GET", `/api/tenants/${id}`),
  ]), []);

  const reload = useCallback(async (id: number) => {
    const [s, v] = await fetchTenant(id);
    setStatus(s);
    setView(v);
    return s;
  }, [fetchTenant]);

  const selectTenant = useCallback(async (id: number, wanted?: Step) => {
    setTenantId(id);
    try {
      const s = await reload(id);
      // Resume at the first step not done yet (the prompt check sits before
      // go-live, so a tenant in staging lands on it once).
      let next = (wanted || s.next_step || "live") as Step;
      if (!wanted && next === "live" && !memory.promptSeen.has(id)) next = "prompt";
      setStep(next);
    } catch (err) {
      toast(errorText(err));
      setTenantId(null);
      setStep("account");
    }
  }, [reload, toast]);

  // Once: pick up where the wizard was left, at the step it was on.
  useEffect(() => {
    const id = memory.tenantId;
    if (!id) return;
    fetchTenant(id).then(([s, v]) => { setStatus(s); setView(v); }, (err) => {
      toast(errorText(err));
      memory.tenantId = null;
      memory.step = "account";
      setTenantIdState(null);
      setStepState("account");
    });
  }, [fetchTenant, toast]);

  const next = (from: Step) => {
    const i = STEPS.findIndex(([key]) => key === from);
    setStep(i >= 0 && i < STEPS.length - 1 ? STEPS[i + 1][0] : "done");
  };

  // Merges `patch` into the client's current overrides and saves them with
  // the revision just read, so a change made elsewhere in between is refused
  // (409) rather than overwritten.
  const saveConfig = async (patch: Record<string, unknown>, reason: string) => {
    const current = await api<TenantView>("GET", `/api/tenants/${tenantId}`);
    setView(await api<TenantView>("PUT", `/api/tenants/${tenantId}/config`, {
      overrides: deepMerge(current.config.overrides || {}, patch), reason, expected_revision: current.config.revision,
    }));
    setStatus(await api<OnboardingStatus>("GET", `/api/onboarding/${tenantId}`));
  };

  const done = (key: Step) => {
    if (!status) return false;
    if (key === "prompt") return (tenantId !== null && memory.promptSeen.has(tenantId)) || !!status.steps.live;
    return !!status.steps[key];
  };

  const ready = step === "account" || step === "done" || (status && view);
  const effective = view?.config.effective as Effective | undefined;

  return (
    <PageShell title="New client" crumb={status?.name} width="w-820">
      <div className="bk-scroll top-pad">
        <div className="ob-progress">
          {STEPS.map(([key, label], i) => (
            <button key={key} type="button" className={cx("ob-step", done(key) && "done", step === key && "on")}
                    disabled={key !== "account" && !tenantId} onClick={() => setStep(key)}>
              <span className="ob-num">{done(key) ? "✓" : String(i + 1)}</span>
              <span className="ob-label">{label}</span>
            </button>
          ))}
        </div>

        {ready && step === "account" && (
          <AccountStep tenantId={tenantId} waiting={waiting} onPick={(id) => selectTenant(id)} onSignIn={async () => {
            setWaiting(true);
            const sessionId = await openAddAccount();
            setWaiting(false);
            if (!sessionId) return;
            try {
              const tenant = await api<{ id: number }>("GET", `/api/tenants/by-session/${encodeURIComponent(sessionId)}`);
              await selectTenant(tenant.id, "business");
            } catch (err) { toast(errorText(err)); }
          }} />
        )}

        {ready && step === "business" && status && (
          <BusinessStep key={tenantId} status={status} onSave={async (name, industryId) => {
            await api("PATCH", `/api/tenants/${tenantId}`, { name, industry_id: industryId, reason: "onboarding" });
            await reload(tenantId!);
            next("business");
          }} />
        )}

        {ready && step === "settings" && effective && (
          <SettingsStep key={`${tenantId}-${view?.config.revision}`} c={effective} onSave={async (patch) => {
            try {
              if (Object.keys(patch).length) await saveConfig(patch, "onboarding: key settings");
              next("settings");
            } catch (err) {
              if (err instanceof ApiError && err.status === 409) { try { await reload(tenantId!); } catch { /* keep */ } }
              throw err;
            }
          }} />
        )}

        {ready && step === "staging" && effective && status && (
          <StagingStep key={`${tenantId}-${view?.config.revision}`} st={effective.staging}
                       onSave={(list) => saveConfig({ staging: { enabled: true, test_chats: list } }, "onboarding: staging on")}
                       onSkip={() => next("staging")}
                       onOpenChats={status.session_id ? async () => {
                         await selectSession(status.session_id!);
                         router.push("/");
                         toast("The wizard keeps its place: open New client again to continue.", "info");
                       } : undefined} />
        )}

        {ready && step === "prompt" && view && <PromptStep view={view} tenantId={tenantId!} onNext={() => next("prompt")}
          onEdit={() => router.push(`/clients?node=tenant-${tenantId}&tab=prompt`)} />}

        {ready && step === "live" && status && effective && (
          <LiveStep status={status} c={effective} onFinish={() => setStep("done")} onGoLive={async () => {
            if (!(await confirm(`Go live for ${status.name}? The bot will answer every customer from now on.`))) return false;
            await saveConfig({ staging: { enabled: false } }, "onboarding: go live");
            setStep("done");
            return true;
          }} />
        )}

        {step === "done" && <>
          <div className="ob-state live">{status ? status.name : "The client"} is live.</div>
          <p className="pf-note">Next: give the owner a login to their dashboard under Client logins (one login can hold
            several of their accounts).</p>
          <div className="sheet-actions ob-actions">
            <button type="button" className="btn ob-big" onClick={() => {
              setTenantId(null); setStatus(null); setView(null); setStep("account");
            }}>Set up another client</button>
            <button type="button" className="btn primary ob-big" onClick={() => router.push("/")}>Close</button>
          </div>
        </>}
      </div>
    </PageShell>
  );
}

/* ------------------------------------------------------- 1. the account */

function AccountStep({ tenantId, waiting, onPick, onSignIn }: {
  tenantId: number | null; waiting: boolean; onPick: (id: number) => void; onSignIn: () => void;
}) {
  const [all, setAll] = useState<OnboardingStatus[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<OnboardingStatus[]>("GET", "/api/onboarding").then(setAll).catch((err) => setError(errorText(err)));
  }, [waiting]);

  const row = (s: OnboardingStatus) => {
    const count = STEPS.filter(([k]) => k !== "prompt" && s.steps[k]).length;
    const label = s.steps.live ? "live" : s.staging.enabled ? "staging" : s.configured ? "set up, not tested" : "not set up";
    return (
      <div key={s.tenant_id} className={cx("bk-row ob-tenant", s.tenant_id === tenantId && "open")}>
        <div className="bk-head" onClick={() => onPick(s.tenant_id)}>
          <b>{s.name}</b>
          <span className="bk-num">{s.session_label || s.session_id}</span>
          <span className="bk-state">{label} · {count}/5</span>
          {s.session_state && s.session_state !== "active" && <span className="warn-note">{s.session_state}</span>}
        </div>
      </div>
    );
  };

  const open = (all || []).filter((s) => s.session_id && !s.steps.live);
  const live = (all || []).filter((s) => s.session_id && s.steps.live);

  return (
    <>
      <p className="pf-note">Every client runs on its own Telegram account. Sign a new one in, or pick an account that is
        already signed in and not set up yet.</p>
      <button type="button" className="btn primary ob-big" onClick={onSignIn}>Sign in a new Telegram account</button>
      {waiting && <p className="pf-note">Finish the sign-in in the dialog; this step continues by itself when it is done.</p>}
      <h3 className="sf-sub">Accounts already signed in</h3>
      {error && <div className="pf-errors">{error}</div>}
      {all && !open.length && <p className="pf-note">None waiting to be set up.</p>}
      {open.map(row)}
      {live.length > 0 && (
        <details className="ob-live-list">
          <summary className="muted">Live clients ({live.length}), to change a step</summary>
          {live.map(row)}
        </details>
      )}
    </>
  );
}

/* ------------------------------------------------------ 2. the business */

function BusinessStep({ status, onSave }: { status: OnboardingStatus; onSave: (name: string, industryId: number) => Promise<void> }) {
  const [industries, setIndustries] = useState<TreeIndustry[] | null>(null);
  const [name, setName] = useState(status.steps.business ? status.name : "");
  const [industry, setIndustry] = useState(String(status.industry_id ?? ""));
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<PlatformTree>("GET", "/api/platform/tree").then((tree) => {
      setIndustries(tree.industries);
      setIndustry((v) => v || String(tree.industries[0]?.id ?? ""));
    }).catch((err) => setError(errorText(err)));
  }, []);

  if (!industries) return error ? <div className="pf-errors">{error}</div> : null;
  return (
    <>
      <Field label="Business name">
        <input type="text" maxLength={200} placeholder="The business name customers know" value={name}
               onChange={(ev) => setName(ev.target.value)} />
      </Field>
      <Field label="Industry" hint="Decides the default settings and the prompt template it starts from.">
        <select value={industry} onChange={(ev) => setIndustry(ev.target.value)}>
          {industries.map((i) => <option key={i.id} value={i.id}>{i.name}</option>)}
        </select>
      </Field>
      <div className="pf-errors">{error}</div>
      <div className="sheet-actions ob-actions">
        <button type="button" className="btn primary ob-big" disabled={busy} onClick={async () => {
          setError("");
          if (!name.trim()) { setError("Enter the business name."); return; }
          setBusy(true);
          try { await onSave(name.trim(), Number(industry)); } catch (err) { setError(errorText(err)); }
          finally { setBusy(false); }
        }}>Save and continue</button>
      </div>
    </>
  );
}

/* ------------------------------------------------------ 3. key settings */

const SETTING_PATHS = ["timezone", "booking.provider", "booking.enabled", "auto_send", "quiet_hours.enabled",
                       "quiet_hours.start", "quiet_hours.end", "daily_message_cap"];

function SettingsStep({ c, onSave }: { c: Effective; onSave: (patch: Record<string, unknown>) => Promise<void> }) {
  const [form, setForm] = useState({
    timezone: c.timezone, provider: c.booking.provider, bookings: c.booking.enabled, autoSend: c.auto_send,
    quiet: c.quiet_hours.enabled, qStart: c.quiet_hours.start, qEnd: c.quiet_hours.end,
    cap: c.daily_message_cap === null ? "" : String(c.daily_message_cap),
  });
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [timeZones] = useState(zones);
  const set = <K extends keyof typeof form>(key: K, value: (typeof form)[K]) => setForm((f) => ({ ...f, [key]: value }));

  return (
    <>
      <p className="pf-note">The settings a new client most often needs. Everything else is under Clients → Config. Only
        what you change here is saved as this client&apos;s own value; the rest keeps following the industry.</p>
      <div className="ob-form">
        <Field label="Timezone" hint="Opening hours, quiet hours, reminders and billing follow it." error={fieldErrors.timezone}>
          <input type="text" list="ob-zones" value={form.timezone} onChange={(ev) => set("timezone", ev.target.value)} />
        </Field>
        <datalist id="ob-zones">{timeZones.map((z) => <option key={z} value={z} />)}</datalist>
        <Field label="Owner's Telegram" hint="Booking requests, escalations and the weekly summary go here."
               error={fieldErrors["booking.provider"]}>
          <input type="text" maxLength={64} placeholder="@username or phone number" value={form.provider}
                 onChange={(ev) => set("provider", ev.target.value)} />
        </Field>
        <Check label="Bookings on (the bot takes booking requests for the owner to confirm)" checked={form.bookings}
               onChange={(v) => set("bookings", v)} error={fieldErrors["booking.enabled"]} />
        <Check label="Auto-send (off: every reply waits in the panel for approval)" checked={form.autoSend}
               onChange={(v) => set("autoSend", v)} error={fieldErrors.auto_send} />
        <Check label="Quiet hours (nothing is sent in this window)" checked={form.quiet}
               onChange={(v) => set("quiet", v)} error={fieldErrors["quiet_hours.enabled"]} />
        <div className="row">
          <Field label="Quiet from" error={fieldErrors["quiet_hours.start"]}>
            <input type="time" value={form.qStart} onChange={(ev) => set("qStart", ev.target.value)} />
          </Field>
          <Field label="until" error={fieldErrors["quiet_hours.end"]}>
            <input type="time" value={form.qEnd} onChange={(ev) => set("qEnd", ev.target.value)} />
          </Field>
        </div>
        <Field label="Messages per day, at most" hint="Every message the account sends, replies included."
               error={fieldErrors.daily_message_cap}>
          <input type="number" min={1} step={1} value={form.cap} onChange={(ev) => set("cap", ev.target.value)} />
        </Field>
      </div>
      <div className="pf-errors">{error}</div>
      <div className="sheet-actions ob-actions">
        <button type="button" className="btn primary ob-big" disabled={busy} onClick={async () => {
          setError("");
          setFieldErrors({});
          if (!form.provider.trim()) { setError("Enter the owner's Telegram: without it, nothing can reach the owner."); return; }
          // Only what differs from the effective value becomes a client override.
          const patch: Record<string, unknown> = {};
          const put = (path: string, value: unknown, current: unknown) => {
            if (value === current) return;
            const keys = path.split(".");
            let node = patch;
            for (const k of keys.slice(0, -1)) node = (node[k] = (node[k] as Record<string, unknown>) || {});
            node[keys[keys.length - 1]] = value;
          };
          put("timezone", form.timezone.trim(), c.timezone);
          put("booking.provider", form.provider.trim(), c.booking.provider);
          put("booking.enabled", form.bookings, c.booking.enabled);
          put("auto_send", form.autoSend, c.auto_send);
          put("quiet_hours.enabled", form.quiet, c.quiet_hours.enabled);
          put("quiet_hours.start", form.qStart, c.quiet_hours.start);
          put("quiet_hours.end", form.qEnd, c.quiet_hours.end);
          put("daily_message_cap", form.cap === "" ? null : Number(form.cap), c.daily_message_cap);
          setBusy(true);
          try { await onSave(patch); }
          catch (err) {
            if (err instanceof ApiError && err.status === 409) { setError(err.message); return; }
            const split = splitErrors(err, SETTING_PATHS);
            setFieldErrors(split.fields);
            setError(split.loose);
          } finally { setBusy(false); }
        }}>Save and continue</button>
      </div>
    </>
  );
}

/* ------------------------------------------------------------ 4. staging */

function StagingStep({ st, onSave, onSkip, onOpenChats }: {
  st: Effective["staging"]; onSave: (list: string[]) => Promise<void>; onSkip: () => void; onOpenChats?: () => void;
}) {
  const [chats, setChats] = useState(st.test_chats.join("\n"));
  const [error, setError] = useState("");
  const [fieldError, setFieldError] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <>
      <p className="pf-note">In staging the bot answers only the test chats listed here. Everyone else&apos;s messages
        are stored and listed as unanswered, not replied to. Use it to try the bot before real customers reach it.</p>
      <div className={cx("ob-state", st.enabled ? "on" : "off")}>
        {st.enabled ? `Staging is ON: answering only ${st.test_chats.join(", ") || "(no test chats)"}.`
          : "Staging is off: the bot answers everyone."}
      </div>
      <div className="ob-form">
        <Field label="Test chats" error={fieldError}
               hint="Telegram usernames (with or without @) or chat ids of people who will test, e.g. your own account.">
          <textarea rows={4} placeholder="@username or numeric chat id, one per line" value={chats}
                    onChange={(ev) => setChats(ev.target.value)} />
        </Field>
      </div>
      <div className="pf-errors">{error}</div>
      <div className="sheet-actions ob-actions">
        <button type="button" className="btn ob-big" onClick={onSkip}>{st.enabled ? "Continue" : "Skip staging"}</button>
        <button type="button" className="btn primary ob-big" disabled={busy} onClick={async () => {
          setError(""); setFieldError("");
          const list = chats.split(/[\n,]+/).map((x) => x.trim()).filter(Boolean);
          if (!list.length) { setError("Add at least one test chat."); return; }
          setBusy(true);
          try { await onSave(list); }
          catch (err) {
            const split = splitErrors(err, ["staging.test_chats"]);
            setFieldError(split.fields["staging.test_chats"] || "");
            setError(split.loose);
          } finally { setBusy(false); }
        }}>{st.enabled ? "Save test chats" : "Turn staging on"}</button>
      </div>
      {st.enabled && (
        <div className="pf-section ob-test">
          <div className="title">Now test it</div>
          <p className="pf-note">Send a message to this account from one of the test chats, e.g. a question a customer
            would ask. The reply appears in the account&apos;s conversations (or waits there for approval while auto-send
            is off).</p>
          {onOpenChats ? <button type="button" className="btn ob-big" onClick={onOpenChats}>Open this account&apos;s conversations</button>
            : <p className="pf-note">Pick the account in the account switcher at the top to see its conversations.</p>}
        </div>
      )}
    </>
  );
}

/* ------------------------------------------------------- 5. prompt check */

function PromptStep({ view, tenantId, onNext, onEdit }: { view: TenantView; tenantId: number; onNext: () => void; onEdit: () => void }) {
  useEffect(() => { memory.promptSeen.add(tenantId); }, [tenantId]);
  return (
    <>
      <p className="pf-note">This is the whole prompt the bot runs on, as it is now. It is read-only here: the business
        sections (services, hours, prices, tone, …) are filled in under Clients → this client → Prompt by the platform
        owner. Check that it describes this business before going live.</p>
      <pre className="pf-rendered">{view.prompt.rendered}</pre>
      <div className="muted">Version {view.prompt.version_tag}</div>
      <div className="sheet-actions ob-actions">
        <button type="button" className="btn ob-big" onClick={onEdit}>Open Clients → Prompt</button>
        <button type="button" className="btn primary ob-big" onClick={onNext}>Looks right, continue</button>
      </div>
    </>
  );
}

/* ----------------------------------------------------------- 6. go live */

function LiveStep({ status: s, c, onFinish, onGoLive }: {
  status: OnboardingStatus; c: Effective; onFinish: () => void; onGoLive: () => Promise<boolean>;
}) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const fact = (ok: boolean, text: string) => <div className={cx("ob-fact", ok ? "ok" : "todo")}>{ok ? "✓ " : "• "}{text}</div>;
  return (
    <>
      <div className="pf-section">
        {fact(!!s.steps.account, `Account: ${s.session_label || s.session_id || "none"}`)}
        {fact(!!s.steps.business, `Business: ${s.name} (${s.industry})`)}
        {fact(!!s.steps.settings, `Owner's Telegram: ${c.booking.provider || "not set"}`)}
        {fact(true, `Auto-send: ${c.auto_send ? "on" : "off (every reply waits for approval)"}`)}
        {fact(s.staging.enabled, s.staging.enabled ? `Staging on, test chats: ${s.staging.test_chats.join(", ")}` : "Staging is off")}
      </div>
      {!s.configured ? <div className="pf-errors">Name the business and set the owner&apos;s Telegram first.</div>
        : !s.staging.enabled ? <>
          <p className="pf-note">Staging is off, so this client already answers everyone.</p>
          <div className="sheet-actions ob-actions">
            <button type="button" className="btn primary ob-big" onClick={onFinish}>Finish</button>
          </div>
        </> : <>
          <p className="pf-note">Going live turns staging off: from then on the bot answers everyone who writes to this
            account. The test chat list is kept, so staging can be turned back on later.</p>
          <div className="pf-errors">{error}</div>
          <div className="sheet-actions ob-actions">
            <button type="button" className="btn primary ob-big" disabled={busy} onClick={async () => {
              setBusy(true);
              try { await onGoLive(); } catch (err) { setError(errorText(err)); }
              finally { setBusy(false); }
            }}>Go live</button>
          </div>
        </>}
    </>
  );
}
