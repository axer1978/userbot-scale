// Dates in the business's own timezone (whatever the phone's), sizes, and
// the words the dashboard shows for the server's states.

export const MB = 1024 * 1024;

function fmt(iso: string, tz: string | undefined, options: Intl.DateTimeFormatOptions): string {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  try {
    return d.toLocaleString([], { ...options, timeZone: tz });
  } catch {
    return d.toLocaleString([], options);
  }
}

export const fmtTime = (iso: string, tz: string) => fmt(iso, tz, { hour: "2-digit", minute: "2-digit" });
export const fmtDayKey = (iso: string, tz: string) => fmt(iso, tz, { year: "numeric", month: "2-digit", day: "2-digit" });
export const fmtDay = (iso: string, tz: string) => fmt(iso, tz, { weekday: "long", day: "numeric", month: "long" });
export const fmtShortDay = (iso: string, tz: string) => fmt(iso, tz, { day: "numeric", month: "short" });
export const fmtWhen = (iso: string, tz?: string) =>
  fmt(iso, tz, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
export const fmtDate = (iso: string) => fmt(iso, undefined, { day: "numeric", month: "long", year: "numeric" });

export function sizeMb(bytes: number): string {
  return (bytes / MB).toFixed(bytes < 10 * MB ? 1 : 0) + " MB";
}

export function percent(rate: number | null | undefined): string {
  return rate === null || rate === undefined ? "—" : Math.round(rate * 100) + "%";
}

export const BOOKING_STATES: Record<string, string> = {
  requested: "new request", pending: "waiting for you", confirmed: "confirmed",
  cancelled: "cancelled", no_show: "no-show", completed: "done",
};

export const QUEUE_REASONS: Record<string, string> = {
  skipped: "Not answered (a reply limit or a no-reply rule)",
  ai_error: "The bot could not write a reply",
  soft_off: "The bot was switched off",
  paused: "The chat was paused or taken over by a person",
  escalated: "Passed on to you",
  policy_hold: "The reply was held back for a check",
  fallback: "The bot did not know the answer",
  staging: "Test mode: not answered",
};

/** Account health: [what to show, how it looks]. */
export const HEALTH: Record<string, [string, "good" | "meh" | "bad"]> = {
  ok: ["Connected", "good"], unknown: ["No report yet", "meh"],
  not_running: ["Not running", "bad"], disconnected: ["Not connected", "bad"],
  logged_out: ["Logged out", "bad"], rate_limited: ["Slowed down", "meh"],
  revoked: ["Switched off", "bad"], stopped: ["Stopped", "bad"],
};

export const SUBMISSION_STATES: Record<string, string> = {
  pending: "Waiting for review", approved: "Approved", rejected: "Not accepted", withdrawn: "Withdrawn",
};

// The server takes a video's type from its name: give a camera recording
// without a usable extension the one its type says.
const VIDEO_EXTENSIONS: Record<string, string> = {
  "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm", "video/3gpp": ".3gp",
  "video/x-m4v": ".m4v",
};

export function videoName(file: File): string {
  const name = String(file.name || "");
  if (/\.(mp4|mov|m4v|webm|3gp)$/i.test(name)) return name;
  const ext = VIDEO_EXTENSIONS[String(file.type || "").toLowerCase().split(";")[0]] || ".mp4";
  return (name.replace(/\.[^./]*$/, "") || "video") + ext;
}

export const PHOTO_TYPES = ["image/jpeg", "image/png", "image/webp"];
export const MAX_DESCRIPTION = 300;

export function photoProblem(file: File, maxMb: number | null): string {
  const type = String(file.type || "").toLowerCase();
  if (type && !PHOTO_TYPES.includes(type)) return "Please choose a JPG, PNG or WebP photo.";
  if (!file.size) return "That file is empty.";
  if (maxMb && file.size > maxMb * MB) return `The photo is larger than ${maxMb} MB. Please choose a smaller one.`;
  return "";
}

/** The terms' tiny text format: "## " heading, "- " bullet, blank line ends a paragraph or list. */
export type TermsBlock =
  | { kind: "h"; text: string }
  | { kind: "p"; text: string }
  | { kind: "ul"; items: string[] };

export function parseTerms(body: string): TermsBlock[] {
  const blocks: TermsBlock[] = [];
  let para: string[] | null = null;
  let list: string[] | null = null;
  const flush = () => {
    if (para) blocks.push({ kind: "p", text: para.join(" ") });
    para = null;
  };
  for (const raw of String(body || "").split("\n")) {
    const line = raw.replace(/\s+$/, "");
    if (!line.trim()) {
      flush();
      list = null;
    } else if (line.startsWith("## ")) {
      flush();
      list = null;
      blocks.push({ kind: "h", text: line.slice(3).trim() });
    } else if (line.startsWith("- ")) {
      flush();
      if (!list) {
        list = [];
        blocks.push({ kind: "ul", items: list });
      }
      list.push(line.slice(2).trim());
    } else {
      list = null;
      if (!para) para = [];
      para.push(line.trim());
    }
  }
  flush();
  return blocks;
}
