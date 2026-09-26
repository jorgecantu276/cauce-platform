import { createContext, useContext, useMemo } from "react";
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createStaffApi } from "./api/client";
import type { StaffApi } from "./api/contracts";
import { useAuth } from "./auth/auth-context";

const ApiContext = createContext<StaffApi | null>(null);

function ApiProvider({ children }: { children: ReactNode }) {
  const auth = useAuth();
  const api = useMemo(() => createStaffApi({ getAccessToken: auth.getAccessToken }), [auth.getAccessToken]);
  return <ApiContext.Provider value={api}>{children}</ApiContext.Provider>;
}

export function AppProviders({ children }: { children: ReactNode }) {
  const client = useMemo(() => new QueryClient({
    defaultOptions: {
      queries: { staleTime: 20_000, retry: 1, refetchOnWindowFocus: false },
      mutations: { retry: false },
    },
  }), []);
  return <QueryClientProvider client={client}><ApiProvider>{children}</ApiProvider></QueryClientProvider>;
}

export function useStaffApi(): StaffApi {
  const api = useContext(ApiContext);
  if (!api) throw new Error("useStaffApi must be rendered inside AppProviders");
  return api;
}
