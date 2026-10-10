"use client";

// The client dashboard: a login of a business owner, separate from the admin
// panel's (own cookie, own API under /api/owner/*). What it shows is decided
// by the server: after every sign-in, and whenever an answer is 401 or a
// "gate", it asks /api/owner/account what stands between this login and the
// dashboard (a temporary password, an approval, new terms, the identity
// check) and opens that screen. Read-only apart from "mark reviewed", the
// owner's own password and authenticator, the identity video, and photos for
// businesses whose photos we review.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { cx } from "@/lib/format";
import { errorMessage, GATE, GATES, OwnerApiError, ownerApi } from "@/lib/owner/api";
import type { Account, Me, SignupOptions } from "@/lib/owner/types";
import {
  ChangePasswordScreen, LoginScreen, PendingScreen, RejectedScreen, SignupScreen,
} from "./AuthScreens";
import { OwnerContext, type OwnerCtx, type ToastKind } from "./context";
import { Dashboard } from "./Dashboard";
import { TermsGateScreen, VerifyScreen } from "./GateScreens";
import { s } from "./parts";

type Screen =
  | { name: "loading" }
  | { name: "login"; message?: string; nonce: number }
  | { name: "signup" }
  | { name: "change" }
  | { name: "pending"; account: Account }
  | { name: "rejected"; account: Account }
  | { name: "terms" }
  | { name: "verify" }
  | { name: "app" };

type Toast = { id: number; text: string; kind: ToastKind };

export function OwnerApp() {
  const [screen, setScreen] = useState<Screen>({ name: "loading" });
  const [me, setMe] = useState<Me | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [signup, setSignup] = useState<SignupOptions | null>(null);
  const signupRef = useRef<SignupOptions | null>(null);
  const counter = useRef(0);

  const toast = useCallback((text: string, kind: ToastKind = "error") => {
    const id = ++counter.current;
    setToasts((all) => [...all, { id, text, kind }]);
    setTimeout(() => setToasts((all) => all.filter((t) => t.id !== id)), kind === "info" ? 3500 : 8000);
  }, []);

  // Asked once; a failed answer is asked again next time. `force` re-reads it
  // (the terms changed during a sign-up).
  const loadSignup = useCallback(async (force = false): Promise<SignupOptions | null> => {
    if (signupRef.current && !force) return signupRef.current;
    try {
      const o = await ownerApi<SignupOptions>("GET", "/api/owner/signup-options");
      signupRef.current = o;
      setSignup(o);
      return o;
    } catch {
      return signupRef.current;
    }
  }, []);

  const toLogin = useCallback((message?: string) => {
    setMe(null);
    setScreen({ name: "login", message, nonce: ++counter.current });
    void loadSignup();
  }, [loadSignup]);

  // What stands between a signed-in login and the dashboard: asks the server, which knows.
  const routeGate = useCallback(async (cause?: Error) => {
    let account: Account;
    try {
      account = await ownerApi<Account>("GET", "/api/owner/account");
    } catch (err) {
      if (err instanceof OwnerApiError && err.status === 401) toLogin("You were signed out. Please sign in again.");
      else toLogin("Could not reach the server: " + errorMessage(err));
      return;
    }
    switch (account.gate) {
      case GATE.changePassword: setScreen({ name: "change" }); break;
      case GATE.pendingApproval: setScreen({ name: "pending", account }); break;
      case GATE.rejected: setScreen({ name: "rejected", account }); break;
      case GATE.acceptTerms: setScreen({ name: "terms" }); break;
      case GATE.verifyIdentity: setScreen({ name: "verify" }); break;
      default: toLogin("Could not open the dashboard: " + (cause ? cause.message : "please try again."));
    }
  }, [toLogin]);

  const boot = useCallback((): Promise<void> => ownerApi<Me>("GET", "/api/owner/me").then(
    (account) => {
      setMe(account);
      setScreen({ name: "app" });
    },
    (err) => {
      if (err instanceof OwnerApiError && err.status === 401) toLogin();
      else if (err instanceof OwnerApiError && err.status === 403) return routeGate(err);
      else toLogin("Could not reach the server: " + errorMessage(err));
    },
  ), [routeGate, toLogin]);

  // A 401 or a gate can come back from any call: the session ran out, the admin
  // disabled the login or reset its password, or new terms came out.
  const authProblem = useCallback((err: unknown): boolean => {
    if (!(err instanceof OwnerApiError)) return false;
    if (err.status === 401) { toLogin("You were signed out. Please sign in again."); return true; }
    if (err.status === 403 && GATES.includes(err.detail)) { void routeGate(err); return true; }
    return false;
  }, [routeGate, toLogin]);

  const signOut = useCallback(async () => {
    try { await ownerApi("POST", "/api/owner/logout"); } catch { /* the cookie is cleared by a later sign-in anyway */ }
    toLogin("You are signed out.");
  }, [toLogin]);

  useEffect(() => {
    // The answer lands in state from the promise's callbacks, not synchronously here.
    boot().catch(() => undefined);
  }, [boot]);

  const ctx = useMemo<OwnerCtx>(
    () => ({ me, setMe, toast, authProblem, boot, toLogin, signOut }),
    [me, toast, authProblem, boot, toLogin, signOut]);

  let body: React.ReactNode;
  switch (screen.name) {
    case "loading": body = <div className={s.center}><div className={s.loading}>Loading…</div></div>; break;
    case "login":
      body = <LoginScreen key={screen.nonce} message={screen.message} signup={signup}
                          onSignup={() => setScreen({ name: "signup" })} />;
      break;
    case "signup":
      body = <SignupScreen options={signup} reloadOptions={() => loadSignup(true)} onBack={() => toLogin()} />;
      break;
    case "change": body = <ChangePasswordScreen />; break;
    case "pending": body = <PendingScreen account={screen.account} />; break;
    case "rejected": body = <RejectedScreen account={screen.account} />; break;
    case "terms": body = <TermsGateScreen />; break;
    case "verify": body = <VerifyScreen />; break;
    case "app": body = me ? <Dashboard /> : null; break;
  }

  return (
    <OwnerContext.Provider value={ctx}>
      <div className={s.root}>
        {body}
        <div className={s.toasts} aria-live="polite">
          {toasts.map((t) => <div key={t.id} className={cx(s.toast, t.kind === "info" && s.toastInfo)}>{t.text}</div>)}
        </div>
      </div>
    </OwnerContext.Provider>
  );
}
