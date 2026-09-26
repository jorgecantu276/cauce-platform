import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { useStaffApi } from "../app-context";
import { EmptyState, ErrorState, LoadingState } from "../components/states";
import { formatMxn } from "../lib/money";
import { PageHeading } from "./today-page";

export function CustomersPage() {
  const api = useStaffApi();
  const { businessId = "" } = useParams();
  const query = useQuery({ queryKey: ["customers", businessId, api.source], queryFn: ({ signal }) => api.listCustomers(businessId, { signal }) });
  if (query.isPending) return <LoadingState label="Cargando clientes…" />;
  if (query.isError) return <ErrorState error={query.error} retry={() => void query.refetch()} />;
  const customers = query.data?.items ?? [];
  return <section className="page"><PageHeading eyebrow="Cobranza" title="Clientes" />{query.data?.hasMore && <p className="page-limit-note" role="status">Mostrando los 200 clientes más recientes.</p>}{customers.length === 0 ? <EmptyState title="Aún no hay clientes" detail="Crea uno desde el nuevo cobro." /> : <section className="customer-list panel">{customers.map((customer) => <article key={customer.customerId} className="customer-row"><span className="avatar">{customer.displayName.slice(0, 1)}</span><div><strong>{customer.displayName}</strong><p>{customer.email ?? "Sin correo"}</p></div><div><small>{customer.openChargeCount ? `${customer.openChargeCount} cobro${customer.openChargeCount === 1 ? "" : "s"} abierto${customer.openChargeCount === 1 ? "" : "s"}` : "Sin cobros abiertos"}</small><strong className="money">{formatMxn(customer.outstandingMinor)}</strong></div></article>)}</section>}</section>;
}
