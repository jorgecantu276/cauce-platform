import { createContext, useContext, useMemo } from "react";
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createPlatformApi } from "./api/platform-client";
import type { PlatformApi } from "./api/platform-contracts";
import { useAuth } from "./auth/auth-context";

const PlatformApiContext = createContext<PlatformApi | null>(null);

function ApiProvider({ children }: { children: ReactNode }) {
  const auth = useAuth();
  const api = useMemo(() => createPlatformApi({ getAccessToken: auth.getAccessToken }), [auth.getAccessToken]);
  return <PlatformApiContext.Provider value={api}>{children}</PlatformApiContext.Provider>;
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

export function usePlatformApi(): PlatformApi {
  const api = useContext(PlatformApiContext);
  if (!api) throw new Error("usePlatformApi must be rendered inside AppProviders");
  return api;
}
