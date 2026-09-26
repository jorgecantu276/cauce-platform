from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from payments.models import CheckoutResult
from payments.service import InvalidChargeLink
from payments.transport import _charge_html, handle_public, handle_webhook
from payments.api_gateway import request_base_url


NOW = datetime(2026, 9, 11, 18, 0, tzinfo=timezone.utc)


class PublicFlow:
    def __init__(self):
        self.started = []

    def get_charge(self, token):
        if token == "missing-charge-token":
            raise InvalidChargeLink()
        return {"folio": "TN-000001", "amountMinor": 12_500, "outstandingMinor": 12_500}

    def start_checkout(self, token, key):
        self.started.append((token, key))
        return CheckoutResult("attempt-1", "ready", "https://sandbox.example/pref-1")


class PublicRuntime:
    def __init__(self):
        self.flow = PublicFlow()

    def read_flow(self):
        return self.flow

    def checkout_flow(self, token):
        return self.flow


def public_event(method, token="secure-charge-token", body=None, headers=None):
    return {
        "requestContext": {"http": {"method": method}},
        "pathParameters": {"token": token},
        "headers": headers or {},
        "body": body,
    }


def test_public_get_returns_only_server_charge_projection_with_no_store():
    response = handle_public(public_event("GET"), PublicRuntime())
    assert response["statusCode"] == 200
    assert response["headers"]["Cache-Control"] == "no-store"
    assert json.loads(response["body"])["amountMinor"] == 12_500


def test_invalid_link_returns_json_for_api_callers():
    response = handle_public(public_event("GET", token="missing-charge-token"), PublicRuntime())
    assert response["statusCode"] == 404
    assert response["headers"]["Content-Type"].startswith("application/json")
    assert json.loads(response["body"]) == {"error": "charge_not_found"}


def test_invalid_revoked_expired_or_cancelled_links_render_the_same_generic_branded_page():
    event = public_event("GET", token="missing-charge-token", headers={"Accept": "text/html"})
    response = handle_public(event, PublicRuntime())
    assert response["statusCode"] == 404
    assert response["headers"]["Content-Type"].startswith("text/html")
    assert response["headers"]["Cache-Control"] == "no-store"
    assert response["headers"]["Content-Security-Policy"]
    assert "Este enlace ya no está disponible" in response["body"]
    # No financial or identity details leak into the generic error page, and it
    # never lets a visitor tell "never existed" apart from "cancelled"/"expired".
    for leaked in ("amountMinor", "folio", "TN-000001", "12500", "12,500"):
        assert leaked not in response["body"]


def test_public_api_origin_comes_from_trusted_gateway_metadata_not_headers():
    event = public_event("POST", headers={"Host": "attacker.example"})
    event["requestContext"].update({"domainName": "api-id.execute-api.us-east-1.amazonaws.com", "stage": "v1"})
    assert request_base_url(event) == "https://api-id.execute-api.us-east-1.amazonaws.com/v1"
    assert request_base_url({"requestContext": {"domainName": "api.example", "stage": "$default"}}) == "https://api.example"


def test_browser_get_renders_safe_status_page_and_ignores_redirect_claims():
    runtime = PublicRuntime()
    runtime.flow.get_charge = lambda token: {
        "folio":"TN-1","description":"<img src=x onerror=alert(1)>",
        "dueDate":"2026-09-20","currency":"MXN","amountMinor":12500,
        "outstandingMinor":12500,"status":"pending",
    }
    event = public_event("GET",headers={"Accept":"text/html"})
    event["queryStringParameters"] = {"collection_status":"approved"}
    response = handle_public(event,runtime)
    assert response["statusCode"] == 200
    assert response["headers"]["Content-Type"].startswith("text/html")
    assert "&lt;img src=x onerror=alert(1)&gt;" in response["body"]
    assert "<img src=x" not in response["body"]
    assert "Esperando pago confirmado" in response["body"]
    assert "Pagar con Mercado Pago" in response["body"]
    assert "Mercado Pago protege tus datos" in response["body"]


def test_browser_page_uses_completed_copy_for_a_paid_charge():
    runtime = PublicRuntime()
    runtime.flow.get_charge = lambda token: {
        "folio":"TN-1","description":"Anticipo","dueDate":"2026-09-20",
        "currency":"MXN","amountMinor":12500,"outstandingMinor":0,"status":"paid",
    }
    response = handle_public(public_event("GET",headers={"Accept":"text/html"}),runtime)
    assert "Tu pago fue confirmado" in response["body"]
    assert "Tienes un pago pendiente" not in response["body"]
    assert "20/09/2026" in response["body"]


def test_browser_page_explains_refund_and_partial_payment_states():
    base = {"folio": "TN-1", "description": "Anticipo", "dueDate": "2026-09-20",
            "currency": "MXN", "amountMinor": 12_500, "outstandingMinor": 12_500}
    for status, heading, copy in (
        ("refunded", "Este pago fue devuelto", "El pago fue devuelto"),
        ("reversed", "Este pago fue revertido", "El pago fue revertido"),
        ("partially_refunded", "Este cobro tuvo una devolución parcial", "Una parte del pago fue devuelta"),
        ("partially_paid", "Este cobro tiene un pago parcial", "Este cobro tiene un pago parcial"),
    ):
        page = _charge_html({**base, "status": status})
        assert heading in page
        assert copy in page
        # A refund or reversal restores some balance, so the payer can use the
        # same link again; only a confirmed fully paid charge disables checkout.
        assert 'id="pay" disabled' not in page


