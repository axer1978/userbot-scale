import { NextResponse, type NextRequest } from "next/server";

// Page headers, the same policy panel.py sends for its own pages
// (panel.SECURITY_HEADERS), except that scripts are allowed by a fresh
// nonce instead of 'self' alone: Next.js inlines its bootstrap scripts and
// tags every script it emits with the nonce it finds in this header.
// Inline style attributes are used by the markup, hence 'unsafe-inline' for
// styles. connect-src: this host only, for the API and the live socket.

const HOST_RE = /^(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?$/;
// The admin's, or a moderator's whose role includes the admin panel.
const COOKIE_NAMES = ["__Host-admin_token", "admin_token", "__Host-manager_token", "manager_token"];

function contentSecurityPolicy(nonce: string, host: string): string {
  const dev = process.env.NODE_ENV === "development";
  const ws = HOST_RE.test(host) ? ` wss://${host} ws://${host}` : "";
  return [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${dev ? " 'unsafe-eval'" : ""}`,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "media-src 'self' blob:",
    `connect-src 'self'${ws}`,
    "font-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
  ].join("; ");
}

export function proxy(request: NextRequest) {
  const { pathname, search } = request.nextUrl;

  // No sign-in cookie at all: straight to the sign-in page. Whether a cookie
  // that is there is still valid only the panel knows; a 401 from the API
  // sends the browser to /login as well (lib/api.ts).
  const signedIn = COOKIE_NAMES.some((name) => request.cookies.has(name));
  if (!signedIn && pathname !== "/login") {
    const url = request.nextUrl.clone();
    url.pathname = "/login";
    url.search = pathname === "/" ? "" : `?next=${encodeURIComponent(pathname + search)}`;
    return NextResponse.redirect(url);
  }

  const nonce = Buffer.from(crypto.randomUUID()).toString("base64");
  const csp = contentSecurityPolicy(nonce, request.headers.get("host") ?? "");

  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("Content-Security-Policy", csp);

  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("Content-Security-Policy", csp);
  response.headers.set("X-Content-Type-Options", "nosniff");
  response.headers.set("X-Frame-Options", "DENY");
  response.headers.set("Referrer-Policy", "no-referrer");
  response.headers.set("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()");
  response.headers.set("Cross-Origin-Opener-Policy", "same-origin");
  response.headers.set("Cache-Control", "no-store");
  return response;
}

export const config = {
  matcher: [
    {
      // Pages only: not Next's static files, not the routes server.mjs hands
      // to panel.py, and not prefetches (they get the page's headers anyway).
      source: "/((?!_next/static|_next/image|icon.svg|api/|ws/|owner(?:/|$)|manager(?:/|$)|terms(?:/|$)).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
