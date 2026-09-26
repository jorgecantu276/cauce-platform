import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { useStaffApi } from "../app-context";
import { ErrorState, LoadingState, StatusPill } from "../components/states";
import { formatDateTime } from "../lib/money";
import { PageHeading } from "./today-page";

export function SettingsPage() {
  const api = useStaffApi();
  const { businessId = "" } = useParams();
  const query = useQuery({ queryKey: ["merchant-connection", businessId, api.source], queryFn: ({ signal }) => api.getMerchantConnection(businessId, { signal }) });
  if (query.isPending) return <LoadingState />;
  if (query.isError) return <ErrorState error={query.error} retry={() => void query.refetch()} />;
  const connection = query.data;
  const verified = connection?.state === "verified_test";
  return <section className="page"><PageHeading eyebrow="Espacio" title="Ajustes" /><section className="settings-grid"><article className="panel"><p className="eyebrow">Mercado Pago</p><h2>Conexión del negocio</h2><StatusPill tone={verified ? "success" : "warning"}>{verified ? "Prueba verificada" : connection?.state === "needs_configuration" ? "Sin configurar" : "No disponible"}</StatusPill><dl className="settings-list"><div><dt>Proveedor</dt><dd>Mercado Pago</dd></div><div><dt>Ámbito</dt><dd>{connection?.environment === "test" ? "Sandbox / test" : "—"}</dd></div><div><dt>Cuenta</dt><dd>{connection?.displayAccount ?? "—"}</dd></div><div><dt>Verificada</dt><dd>{connection?.verifiedAt ? formatDateTime(connection.verifiedAt) : "—"}</dd></div></dl><p className="field-help">No se muestran tokens, secretos ni identificadores privados de proveedor.</p></article><article className="panel"><p className="eyebrow">Estado financiero</p><h2>Regla de confianza</h2><p>Solo la confirmación del proveedor y la conciliación autoritativa cambian el estado financiero. Volver desde el navegador no confirma un pago.</p><p className="field-help">Este espacio no contiene una acción “Marcar pagado”.</p></article></section></section>;
}
