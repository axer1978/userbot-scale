import { Owners } from "@/components/pages/Owners";

const TABS = ["waiting", "logins", "new"] as const;

export default async function OwnersPage({ searchParams }: PageProps<"/owners">) {
  const { tab } = await searchParams;
  const initialTab = TABS.find((t) => t === tab);
  return <Owners key={initialTab} initialTab={initialTab} />;
}
