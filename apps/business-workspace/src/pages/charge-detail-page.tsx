import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useStaffApi } from "../app-context";
import type { ChargeDetail } from "../api/contracts";
import { ErrorState, LoadingState, StatusPill } from "../components/states";
import { chargeState, formatDate, formatDateTime, formatMxn } from "../lib/money";
import { clearOperation, getOrCreateOperation, newKey } from "../lib/idempotency";

export function ChargeDetailPage() {
  const api = useStaffApi();
  const { businessId = "", chargeId = "" } = useParams();
  const client = useQueryClient();
  const query = useQuery({ queryKey: ["charge-detail", businessId, chargeId, api.source], queryFn: ({ signal }) => api.getChargeDetail(businessId, chargeId, { signal }) });
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ["charge-detail", businessId, chargeId] });
    void client.invalidateQueries({ queryKey: ["charges", businessId] });
    void client.invalidateQueries({ queryKey: ["work-queue", businessId] });
  };
  // A cancel/refund click stays bound to the same idempotency key until the
  // action actually succeeds, so a lost response and a manual retry reconcile
  // as one operation instead of racing the server with a second, distinct key.
  const cancel = useMutation({
    mutationFn: () => api.cancelCharge(businessId, chargeId, getOrCreateOperation("cancel-charge", chargeId, () => ({ key: newKey("cancel-charge") })).key),
    onSuccess: () => { clearOperation("cancel-charge", chargeId); refresh(); },
  });
  const refund = useMutation({
    mutationFn: (paymentId: string) => api.requestRefund(businessId, paymentId, {}, getOrCreateOperation("refund", paymentId, () => ({ key: newKey("refund") })).key),
    onSuccess: (_result, paymentId) => { clearOperation("refund", paymentId); refresh(); },
  });

  if (query.isPending) return <LoadingState label="Cargando evidencia financiera…" />;
  if (query.isError) return <ErrorState error={query.error} retry={() => void query.refetch()} />;
  const detail = query.data;
  const state = chargeState(detail.charge);
  const refundable = detail.payments.filter((payment) => payment.providerStatus === "approved" && !payment.reviewReason && !detail.refundOperations.some((refund) => refund.paymentId === payment.paymentId && refund.status !== "resolved"));

  return <section className="page"><Link className="back-link" to={`/app/${businessId}/cobros`}>← Cobros</Link><header className="detail-heading"><div><p className="eyebrow">Folio {detail.charge.folio}</p><h1>{detail.charge.description}</h1><p>{detail.customer.displayName} · vence {formatDate(detail.charge.dueDate)}</p></div><StatusPill tone={state.tone}>{state.label}</StatusPill></header>
    <section className="balance-banner"><div><p>Saldo pendiente</p><strong>{formatMxn(detail.charge.outstandingMinor)}</strong></div><div><p>Importe original</p><strong>{formatMxn(detail.charge.amountMinor)}</strong></div><div><p>Última evidencia</p><strong>{detail.payments.length ? formatDateTime(detail.payments[0].observedAt) : "Sin pago observado"}</strong></div></section>
    <section className="detail-grid"><article className="panel"><p className="eyebrow">Cliente</p><h2>{detail.customer.displayName}</h2><p className="muted">{detail.customer.email ?? "Sin correo registrado"}</p><hr /><p className="eyebrow">Enlace de pago</p><p>{detail.paymentLink.state === "available_once" ? "El enlace se entregó al crear el cobro. El token no se puede recuperar desde el backend." : "No disponible"}</p><button className="button secondary" disabled title="La rotación de enlace no forma parte del piloto">Generar enlace nuevo</button></article>
      <article className="panel"><p className="eyebrow">Acciones</p><h2>Controladas por servidor</h2><p className="muted">Las acciones se registran con una clave única y nunca cambian un saldo desde el navegador.</p>{!detail.charge.cancelled && detail.charge.outstandingMinor > 0 && <button className="button danger" disabled={cancel.isPending} onClick={() => { if (window.confirm("¿Cancelar este cobro? El enlace dejará de funcionar.")) cancel.mutate(); }}>{cancel.isPending ? "Cancelando…" : "Cancelar cobro"}</button>}{refundable.map((payment) => <button className="button secondary" key={payment.paymentId} disabled={refund.isPending} onClick={() => { if (window.confirm("¿Solicitar el reembolso completo de este pago?")) refund.mutate(payment.paymentId); }}>{refund.isPending ? "Solicitando…" : "Solicitar reembolso"}</button>)}{(cancel.error || refund.error) && <p className="form-error">No se pudo completar la acción. El saldo no cambió.</p>}</article></section>
    <EvidenceTimeline detail={detail} />
  </section>;
}

function EvidenceTimeline({ detail }: { detail: ChargeDetail }) {
  const events = [
    { at: detail.charge.createdAt, kind: "Cobro emitido", detail: `${formatMxn(detail.charge.amountMinor)} · ${detail.charge.folio}` },
    ...detail.attempts.map((attempt) => ({ at: attempt.createdAt, kind: "Intento de checkout", detail: `Estado: ${attempt.status}.` })),
    ...detail.payments.map((payment) => ({ at: payment.observedAt, kind: "Pago observado", detail: `${formatMxn(payment.amountMinor)} · estado del proveedor: ${payment.providerStatus}.` })),
    ...detail.allocations.map((allocation) => ({ at: allocation.createdAt, kind: "Asignación", detail: `${formatMxn(allocation.amountMinor)} aplicado al cobro.` })),
    ...detail.adjustments.map((adjustment) => ({ at: adjustment.occurredAt ?? detail.charge.createdAt, kind: `Ajuste: ${adjustment.kind}`, detail: `${formatMxn(adjustment.amountMinor)} · ${adjustment.status}.` })),
    ...detail.providerEvents.map((event) => ({ at: event.receivedAt, kind: "Evento del proveedor", detail: `${event.eventType} · ${event.processingStatus}${event.reason ? ` · ${event.reason}` : ""}` })),
    ...detail.refundOperations.map((refund) => ({ at: refund.updatedAt, kind: "Solicitud de reembolso", detail: `${formatMxn(refund.amountMinor)} · ${refund.status}${refund.lastError ? ` · ${refund.lastError}` : ""}` })),
  ].sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
  return <section className="panel evidence"><div className="panel-heading"><div><p className="eyebrow">Evidencia financiera</p><h2>Línea de tiempo</h2></div><p className="muted">Los registros permanecen separados.</p></div><div className="timeline">{events.map((event, index) => <article className="timeline-event" key={`${event.kind}-${index}`}><span className="timeline-marker" aria-hidden="true" /><div><strong>{event.kind}</strong><p>{event.detail}</p><time>{formatDateTime(event.at)}</time></div></article>)}</div>{detail.payments.some((payment) => payment.environment === "test" && payment.providerLiveMode) && <div className="sandbox-note"><strong>Prueba verificada</strong><p>La credencial del vendedor está registrada como usuario de prueba. Mercado Pago reportó <code>live_mode=true</code> como evidencia retenida; esto no se muestra como pago de producción.</p></div>}</section>;
}
