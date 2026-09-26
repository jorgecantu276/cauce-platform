from datetime import datetime
import html
import re


COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _color(value, fallback):
    return value if isinstance(value, str) and COLOR_RE.fullmatch(value) else fallback


def charge_html(charge):
    amount = charge["amountMinor"] / 100
    outstanding = charge["outstandingMinor"] / 100
    status = str(charge["status"])
    paid = status == "paid"
    brand_name = html.escape(str(charge.get("merchantDisplayName") or "Pago seguro"))
    folio = html.escape(str(charge["folio"]))
    description = html.escape(str(charge["description"]))
    currency = html.escape(str(charge["currency"]))
    try:
        due_date = datetime.fromisoformat(str(charge["dueDate"])).strftime("%d/%m/%Y")
    except ValueError:
        due_date = str(charge["dueDate"])
    status_copy = {
        "paid": "Pago confirmado por el proveedor.",
        "refunded": "El pago fue devuelto; el saldo volvió a estar pendiente.",
        "reversed": "El pago fue revertido; el saldo volvió a estar pendiente.",
        "partially_refunded": "Una parte del pago fue devuelta.",
        "partially_paid": "Este cobro tiene un pago parcial.",
    }.get(status, "Esperando pago confirmado por el proveedor.")
    heading = {
        "paid": "Tu pago fue confirmado",
        "refunded": "Este pago fue devuelto",
        "reversed": "Este pago fue revertido",
        "partially_refunded": "Este cobro tuvo una devolución parcial",
        "partially_paid": "Este cobro tiene un pago parcial",
    }.get(status, "Tienes un pago pendiente")
    action = "Pago confirmado" if paid else "Pagar con Mercado Pago"
    disabled = " disabled" if paid else ""
    styles = """:root{color:#17213b;background:#f6f5f2;font-family:Inter,ui-sans-serif,system-ui,sans-serif}*{box-sizing:border-box}body{min-width:320px;min-height:100vh;margin:0;display:grid;place-items:center;padding:24px;background:radial-gradient(circle at 85% 0,#f9d9ca88,transparent 22rem),#f6f5f2}main{width:min(100%,480px)}.brand{display:flex;align-items:center;gap:10px;margin:0 0 19px 8px;color:#283653;font-size:.74rem;font-weight:850;letter-spacing:.12em;text-transform:uppercase}.mark{width:29px;height:29px;display:grid;place-items:center;border-radius:9px;background:linear-gradient(145deg,#ffb079,#ef664b);color:#17213d}.card{overflow:hidden;border:1px solid #e3e1dd;border-radius:22px;background:#fffefd;box-shadow:0 18px 48px #18213b14}.top{padding:29px 28px 24px;background:linear-gradient(135deg,#172543,#101930);color:#fff}.eyebrow{margin:0 0 10px;color:#bec8dc;font-size:.67rem;font-weight:800;letter-spacing:.13em;text-transform:uppercase}.top h1{margin:0;font-size:clamp(1.55rem,7vw,2.15rem);line-height:1.08;letter-spacing:-.045em}.folio{margin:11px 0 0;color:#aebbd1;font-size:.78rem}.details{padding:25px 28px 28px}.label{margin:0;color:#747c8d;font-size:.72rem;font-weight:760}.amount{margin:4px 0 22px;color:#17213b;font-size:2.3rem;font-weight:780}.amount small{font-size:.92rem;color:#697184}dl{display:grid;grid-template-columns:1fr auto;gap:11px;margin:0;padding:17px 0;border-block:1px solid #e9e7e2;color:#6d7585;font-size:.84rem}dd{margin:0;color:#26324b;font-weight:760;text-align:right}.status{margin:18px 0;padding:12px 13px;border:1px solid #f0d99d;border-radius:11px;background:#fff7e4;color:#4e5a6f;font-size:.81rem;line-height:1.42}.status strong{display:block;margin-bottom:2px;color:#24314a}button{width:100%;min-height:52px;border:0;border-radius:12px;background:#ed684c;color:#fff;font-size:.96rem;font-weight:800}button:disabled{background:#19736e;cursor:not-allowed}.provider{margin:15px 0 0;color:#7d8492;font-size:.73rem;text-align:center}.provider b{color:#505b6d}@media(max-width:480px){body{padding:16px}.top,.details{padding-inline:22px}}"""
    branding = charge.get("branding") or {}
    accent = _color(branding.get("accent"), "#ed684c")
    styles = styles.replace("#ed684c", accent).replace("#d8563c", _color(branding.get("accentHover"), "#d8563c")).replace("#172543", _color(branding.get("navAlt"), "#172545")).replace("#101930", _color(branding.get("nav"), "#101931")).replace("#f6f5f2", _color(branding.get("canvas"), "#f5f5f2"))
    script = """<script>const b=document.getElementById('pay'),s=document.getElementById('status'),ck='charge-attempt-key:'+location.pathname;if(!b.disabled)b.onclick=async()=>{b.disabled=true;b.textContent='Abriendo pago seguro…';let k=sessionStorage.getItem(ck);if(!k){k=crypto.randomUUID();sessionStorage.setItem(ck,k)}try{const r=await fetch(location.pathname+'/attempts',{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':k},body:'{}'}),x=await r.json();if(x.checkoutUrl)location.assign(x.checkoutUrl);else{s.innerHTML='<strong>Confirmando el estado del pago…</strong>Consulta de nuevo en unos segundos.';setTimeout(()=>location.reload(),3000)}}catch(e){s.innerHTML='<strong>No pudimos iniciar el pago.</strong>Intenta de nuevo.';b.textContent='Pagar con Mercado Pago →';b.disabled=false}};</script>"""
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{description} · {brand_name}</title><style>{styles}</style></head><body><main><p class="brand"><span class="mark" aria-hidden="true">↗</span>{brand_name}</p><section class="card" aria-labelledby="charge-title"><header class="top"><p class="eyebrow">{heading}</p><h1 id="charge-title">{description}</h1><p class="folio">Folio {folio}</p></header><div class="details"><p class="label">{'Importe pagado' if paid else 'Importe por pagar'}</p><p class="amount">${amount:,.2f} <small>{currency}</small></p><dl><dt>Vence</dt><dd>{html.escape(due_date)}</dd><dt>Saldo pendiente</dt><dd>${outstanding:,.2f} {currency}</dd></dl><p class="status" id="status"><strong>{status_copy}</strong>El estado final se actualiza cuando el proveedor confirma el pago.</p><button id="pay"{disabled}>{action} <span aria-hidden="true">→</span></button><p class="provider"><b>Pago seguro</b> · Mercado Pago protege tus datos.</p></div></section></main>{script}</body></html>"""


def error_html(heading, detail):
    """Render one generic response for every unavailable opaque link."""
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(heading)}</title><style>:root{{color:#17213b;background:#f6f5f2;font-family:Inter,ui-sans-serif,system-ui,sans-serif}}*{{box-sizing:border-box}}body{{min-width:320px;min-height:100vh;margin:0;display:grid;place-items:center;padding:24px;background:#f6f5f2}}main{{width:min(100%,440px);text-align:center}}.card{{border:1px solid #e3e1dd;border-radius:22px;background:#fffefd;box-shadow:0 18px 48px #18213b14;padding:36px 28px}}h1{{margin:0 0 12px;font-size:1.4rem;color:#17213b}}p{{margin:0;color:#6d7585;font-size:.92rem;line-height:1.5}}</style></head><body><main><section class="card"><h1>{html.escape(heading)}</h1><p>{html.escape(detail)}</p></section></main></body></html>"""
