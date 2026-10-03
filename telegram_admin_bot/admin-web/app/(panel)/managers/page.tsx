import { Managers } from "@/components/pages/Managers";

// Not /manager: /manager/ is where the managers themselves sign in (the
// Python backend's moderator panel). ?tab=new opens the new-manager form.
export default async function ManagersPage({ searchParams }: PageProps<"/managers">) {
  const { tab } = await searchParams;
  return <Managers key={String(tab)} initialTab={tab === "new" ? "new" : "list"} />;
}
