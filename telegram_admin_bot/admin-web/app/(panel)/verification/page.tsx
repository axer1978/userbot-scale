import { Verification, type VerifyTab } from "@/components/pages/Verification";

const TABS: VerifyTab[] = ["videos", "photos", "businesses"];

// ?tab=videos | photos | businesses
export default async function VerificationPage({ searchParams }: PageProps<"/verification">) {
  const { tab } = await searchParams;
  const initialTab = TABS.find((t) => t === tab);
  // A link to another tab starts the page over.
  return <Verification key={initialTab} initialTab={initialTab} />;
}
