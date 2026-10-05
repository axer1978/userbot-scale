"use client";

// Toasts, and the confirm / prompt dialogs every page uses for a reason
// that goes into the audit log or a destructive step that needs a yes.

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";

/* ------------------------------------------------------------------ toasts */

type ToastKind = "error" | "info";
type ToastFn = (text: string, kind?: ToastKind) => void;

const ToastContext = createContext<ToastFn>(() => {});

export function useToast(): ToastFn {
  return useContext(ToastContext);
}

/* ----------------------------------------------------------------- dialogs */

type Ask = {
  id: number;
  kind: "confirm" | "prompt";
  message: string;
  defaultValue?: string;
  resolve: (value: string | null) => void;
};

type Dialogs = {
  confirm: (message: string) => Promise<boolean>;
  prompt: (message: string, defaultValue?: string) => Promise<string | null>;
};

const DialogContext = createContext<Dialogs>({
  confirm: async () => false,
  prompt: async () => null,
});

export function useDialogs(): Dialogs {
  return useContext(DialogContext);
}

function AskDialog({ ask, onDone }: { ask: Ask; onDone: (value: string | null) => void }) {
  const [value, setValue] = useState(ask.defaultValue ?? "");
  const input = useRef<HTMLInputElement>(null);
  const ok = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (ask.kind === "prompt") input.current?.select();
    else ok.current?.focus();
  }, [ask.kind]);

  return (
    <div className="overlay top" role="dialog" aria-modal="true"
         onKeyDown={(ev) => { if (ev.key === "Escape") onDone(null); }}>
      <form className="sheet w-440" onSubmit={(ev) => { ev.preventDefault(); onDone(ask.kind === "prompt" ? value : "yes"); }}>
        <p className="dialog-text">{ask.message}</p>
        {ask.kind === "prompt" && (
          <div className="field">
            <input ref={input} type="text" value={value} onChange={(ev) => setValue(ev.target.value)} autoComplete="off" />
          </div>
        )}
        <div className="sheet-actions">
          <button type="button" className="btn" onClick={() => onDone(null)}>Cancel</button>
          <button ref={ok} type="submit" className="btn primary">OK</button>
        </div>
      </form>
    </div>
  );
}

/* ---------------------------------------------------------------- provider */

export function FeedbackProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<{ id: number; text: string; kind: ToastKind }[]>([]);
  const [queue, setQueue] = useState<Ask[]>([]);
  const seq = useRef(0);
  const askSeq = useRef(0);

  const toast = useCallback<ToastFn>((text, kind = "error") => {
    const id = ++seq.current;
    setToasts((list) => [...list, { id, text, kind }]);
    setTimeout(() => setToasts((list) => list.filter((t) => t.id !== id)), kind === "info" ? 4000 : 9000);
  }, []);

  const dialogs = useMemo<Dialogs>(() => ({
    confirm: (message) => new Promise((resolve) => {
      const ask: Ask = { id: ++askSeq.current, kind: "confirm", message, resolve: (v) => resolve(v !== null) };
      setQueue((q) => [...q, ask]);
    }),
    prompt: (message, defaultValue = "") => new Promise((resolve) => {
      const ask: Ask = { id: ++askSeq.current, kind: "prompt", message, defaultValue, resolve };
      setQueue((q) => [...q, ask]);
    }),
  }), []);

  const current = queue[0];

  return (
    <ToastContext.Provider value={toast}>
      <DialogContext.Provider value={dialogs}>
        {children}
        {current && (
          <AskDialog key={current.id} ask={current} onDone={(value) => {
            setQueue((q) => q.slice(1));
            current.resolve(value);
          }} />
        )}
        <div id="toasts" aria-live="polite">
          {toasts.map((t) => (
            <div key={t.id} className={t.kind === "info" ? "toast info" : "toast"}>{t.text}</div>
          ))}
        </div>
      </DialogContext.Provider>
    </ToastContext.Provider>
  );
}
