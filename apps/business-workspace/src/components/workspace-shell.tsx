import { NavLink, Outlet, useParams } from "react-router-dom";
import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "../auth/auth-context";
import { useStaffApi } from "../app-context";
import { ForbiddenState, LoadingState, SourceNotice } from "./states";
import { applyClientTheme } from "../theme";

const entries = [
  ["hoy", "Hoy", "▦"],
  ["cobros", "Cobros", "◫"],
  ["clientes", "Clientes", "♧"],
  ["revision", "Revisión", "!"],
  ["ajustes", "Ajustes", "⚙"],
] as const;

export function WorkspaceShell() {
  const { businessId = "" } = useParams();
  const { session, loading, signOut } = useAuth();
  const api = useStaffApi();
  const membership = session?.memberships.find((item) => item.businessId === businessId);
  const branding = useQuery({ queryKey: ["branding", businessId, api.source], queryFn: ({ signal }) => api.getBranding(businessId, { signal }), enabled: !!membership });
  useEffect(() => { if (branding.data) applyClientTheme(branding.data); }, [branding.data]);
  // A direct navigation or reload starts with session === null while
  // restoreSession() is still resolving; evaluating membership before it
  // settles showed "Sin acceso" for a perfectly valid session.
  if (loading) return <LoadingState label="Verificando tu sesión…" />;
  if (!membership) return <ForbiddenState />;

  return <div className="workspace-shell">
    <aside className="side-nav">
      <div className="brand"><span className="brand-mark" aria-hidden="true"><i /><i /><i /></span><span><b>CAUCE</b><small>PAYMENTS</small></span></div>
      <nav aria-label="Navegación principal">
        {entries.map(([segment, label, icon]) => {
          const ownerOnly = segment === "revision";
          if (ownerOnly && membership.role !== "owner") return null;
          return <NavLink key={segment} to={`/app/${businessId}/${segment}`} className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}>
            <span aria-hidden="true">{icon}</span><span>{label}</span>
          </NavLink>;
        })}
      </nav>
      <div className="nav-account"><span className="avatar">{session?.displayName.slice(0, 1) ?? "?"}</span><div><strong>{session?.displayName}</strong><small>{membership.role === "owner" ? "Propietaria" : "Equipo"}</small></div><button className="text-button" onClick={() => void signOut()} aria-label="Cerrar sesión">Salir</button></div>
    </aside>
    <main className="workspace-main">
      <header className="mobile-header"><span className="brand-mark" aria-hidden="true"><i /><i /><i /></span><strong>CAUCE</strong><span>Payments</span></header>
      <div className="page-frame"><SourceNotice source={api.source} /><Outlet /></div>
    </main>
  </div>;
}
