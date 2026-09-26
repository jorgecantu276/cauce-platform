import { Link, useParams } from "react-router-dom";
import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { useStaffApi } from "../app-context";
import { formatMxn } from "../lib/money";
import { EmptyState, ErrorState, LoadingState, StatusPill } from "../components/states";

export function TodayPage() {
  const api = useStaffApi();
  const { businessId = "" } = useParams();
  const queue = useQuery({ queryKey: ["work-queue", businessId, api.source], queryFn: ({ signal }) => api.getWorkQueue(businessId, { signal }) });
  // Deliberately not derived from listCharges: that list is capped for
  // display, so it silently under-counts once a business passes ~100 open
  // charges. This summary is a dedicated, uncapped aggregate.
  const summary = useQuery({ queryKey: ["business-summary", businessId, api.source], queryFn: ({ signal }) => api.getBusinessSummary(businessId, { signal }) });

  if (queue.isPending || summary.isPending) return <LoadingState />;
  if (queue.isError) return <ErrorState error={queue.error} retry={() => void queue.refetch()} />;
  if (summary.isError) return <ErrorState error={summary.error} retry={() => void summary.refetch()} />;
  const items = queue.data;
  const reviewCount = items.filter((item) => item.kind === "review").length;
  return <section className="page"><PageHeading eyebrow="Operación" title="Hoy" action={<Link className="button primary" to={`/app/${businessId}/cobros/nuevo`}>Nuevo cobro <span aria-hidden="true">→</span></Link>} />
    <section className="summary-grid"><Summary label="Pendientes" value={String(summary.data.openChargeCount)} /><Summary label="Saldo por cobrar" value={formatMxn(summary.data.outstandingMinor)} /><Summary label="En revisión" value={String(reviewCount)} tone={reviewCount ? "danger" : undefined} /></section>
    <section className="panel"><div className="panel-heading"><div><p className="eyebrow">Cola de trabajo</p><h2>Lo que necesita atención</h2></div></div>
      {items.length === 0 ? <EmptyState title="Todo está al día" detail="No hay cobros vencidos ni revisiones abiertas." /> : <div className="work-list">{items.map((item) => <Link key={item.id} className="work-item" to={item.kind === "review" ? `/app/${businessId}/revision` : `/app/${businessId}/cobros/${item.id}`}><StatusPill tone={item.kind === "review" ? "danger" : item.kind === "overdue_charge" ? "danger" : "warning"}>{item.kind === "review" ? "Revisión" : item.kind === "overdue_charge" ? "Vencido" : "Vence hoy"}</StatusPill><div><strong>{item.title}</strong><p>{item.detail}</p></div>{item.amountMinor ? <strong className="money">{formatMxn(item.amountMinor)}</strong> : <span aria-hidden="true">→</span>}</Link>)}</div>}
    </section>
  </section>;
}

export function PageHeading({ eyebrow, title, action }: { eyebrow: string; title: string; action?: ReactNode }) {
  return <header className="page-heading"><div><p className="eyebrow">{eyebrow}</p><h1>{title}</h1></div>{action}</header>;
}

function Summary({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return <section className={`summary-card ${tone ?? ""}`}><p>{label}</p><strong>{value}</strong></section>;
}
