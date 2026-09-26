import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useStaffApi } from "../app-context";
import { EmptyState, ErrorState, LoadingState, StatusPill } from "../components/states";
import { formatDateTime, formatMxn } from "../lib/money";
import { PageHeading } from "./today-page";
import { getOrCreateOperation, newKey } from "../lib/idempotency";

export function ReviewsPage() {
  const api = useStaffApi();
  const { businessId = "" } = useParams();
  const client = useQueryClient();
  const [selected, setSelected] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const query = useQuery({ queryKey: ["reviews", businessId], queryFn: ({ signal }) => api.listReviews(businessId, { signal }) });
  const resolve = useMutation({
    mutationFn: ({ item, action }: { item: import("../api/contracts").ReviewItem; action: "retry" | "acknowledge" }) => {
      const operation = getOrCreateOperation("review-resolution", `${item.id}:${action}:${note.trim()}`, () => ({ key: newKey("review") }));
      return api.resolveReview(businessId, item, { action, note: note.trim() }, operation.key);
    },
    onSuccess: () => { setSelected(null); setNote(""); void client.invalidateQueries({ queryKey: ["reviews", businessId] }); },
  });
  if (query.isPending) return <LoadingState label="Cargando revisiones…" />;
  if (query.isError) return <ErrorState error={query.error} retry={() => void query.refetch()} />;
  const items = query.data.items;
  return <section className="page"><PageHeading eyebrow="Control" title="Revisión" /><p className="page-intro">Solo las propietarias pueden revisar evidencia. Reconocer una excepción nunca crea una asignación ni confirma un pago.</p>{query.data.hasMore && <p className="page-limit-note" role="status">Mostrando las 200 revisiones más recientes.</p>}{items.length === 0 ? <EmptyState title="Sin revisiones abiertas" detail="No hay excepciones que requieran acción de la propietaria." /> : <section className="review-list">{items.map((item) => <article className="review-card" key={`${item.kind}-${item.id}`}><div><StatusPill tone="danger">{labelFor(item.kind)}</StatusPill><h2>{item.kind === "refund" ? "Solicitud de reembolso" : item.kind === "payment" ? "Pago requiere revisión" : "Evento del proveedor"}</h2><p>{humanReason(item.reason)}</p>{item.amountMinor ? <strong className="money">{formatMxn(item.amountMinor)}</strong> : null}<time>{formatDateTime(item.updatedAt)}</time></div><div className="review-actions">{selected === item.id ? <><label className="review-note">Nota para auditoría<textarea value={note} onChange={(event) => setNote(event.target.value)} maxLength={500} placeholder="Explica la decisión" /></label>{resolve.error && <p className="form-error">No se pudo guardar la resolución.</p>}<button className="button primary" disabled={!note.trim() || resolve.isPending} onClick={() => resolve.mutate({ item, action: "retry" })}>Reintentar verificación</button><button className="button secondary" disabled={!note.trim() || resolve.isPending} onClick={() => resolve.mutate({ item, action: "acknowledge" })}>Reconocer excepción</button><button className="text-button" disabled={resolve.isPending} onClick={() => { setSelected(null); setNote(""); }}>Cancelar</button></> : <button className="button secondary" onClick={() => setSelected(item.id)}>Resolver revisión</button>}</div></article>)}</section>}{api.source === "fixture" && <p className="caption">La solicitud con HTTP 401 representa el bloqueo externo documentado del sandbox; no afirma una devolución ni simula saldo revertido.</p>}<Link className="text-link" to={`/app/${businessId}/cobros`}>Ver cobros →</Link></section>;
}

function labelFor(kind: string) { return kind === "refund" ? "Reembolso" : kind === "payment" ? "Pago" : "Evento"; }
function humanReason(reason: string | null) {
  if (reason === "extra_payment") return "Se observó dinero adicional. Permanece registrado y requiere resolución; no se descarta.";
  return reason || "Se requiere una revisión del proveedor.";
}
