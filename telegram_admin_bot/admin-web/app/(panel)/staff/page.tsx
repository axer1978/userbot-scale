import { Staff, type StaffTab } from "@/components/pages/Staff";

const TABS: StaffTab[] = ["waiting", "activity", "roles"];

// ?tab=waiting | activity | roles (Managers links to the roles).
export default async function StaffPage({ searchParams }: PageProps<"/staff">) {
  const { tab } = await searchParams;
  const initialTab = TABS.find((t) => t === tab);
  // A link to another tab starts the page over.
  return <Staff key={initialTab} initialTab={initialTab} />;
}
