// The admin panel's front door: serves the Next.js app and passes /api/*,
// /ws/* and /owner/* through to the FastAPI panel (panel.py) unchanged.
//
// Why not next.config rewrites: Next's rewrite proxy sets Host to the
// upstream's address (changeOrigin), and panel.py refuses every POST/PUT/
// PATCH/DELETE and the websocket unless the browser's Origin matches Host
// (panel._same_origin). Passing the request through byte for byte keeps
// that check, the SameSite=Strict cookie and the login rate limit working
// exactly as they do when the browser talks to the panel directly.
//
//   node server.mjs                 development (npm run dev)
//   node server.mjs --production    after `next build` (npm start)
//
// PANEL_URL   where panel.py listens        (default http://127.0.0.1:8787)
// HOSTNAME    where this server listens     (default 127.0.0.1)
// PORT                                       (default 3000)

import http from "node:http";
import net from "node:net";
import next from "next";

if (process.argv.includes("--production")) process.env.NODE_ENV = "production";
const dev = process.env.NODE_ENV !== "production";
const hostname = process.env.HOSTNAME || "127.0.0.1";
const port = Number(process.env.PORT || 3000);
const panel = new URL(process.env.PANEL_URL || "http://127.0.0.1:8787");
const panelPort = Number(panel.port || (panel.protocol === "https:" ? 443 : 80));

if (panel.protocol !== "http:") {
  // The panel is reached over the compose network or loopback; TLS is Caddy's job.
  throw new Error(`PANEL_URL must be an http:// address, got ${panel.href}`);
}

/** Routes owned by panel.py, never by Next. */
function isPanelPath(url = "/") {
  const path = url.split("?", 1)[0];
  return path === "/api" || path.startsWith("/api/") || path.startsWith("/ws/") ||
    path === "/owner" || path.startsWith("/owner/");
}

// The panel takes the visitor's address from the leftmost X-Forwarded-For
// entry (uvicorn proxy_headers, forwarded_allow_ips="*"). Caddy, when it is
// in front, overwrites whatever the visitor sent; this appends our hop.
function forwardedHeaders(req) {
  const headers = { ...req.headers };
  const peer = req.socket.remoteAddress || "";
  headers["x-forwarded-for"] = headers["x-forwarded-for"] ? `${headers["x-forwarded-for"]}, ${peer}` : peer;
  if (!headers["x-forwarded-proto"]) headers["x-forwarded-proto"] = "http";
  return headers;
}

function proxyHttp(req, res) {
  const upstream = http.request({
    host: panel.hostname, port: panelPort, method: req.method, path: req.url,
    headers: forwardedHeaders(req),
  }, (answer) => {
    res.writeHead(answer.statusCode || 502, answer.rawHeaders);
    answer.pipe(res);
  });
  upstream.on("error", (err) => {
    console.error(`[gateway] ${req.method} ${req.url}: ${err.message}`);
    if (!res.headersSent) {
      res.writeHead(502, { "Content-Type": "application/json", "Cache-Control": "no-store" });
    }
    res.end(JSON.stringify({ detail: "The admin API (panel.py) is not reachable." }));
  });
  req.pipe(upstream);
}

// A websocket is an HTTP/1.1 upgrade: replay the handshake to the panel and
// then splice the two sockets together.
function proxyUpgrade(req, socket, head) {
  const upstream = net.connect(panelPort, panel.hostname, () => {
    const headers = forwardedHeaders(req);
    const lines = [`${req.method} ${req.url} HTTP/${req.httpVersion}`];
    for (const [name, value] of Object.entries(headers)) {
      for (const v of Array.isArray(value) ? value : [value]) lines.push(`${name}: ${v}`);
    }
    upstream.write(lines.join("\r\n") + "\r\n\r\n");
    if (head && head.length) upstream.write(head);
    upstream.pipe(socket);
    socket.pipe(upstream);
  });
  const drop = () => { upstream.destroy(); socket.destroy(); };
  upstream.on("error", drop);
  socket.on("error", drop);
}

const app = next({ dev, hostname, port });
await app.prepare();
const handle = app.getRequestHandler();
const handleUpgrade = app.getUpgradeHandler();

const server = http.createServer((req, res) => {
  if (isPanelPath(req.url)) return proxyHttp(req, res);
  return handle(req, res);
});

server.on("upgrade", (req, socket, head) => {
  if (isPanelPath(req.url)) return proxyUpgrade(req, socket, head);
  // Next's own sockets (hot reload in development).
  return handleUpgrade(req, socket, head);
});

server.listen(port, hostname, () => {
  console.log(`> Admin panel on http://${hostname}:${port} (${dev ? "development" : "production"}), ` +
    `API from ${panel.origin}`);
});
