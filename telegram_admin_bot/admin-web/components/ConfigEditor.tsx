"use client";

// One table for both layers that have config: an industry's defaults (over
// the platform's) and a client's overrides (over its industry's). Rows the
// layer does not set are greyed and read-only until "Override" is pressed;
// rows it sets are highlighted and can be reset to inherit. Values are
// checked by the schema on save; nothing is clamped.

import { useState } from "react";
import { useToast } from "@/components/feedback";
import { ApiError, errorText } from "@/lib/api";
import { cx } from "@/lib/format";
import type { ConfigField } from "@/lib/types";

type Input = string | boolean;
type Row = { set: boolean; mode: "replace" | "append"; dirty: boolean; value: Input; error: string | null };

export type ConfigSave = (body: { overrides: Record<string, unknown>; reason: string; expected_revision: number }) => Promise<void>;

function getPath(obj: unknown, path: string): unknown {
  let node = obj;
  for (const part of path.split(".")) {
    if (node === null || typeof node !== "object" || !(part in (node as object))) return undefined;
    node = (node as Record<string, unknown>)[part];
  }
  return node;
}

function setPath(obj: Record<string, unknown>, path: string, value: unknown) {
  const parts = path.split(".");
  let node = obj;
  for (const part of parts.slice(0, -1)) node = (node[part] = (node[part] as Record<string, unknown>) || {});
  node[parts[parts.length - 1]] = value;
}

function valueText(kind: string, value: unknown): string {
  if (kind === "json") return JSON.stringify(value === undefined ? null : value, null, 2);
  if (kind === "list") return ((value as string[]) || []).join("\n");
  if (kind === "map") return Object.entries((value as Record<string, unknown>) || {}).map(([k, v]) => `${k} = ${v}`).join("\n");
  return value === null || value === undefined ? "" : String(value);
}

function parseValue(kind: string, input: Input): unknown {
  if (kind === "bool") return !!input;
  const text = String(input);
  if (kind === "int" || kind === "float") return text.trim() === "" ? null : Number(text);
  if (kind === "list") return text.split("\n").map((s) => s.trim()).filter(Boolean);
  if (kind === "json") {
    // Left as text when it doesn't parse: the save is then refused with the
    // schema's message next to the field.
    try { return JSON.parse(text); } catch { return text; }
  }
  if (kind === "map") {
    const out: Record<string, number> = {};
    for (const line of text.split("\n")) {
      if (!line.trim()) continue;
      const i = line.lastIndexOf("=");
      out[(i < 0 ? line : line.slice(0, i)).trim()] = i < 0 ? NaN : Number(line.slice(i + 1).trim());
    }
    return out;
  }
  return text;
}

function isAppend(field: ConfigField, raw: unknown): raw is { append: unknown } {
  return field.kind === "list" && !!raw && typeof raw === "object" && !Array.isArray(raw);
}

function shownValue(field: ConfigField, set: boolean, raw: unknown, layer: string): Input {
  const value = set
    ? (isAppend(field, raw) ? raw.append : raw === undefined ? field.value : raw)
    : field.inherited_value !== undefined && field.source === layer ? field.inherited_value : field.value;
  return field.kind === "bool" ? !!value : valueText(field.kind, value);
}

function FieldInput({ field, value, disabled, onChange }: {
  field: ConfigField; value: Input; disabled: boolean; onChange: (value: Input) => void;
}) {
  if (field.kind === "bool") {
    return <input type="checkbox" checked={!!value} disabled={disabled} onChange={(ev) => onChange(ev.target.checked)} />;
  }
  const text = String(value);
  if (field.kind === "choice") {
    return (
      <select value={text} disabled={disabled} onChange={(ev) => onChange(ev.target.value)}>
        {(field.choices || []).map((c) => <option key={String(c)} value={String(c)}>{String(c)}</option>)}
      </select>
    );
  }
  if (field.kind === "json" || field.kind === "longtext") {
    return (
      <textarea className={field.kind === "json" ? "mono" : undefined} disabled={disabled} value={text}
                rows={Math.min(12, Math.max(3, text.split("\n").length))} onChange={(ev) => onChange(ev.target.value)}
                placeholder={field.kind === "json" ? '[{"minutes_before": 1440, "instruction": "what this reminder should say"}]' : undefined} />
    );
  }
  if (field.kind === "list" || field.kind === "map") {
    return (
      <textarea disabled={disabled} value={text} rows={Math.min(6, Math.max(2, text.split("\n").length))}
                placeholder={field.kind === "map" ? "one per line: service = price" : "one per line"}
                onChange={(ev) => onChange(ev.target.value)} />
    );
  }
  const numeric = field.kind === "int" || field.kind === "float";
  return (
    <input type={numeric ? "number" : "text"} step={field.kind === "float" ? "any" : undefined} disabled={disabled}
           value={text} onChange={(ev) => onChange(ev.target.value)} />
  );
}

