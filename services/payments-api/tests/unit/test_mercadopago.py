from io import BytesIO
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
import sys
import urllib.parse

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from payments.mercadopago import MercadoPagoSandbox
from payments.models import CheckoutRequest


class Response:
    def __init__(self, data):
        self.data = json.dumps(data).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.data


def test_sandbox_checks_token_owner_and_sends_one_generic_server_priced_item():
    calls = []

    def opener(req, timeout):
        calls.append(req)
        if req.full_url.endswith("/users/me"):
            return Response({"id": 123, "tags": ["test_user"]})
        body = json.loads(req.data)
        assert body["items"] == [{
            "id": "attempt-1",
            "title": "Anticipo",
            "description": "Anticipo",
            "quantity": 1,
            "unit_price": 125.5,
            "currency_id": "MXN",
        }]
        assert body["back_urls"] == {
            "success": "https://pay.example/pay/secure-token",
            "failure": "https://pay.example/pay/secure-token",
            "pending": "https://pay.example/pay/secure-token",
        }
        assert body["notification_url"].endswith("/connection-1")
        return Response({
            "id": "pref-1",
            "init_point": "https://www.example/pref-1",
            "sandbox_init_point": "https://sandbox.example/pref-1",
            "external_reference": "attempt-1",
            "items": body["items"],
        })

    client = MercadoPagoSandbox(
        "APP_USR-secret-not-a-mode-signal",
        "123",
        credential_source="test_credentials",
        return_url="https://pay.example/pay/secure-token",
        notification_url="https://api.example/webhooks/mercado-pago/connection-1",
        opener=opener,
    )
    checkout = client.create_checkout(CheckoutRequest(
        external_reference="attempt-1",
        operation_key="operation-1",
        description="Anticipo",
        amount_minor=12_550,
        currency="MXN",
        provider_account_id="123",
        environment="test",
    ))
    assert checkout.amount_minor == 12_550
    assert checkout.environment == "test"
    assert checkout.checkout_url == "https://www.example/pref-1"
    assert len(calls) == 2


def test_token_prefix_is_never_accepted_as_environment_verification():
    with pytest.raises(ValueError, match="Test credentials"):
        MercadoPagoSandbox(
            "APP_USR-could-be-production",
            "123",
            credential_source="token_prefix",
        )


def test_refund_uses_stable_provider_idempotency_key_and_minor_unit_conversion():
    calls = []

    def opener(req, timeout):
        if req.full_url.endswith("/users/me"):
            return Response({"id":123,"tags":["test_user"]})
        calls.append(req)
        return Response({"id":44,"status":"approved"})

    client = MercadoPagoSandbox(
        "secret","123",credential_source="test_credentials",opener=opener
    )
    result = client.request_refund("pay-1","refund-operation-123",2550)
    assert result == {"id":"44","status":"approved"}
    assert calls[0].get_header("X-idempotency-key") == "refund-operation-123"
    assert json.loads(calls[0].data) == {"amount":25.5}


def test_refund_accepts_a_whole_number_decimal_amount_but_rejects_a_fraction():
    """A partial refund's amount_minor read back from a real DynamoDB query
    (boto3.resource) comes back as Decimal, not int -- this must work exactly
    like the plain-int case above, and a fractional Decimal (which should
    never occur for minor units, but must never be silently truncated either)
    must be rejected rather than sent to the provider."""
    calls = []

    def opener(req, timeout):
        if req.full_url.endswith("/users/me"):
            return Response({"id": 123, "tags": ["test_user"]})
        calls.append(req)
        return Response({"id": "44", "status": "approved"})

    client = MercadoPagoSandbox(
        "secret", "123", credential_source="test_credentials", opener=opener
    )
    result = client.request_refund("pay-1", "refund-operation-decimal", Decimal("2550"))
    assert result == {"id": "44", "status": "approved"}
    assert json.loads(calls[0].data) == {"amount": 25.5}

    with pytest.raises(ValueError):
        client.request_refund("pay-1", "refund-operation-fraction", Decimal("25.5"))


def test_obsolete_checkout_is_expired_with_an_idempotent_preference_update():
    calls = []

    def opener(req, timeout):
        if req.full_url.endswith("/users/me"):
            return Response({"id":123,"tags":["test_user"]})
        calls.append(req)
        return Response({"id":"pref-1","expires":True})

    client = MercadoPagoSandbox(
        "secret","123",credential_source="test_credentials",opener=opener
    )
    client.expire_checkout("pref-1",datetime(2026,9,11,18,0,tzinfo=timezone.utc))
    assert calls[0].method == "PUT"
    assert calls[0].full_url.endswith("/checkout/preferences/pref-1")
    body = json.loads(calls[0].data)
    assert body["expires"] is True
    assert body["expiration_date_to"] == "2026-09-11T18:00:00.000+00:00"


def test_credential_owner_must_match_configured_test_seller():
    client = MercadoPagoSandbox(
        "secret",
        "123",
        credential_source="test_credentials",
        opener=lambda req, timeout: Response({"id": 999, "tags": ["test_user"]}),
    )
    with pytest.raises(RuntimeError, match="does not match"):
        client.verify_connection()


