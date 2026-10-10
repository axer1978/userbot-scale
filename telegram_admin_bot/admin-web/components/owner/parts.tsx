"use client";

import { useMemo } from "react";
import { cx } from "@/lib/format";
import { fmtDate, HEALTH, parseTerms } from "@/lib/owner/format";
import type { OwnerTenant, TermsVersion } from "@/lib/owner/types";
import s from "./owner.module.css";

export { s };

/** A message above a form: an error, or an `info` note. Nothing when there is no text. */
export function Notice({ text, info }: { text?: string | null; info?: boolean }) {
  if (!text) return null;
  return <div className={cx(s.notice, info && s.info)} role="alert">{text}</div>;
}

/** The terms' text format as plain React nodes (never HTML). */
export function TermsText({ body }: { body: string }) {
  const blocks = useMemo(() => parseTerms(body), [body]);
  return (
    <>
      {blocks.map((b, i) => {
        if (b.kind === "h") return <h2 key={i}>{b.text}</h2>;
        if (b.kind === "ul") return <ul key={i}>{b.items.map((item, j) => <li key={j}>{item}</li>)}</ul>;
        return <p key={i}>{b.text}</p>;
      })}
    </>
  );
}

export function termsMeta(terms: Pick<TermsVersion, "version" | "published_at">): string {
  const date = fmtDate(terms.published_at);
  return `Version ${terms.version}` + (date ? `, published ${date}` : "");
}

/** The scrollable box the terms are read in. */
export function TermsBox({ terms, className, noMeta }: { terms: TermsVersion; className?: string; noMeta?: boolean }) {
  return (
    <div className={cx(s.termsBox, s.termsText, className)} tabIndex={0}>
      {!noMeta && <p className={cx(s.muted, s.small)}>{termsMeta(terms)}</p>}
      <TermsText body={terms.body} />
    </div>
  );
}

export function Logo() {
  return (
    <div className={s.logo} aria-hidden="true">
      <svg width="24" height="24" viewBox="0 0 32 32"><path d="M7 16.5l17-7-6 16-3.5-6z" fill="#fff" /></svg>
    </div>
  );
}

/** "Bot is answering" / "Bot is off: why", and the account's health. */
export function StatusLine({ tenant }: { tenant: OwnerTenant }) {
  const bot = tenant.bot;
  const [healthText, healthKind] = HEALTH[tenant.health.status] ?? [tenant.health.status || "unknown", "meh"];
  return (
    <div className={s.statusLine}>
      <span className={cx(s.pill, bot.sending ? s.good : s.bad)} title={bot.sending ? undefined : bot.off_reason}>
        <span className={s.dot} />
        <span className={s.pillText}>{bot.sending ? "Bot is answering" : "Bot is off: " + bot.off_reason}</span>
      </span>
      <span className={cx(s.pill, s[healthKind])}>
        <span className={s.dot} />
        <span className={s.pillText}>{healthText}</span>
      </span>
    </div>
  );
}

export function Section({ title, extra, children }: {
  title: string;
  extra?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className={s.section}>
      <div className={s.sectionHead}>
        <h2>{title}</h2>
        {extra}
      </div>
      {children}
    </section>
  );
}

export function Tile({ value, label, alert }: { value: React.ReactNode; label: string; alert?: boolean }) {
  return (
    <div className={cx(s.tile, alert && s.alert)}>
      <div className={s.num}>{value}</div>
      <div className={s.lbl}>{label}</div>
    </div>
  );
}

/** Two or three buttons that switch one view; the active one is highlighted. */
export function Segmented<K extends string>({ options, value, onChange }: {
  options: readonly (readonly [K, string])[];
  value: K;
  onChange: (key: K) => void;
}) {
  return (
    <div className={s.seg}>
      {options.map(([key, label]) => (
        <button key={key} type="button" className={cx(value === key && s.segOn)} onClick={() => onChange(key)}>
          {label}
        </button>
      ))}
    </div>
  );
}
