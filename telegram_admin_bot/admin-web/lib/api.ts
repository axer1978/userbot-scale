// The panel's JSON API (panel.py). Same origin: server.mjs (or Caddy) puts
// /api on this host, so the admin cookie and panel.py's Origin check just work.

export type FieldError = { path: string; message: string };

export class ApiError extends Error {
  status: number;
  /** Config validation answers {message, errors: [{path, message}]}. */
  errors: FieldError[] | null;

  constructor(message: string, status: number, errors: FieldError[] | null = null) {
    super(message);
    this.status = status;
    this.errors = errors;
  }
}

/** Fired on any 401 except a wrong password: the panel layout goes to /login. */
export const UNAUTHORIZED_EVENT = "panel:unauthorized";

export async function api<T = unknown>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
    credentials: "same-origin",
  });
  if (!res.ok) {
    let detail: unknown = res.statusText;
    let errors: FieldError[] | null = null;
    try {
      const data = await res.json();
      detail = data.detail ?? detail;
    } catch {
      /* not JSON */
    }
    if (detail && typeof detail === "object") {
      const d = detail as { message?: string; errors?: FieldError[] };
      errors = d.errors ?? null;
      detail = d.message ?? JSON.stringify(detail);
    }
    if (res.status === 401 && path !== "/api/login" && typeof window !== "undefined") {
      window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
    }
    throw new ApiError(String(detail), res.status, errors);
  }
  return (res.status === 204 ? null : await res.json()) as T;
}

/** Every per-account route lives under /api/sessions/{session_id}/... */
export function sessionPath(sessionId: string, subpath: string): string {
  return `/api/sessions/${encodeURIComponent(sessionId)}${subpath}`;
}

export function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
