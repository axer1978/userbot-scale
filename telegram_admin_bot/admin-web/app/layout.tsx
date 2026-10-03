import type { Metadata, Viewport } from "next";
import { connection } from "next/server";
import "./globals.css";

// No title here: the panel's header renders it (it names the instance), the
// sign-in page has its own.
export const metadata: Metadata = {
  appleWebApp: { capable: true },
  other: { "mobile-web-app-capable": "yes" },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  themeColor: "#151a21",
};

export default async function RootLayout({ children }: LayoutProps<"/">) {
  // Every page is rendered per request: the CSP nonce (proxy.ts) is new each time.
  await connection();
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
