from datetime import date, datetime, timezone
import hashlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from payments.models import Charge, MerchantConnection, MerchantRuntimeConfig
from payments.runtime import Runtime


NOW = datetime(2026, 9, 11, 18, 0, tzinfo=timezone.utc)
TOKEN = "secure-charge-token"


class Resolver:
    def field(self, reference, field):
        return {
            ("arn:token", "access_token"): "private-token",
            ("arn:webhook", "webhook_secret"): "private-webhook-secret",
            ("arn:application", "link_token_hmac"): "private-link-hmac",
        }[(reference, field)]


class Repository:
    instances = []

    def __init__(self, table_name, webhook_connection_id=None):
        assert table_name == "payments-test"
        self.webhook_connection_id = webhook_connection_id
        self.charge = Charge(
            "charge-1", "business-1", "customer-1", "TN-000001",
            12_500, 12_500, "MXN", "Anticipo", date(2026, 9, 20),
        )
        self.connection = MerchantConnection(
            "connection-1", "business-1", "mercado_pago", "seller-1", "test", NOW
        )
        Repository.instances.append(self)

    def find_charge_by_token_digest(self, digest, now):
        return self.charge if digest == hashlib.sha256(TOKEN.encode()).digest() else None

    def active_connection(self, business_id, provider):
        return self.connection

    def connection_runtime_config(self, connection_id):
        return MerchantRuntimeConfig(
            self.connection, "arn:token", "arn:webhook", "test_credentials"
        )


class Provider:
    calls = []

    def __init__(self, access_token, expected_account_id, **kwargs):
        Provider.calls.append((access_token, expected_account_id, kwargs))


def runtime():
    return Runtime(
        environ={
            "PAYMENTS_TABLE_NAME": "payments-test",
            "PAYMENT_PUBLIC_BASE_URL": "https://pay.example",
            "PUBLIC_API_BASE_URL": "https://api.example/v1",
            "APPLICATION_SECRET_ARN": "arn:application",
        },
        secret_resolver=Resolver(),
        repository_factory=Repository,
        provider_factory=Provider,
        clock=lambda: NOW,
    )


def test_checkout_runtime_loads_secrets_by_reference_and_builds_status_urls():
    Provider.calls = []
    flow = runtime().checkout_flow(TOKEN)
    assert flow.repository.charge.id == "charge-1"
    token, account, options = Provider.calls[0]
    assert token == "private-token"
    assert account == "seller-1"
    assert options == {
        "credential_source": "test_credentials",
        "return_url": "https://pay.example/pay/secure-charge-token",
        "notification_url": "https://api.example/v1/webhooks/mercado-pago/connection-1",
    }


def test_checkout_runtime_can_use_the_trusted_api_gateway_origin_for_all_public_urls():
    Provider.calls = []
    Runtime(
        environ={"PAYMENTS_TABLE_NAME": "payments-test"},
        secret_resolver=Resolver(), repository_factory=Repository, provider_factory=Provider, clock=lambda: NOW,
        public_api_base_url="https://api-id.execute-api.us-east-1.amazonaws.com/v1",
    ).checkout_flow(TOKEN)
    assert Provider.calls[-1][2]["return_url"] == "https://api-id.execute-api.us-east-1.amazonaws.com/v1/pay/secure-charge-token"
    assert Provider.calls[-1][2]["notification_url"] == "https://api-id.execute-api.us-east-1.amazonaws.com/v1/webhooks/mercado-pago/connection-1"


def test_webhook_runtime_binds_repository_and_both_secrets_to_connection():
    Provider.calls = []
    context = runtime().webhook_context("connection-1")
    assert context.connection_id == "connection-1"
    assert context.repository.webhook_connection_id == "connection-1"
    assert context.webhook_secret == "private-webhook-secret"
    assert Provider.calls[0][2] == {"credential_source": "test_credentials"}


def test_staff_runtime_loads_link_hmac_from_secret_manager():
    staff = runtime().staff_service()
    assert staff.public_base_url == "https://pay.example"
    assert staff.link_token_secret == b"private-link-hmac"


def test_staff_runtime_uses_the_trusted_api_gateway_origin_when_available():
    staff = Runtime(
        environ={"PAYMENTS_TABLE_NAME": "payments-test", "APPLICATION_SECRET_ARN": "arn:application"},
        secret_resolver=Resolver(), repository_factory=Repository, provider_factory=Provider, clock=lambda: NOW,
        public_api_base_url="https://api-id.execute-api.us-east-1.amazonaws.com/v1",
    ).staff_service()
    assert staff.public_base_url == "https://api-id.execute-api.us-east-1.amazonaws.com/v1"
