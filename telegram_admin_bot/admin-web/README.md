# admin-web — the admin panel in Next.js

The platform admin's panel (conversations, bookings, clients, safety,
unanswered, review, onboarding, client logins), rewritten from
`../static/index.html` + `../static/js/*` as a Next.js 16 App Router app in
TypeScript. It talks to the same FastAPI panel (`panel.py`) over the same
`/api/*` routes and `/ws/{session_id}` socket; nothing in the Python code
changed. The client dashboard (`/owner/`) is still served by `panel.py`.

## How it fits

```
browser ──▶ server.mjs (:3000) ──┬─ /api/*, /ws/*, /owner/* ──▶ panel.py (:8787), passed through unchanged
                                 └─ everything else ──────────▶ Next.js (pages)
```

`server.mjs` is a small custom server (the documented Next.js pattern). It
exists because `panel.py` refuses every state-changing request and the
websocket unless the browser's `Origin` matches the request's `Host`
(`panel._same_origin`), and Next's own `rewrites` proxy rewrites `Host` to the
upstream's. Passing requests through byte for byte keeps that check, the
`SameSite=Strict` admin cookie and the login rate limit (`X-Forwarded-For`)
working as they do today.

Pages get the same security headers `panel.py` sends, with a per-request
CSP nonce instead of `script-src 'self'` alone (`proxy.ts`, Next 16's
replacement for `middleware.ts`). Next.js tags every script it emits with
that nonce, so no inline script runs without it. A browser with no admin
cookie is sent to `/login`; a 401 from the API does the same.

## Run it

Node 20.9 or newer. With `panel.py` running on 127.0.0.1:8787:

```bash
npm install
npm run dev          # http://127.0.0.1:3000, hot reload
npm run build && npm start   # production
```

Settings (environment, see `.env.example`): `PANEL_URL` (default
`http://127.0.0.1:8787`), `HOSTNAME` (default `127.0.0.1`), `PORT` (default
`3000`). No secrets: the admin password and every key stay with `panel.py`.

Checks: `npm run lint`, `npm run typecheck`.

## Deploy next to the existing stack

From `telegram_admin_bot/`:

```bash
docker compose -f docker-compose.yml -f admin-web/docker-compose.admin-web.yml up -d --build
```

The `admin-web` service is published on the host's 127.0.0.1:3000 only, like
the panel. Over an SSH tunnel: `ssh -N -L 3000:127.0.0.1:3000 you@server`, then
open http://127.0.0.1:3000.

For the public HTTPS address, point Caddy at it while keeping the API on the
panel (Caddy preserves `Host`, so the origin check passes):

```caddy
{$PANEL_DOMAIN} {
	# … the existing tls / header blocks …
	@panel path /api/* /ws/* /owner /owner/*
	reverse_proxy @panel panel:8787
	reverse_proxy admin-web:3000
}
```

That Caddyfile change is not made in `../Caddyfile`: `tests/test_deploy_files.py`
pins its current shape, so switching the public site over is a deliberate
step (update the file and that test together).

## Layout

| Path | What |
|---|---|
| `server.mjs` | Gateway: Next.js + pass-through to `panel.py` |
| `proxy.ts` | CSP nonce and page headers; sign-in redirect |
| `app/login/` | Admin sign-in (password, and authenticator code when `ADMIN_TOTP_SECRET` is set) |
| `app/(panel)/layout.tsx` | Shell: header, live state, dialogs |
| `app/(panel)/page.tsx` | Conversations (was `conversations.js`) |
| `app/(panel)/{bookings,clients,safety,unanswered,review,onboarding,owners}/` | One route per former full-screen sheet |
| `lib/panel.tsx` | The open account's state and its websocket (was `core.js`, `socket.js`, `accounts.js`) |
| `lib/api.ts`, `lib/types.ts` | API client and response types |
| `components/` | Header, dialogs (add account, outreach, style, media), config editor, pages |

Differences from the old page: the sheets are routes (`/clients?node=tenant-7&tab=prompt`
can be bookmarked and the browser's back button works), and `prompt()` /
`confirm()` are in-page dialogs.

## MCP

`.mcp.json` registers [`next-devtools-mcp`](https://www.npmjs.com/package/next-devtools-mcp),
which gives a coding agent the running dev server's errors, routes and logs and
the docs for the installed Next.js version. The docs themselves ship in
`node_modules/next/dist/docs/` (see `AGENTS.md`).