def test_credential_owner_must_be_marked_as_a_test_user_by_provider():
    client = MercadoPagoSandbox(
        "secret",
        "123",
        credential_source="test_credentials",
        opener=lambda req, timeout: Response({"id": 123, "tags": ["normal"]}),
    )
    with pytest.raises(RuntimeError, match="not a verified test user"):
        client.verify_connection()


def test_preference_recovery_reads_current_elements_collection_and_init_point():
    def opener(req, timeout):
        if req.full_url.endswith("/users/me"):
            return Response({"id": 123, "tags": ["test_user"]})
        return Response({
            "elements": [{
                "id": "pref-1",
                "init_point": "https://www.example/pref-1",
                "sandbox_init_point": "https://sandbox.example/pref-1",
                "external_reference": "attempt-1",
                "items": [{
                    "quantity": 1,
                    "unit_price": 125.5,
                    "currency_id": "MXN",
                }],
            }],
        })

    client = MercadoPagoSandbox(
        "secret", "123", credential_source="test_credentials", opener=opener
    )
    checkout = client.recover_checkout("attempt-1")
    assert checkout.preference_id == "pref-1"
    assert checkout.checkout_url == "https://www.example/pref-1"
    assert checkout.amount_minor == 12_550


def test_payment_reconciliation_search_paginates_and_keeps_each_matching_payment():
    offsets = []

    def opener(req, timeout):
        if req.full_url.endswith("/users/me"):
            return Response({"id":123,"tags":["test_user"]})
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)
        offset = int(query["offset"][0])
        offsets.append(offset)
        if offset == 0:
            return Response({"paging":{"total":3},"results":[
                {"id":1,"external_reference":"attempt-1","date_last_updated":"one"},
                {"id":99,"external_reference":"other-attempt","date_last_updated":"other"},
            ]})
        return Response({"paging":{"total":3},"results":[
            {"id":2,"external_reference":"attempt-1","date_last_updated":"two"},
        ]})

    client = MercadoPagoSandbox(
        "secret","123",credential_source="test_credentials",opener=opener
    )
    assert client.search_payments("attempt-1",page_size=2) == [
        {"id":"1","updated_at":"one"},{"id":"2","updated_at":"two"},
    ]
    assert offsets == [0,2]


@pytest.mark.parametrize(
    "provider_live_mode",
    [False, True],
)
def test_verified_sandbox_payment_keeps_live_mode_as_evidence_and_refunds(provider_live_mode):
    def opener(req, timeout):
        if req.full_url.endswith("/users/me"):
            return Response({"id": 123, "tags": ["test_user"]})
        return Response({
            "id": 9001,
            "collector_id": 123,
            "live_mode": provider_live_mode,
            "external_reference": "attempt-1",
            "transaction_amount": 125.50,
            "currency_id": "MXN",
            "status": "refunded",
            "date_approved": "2026-09-11T12:00:00Z",
            "refunds": [{
                "id": 44,
                "amount": 125.50,
                "status": "approved",
                "date_created": "2026-09-12T12:00:00Z",
            }],
        })

    client = MercadoPagoSandbox(
        "secret", "123", credential_source="test_credentials", opener=opener
    )
    payment = client.get_payment("9001")
    assert payment.provider_account_id == "123"
    assert payment.environment == "test"
    assert payment.provider_live_mode is provider_live_mode
    assert payment.amount_minor == 12_550
    assert payment.adjustments[0].amount_minor == 12_550


def test_verified_sandbox_payment_without_live_mode_is_still_test_scoped():
    def opener(req, timeout):
        if req.full_url.endswith("/users/me"):
            return Response({"id": 123, "tags": ["test_user"]})
        return Response({
            "id": 9003,
            "collector_id": 123,
            "external_reference": "attempt-1",
            "transaction_amount": 125.50,
            "currency_id": "MXN",
            "status": "approved",
        })

    payment = MercadoPagoSandbox(
        "secret", "123", credential_source="test_credentials", opener=opener
    ).get_payment("9003")
    assert payment.environment == "test"
    assert payment.provider_live_mode is None


def test_chargeback_after_partial_refund_records_only_remaining_financial_effect():
    def opener(req, timeout):
        if req.full_url.endswith("/users/me"):
            return Response({"id":123,"tags":["test_user"]})
        return Response({
            "id":9002,"collector_id":123,"external_reference":"attempt-1",
            "transaction_amount":125.00,"currency_id":"MXN","status":"charged_back",
            "date_last_updated":"2026-09-12T13:00:00Z",
            "refunds":[{"id":45,"amount":25.00,"status":"approved",
                        "date_created":"2026-09-12T12:00:00Z"}],
        })

    payment = MercadoPagoSandbox(
        "secret","123",credential_source="test_credentials",opener=opener
    ).get_payment("9002")
    assert [(item.kind,item.amount_minor) for item in payment.adjustments] == [
        ("refund",2500),("chargeback",10000),
    ]