def test_browser_page_accepts_only_valid_tenant_color_tokens():
    charge = {"folio":"TN-1","description":"Anticipo","dueDate":"2026-09-20",
              "currency":"MXN","amountMinor":12500,"outstandingMinor":12500,"status":"pending",
              "branding":{"accent":"#123456","nav":"not-a-color"}}
    page = _charge_html(charge)
    assert "#123456" in page
    assert "not-a-color" not in page


def test_public_attempt_requires_idempotency_key_and_rejects_buyer_amount():
    runtime = PublicRuntime()
    missing = handle_public(public_event("POST", body="{}"), runtime)
    assert missing["statusCode"] == 400
    supplied_amount = handle_public(public_event(
        "POST", body=json.dumps({"amountMinor": 1}),
        headers={"Idempotency-Key": "browser-request-123"},
    ), runtime)
    assert supplied_amount["statusCode"] == 400
    assert runtime.flow.started == []


def test_public_attempt_returns_checkout_without_accepting_payment_fields():
    runtime = PublicRuntime()
    response = handle_public(public_event(
        "POST", body="{}", headers={"IDEMPOTENCY-KEY": "browser-request-123"}
    ), runtime)
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {
        "attemptId": "attempt-1",
        "status": "ready",
        "checkoutUrl": "https://sandbox.example/pref-1",
    }
    assert runtime.flow.started == [("secure-charge-token", "browser-request-123")]


class InvalidWebhookSignatureError(Exception):
    pass


class Validator:
    calls = []
    invalid = False

    @classmethod
    def validate(cls, *args, **kwargs):
        cls.calls.append((args, kwargs))
        if cls.invalid:
            raise InvalidWebhookSignatureError()


class WebhookRepository:
    def __init__(self, sequence):
        self.sequence = sequence
        self.status = "accepted"
        self.failed = []
        self.review = []

    def capture_provider_event(self, connection_id, event_key, resource_id,
                               event_type, payload, signature_valid, now):
        self.sequence.append("captured")
        return {"id": "event-db-1", "status": self.status}

    def mark_provider_event_failed(self, connection_id, event_key, error, now):
        self.failed.append(error)

    def mark_provider_event_review(self, connection_id, event_key, reason, now):
        self.review.append(reason)


class WebhookFlow:
    def __init__(self, sequence):
        self.sequence = sequence
        self.fail = False

    def reconcile_payment(self, event_key, resource_id, expected_connection_id=None):
        self.sequence.append("reconciled")
        assert expected_connection_id == "connection-1"
        if self.fail:
            raise RuntimeError("provider unavailable")


@dataclass
class Context:
    connection_id: str
    repository: object
    flow: object
    webhook_secret: str = "webhook-secret"


class WebhookRuntime:
    def __init__(self):
        self.sequence = []
        self.repo = WebhookRepository(self.sequence)
        self.flow = WebhookFlow(self.sequence)

    def webhook_context(self, connection_id):
        assert connection_id == "connection-1"
        return Context(connection_id, self.repo, self.flow)


def webhook_event(body_id="pay-1", query_id="pay-1"):
    return {
        "pathParameters": {"connectionId": "connection-1"},
        "headers": {
            "X-Signature": "ts=1,v1=fake",
            "X-Request-Id": "request-1",
        },
        "queryStringParameters": {"data.id": query_id, "type": "payment"},
        "body": json.dumps({"type": "payment", "data": {"id": body_id}}),
    }


def test_webhook_validates_then_durably_queues_without_provider_call():
    Validator.calls = []
    Validator.invalid = False
    runtime = WebhookRuntime()
    response = handle_webhook(webhook_event(), runtime, Validator)
    assert response["statusCode"] == 202
    assert json.loads(response["body"]) == {"received": True, "queued": True}
    assert runtime.sequence == ["captured"]
    args, kwargs = Validator.calls[0]
    assert args == ("ts=1,v1=fake", "request-1", "pay-1", "webhook-secret")
    assert kwargs == {"tolerance_seconds": 300}


def test_invalid_signature_is_not_persisted_or_processed():
    Validator.invalid = True
    runtime = WebhookRuntime()
    response = handle_webhook(webhook_event(), runtime, Validator)
    assert response["statusCode"] == 401
    assert runtime.sequence == []
    Validator.invalid = False


def test_duplicate_processed_event_is_acknowledged_without_processing_again():
    runtime = WebhookRuntime()
    runtime.repo.status = "processed"
    response = handle_webhook(webhook_event(), runtime, Validator)
    assert response["statusCode"] == 200
    assert runtime.sequence == ["captured"]


def test_mismatched_signed_resource_is_quarantined():
    runtime = WebhookRuntime()
    response = handle_webhook(webhook_event(body_id="pay-2", query_id="pay-1"), runtime, Validator)
    assert response["statusCode"] == 200
    assert runtime.repo.review == ["resource_id_mismatch"]
    assert runtime.sequence == ["captured"]


def test_provider_outage_cannot_prevent_durable_webhook_acceptance():
    runtime = WebhookRuntime()
    runtime.flow.fail = True
    response = handle_webhook(webhook_event(), runtime, Validator)
    assert response["statusCode"] == 202
    assert runtime.sequence == ["captured"]
    assert runtime.repo.failed == []


def test_current_official_sdk_accepts_the_documented_signature_manifest():
    from mercadopago.webhook import WebhookSignatureValidator

    runtime = WebhookRuntime()
    event = webhook_event()
    timestamp = str(int(time.time()))
    manifest = f"id:pay-1;request-id:request-1;ts:{timestamp};"
    digest = hmac.new(b"webhook-secret", manifest.encode(), hashlib.sha256).hexdigest()
    event["headers"]["X-Signature"] = f"ts={timestamp},v1={digest}"
    response = handle_webhook(event, runtime, WebhookSignatureValidator)
    assert response["statusCode"] == 202
    assert runtime.sequence == ["captured"]
