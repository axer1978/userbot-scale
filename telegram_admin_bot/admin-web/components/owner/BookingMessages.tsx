"use client";

// "Booking messages": how the bot's messages to the person who answers the
// business's booking requests look — her language and the business's own
// wording. The same editor as the admin panel's Booker messages tab, on the
// owner's API (/api/owner/booker-messages?tenant_id=N).

import { useMemo } from "react";
import { BookerMessagesEditor, type BookerMessagesClient, type BookerUi } from "@/components/BookerMessagesEditor";
import { cx } from "@/lib/format";
import { ownerApi } from "@/lib/owner/api";
import type { OwnerTenant } from "@/lib/owner/types";
import type { BookerMessagesView, BookerPreview } from "@/lib/types";
import { useOwner } from "./context";
import { s } from "./parts";

const OWNER_UI: BookerUi = { btn: s.btn, primary: cx(s.btn, s.primary), small: cx(s.btn, s.btnSmall) };

export function BookingMessages({ tenant, onDirtyChange }: {
  tenant: OwnerTenant;
  onDirtyChange: (dirty: boolean) => void;
}) {
  const { authProblem } = useOwner();
  const client = useMemo<BookerMessagesClient>(() => {
    const q = `tenant_id=${encodeURIComponent(tenant.id)}`;
    return {
      load: () => ownerApi<BookerMessagesView>("GET", `/api/owner/booker-messages?${q}`),
      preview: (body) => ownerApi<BookerPreview>("POST", `/api/owner/booker-messages/preview?${q}`, body),
      save: (body) => ownerApi<BookerMessagesView>("PUT", `/api/owner/booker-messages?${q}`, body),
    };
  }, [tenant.id]);

  return (
    <>
      <h1>Booking messages</h1>
      <p className={s.hint}>
        {tenant.name}: what the bot sends the person who answers your booking requests — her language, and your own
        wording of the main messages. Changes apply from the bot&apos;s next message.
      </p>
      <BookerMessagesEditor client={client} canSave ui={OWNER_UI} onError={authProblem} onDirtyChange={onDirtyChange} />
    </>
  );
}
