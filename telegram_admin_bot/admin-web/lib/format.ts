/** Time today, or a short date and time for anything older. */
export function fmtTime(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  const sameDay = d.toDateString() === new Date().toDateString();
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return sameDay ? time : `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}`;
}

/** Medium date and short time, or a placeholder when there is none. */
export function fmtDateTime(iso?: string | null, empty = "—"): string {
  return iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : empty;
}

/** YYYY-MM-DD in the browser's own timezone. */
export function isoDate(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

export function contactLabel(c: { display_name?: string | null; username?: string | null; chat_id?: number }): string {
  return (c.display_name || String(c.chat_id ?? "")) + (c.username ? ` (@${c.username})` : "");
}

export function cx(...names: (string | false | null | undefined)[]): string {
  return names.filter(Boolean).join(" ");
}
