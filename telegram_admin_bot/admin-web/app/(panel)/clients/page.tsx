import { Clients, type TreeNode } from "@/components/pages/Clients";

// ?node=tenant-5 | industry-2 | base, and optionally &tab=prompt
function parseNode(value: string | string[] | undefined): TreeNode | null {
  if (value === "base") return { kind: "base", id: 0 };
  const m = typeof value === "string" ? value.match(/^(tenant|industry)-(\d+)$/) : null;
  return m ? { kind: m[1] as "tenant" | "industry", id: Number(m[2]) } : null;
}

export default async function ClientsPage({ searchParams }: PageProps<"/clients">) {
  const { node, tab } = await searchParams;
  const initialNode = parseNode(node);
  const initialTab = typeof tab === "string" ? tab : undefined;
  // A link to another client (Settings in the top bar) starts the page over.
  return <Clients key={`${node}-${tab}`} initialNode={initialNode} initialTab={initialTab} />;
}
