import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider } from "./auth/auth-context";
import { AppProviders } from "./app-context";
import { WorkspaceShell } from "./components/workspace-shell";
import { ChargesPage } from "./pages/charges-page";
import { ChargeDetailPage } from "./pages/charge-detail-page";
import { CreateChargePage } from "./pages/create-charge-page";
import { CustomersPage } from "./pages/customers-page";
import { HomeRedirect } from "./pages/home-redirect";
import { ReviewsPage } from "./pages/reviews-page";
import { SettingsPage } from "./pages/settings-page";
import { TodayPage } from "./pages/today-page";
import { applyClientTheme } from "./theme";
import "./styles.css";
import "./theme.css";

applyClientTheme();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AuthProvider>
      <AppProviders>
        <BrowserRouter>
          <Routes>
            <Route path="/" element={<HomeRedirect />} />
            <Route path="/app/:businessId" element={<WorkspaceShell />}>
              <Route index element={<Navigate replace to="hoy" />} />
              <Route path="hoy" element={<TodayPage />} />
              <Route path="cobros" element={<ChargesPage />} />
              <Route path="cobros/nuevo" element={<CreateChargePage />} />
              <Route path="cobros/:chargeId" element={<ChargeDetailPage />} />
              <Route path="clientes" element={<CustomersPage />} />
              <Route path="revision" element={<ReviewsPage />} />
              <Route path="ajustes" element={<SettingsPage />} />
            </Route>
            <Route path="*" element={<Navigate replace to="/" />} />
          </Routes>
        </BrowserRouter>
      </AppProviders>
    </AuthProvider>
  </StrictMode>,
);
