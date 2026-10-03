import { Safety, type SafetyTab } from "@/components/pages/Safety";

const TABS: SafetyTab[] = ["clients", "alerts", "client", "billing"];

export default async function SafetyPage({ searchParams }: PageProps<"/safety">) {
  const { tab, tenant } = await searchParams;
  const initialTab = TABS.find((t) => t === tab);
  const initialTenant = typeof tenant === "string" && /^\d+$/.test(tenant) ? Number(tenant) : null;
  // A link to another tab or client (the top bar's chip) starts the page over.
  return <Safety key={`${initialTab}-${initialTenant}`} initialTab={initialTab} initialTenant={initialTenant} />;
}
