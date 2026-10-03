"use client";

import { useSyncExternalStore } from "react";

const noSubscription = () => () => {};

/**
 * This site's address (https://panel.example.com), for links shown to be
 * passed on. Only the browser knows it; the server render gets "".
 */
export function useOrigin(): string {
  return useSyncExternalStore(noSubscription, () => window.location.origin, () => "");
}
