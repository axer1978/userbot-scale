"use client";

import { createContext, useContext } from "react";
import type { Me } from "@/lib/owner/types";

export type ToastKind = "error" | "info";

export type OwnerCtx = {
  /** The signed-in client; null on the sign-in, approval and terms screens. */
  me: Me | null;
  setMe: (me: Me) => void;
  toast: (text: string, kind?: ToastKind) => void;
  /** A 401 (signed out) or a gate (new terms, a password reset, ...) can come back
   * from any call: opens the right screen and returns true. */
  authProblem: (err: unknown) => boolean;
  /** Asks the server who this is and what, if anything, stands in the way; opens that screen. */
  boot: () => Promise<void>;
  /** Back to the sign-in screen, with a message above the form. */
  toLogin: (message?: string) => void;
  signOut: () => Promise<void>;
};

export const OwnerContext = createContext<OwnerCtx | null>(null);

export function useOwner(): OwnerCtx {
  const ctx = useContext(OwnerContext);
  if (!ctx) throw new Error("useOwner outside the dashboard");
  return ctx;
}

/** For the dashboard proper, which only exists for a signed-in client. */
export function useMe(): Me {
  const { me } = useOwner();
  if (!me) throw new Error("useMe before sign-in");
  return me;
}
