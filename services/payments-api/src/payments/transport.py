import base64
from datetime import datetime, timezone
import hashlib
import json

from payment_page.render import charge_html, error_html
from payments.service import InvalidChargeLink, SUBMISSION_KEY_RE

def _headers(event):
    return {str(k).lower(): str(v) for k, v in (event.get("headers") or {}).items()}


def _body_text(event):
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        return base64.b64decode(raw).decode("utf-8")
    return raw


def _json_response(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "no-store",
        },
        "body": json.dumps(body, separators=(",", ":")),
    }


def _html_response(status, body):
    return {
        "statusCode":status,
        "headers":{
            "Content-Type":"text/html; charset=utf-8",
            "Cache-Control":"no-store",
            "Content-Security-Policy":(
                "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                "connect-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'"
            ),
            "Referrer-Policy":"no-referrer",
            "X-Content-Type-Options":"nosniff",
        },
        "body":body,
    }


# Compatibility aliases for the former private renderer names.
_charge_html = charge_html
_error_html = error_html
def handle_public(event, runtime):
    method = ((event.get("requestContext") or {}).get("http") or {}).get("method", "")
    token = (event.get("pathParameters") or {}).get("token", "")
    wants_html = method == "GET" and "text/html" in _headers(event).get("accept","").lower()
    try:
        if method == "GET":
            charge = runtime.read_flow().get_charge(token)
            if wants_html:
                return _html_response(200,_charge_html(charge))
            return _json_response(200,charge)
        if method == "POST":
            raw = _body_text(event)
            if raw:
                try:
                    body = json.loads(raw)
                except (ValueError, TypeError):
                    return _json_response(400, {"error": "invalid_request"})
                if body not in ({}, None):
                    return _json_response(400, {"error": "charge_fields_are_server_controlled"})
            submission_key = _headers(event).get("idempotency-key", "")
            if not SUBMISSION_KEY_RE.fullmatch(submission_key):
                return _json_response(400, {"error": "invalid_idempotency_key"})
            result = runtime.checkout_flow(token).start_checkout(token, submission_key)
            status = 202 if result.status == "recovering" else 200
            return _json_response(status, {
                "attemptId": result.attempt_id or None,
                "status": result.status,
                "checkoutUrl": result.checkout_url,
            })
        return _json_response(405, {"error": "method_not_allowed"})
    except InvalidChargeLink:
        if wants_html:
            return _html_response(404, _error_html("Este enlace ya no está disponible", "Puede haber vencido, sido cancelado o ya no ser válido. Contacta al negocio para obtener un enlace nuevo."))
        return _json_response(404, {"error": "charge_not_found"})
    except ValueError:
        return _json_response(400, {"error": "invalid_request"})
    except RuntimeError:
        if wants_html:
            return _html_response(503, _error_html("No pudimos cargar tu pago", "Ocurrió un problema temporal. Intenta de nuevo en unos minutos."))
        return _json_response(503, {"error": "payment_temporarily_unavailable"})


def handle_webhook(event, runtime, signature_validator=None):
    if signature_validator is None:
        from mercadopago.webhook import WebhookSignatureValidator
        signature_validator = WebhookSignatureValidator

    connection_id = (event.get("pathParameters") or {}).get("connectionId", "")
    headers = _headers(event)
    query = event.get("queryStringParameters") or {}
    data_id = query.get("data.id") or query.get("data_id")
    raw = _body_text(event)
    try:
        payload = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return _json_response(400, {"error": "invalid_request"})

    try:
        context_factory = getattr(runtime, "webhook_ingest_context", runtime.webhook_context)
        context = context_factory(connection_id)
    except RuntimeError:
        return _json_response(404, {"error": "webhook_not_found"})

    try:
        signature_validator.validate(
            headers.get("x-signature"),
            headers.get("x-request-id"),
            data_id,
            context.webhook_secret,
            tolerance_seconds=300,
        )
    except Exception as exc:
        # The SDK exposes version-specific exception classes. No exception
        # detail or signature material is returned or logged here.
        if exc.__class__.__name__ in (
            "InvalidWebhookSignatureError",
            "InvalidWebhookSignatureException",
        ):
            return _json_response(401, {"error": "invalid_signature"})
        raise

    body_data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    body_id = body_data.get("id")
    resource_id = str(data_id or body_id or "")
    event_type = str(payload.get("type") or query.get("type") or "")
    if not resource_id:
        return _json_response(400, {"error": "missing_resource"})
    event_key = headers.get("x-request-id") or hashlib.sha256(
        (headers.get("x-signature", "") + "\n" + raw).encode()
    ).hexdigest()
    now = datetime.now(timezone.utc)
    try:
        captured = context.repository.capture_provider_event(
            connection_id, event_key, resource_id, event_type, payload, True, now
        )
    except Exception:
        return _json_response(503, {"error": "temporary_failure"})
    if captured["status"] in ("processed", "review"):
        return _json_response(200, {"received": True})
    if body_id is not None and data_id is not None and str(body_id) != str(data_id):
        context.repository.mark_provider_event_review(
            connection_id, event_key, "resource_id_mismatch", now
        )
        return _json_response(200, {"received": True})
    if event_type != "payment":
        context.repository.mark_provider_event_review(
            connection_id, event_key, "unsupported_event_type", now
        )
        return _json_response(200, {"received": True})
    # Acknowledge after durable acceptance. Provider retrieval and financial
    # writes happen in a leased worker, so an unavailable provider cannot make
    # webhook delivery exceed its request budget.
    return _json_response(202, {"received": True, "queued": True})
