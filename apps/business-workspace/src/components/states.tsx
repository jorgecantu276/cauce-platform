import { useEffect } from "react";
import type { ReactNode } from "react";
import { ApiError } from "../api/client";
import { useAuth } from "../auth/auth-context";

export function SourceNotice({ source, children }: { source: "fixture" | "live"; children?: ReactNode }) {
  if (source === "live") return null;
  return <div className="source-notice" role="status"><span aria-hidden="true">◌</span><div><strong>Datos de demostración</strong><br />{children ?? "Esta sesión de desarrollo usa datos simulados; producción requiere autenticación y APIs configuradas."}</div></div>;
}

export function LoadingState({ label = "Cargando información…" }: { label?: string }) {
  return <div className="center-state" aria-live="polite"><span className="spinner" aria-hidden="true" /><p>{label}</p></div>;
}

export function EmptyState({ title, detail, action }: { title: string; detail: string; action?: ReactNode }) {
  return <section className="empty-state"><div className="empty-icon" aria-hidden="true">○</div><h2>{title}</h2><p>{detail}</p>{action}</section>;
}

export function ErrorState({ error, retry }: { error: unknown; retry?: () => void }) {
  const apiError = error instanceof ApiError ? error : null;
  if (apiError?.status === 403) return <ForbiddenState />;
  if (apiError?.status === 401) return <SessionExpiredState />;
  if (apiError?.code === "staff_api_not_configured") {
    return <section className="empty-state error-state" role="alert"><div className="empty-icon" aria-hidden="true">!</div><h2>Aplicación no configurada</h2><p>Esta aplicación no tiene configurado el servicio de datos. Contacta a soporte técnico; no es un problema de tu conexión.</p></section>;
  }
  const detail = apiError?.code === "request_timeout"
    ? "El servidor tardó demasiado en responder. Intenta de nuevo en un momento; no se cambió ningún saldo."
    : apiError?.status === 0
      ? "No pudimos conectarnos. Revisa tu internet e intenta de nuevo."
      : "No pudimos cargar esta información. No se cambió ningún saldo.";
  return <section className="empty-state error-state" role="alert"><div className="empty-icon" aria-hidden="true">!</div><h2>No se pudo cargar</h2><p>{detail}</p>{retry && <button className="button secondary" onClick={retry}>Intentar de nuevo</button>}</section>;
}

export function ForbiddenState() {
  return <section className="empty-state" role="alert"><div className="empty-icon" aria-hidden="true">⊘</div><h2>Sin acceso</h2><p>Tu sesión no tiene permiso para ver esta información en este negocio.</p></section>;
}

export function SessionExpiredState() {
  const { clearSession, signIn } = useAuth();
  // Discard the rejected token immediately: without this, every other query
  // on screen keeps retrying against the same invalid token and shows this
  // same state redundantly instead of a single clear prompt.
  useEffect(() => { void clearSession(); }, [clearSession]);
  return <section className="empty-state error-state" role="alert"><div className="empty-icon" aria-hidden="true">⊘</div><h2>Tu sesión expiró</h2><p>Por seguridad no renovamos el acceso automáticamente. Vuelve a iniciar sesión para continuar.</p><button className="button primary" onClick={() => void signIn()}>Iniciar sesión de nuevo</button></section>;
}

export function StatusPill({ tone, children }: { tone: string; children: ReactNode }) {
  return <span className={`status-pill ${tone}`}><span className="status-dot" aria-hidden="true" />{children}</span>;
}
