"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { errorText } from "@/lib/api";

/**
 * Data from the API that a page shows and reloads after its own changes.
 * `fetch` (memoised by the caller) runs again whenever it changes; only the
 * newest request may land, so a slow answer never replaces a newer one.
 */
export function useLoader<T>(fetch: () => Promise<T>) {
  const [state, setState] = useState<{ data: T | null; error: string | null }>({ data: null, error: null });
  const latest = useRef(0);
  const current = useRef(fetch);

  useEffect(() => {
    current.current = fetch;
    const ticket = ++latest.current;
    fetch().then(
      (data) => { if (ticket === latest.current) setState({ data, error: null }); },
      (err) => { if (ticket === latest.current) setState((s) => ({ data: s.data, error: errorText(err) })); },
    );
  }, [fetch]);

  const reload = useCallback(async (): Promise<T | null> => {
    const ticket = ++latest.current;
    try {
      const data = await current.current();
      if (ticket === latest.current) setState({ data, error: null });
      return data;
    } catch (err) {
      if (ticket === latest.current) setState((s) => ({ data: s.data, error: errorText(err) }));
      return null;
    }
  }, []);

  return { data: state.data, error: state.error, reload };
}
