"use client";

import Link from "next/link";
import { cx } from "@/lib/format";

/** A full page in the panel: the old full-screen sheets (Clients, Bookings, …). */
export function PageShell({ title, crumb, width, children }: {
  title: string;
  crumb?: React.ReactNode;
  width?: "w-980" | "w-900" | "w-860" | "w-820";
  children: React.ReactNode;
}) {
  return (
    <div className="page">
      <div className={cx("pf-shell", width)}>
        <div className="pf-top">
          <h2>{title}</h2>
          {crumb ? <span className="muted">{crumb}</span> : null}
          <span className="spacer" />
          <Link href="/" className="btn">Close</Link>
        </div>
        {children}
      </div>
    </div>
  );
}

export function Tabs<K extends string>({ tabs, value, onChange }: {
  tabs: readonly (readonly [K, React.ReactNode])[];
  value: K;
  onChange: (key: K) => void;
}) {
  return (
    <div className="pf-tabs" role="tablist">
      {tabs.map(([key, label]) => (
        <button key={key} type="button" role="tab" aria-selected={value === key}
                className={cx("pf-tab", value === key && "on")} onClick={() => onChange(key)}>
          {label}
        </button>
      ))}
    </div>
  );
}

/** Label: value, skipped when the value is empty. */
export function Fact({ label, value }: { label: string; value?: React.ReactNode }) {
  if (!value) return null;
  return (
    <div className="bk-fact">
      <span className="muted">{label}: </span>
      <span>{value}</span>
    </div>
  );
}

/** A dimmed backdrop that closes the dialog when clicked outside it. */
export function Overlay({ onClose, children }: { onClose: () => void; children: React.ReactNode }) {
  return (
    <div className="overlay" role="dialog" aria-modal="true"
         onClick={(ev) => { if (ev.target === ev.currentTarget) onClose(); }}
         onKeyDown={(ev) => { if (ev.key === "Escape") onClose(); }}>
      {children}
    </div>
  );
}
