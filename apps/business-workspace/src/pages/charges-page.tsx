import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useStaffApi } from "../app-context";
import { EmptyState, ErrorState, LoadingState, StatusPill } from "../components/states";
import { chargeState, formatDate, formatMxn } from "../lib/money";
import { PageHeading } from "./today-page";

export function ChargesPage() {
  const api = useStaffApi();
  const { businessId = "" } = useParams();
  const [search, setSearch] = useState("");
  const query = useQuery({ queryKey: ["charges", businessId], queryFn: ({ signal }) => api.listCharges(businessId, { signal }) });
  const charges = useMemo(() => {
    const term = search.trim().toLocaleLowerCase("es-MX");
    const values = query.data ?? [];
    return term ? values.filter((charge) => [charge.folio, charge.customerName, charge.description].some((value) => value?.toLocaleLowerCase("es-MX").includes(term))) : values;
  }, [query.data, search]);
  if (query.isPending) return <LoadingState label="Cargando cobros…" />;
  if (query.isError) return <ErrorState error={query.error} retry={() => void query.refetch()} />;
  return <section className="page"><PageHeading eyebrow="Cobranza" title="Cobros" action={<Link className="button primary" to={`/app/${businessId}/cobros/nuevo`}>Nuevo cobro</Link>} />
    <section className="list-toolbar"><label className="search"><span aria-hidden="true">⌕</span><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Buscar folio, cliente o concepto" aria-label="Buscar cobros" /></label><span className="muted">{charges.length} mostrados</span></section>
    {charges.length === 0 ? <EmptyState title="Aún no hay cobros" detail="Crea el primer cobro cuando tengas un cliente y un importe definidos." action={<Link className="button primary" to={`/app/${businessId}/cobros/nuevo`}>Crear cobro</Link>} /> : <section className="charge-table panel"><div className="charge-row charge-head"><span>Folio</span><span>Cliente</span><span>Concepto</span><span>Vence</span><span>Importe</span><span>Saldo</span><span>Estado</span></div>{charges.map((charge) => {
      const state = chargeState(charge);
      return <Link key={charge.chargeId} className="charge-row" to={`/app/${businessId}/cobros/${charge.chargeId}`}><strong className="folio">{charge.folio}</strong><span>{charge.customerName}</span><span className="truncate">{charge.description}</span><span>{formatDate(charge.dueDate)}</span><strong className="money">{formatMxn(charge.amountMinor)}</strong><strong className="money">{formatMxn(charge.outstandingMinor)}</strong><StatusPill tone={state.tone}>{state.label}</StatusPill></Link>;
    })}</section>}
  </section>;
}
