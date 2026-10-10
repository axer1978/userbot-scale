// The client dashboard's JSON API (owner_api.py, owner_review_api.py), under
// /api/owner/*. Same origin: server.mjs (or Caddy) hands /api to panel.py, so
// the owner cookie and panel.py's Origin check just work. A separate world
// from lib/api.ts on purpose: a 401 here means "sign in as a client", never
// "go to the admin sign-in".

export class OwnerApiError extends Error {
  status: number;
  /** The server's own words (what a gate looks like: "change_password", ...). */
  detail: string;

  constructor(detail: string, status: number) {
    super(detail);
    this.status = status;
    this.detail = detail;
  }
}

/** The login steps the server can put between a signed-in client and the dashboard. */
export const GATE = {
  changePassword: "change_password",
  pendingApproval: "pending_approval",
  rejected: "rejected",
  acceptTerms: "accept_terms",
  verifyIdentity: "verify_identity",
} as const;
export const GATES: readonly string[] = Object.values(GATE);

export const CODE_REQUIRED = "code_required";
export const MIN_PASSWORD = 10;

async function answer<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = res.statusText || "Request failed";
    try {
      const data = await res.json();
      if (data && data.detail) detail = typeof data.detail === "string" ? data.detail : "Please check the form.";
    } catch {
      /* not JSON */
    }
    throw new OwnerApiError(detail, res.status);
  }
  return (res.status === 204 ? null : await res.json()) as T;
}

export async function ownerApi<T = unknown>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    credentials: "same-origin",
    cache: "no-store",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return answer<T>(res);
}

// Body types a form can send; the server refuses them on uploads (415).
const FORM_TYPES = ["multipart/form-data", "application/x-www-form-urlencoded", "text/plain"];

function bodyType(file: File): string {
  const type = String(file.type || "").toLowerCase();
  if (!type || FORM_TYPES.some((f) => type.startsWith(f))) return "application/octet-stream";
  return type;
}

/** A file as the raw request body (never a form: the server refuses those). */
export async function ownerUpload<T = unknown>(path: string, file: File): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, {
      method: "PUT",
      body: file,
      credentials: "same-origin",
      cache: "no-store",
      headers: { "Content-Type": bodyType(file) },
    });
  } catch {
    throw new OwnerApiError("The upload did not go through. Please check your connection and try again.", 0);
  }
  return answer<T>(res);
}

export function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
