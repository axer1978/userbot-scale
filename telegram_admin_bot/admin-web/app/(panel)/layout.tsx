"use client";

import { AddAccountDialog } from "@/components/AddAccountDialog";
import { FeedbackProvider } from "@/components/feedback";
import { Header } from "@/components/Header";
import { MediaDialog } from "@/components/MediaDialog";
import { OutreachDialog } from "@/components/OutreachDialog";
import { StyleDialog } from "@/components/StyleDialog";
import { PanelProvider, usePanel } from "@/lib/panel";

function Dialogs() {
  const { modal, setModal, state } = usePanel();
  const close = () => setModal(null);
  // Every dialog belongs to the open account; switching accounts closes it.
  if (!state.sessionId) return <AddAccountDialog />;
  return (
    <>
      {modal === "outreach" && <OutreachDialog key={state.sessionId} onClose={close} />}
      {modal === "style" && <StyleDialog key={state.sessionId} onClose={close} />}
      {modal === "media" && <MediaDialog key={state.sessionId} onClose={close} />}
      <AddAccountDialog />
    </>
  );
}

export default function PanelLayout({ children }: LayoutProps<"/">) {
  return (
    <FeedbackProvider>
      <PanelProvider>
        <Header />
        {children}
        <Dialogs />
      </PanelProvider>
    </FeedbackProvider>
  );
}
