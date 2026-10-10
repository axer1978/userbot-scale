import type { Metadata } from "next";
import { OwnerApp } from "@/components/owner/OwnerApp";

// The business owners' own dashboard. Its sign-in is separate from the admin
// panel's (proxy.ts lets /owner through without the admin cookie); everything
// it shows comes from /api/owner/*, which panel.py checks per request.
export const metadata: Metadata = {
  title: "My business",
  robots: { index: false, follow: false },
};

export default function OwnerPage() {
  return <OwnerApp />;
}