export function ConfigEditor({ fields, overrides, layer, inheritedLabel, revision, save }: {
  fields: ConfigField[];
  overrides: Record<string, unknown>;
  layer: "client" | "industry";
  inheritedLabel: string;
  revision: number;
  save: ConfigSave;
}) {
  const toast = useToast();
  const [rows, setRows] = useState<Row[]>(() => fields.map((field) => {
    const raw = getPath(overrides, field.path);
    const set = field.source === layer;
    return { set, mode: isAppend(field, raw) ? "append" : "replace", dirty: false,
             value: shownValue(field, set, raw, layer), error: null };
  }));
  const [reason, setReason] = useState("");
  const [errors, setErrors] = useState("");
  const [busy, setBusy] = useState(false);

  const update = (i: number, patch: Partial<Row>) =>
    setRows((list) => list.map((r, n) => (n === i ? { ...r, ...patch } : r)));

  const toggle = (i: number) => {
    const field = fields[i];
    const set = !rows[i].set;
    update(i, { set, dirty: true, error: null, value: shownValue(field, set, getPath(overrides, field.path), layer) });
  };

  const submit = async () => {
    const out: Record<string, unknown> = {};
    rows.forEach((row, i) => {
      if (!row.set) return;
      const field = fields[i];
      let value = parseValue(field.kind, row.value);
      if (field.kind === "list" && row.mode === "append") value = { append: value };
      setPath(out, field.path, value);
    });
    setErrors("");
    setBusy(true);
    try {
      await save({ overrides: out, reason: reason.trim(), expected_revision: revision });
      toast("Config saved.", "info");
    } catch (err) {
      setErrors(errorText(err));
      const list = err instanceof ApiError ? err.errors || [] : [];
      setRows((current) => current.map((row, i) => {
        const path = fields[i].path;
        const hit = list.find((e) => e.path === path || e.path.startsWith(path + "."));
        return { ...row, error: hit ? hit.message : null };
      }));
    } finally {
      setBusy(false);
    }
  };

  let group: string | null = null;
  const body: React.ReactNode[] = [];
  fields.forEach((field, i) => {
    const top = field.path.includes(".") ? field.path.split(".")[0] : null;
    if (top !== group) {
      group = top;
      if (top) body.push(<tr key={`g-${top}`} className="cfg-group"><td colSpan={4}>{top.replace(/_/g, " ")}</td></tr>);
    }
    const row = rows[i];
    const srcClass = row.set ? layer : field.source === layer ? "platform" : field.source;
    const srcText = row.set ? (row.dirty ? "changed" : layer) : field.source === layer ? "inherit" : field.source;
    body.push(
      <tr key={field.path} className={cx("cfg-row", row.set ? "overridden" : "inherited", row.dirty && "dirty")}>
        <td className="path">{field.path}</td>
        <td><span className={`src ${srcClass}`}>{srcText}</span></td>
        <td className="val">
          <FieldInput field={field} value={row.value} disabled={!row.set}
                      onChange={(value) => update(i, { value, dirty: true })} />
          {row.set && field.kind === "list" && (
            <select className="list-mode" value={row.mode}
                    onChange={(ev) => update(i, { mode: ev.target.value as Row["mode"], dirty: true })}>
              <option value="replace">replaces the inherited list</option>
              <option value="append">adds to the inherited list</option>
            </select>
          )}
          {row.error && <div className="err">{row.error}</div>}
        </td>
        <td className="act">
          <button type="button" className="btn small" onClick={() => toggle(i)}
                  title={row.set ? `Remove this value and use the one from ${inheritedLabel}` : "Set a value here"}>
            {row.set ? "Inherit" : "Override"}
          </button>
        </td>
      </tr>,
    );
  });

  return (
    <>
      <p className="pf-note">Greyed rows are inherited from {inheritedLabel}. Highlighted rows are set here. Values are
        checked against the schema when you save; nothing is clamped.</p>
      <table className="cfg-table"><tbody>{body}</tbody></table>
      <div className="pf-errors">{errors}</div>
      <div className="pf-actions">
        <input placeholder="Reason for the change (goes into the audit log)" value={reason}
               onChange={(ev) => setReason(ev.target.value)} />
        <button type="button" className="btn primary" disabled={busy} onClick={submit}>Save config</button>
      </div>
    </>
  );
}
