import type { NextConfig } from "next";

// /api, /ws and /owner never reach Next: server.mjs passes them to panel.py
// before routing (see the comment there for why not `rewrites`).
const nextConfig: NextConfig = {
  poweredByHeader: false,
  reactStrictMode: true,
};

export default nextConfig;
