import { TermsSignup } from "@/components/pages/TermsSignup";

// Not /terms: /terms/ is the public terms page the Python backend serves.
// ?tab=history opens the list of published versions.
export default async function TermsSignupPage({ searchParams }: PageProps<"/terms-and-signup">) {
  const { tab } = await searchParams;
  return <TermsSignup key={String(tab)} initialTab={tab === "history" ? "history" : "edit"} />;
}
