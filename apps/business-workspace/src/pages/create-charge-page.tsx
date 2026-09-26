import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "../api/client";
import { useStaffApi } from "../app-context";
import { ErrorState } from "../components/states";
import { clearOperation, getOrCreateOperation, newKey } from "../lib/idempotency";
import { formatMxn, parseMxnToMinor } from "../lib/money";
import { PageHeading } from "./today-page";

type FormState = { customerId: string; customerName: string; email: string; description: string; amount: string; dueDate: string };
const initialForm: FormState = { customerId: "", customerName: "", email: "", description: "", amount: "", dueDate: "" };

export function CreateChargePage() {
  const api = useStaffApi();
  const { businessId = "" } = useParams();
  const navigate = useNavigate();
  const client = useQueryClient();
  const [form, setForm] = useState<FormState>(initialForm);
  const [draftId, setDraftId] = useState(() => crypto.randomUUID());
  const [result, setResult] = useState<{ folio: string; paymentUrl: string; chargeId: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const amountMinor = useMemo(() => parseMxnToMinor(form.amount), [form.amount]);
  const customers = useQuery({ queryKey: ["customers", businessId, api.source], queryFn: ({ signal }) => api.listCustomers(businessId, { signal }) });
  const selectedCustomer = customers.data?.items.find((item) => item.customerId === form.customerId);

  const create = useMutation({
    mutationFn: async () => {
      if (!amountMinor || (!form.customerId && !form.customerName.trim()) || !form.description.trim() || !form.dueDate) throw new Error("Completa cliente, concepto, importe y fecha de vencimiento.");
      const operation = getOrCreateOperation("create-charge", draftId, () => ({
        customerKey: newKey("staff-customer"),
        chargeKey: newKey("staff-charge"),
      }));
      const customer = form.customerId
        ? { customerId: form.customerId }
        : await api.createCustomer(businessId, { displayName: form.customerName.trim(), email: form.email.trim() || undefined }, operation.customerKey);
      return api.createCharge(businessId, {
        customerId: customer.customerId,
        amountMinor,
        currency: "MXN",
        description: form.description.trim(),
        dueDate: form.dueDate,
      }, operation.chargeKey);
    },
    onSuccess: (value) => {
      clearOperation("create-charge", draftId);
      setResult(value);
      void client.invalidateQueries({ queryKey: ["charges", businessId] });
    },
    onError: (value) => setError(messageFor(value)),
  });

  function update<K extends keyof FormState>(key: K, value: FormState[K]) { setForm((current) => ({ ...current, [key]: value })); }
  function resetAttempt() { clearOperation("create-charge", draftId); setDraftId(crypto.randomUUID()); setError(null); }
  async function share() {
    if (!result) return;
    if (navigator.share) await navigator.share({ title: `Cobro ${result.folio}`, text: `Comparte este enlace para pagar ${result.folio}.`, url: result.paymentUrl });
    else await navigator.clipboard.writeText(result.paymentUrl);
  }

  if (result) return <section className="page"><PageHeading eyebrow="Cobranza" title="Cobro creado" />
    <section className="success-card"><span className="success-icon" aria-hidden="true">✓</span><p className="eyebrow">Folio {result.folio}</p><h1>El enlace está listo para compartir.</h1><p>El pago seguirá pendiente hasta que Mercado Pago sea confirmado y conciliado por el servidor.</p><div className="share-url"><code>{result.paymentUrl}</code><button className="button secondary" onClick={() => void navigator.clipboard.writeText(result.paymentUrl)}>Copiar</button></div><div className="button-row"><button className="button primary" onClick={() => void share()}>Compartir</button><Link className="button secondary" to={`/app/${businessId}/cobros/${result.chargeId}`}>Ver cobro</Link></div></section>
  </section>;

  if (customers.isError) return <section className="page"><PageHeading eyebrow="Cobranza" title="Nuevo cobro" />
    <ErrorState error={customers.error} retry={() => void customers.refetch()} />
  </section>;

  return <section className="page"><PageHeading eyebrow="Cobranza" title="Nuevo cobro" action={<Link className="button secondary" to={`/app/${businessId}/cobros`}>Cancelar</Link>} />
    <form className="create-layout" onSubmit={(event) => { event.preventDefault(); setError(null); create.mutate(); }}>
      <section className="panel form-panel"><p className="step-label">1 · Cliente</p><h2>¿A quién corresponde?</h2><label>Cliente existente<select value={form.customerId} onChange={(event) => update("customerId", event.target.value)} disabled={customers.isPending}><option value="">Crear cliente nuevo</option>{customers.data?.items.map((customer) => <option value={customer.customerId} key={customer.customerId}>{customer.displayName}{customer.email ? ` · ${customer.email}` : ""}</option>)}</select></label>{customers.data?.hasMore && <p className="field-help">Aquí aparecen los 200 clientes más recientes.</p>}{form.customerId ? <p className="field-help">Usarás el registro de {selectedCustomer?.displayName ?? "cliente seleccionado"}. No se creará un duplicado.</p> : <><label>Nombre o razón de cliente<input value={form.customerName} onChange={(event) => update("customerName", event.target.value)} autoComplete="name" required /></label><label>Correo electrónico <span className="optional">opcional</span><input value={form.email} onChange={(event) => update("email", event.target.value)} type="email" autoComplete="email" /></label><p className="field-help">El cliente nuevo se crea una sola vez aunque tengas que reintentar el envío.</p></>}</section>
      <section className="panel form-panel"><p className="step-label">2 · Cobro</p><h2>Define la obligación</h2><label>Concepto<input value={form.description} onChange={(event) => update("description", event.target.value)} maxLength={240} required /></label><label>Importe en MXN<input inputMode="decimal" value={form.amount} onChange={(event) => update("amount", event.target.value)} placeholder="1250.00" required aria-describedby="amount-help" /></label><p className="field-help" id="amount-help">{amountMinor ? `Se guardará como ${formatMxn(amountMinor)} en centavos enteros.` : "Usa un importe mayor que cero, con máximo dos decimales."}</p><label>Fecha de vencimiento<input type="date" value={form.dueDate} onChange={(event) => update("dueDate", event.target.value)} required /></label></section>
      <section className="panel confirmation-panel"><p className="step-label">3 · Confirmar</p><h2>Revisa antes de crear</h2><dl><div><dt>Cliente</dt><dd>{(selectedCustomer?.displayName ?? form.customerName) || "—"}</dd></div><div><dt>Concepto</dt><dd>{form.description || "—"}</dd></div><div><dt>Importe</dt><dd className="money">{amountMinor ? formatMxn(amountMinor) : "—"}</dd></div><div><dt>Vence</dt><dd>{form.dueDate || "—"}</dd></div></dl>{error && <div className="form-error" role="alert">{error}</div>}<button className="button primary wide" disabled={create.isPending || customers.isPending}>{create.isPending ? "Creando cobro…" : "Crear cobro"}</button>{error && <button type="button" className="text-button" onClick={resetAttempt}>Restablecer intento para datos distintos</button>}<p className="field-help">Un reintento conserva sus claves de idempotencia. No se inicia ningún checkout ni cobro desde esta pantalla.</p></section>
    </form>
  </section>;
}

function messageFor(error: unknown): string {
  if (error instanceof ApiError && error.status === 409) return "La operación está en conflicto. Reintenta con los mismos datos; no generes un segundo cobro.";
  if (error instanceof ApiError && error.status === 403) return "Tu sesión no puede crear cobros para este negocio.";
  if (error instanceof ApiError && error.code === "request_timeout") return "El servidor tardó demasiado en responder. Puede que el cobro sí se haya creado: reintenta con los mismos datos y usaremos la misma clave, sin duplicarlo.";
  if (error instanceof ApiError && error.status === 0) return "Sin conexión. Conservamos este intento para que puedas reintentarlo con seguridad.";
  return error instanceof Error ? error.message : "No se pudo crear el cobro.";
}
