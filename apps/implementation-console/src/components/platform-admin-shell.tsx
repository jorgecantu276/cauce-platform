import { useEffect } from "react";
import { Link, Outlet } from "react-router-dom";
import { useAuth } from "../auth/auth-context";
import { resetClientTheme } from "../theme";
import { ForbiddenState, LoadingState, SourceNotice } from "./states";

export function PlatformAdminShell() {
  const { loading, session, source, signOut } = useAuth();
  useEffect(() => {
    const previousTitle = document.title;
    resetClientTheme();
    document.title = "Implementación · Cauce";
    return () => { document.title = previousTitle; };
  }, []);
  if (loading) return <LoadingState label="Verificando acceso de plataforma…" />;
  if (session?.platformRole !== "super_admin") return <ForbiddenState />;
  return <div className="platform-shell">
    <header className="platform-header">
      <Link className="platform-brand" to="/platform/implementacion"><span className="platform-brand-mark" aria-hidden="true">C</span><span><strong>CAUCE</strong><small>CONTROL DE PLATAFORMA</small></span></Link>
      <div className="platform-account"><span><strong>{session.displayName}</strong><small>Super admin</small></span><button className="text-button" onClick={() => void signOut()}>Salir</button></div>
    </header>
    <main className="platform-main"><SourceNotice source={source}>Modo local de implementación. Validar, aplicar y eliminar se simulan en memoria: no se escribe ningún dato real.</SourceNotice><Outlet /></main>
  </div>;
}
