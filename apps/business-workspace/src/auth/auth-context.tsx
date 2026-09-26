import { createContext, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import type { StaffSession } from "../api/contracts";
import { getAuthAdapter } from "./auth-adapter";

type AuthState = {
  session: StaffSession | null;
  loading: boolean;
  source: "live" | "fixture";
  getAccessToken: () => Promise<string | null>;
  signIn: () => Promise<void>;
  signOut: () => Promise<void>;
  clearSession: () => Promise<void>;
};

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const configured = useMemo(getAuthAdapter, []);
  const [session, setSession] = useState<StaffSession | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    configured.adapter.restoreSession()
      .then((value) => active && setSession(value))
      .catch(() => active && setSession(null))
      .finally(() => active && setLoading(false));
    return () => { active = false; };
  }, [configured]);

  const value = useMemo<AuthState>(() => ({
    session,
    loading,
    source: configured.source,
    getAccessToken: () => configured.adapter.getAccessToken(),
    signIn: () => configured.adapter.signIn(),
    signOut: async () => { await configured.adapter.signOut(); setSession(null); },
    clearSession: async () => { await configured.adapter.clearSession(); setSession(null); },
  }), [configured, loading, session]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be rendered inside AuthProvider");
  return value;
}
