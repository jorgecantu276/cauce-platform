import { Navigate } from "react-router-dom";
import { LoadingState } from "../components/states";
import { useAuth } from "../auth/auth-context";

export function HomeRedirect() {
  const { loading, session, signIn } = useAuth();
  if (loading) return <LoadingState label="Abriendo tu espacio…" />;
  const business = session?.memberships[0];
  if (!business) return <section className="empty-state" role="alert"><div className="empty-icon" aria-hidden="true">⊘</div><h2>Acceso de equipo</h2><p>Inicia sesión con la cuenta que tiene acceso a este negocio.</p><button className="button primary" onClick={() => void signIn()}>Iniciar sesión</button></section>;
  return <Navigate replace to={`/app/${business.businessId}/hoy`} />;
}
