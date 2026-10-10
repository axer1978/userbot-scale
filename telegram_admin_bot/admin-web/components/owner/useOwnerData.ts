"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { errorMessage, OwnerApiError } from "@/lib/owner/api";
import { useOwner } from "./context";

type State<T> = { data: T | null; error: OwnerApiError | Error | null };

/**
 * Data from the client API that a part of the dashboard shows. `load`
 * (memoised by the caller) runs again whenever it changes; only the newest
 * request may land, so a slow answer never replaces a newer one. A signed-out
 * session or a gate is handed to the dashboard (which opens the right screen)
 * and is not shown as an error here.
 */
export function useOwnerData<T>(load: () => Promise<T>) {
  const { authProblem } = useOwner();
  const [state, setState] = useState<State<T>>({ data: null, error: null });
  const latest = useRef(0);
  const current = useRef(load);

  const run = useCallback(async (): Promise<T | null> => {
    const ticket = ++latest.current;
    try {
      const data = await current.current();
      if (ticket === latest.current) setState({ data, error: null });
      return data;
    } catch (err) {
      if (ticket !== latest.current || authProblem(err)) return null;
      setState((prev) => ({ data: prev.data, error: err instanceof Error ? err : new Error(errorMessage(err)) }));
      return null;
    }
  }, [authProblem]);

  useEffect(() => {
    current.current = load;
    // The answer lands in state from the promise, not synchronously here.
    void run();
  }, [load, run]);

  return { data: state.data, error: state.error, reload: run };
}
