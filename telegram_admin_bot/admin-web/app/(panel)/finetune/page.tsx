import { Finetune } from "@/components/pages/Finetune";

// ?tab=review&tenant=6&view=chats&review=3…: what is open. Finetune reads it
// from the address itself (coming Back these props are the first address's)
// and keeps it up to date there; a link to another address starts it over.
export default async function FinetunePage({ searchParams }: PageProps<"/finetune">) {
  const params = await searchParams;
  return <Finetune key={JSON.stringify(params)} />;
}
