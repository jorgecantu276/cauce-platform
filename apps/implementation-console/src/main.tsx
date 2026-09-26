import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider } from "./auth/auth-context";
import { AppProviders } from "./app-context";
import { PlatformAdminShell } from "./components/platform-admin-shell";
import { ImplementationPage } from "./pages/implementation-page";
import { applyClientTheme } from "./theme";
import "./styles.css";
import "./implementation.css";
import "./theme.css";

applyClientTheme();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AuthProvider>
      <AppProviders>
        <BrowserRouter>
          <Routes>
            <Route path="/" element={<Navigate replace to="/platform/implementacion" />} />
            <Route path="/platform" element={<PlatformAdminShell />}>
              <Route index element={<Navigate replace to="implementacion" />} />
              <Route path="implementacion" element={<ImplementationPage />} />
            </Route>
            <Route path="*" element={<Navigate replace to="/" />} />
          </Routes>
        </BrowserRouter>
      </AppProviders>
    </AuthProvider>
  </StrictMode>,
);
