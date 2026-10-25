// The logic behind the "messages to the booker" page that is the same for
// the admin panel and the owner's pages, kept out of the component: which
// card a refused save's errors belong to, and a live preview that waits for
// typing to stop and never lets an older answer replace a newer one.

export type FieldProblem = { path: string; message: string };

export type SplitProblems = {
  /** Problems by template key, for the card of that message. */
  byKey: Record<string, string[]>;
  /** Everything that names no message (the language, say). */
  other: string[];
};

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * A refused save (422) as problems by message. The server puts every
 * problem of booking.booker_messages into one message, each as
 * "key: problem", joined with "; " (and pydantic may put "Value error, " in
 * front). A problem can itself contain "; " ("... is not a blank of this
 * message; it has: ..."), so the split is on the known keys only.
 */
export function splitProblems(errors: readonly FieldProblem[], keys: readonly string[]): SplitProblems {
  const byKey: Record<string, string[]> = {};
  const other: string[] = [];
  const add = (key: string, text: string) => {
    const t = text.trim().replace(/;$/, "").trim();
    if (t) (byKey[key] ||= []).push(t);
  };
  // Longest first, so "customer_cancelled" is never read as "cancelled".
  const names = [...keys].sort((a, b) => b.length - a.length).map(escapeRegExp);
  const marker = names.length ? new RegExp(`(?:^|;\\s*)(${names.join("|")}):\\s*`, "g") : null;

  for (const err of errors) {
    const message = String(err.message || "").replace(/^Value error,\s*/i, "");
    const marks = marker ? [...message.matchAll(marker)] : [];
    if (!marks.length) {
      // Not "key: ..." — maybe the path names the message.
      const last = String(err.path || "").split(".").pop() || "";
      if (keys.includes(last)) add(last, message);
      else other.push(err.path && err.path !== "(root)" ? `${err.path}: ${message}` : message);
      continue;
    }
    const lead = message.slice(0, marks[0].index).trim();
    if (lead) other.push(lead);
    marks.forEach((m, i) => {
      const start = (m.index ?? 0) + m[0].length;
      const end = i + 1 < marks.length ? marks[i + 1].index ?? message.length : message.length;
      add(m[1], message.slice(start, end));
    });
  }
  return { byKey, other };
}

/**
 * Runs `fetch` `delay` ms after the last `schedule`, and hands only the
 * answer to the newest request to `onResult` / `onError`: an answer that
 * comes back after a newer request was made is dropped. `cancel` drops
 * whatever is waiting or on its way (a save replaced everything, or the
 * page closed).
 */
export function latestDebounced<A, R>(
  fetch: (arg: A) => Promise<R>,
  delay: number,
  handlers: { onStart?: () => void; onResult: (result: R) => void; onError: (err: unknown) => void },
) {
  let timer: ReturnType<typeof setTimeout> | null = null;
  let ticket = 0;
  return {
    schedule(arg: A) {
      if (timer) clearTimeout(timer);
      const mine = ++ticket;
      handlers.onStart?.();
      timer = setTimeout(() => {
        timer = null;
        fetch(arg).then(
          (result) => { if (mine === ticket) handlers.onResult(result); },
          (err) => { if (mine === ticket) handlers.onError(err); },
        );
      }, delay);
    },
    cancel() {
      if (timer) clearTimeout(timer);
      timer = null;
      ticket += 1;
    },
  };
}

/** The account's own texts to send: empty ones are the default and left out. */
export function ownTexts(texts: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, text] of Object.entries(texts)) if (text.trim()) out[key] = text;
  return out;
}
