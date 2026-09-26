"""Repository-to-domain acceptance test; no AWS account or local daemon needed."""

from datetime import date, datetime, timezone
import hashlib
import json
import os
import sys

import boto3
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from payments.dynamodb import DynamoRepository
from payments.mercadopago import MercadoPagoSandbox
from payments.models import ProviderAdjustment, ProviderCheckout, ProviderPayment
from payments.service import PaymentFlow, assess_payment
from payments.workers import (
    run_provider_event_worker, run_reconciliation_worker, run_refund_worker,
)


NOW = datetime(2026, 9, 13, 15, 0, tzinfo=timezone.utc)


def table():
    resource = boto3.resource("dynamodb", region_name="us-east-1")
    resource.create_table(
        TableName="payments", BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"} for key in ("PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK", "GSI3PK", "GSI3SK")],
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        GlobalSecondaryIndexes=[
            {"IndexName": "LookupIndex", "KeySchema": [{"AttributeName": "GSI1PK", "KeyType": "HASH"}, {"AttributeName": "GSI1SK", "KeyType": "RANGE"}], "Projection": {"ProjectionType": "ALL"}},
            {"IndexName": "SubjectIndex", "KeySchema": [{"AttributeName": "GSI2PK", "KeyType": "HASH"}, {"AttributeName": "GSI2SK", "KeyType": "RANGE"}], "Projection": {"ProjectionType": "ALL"}},
            {"IndexName": "WorkIndex", "KeySchema": [{"AttributeName": "GSI3PK", "KeyType": "HASH"}, {"AttributeName": "GSI3SK", "KeyType": "RANGE"}], "Projection": {"ProjectionType": "ALL"}},
        ],
    )
    return resource


class Provider:
    def create_checkout(self, request):
        return ProviderCheckout("preference-1", "https://sandbox.example/checkout", request.external_reference, request.amount_minor, request.currency, request.provider_account_id, request.environment)


class WorkerProvider(Provider):
    def __init__(self, payment):
        self.payment = payment

    def get_payment(self, payment_id):
        assert payment_id == self.payment.provider_payment_id
        return self.payment


class RefundProvider(WorkerProvider):
    def request_refund(self, payment_id, operation_key, amount_minor):
        assert payment_id == self.payment.provider_payment_id
        assert operation_key == "refund-worker-1" and amount_minor is None
        return {"id": "refund-provider-1"}


class ReconciliationProvider(Provider):
    """Fake provider for run_reconciliation_worker: no pending Mercado Pago
    payments to discover by default, records which stale preferences get
    expired, and optionally recovers a preference for an "unknown" attempt."""

    def __init__(self, recovered_checkout=None):
        self.expired_preferences = []
        self.recovered_checkout = recovered_checkout

    def expire_checkout(self, preference_id, now):
        self.expired_preferences.append(preference_id)

    def search_payments(self, attempt_id):
        return []

    def recover_checkout(self, attempt_id):
        return self.recovered_checkout


class WorkerRuntime:
    def __init__(self, repository, flow):
        self.repository_value = repository
        self.flow = flow

    def repository(self):
        return self.repository_value

    def payment_context(self, connection_id):
        return type("Context", (), {"flow": self.flow, "repository": self.repository_value})()


@mock_aws
def test_charge_checkout_and_provider_confirmation_are_durable_and_idempotent():
    business_id, connection_id = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    repo = DynamoRepository("payments", resource=table(), webhook_connection_id=connection_id)
    repo.put_tenant_manifest({"business": {"id": business_id, "displayName": "Cobranza Norte", "folioPrefix": "CN", "branding": {"publicName": "Cobranza Norte"}}, "memberships": [{"id": "33333333-3333-4333-8333-333333333333", "subjectId": "owner", "role": "owner"}], "mercadoPago": {"id": connection_id, "providerAccountId": "seller-1", "credentialSecretRef": "arn:credentials", "webhookSecretRef": "arn:webhook"}})
    repo.mark_connection_verified(connection_id, NOW)
    customer = repo.create_customer(business_id, "Ana", None, "customer-1")
    repo.create_charge(business_id, customer["customerId"], 12_500, "MXN", "Anticipo", date(2026, 9, 20), creation_key="charge-1", link_token="long-private-payment-token")
    flow = PaymentFlow(repo, Provider(), clock=lambda: NOW)
    checkout = flow.start_checkout("long-private-payment-token", "submission-1")
    assert checkout.status == "ready"
    context = repo.payment_context(checkout.attempt_id)
    payment = ProviderPayment("payment-1", checkout.attempt_id, "seller-1", "test", 12_500, "MXN", "approved", NOW, approved_at=NOW)
    first = repo.record_payment_observation(context, payment, assess_payment(*context, payment), "event-1", NOW)
    second = repo.record_payment_observation(context, payment, assess_payment(*context, payment), "event-1", NOW)
    assert first.allocation_minor == 12_500 and second.allocation_minor == 0
    assert flow.get_charge("long-private-payment-token")["status"] == "paid"


@mock_aws
def test_leased_webhook_event_recovers_to_a_single_payment_commit():
    business_id, connection_id = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    repo = DynamoRepository("payments", resource=table(), webhook_connection_id=connection_id)
    repo.put_tenant_manifest({"business": {"id": business_id, "displayName": "Cobranza Norte", "folioPrefix": "CN", "branding": {"publicName": "Cobranza Norte"}}, "memberships": [{"id": "33333333-3333-4333-8333-333333333333", "subjectId": "owner", "role": "owner"}], "mercadoPago": {"id": connection_id, "providerAccountId": "seller-1", "credentialSecretRef": "arn:credentials", "webhookSecretRef": "arn:webhook"}})
    repo.mark_connection_verified(connection_id, NOW)
    customer = repo.create_customer(business_id, "Ana", None, "customer-worker-1")
    repo.create_charge(business_id, customer["customerId"], 500, "MXN", "Anticipo", date(2026, 9, 20), creation_key="charge-worker-1", link_token="worker-private-payment-token")
    flow = PaymentFlow(repo, Provider(), clock=lambda: NOW)
    checkout = flow.start_checkout("worker-private-payment-token", "submission-worker-1")
    payment = ProviderPayment("payment-worker-1", checkout.attempt_id, "seller-1", "test", 500, "MXN", "approved", NOW, approved_at=NOW)
    repo.capture_provider_event(connection_id, "webhook-worker-1", payment.provider_payment_id, "payment", {"data": {"id": payment.provider_payment_id}}, True, NOW)
    result = run_provider_event_worker(WorkerRuntime(repo, PaymentFlow(repo, WorkerProvider(payment), clock=lambda: NOW)), now=NOW)
    assert result == {"claimed": 1, "processed": 1, "failed": 0, "review": 0}
    assert flow.get_charge("worker-private-payment-token")["status"] == "paid"


@mock_aws
def test_refund_worker_requires_provider_evidence_before_restoring_balance():
    business_id, connection_id = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    repo = DynamoRepository("payments", resource=table(), webhook_connection_id=connection_id)
    repo.put_tenant_manifest({"business": {"id": business_id, "displayName": "Cobranza Norte", "folioPrefix": "CN", "branding": {"publicName": "Cobranza Norte"}}, "memberships": [{"id": "33333333-3333-4333-8333-333333333333", "subjectId": "owner", "role": "owner"}], "mercadoPago": {"id": connection_id, "providerAccountId": "seller-1", "credentialSecretRef": "arn:credentials", "webhookSecretRef": "arn:webhook"}})
    repo.mark_connection_verified(connection_id, NOW)
    customer = repo.create_customer(business_id, "Ana", None, "customer-refund-worker")
    created = repo.create_charge(business_id, customer["customerId"], 500, "MXN", "Anticipo", date(2026, 9, 20), creation_key="charge-refund-worker", link_token="refund-worker-private-token")
    flow = PaymentFlow(repo, Provider(), clock=lambda: NOW)
    checkout = flow.start_checkout("refund-worker-private-token", "submission-refund-worker")
    approved = ProviderPayment("payment-refund-worker", checkout.attempt_id, "seller-1", "test", 500, "MXN", "approved", NOW, approved_at=NOW)
    context = repo.payment_context(checkout.attempt_id)
    repo.record_payment_observation(context, approved, assess_payment(*context, approved), "approved-refund-worker", NOW)
    payment_id = repo.charge_detail(business_id, created["chargeId"])["payments"][0]["paymentId"]
    repo.create_refund_operation(business_id, payment_id, "33333333-3333-4333-8333-333333333333", "refund-worker-1", None, NOW)
    refunded_at = NOW.replace(minute=1)
    refunded = ProviderPayment("payment-refund-worker", checkout.attempt_id, "seller-1", "test", 500, "MXN", "refunded", refunded_at, approved_at=NOW, provider_updated_at=refunded_at, adjustments=(ProviderAdjustment("refund-provider-1", "refund", 500, "approved", refunded_at),))
    result = run_refund_worker(WorkerRuntime(repo, PaymentFlow(repo, RefundProvider(refunded), clock=lambda: refunded_at)), now=NOW)
    assert result == {"claimed": 1, "completed": 1, "retrying": 0, "review": 0}
    assert flow.get_charge("refund-worker-private-token")["outstandingMinor"] == 500


def _setup_business(repo, business_id, connection_id, membership_id="33333333-3333-4333-8333-333333333333"):
    repo.put_tenant_manifest({
        "business": {"id": business_id, "displayName": "Cobranza Norte", "folioPrefix": "CN", "branding": {"publicName": "Cobranza Norte"}},
        "memberships": [{"id": membership_id, "subjectId": "owner", "role": "owner"}],
        "mercadoPago": {"id": connection_id, "providerAccountId": "seller-1", "credentialSecretRef": "arn:credentials", "webhookSecretRef": "arn:webhook"},
    })
    repo.mark_connection_verified(connection_id, NOW)


@mock_aws
def test_two_checkout_requests_before_any_payment_share_a_single_active_attempt():
    """Two callers racing to start checkout for the same charge (e.g. a double
    click, or a retried browser request with a fresh Idempotency-Key) must
    never end up with two independent Mercado Pago preferences in flight."""
    business_id, connection_id = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    repo = DynamoRepository("payments", resource=table(), webhook_connection_id=connection_id)
    _setup_business(repo, business_id, connection_id)
    customer = repo.create_customer(business_id, "Ana", None, "customer-concurrent")
    repo.create_charge(business_id, customer["customerId"], 500, "MXN", "Anticipo", date(2026, 9, 20), creation_key="charge-concurrent", link_token="concurrent-private-token")
    flow = PaymentFlow(repo, Provider(), clock=lambda: NOW)
    first = flow.start_checkout("concurrent-private-token", "submission-a")
    second = flow.start_checkout("concurrent-private-token", "submission-b")
    assert first.status == "ready" and second.status == "ready"
    assert first.attempt_id == second.attempt_id


@mock_aws
def test_effective_refund_expires_the_stale_attempt_and_a_clean_checkout_follows():
    """The full lifecycle priority-1 fix: an effective refund must not leave a
    stale Checkout Pro preference reachable, must coordinate its expiry with
    the ACTIVE_ATTEMPT guard, and a subsequent checkout must get a genuinely
    new attempt for the restored balance rather than reusing the old one."""
    business_id, connection_id = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    membership_id = "33333333-3333-4333-8333-333333333333"
    repo = DynamoRepository("payments", resource=table(), webhook_connection_id=connection_id)
    _setup_business(repo, business_id, connection_id, membership_id)
    customer = repo.create_customer(business_id, "Ana", None, "customer-refund-expiry")
    created = repo.create_charge(business_id, customer["customerId"], 500, "MXN", "Anticipo", date(2026, 9, 20), creation_key="charge-refund-expiry", link_token="refund-expiry-private-token")
    flow = PaymentFlow(repo, Provider(), clock=lambda: NOW)

    checkout = flow.start_checkout("refund-expiry-private-token", "submission-initial")
    assert checkout.status == "ready"
    first_attempt_id = checkout.attempt_id
    context = repo.payment_context(checkout.attempt_id)
    approved = ProviderPayment("payment-refund-expiry", checkout.attempt_id, "seller-1", "test", 500, "MXN", "approved", NOW, approved_at=NOW)
    repo.record_payment_observation(context, approved, assess_payment(*context, approved), "approved-refund-expiry", NOW)
    assert flow.get_charge("refund-expiry-private-token")["status"] == "paid"

    payment_id = repo.charge_detail(business_id, created["chargeId"])["payments"][0]["paymentId"]
    repo.create_refund_operation(business_id, payment_id, membership_id, "refund-worker-1", None, NOW)
    refunded_at = NOW.replace(minute=1)
    refunded = ProviderPayment(
        "payment-refund-expiry", checkout.attempt_id, "seller-1", "test", 500, "MXN", "refunded",
        refunded_at, approved_at=NOW, provider_updated_at=refunded_at,
        adjustments=(ProviderAdjustment("refund-provider-1", "refund", 500, "approved", refunded_at),),
    )
    refund_result = run_refund_worker(
        WorkerRuntime(repo, PaymentFlow(repo, RefundProvider(refunded), clock=lambda: refunded_at)), now=NOW,
    )
    assert refund_result == {"claimed": 1, "completed": 1, "retrying": 0, "review": 0}

    # Saldo restaurado; intento previo marcado para expirar, no reutilizable.
    detail = repo.charge_detail(business_id, created["chargeId"])
    assert detail["charge"]["outstandingMinor"] == 500
    attempts_by_id = {item["attemptId"]: item["status"] for item in detail["attempts"]}
    assert attempts_by_id[first_attempt_id] == "expiring"

    # No hay ventana con dos intentos activos: mientras el viejo solo está
    # "expiring", un nuevo intento de pago debe reconocerlo, no crear otro.
    still_recovering = flow.start_checkout("refund-expiry-private-token", "submission-before-expiry")
    assert still_recovering.status == "recovering"
    assert still_recovering.attempt_id == first_attempt_id

    # El worker de reconciliación coordina la expiración real de la preferencia
    # y libera el guard ACTIVE_ATTEMPT.
    reconciliation_provider = ReconciliationProvider()
    reconciliation_result = run_reconciliation_worker(
        WorkerRuntime(repo, PaymentFlow(repo, reconciliation_provider, clock=lambda: refunded_at)),
        now=refunded_at,
    )
    assert reconciliation_result["claimed"] >= 1
    assert reconciliation_provider.expired_preferences == ["preference-1"]
    detail = repo.charge_detail(business_id, created["chargeId"])
    attempts_by_id = {item["attemptId"]: item["status"] for item in detail["attempts"]}
    assert attempts_by_id[first_attempt_id] == "expired"

    # Ahora un checkout nuevo obtiene un intento distinto para el saldo actual,
    # y el guard activo ya apunta a este intento nuevo (una tercera llamada
    # concurrente lo reutiliza a él, no crea un cuarto intento).
    fresh = flow.start_checkout("refund-expiry-private-token", "submission-after-expiry")
    assert fresh.status == "ready"
    assert fresh.attempt_id != first_attempt_id
    concurrent_again = flow.start_checkout("refund-expiry-private-token", "submission-after-expiry-2")
    assert concurrent_again.attempt_id == fresh.attempt_id


@mock_aws
def test_stale_provider_snapshot_after_a_refund_does_not_revert_the_refund():
    """A reconciliation run or duplicate webhook that resurfaces the payment's
    original *approved* snapshot after it has already been refunded must not
    regress the restored balance just because it arrives late."""
    business_id, connection_id = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    membership_id = "33333333-3333-4333-8333-333333333333"
    repo = DynamoRepository("payments", resource=table(), webhook_connection_id=connection_id)
    _setup_business(repo, business_id, connection_id, membership_id)
    customer = repo.create_customer(business_id, "Ana", None, "customer-stale-snapshot")
    created = repo.create_charge(business_id, customer["customerId"], 500, "MXN", "Anticipo", date(2026, 9, 20), creation_key="charge-stale-snapshot", link_token="stale-snapshot-private-token")
    flow = PaymentFlow(repo, Provider(), clock=lambda: NOW)

    checkout = flow.start_checkout("stale-snapshot-private-token", "submission-initial")
    context = repo.payment_context(checkout.attempt_id)
    approved = ProviderPayment("payment-stale-snapshot", checkout.attempt_id, "seller-1", "test", 500, "MXN", "approved", NOW, approved_at=NOW)
    repo.record_payment_observation(context, approved, assess_payment(*context, approved), "approved-stale-snapshot", NOW)

    payment_id = repo.charge_detail(business_id, created["chargeId"])["payments"][0]["paymentId"]
    repo.create_refund_operation(business_id, payment_id, membership_id, "refund-worker-1", None, NOW)
    refunded_at = NOW.replace(minute=1)
    refunded = ProviderPayment(
        "payment-stale-snapshot", checkout.attempt_id, "seller-1", "test", 500, "MXN", "refunded",
        refunded_at, approved_at=NOW, provider_updated_at=refunded_at,
        adjustments=(ProviderAdjustment("refund-provider-stale", "refund", 500, "approved", refunded_at),),
    )
    run_refund_worker(
        WorkerRuntime(repo, PaymentFlow(repo, RefundProvider(refunded), clock=lambda: refunded_at)), now=NOW,
    )
    assert flow.get_charge("stale-snapshot-private-token")["outstandingMinor"] == 500

    # The provider's original (now stale) "approved" snapshot, observed before
    # the refund, arrives late through a duplicate/out-of-order webhook replay.
    stale_context = repo.payment_context(checkout.attempt_id)
    stale_assessment = assess_payment(*stale_context, approved)
    repo.record_payment_observation(stale_context, approved, stale_assessment, "late-duplicate-webhook", refunded_at)

    assert flow.get_charge("stale-snapshot-private-token")["outstandingMinor"] == 500
    detail = repo.charge_detail(business_id, created["chargeId"])
    assert detail["charge"]["allocatedMinor"] == 0
    attempts_by_id = {item["attemptId"]: item["status"] for item in detail["attempts"]}
    assert attempts_by_id[checkout.attempt_id] == "expiring"


def _attempt_status(repo, attempt_id):
    return repo.payment_context(attempt_id)[0].status


@mock_aws
def test_reconciliation_recovers_an_unknown_attempt_to_ready_without_getting_stuck():
    """An attempt left "unknown" (provider call made, response never arrived)
    must come back as "ready" once reconciliation successfully recovers its
    preference -- not get stuck at an intermediate lease state that neither
    mark_attempt_ready nor a future claim recognizes."""
    business_id, connection_id = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    repo = DynamoRepository("payments", resource=table(), webhook_connection_id=connection_id)
    _setup_business(repo, business_id, connection_id)
    customer = repo.create_customer(business_id, "Ana", None, "customer-unknown-recovery")
    repo.create_charge(business_id, customer["customerId"], 500, "MXN", "Anticipo", date(2026, 9, 20), creation_key="charge-unknown-recovery", link_token="unknown-recovery-private-token")
    charge = repo.find_charge_by_token_digest(hashlib.sha256(b"unknown-recovery-private-token").digest(), NOW)
    connection = repo.active_connection(business_id, "mercado_pago")
    attempt, created = repo.get_or_create_attempt(charge, connection, "submission-unknown", NOW)
    assert created and attempt.status == "creating"
    repo.mark_attempt_unknown(business_id, attempt.id, NOW)
    assert _attempt_status(repo, attempt.id) == "unknown"

    recovered_checkout = ProviderCheckout("preference-recovered-1", "https://sandbox.example/recovered", attempt.id, 500, "MXN", "seller-1", "test")
    provider = ReconciliationProvider(recovered_checkout=recovered_checkout)
    result = run_reconciliation_worker(WorkerRuntime(repo, PaymentFlow(repo, provider, clock=lambda: NOW)), now=NOW)
    assert result == {"claimed": 1, "paymentsQueued": 0, "recovered": 1, "failed": 0}
    assert _attempt_status(repo, attempt.id) == "ready"

    # Reclaimable again after its next scheduled reconciliation, not stuck.
    later = NOW.replace(hour=NOW.hour + 1)
    again = run_reconciliation_worker(WorkerRuntime(repo, PaymentFlow(repo, ReconciliationProvider(), clock=lambda: later)), now=later)
    assert again["claimed"] == 1 and again["failed"] == 0
    assert _attempt_status(repo, attempt.id) == "ready"


@mock_aws
def test_reconciliation_of_a_ready_attempt_preserves_ready_and_is_reclaimable_after_next_at():
    """A "ready" attempt that reconciliation checks (no news from the
    provider) must stay "ready" -- not be left at a transient state -- and
    must become reclaimable again once its own next_at arrives, not before."""
    business_id, connection_id = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    repo = DynamoRepository("payments", resource=table(), webhook_connection_id=connection_id)
    _setup_business(repo, business_id, connection_id)
    customer = repo.create_customer(business_id, "Ana", None, "customer-ready-recon")
    repo.create_charge(business_id, customer["customerId"], 500, "MXN", "Anticipo", date(2026, 9, 20), creation_key="charge-ready-recon", link_token="ready-recon-private-token")
    flow = PaymentFlow(repo, Provider(), clock=lambda: NOW)
    checkout = flow.start_checkout("ready-recon-private-token", "submission-ready-recon")
    assert checkout.status == "ready"
    assert _attempt_status(repo, checkout.attempt_id) == "ready"

    first_pass = run_reconciliation_worker(WorkerRuntime(repo, PaymentFlow(repo, ReconciliationProvider(), clock=lambda: NOW)), now=NOW)
    assert first_pass == {"claimed": 1, "paymentsQueued": 0, "recovered": 0, "failed": 0}
    assert _attempt_status(repo, checkout.attempt_id) == "ready"

    # Immediately after: available_at was pushed into the future, so a second
    # pass at the same "now" must not reclaim it again.
    immediate_retry = run_reconciliation_worker(WorkerRuntime(repo, PaymentFlow(repo, ReconciliationProvider(), clock=lambda: NOW)), now=NOW)
    assert immediate_retry["claimed"] == 0
    assert _attempt_status(repo, checkout.attempt_id) == "ready"

    # Once next_at has actually arrived, it is reclaimable again -- this is
    # the exact case the independent review reproduced as permanently stuck.
    later = NOW.replace(hour=NOW.hour + 1)
    later_pass = run_reconciliation_worker(WorkerRuntime(repo, PaymentFlow(repo, ReconciliationProvider(), clock=lambda: later)), now=later)
    assert later_pass == {"claimed": 1, "paymentsQueued": 0, "recovered": 0, "failed": 0}
    assert _attempt_status(repo, checkout.attempt_id) == "ready"


@mock_aws
def test_reconciliation_lease_prevents_a_concurrent_second_claim():
    business_id, connection_id = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    repo = DynamoRepository("payments", resource=table(), webhook_connection_id=connection_id)
    _setup_business(repo, business_id, connection_id)
    customer = repo.create_customer(business_id, "Ana", None, "customer-lease-guard")
    repo.create_charge(business_id, customer["customerId"], 500, "MXN", "Anticipo", date(2026, 9, 20), creation_key="charge-lease-guard", link_token="lease-guard-private-token")
    flow = PaymentFlow(repo, Provider(), clock=lambda: NOW)
    checkout = flow.start_checkout("lease-guard-private-token", "submission-lease-guard")

    first_claim = repo.claim_reconciliation_attempts(NOW, NOW.replace(minute=30), 10)
    assert len(first_claim) == 1 and first_claim[0]["id"] == checkout.attempt_id
    # A second worker asking for work at the same moment, while the first
    # lease is still live, must not also be handed this attempt.
    second_claim = repo.claim_reconciliation_attempts(NOW, NOW.replace(minute=30), 10)
    assert second_claim == []
    # Status was never touched by claiming -- confirmed via a fresh read.
    assert _attempt_status(repo, checkout.attempt_id) == "ready"


@mock_aws
def test_no_terminal_reconciliation_path_leaves_an_attempt_stuck_at_processing():
    """Regression guard for the specific bug the independent review found:
    claiming must never persist status="processing", in any of the three
    branches run_reconciliation_worker can take (recovered, still-ready, and
    expired-via-refund)."""
    business_id, connection_id = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    membership_id = "33333333-3333-4333-8333-333333333333"
    repo = DynamoRepository("payments", resource=table(), webhook_connection_id=connection_id)
    _setup_business(repo, business_id, connection_id, membership_id)

    # Branch 1: unknown -> recovered -> ready.
    customer_a = repo.create_customer(business_id, "Ana", None, "customer-stuck-a")
    repo.create_charge(business_id, customer_a["customerId"], 500, "MXN", "Anticipo", date(2026, 9, 20), creation_key="charge-stuck-a", link_token="stuck-a-private-token")
    charge_a = repo.find_charge_by_token_digest(hashlib.sha256(b"stuck-a-private-token").digest(), NOW)
    connection = repo.active_connection(business_id, "mercado_pago")
    attempt_a, _ = repo.get_or_create_attempt(charge_a, connection, "submission-stuck-a", NOW)
    repo.mark_attempt_unknown(business_id, attempt_a.id, NOW)
    recovered = ProviderCheckout("preference-stuck-a", "https://sandbox.example/stuck-a", attempt_a.id, 500, "MXN", "seller-1", "test")
    run_reconciliation_worker(WorkerRuntime(repo, PaymentFlow(repo, ReconciliationProvider(recovered_checkout=recovered), clock=lambda: NOW)), now=NOW)
    assert _attempt_status(repo, attempt_a.id) != "processing"

    # Branch 2: ready -> still ready.
    customer_b = repo.create_customer(business_id, "Ana", None, "customer-stuck-b")
    repo.create_charge(business_id, customer_b["customerId"], 500, "MXN", "Anticipo", date(2026, 9, 20), creation_key="charge-stuck-b", link_token="stuck-b-private-token")
    flow = PaymentFlow(repo, Provider(), clock=lambda: NOW)
    checkout_b = flow.start_checkout("stuck-b-private-token", "submission-stuck-b")
    run_reconciliation_worker(WorkerRuntime(repo, PaymentFlow(repo, ReconciliationProvider(), clock=lambda: NOW)), now=NOW)
    assert _attempt_status(repo, checkout_b.attempt_id) != "processing"

    # Branch 3: ready -> refunded -> expiring -> expired (via reconciliation).
    customer_c = repo.create_customer(business_id, "Ana", None, "customer-stuck-c")
    created_c = repo.create_charge(business_id, customer_c["customerId"], 500, "MXN", "Anticipo", date(2026, 9, 20), creation_key="charge-stuck-c", link_token="stuck-c-private-token")
    checkout_c = flow.start_checkout("stuck-c-private-token", "submission-stuck-c-checkout")
    context_c = repo.payment_context(checkout_c.attempt_id)
    approved_c = ProviderPayment("payment-stuck-c", checkout_c.attempt_id, "seller-1", "test", 500, "MXN", "approved", NOW, approved_at=NOW)
    repo.record_payment_observation(context_c, approved_c, assess_payment(*context_c, approved_c), "approved-stuck-c", NOW)
    payment_id_c = repo.charge_detail(business_id, created_c["chargeId"])["payments"][0]["paymentId"]
    repo.create_refund_operation(business_id, payment_id_c, membership_id, "refund-worker-1", None, NOW)
    refunded_at = NOW.replace(minute=1)
    refunded_c = ProviderPayment(
        "payment-stuck-c", checkout_c.attempt_id, "seller-1", "test", 500, "MXN", "refunded",
        refunded_at, approved_at=NOW, provider_updated_at=refunded_at,
        adjustments=(ProviderAdjustment("refund-provider-stuck-c", "refund", 500, "approved", refunded_at),),
    )
    run_refund_worker(WorkerRuntime(repo, PaymentFlow(repo, RefundProvider(refunded_c), clock=lambda: refunded_at)), now=NOW)
    assert _attempt_status(repo, checkout_c.attempt_id) == "expiring"
    run_reconciliation_worker(WorkerRuntime(repo, PaymentFlow(repo, ReconciliationProvider(), clock=lambda: refunded_at)), now=refunded_at)
    assert _attempt_status(repo, checkout_c.attempt_id) == "expired"


@mock_aws
def test_by_id_lookups_resolve_correctly_with_over_100_irrelevant_items_in_the_table():
    """_by_id resolves payment/refund/outbox/provider_event through a
    dedicated LookupIndex key, not a table-wide Scan(Limit=100) that only
    examines (never targets) the first ~100 items it happens to see. Pad the
    table well past that before exercising every _by_id-dependent path."""
    business_id, connection_id = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    membership_id = "33333333-3333-4333-8333-333333333333"
    repo = DynamoRepository("payments", resource=table(), webhook_connection_id=connection_id)
    _setup_business(repo, business_id, connection_id, membership_id)
    padding_customer = repo.create_customer(business_id, "Relleno", None, "customer-padding")
    for index in range(130):
        repo.create_charge(business_id, padding_customer["customerId"], 100, "MXN", "Relleno", date(2026, 9, 20), link_token=f"padding-token-{index:04d}")

    customer = repo.create_customer(business_id, "Ana", None, "customer-by-id-target")
    created = repo.create_charge(business_id, customer["customerId"], 500, "MXN", "Anticipo", date(2026, 9, 20), creation_key="charge-by-id-target", link_token="by-id-target-private-token")
    flow = PaymentFlow(repo, Provider(), clock=lambda: NOW)
    checkout = flow.start_checkout("by-id-target-private-token", "submission-by-id-target")
    context = repo.payment_context(checkout.attempt_id)
    approved = ProviderPayment("payment-by-id-target", checkout.attempt_id, "seller-1", "test", 500, "MXN", "approved", NOW, approved_at=NOW)
    repo.record_payment_observation(context, approved, assess_payment(*context, approved), "approved-by-id-target", NOW)

    # payment lookup by id (create_refund_operation's own _by_id("payment", ...))
    payment_id = repo.charge_detail(business_id, created["chargeId"])["payments"][0]["paymentId"]
    refund = repo.create_refund_operation(business_id, payment_id, membership_id, "refund-worker-1", None, NOW)
    assert refund["status"] == "requested"

    # refund lookup by id, via claim + finish_refund_operation
    claimed_refunds = repo.claim_refund_operations(NOW, NOW.replace(minute=1), 10)
    assert len(claimed_refunds) == 1 and claimed_refunds[0]["id"] == refund["refundId"]
    repo.finish_refund_operation(refund["refundId"], claimed_refunds[0]["lease_token"], "completed", NOW, provider_refund_id="refund-provider-by-id-target")
    assert repo.charge_detail(business_id, created["chargeId"])["refundOperations"][0]["status"] == "completed"

    # outbox lookup by id, via claim + finish_outbox
    claimed_outbox = repo.claim_outbox(NOW, NOW.replace(minute=1), 10)
    assert len(claimed_outbox) == 1
    repo.finish_outbox(claimed_outbox[0]["id"], claimed_outbox[0]["lease_token"], NOW)
    assert repo.claim_outbox(NOW, NOW.replace(minute=1), 10) == []

    # provider_event lookup by id, via claim + fail_provider_event
    repo.capture_provider_event(connection_id, "event-by-id-target", "resource-by-id-target", "payment", {}, True, NOW)
    claimed_events = repo.claim_provider_events(NOW, NOW.replace(minute=1), 10)
    assert len(claimed_events) == 1
    repo.fail_provider_event(claimed_events[0]["id"], claimed_events[0]["lease_token"], "boom", NOW.replace(minute=2))


class _FakeHttpResponse:
    def __init__(self, data):
        self.data = json.dumps(data).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.data


@mock_aws
def test_partial_refund_amount_from_a_real_claim_reaches_the_provider_correctly():
    """amount_minor on a refund operation claimed via claim_refund_operations
    comes back from a genuine DynamoDB query (boto3.resource), which
    deserializes DynamoDB's Number type as Decimal, never int -- prove that
    value survives claim_refund_operations and then MercadoPagoSandbox's own
    minor-unit conversion (_provider_number) for a real partial refund,
    instead of only ever exercising a fake provider that never touches
    _provider_number's isinstance(int) check at all."""
    business_id, connection_id = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    membership_id = "33333333-3333-4333-8333-333333333333"
    repo = DynamoRepository("payments", resource=table(), webhook_connection_id=connection_id)
    _setup_business(repo, business_id, connection_id, membership_id)
    customer = repo.create_customer(business_id, "Ana", None, "customer-partial-refund")
    created = repo.create_charge(business_id, customer["customerId"], 1000, "MXN", "Anticipo", date(2026, 9, 20), creation_key="charge-partial-refund", link_token="partial-refund-private-token")
    flow = PaymentFlow(repo, Provider(), clock=lambda: NOW)
    checkout = flow.start_checkout("partial-refund-private-token", "submission-partial-refund")
    context = repo.payment_context(checkout.attempt_id)
    approved = ProviderPayment("payment-partial-refund", checkout.attempt_id, "seller-1", "test", 1000, "MXN", "approved", NOW, approved_at=NOW)
    repo.record_payment_observation(context, approved, assess_payment(*context, approved), "approved-partial-refund", NOW)
    payment_id = repo.charge_detail(business_id, created["chargeId"])["payments"][0]["paymentId"]
    repo.create_refund_operation(business_id, payment_id, membership_id, "refund-worker-1", 400, NOW)

    claimed = repo.claim_refund_operations(NOW, NOW.replace(minute=1), 10)
    assert len(claimed) == 1
    # This is the exact value run_refund_worker passes to provider.request_refund.
    assert claimed[0]["amount_minor"] == 400 and type(claimed[0]["amount_minor"]) is int

    calls = []

    def opener(request, timeout):
        if request.full_url.endswith("/users/me"):
            return _FakeHttpResponse({"id": "seller-1", "tags": ["test_user"]})
        calls.append(request)
        return _FakeHttpResponse({"id": "refund-provider-partial-1", "status": "approved"})

    provider = MercadoPagoSandbox("secret", "seller-1", credential_source="test_credentials", opener=opener)
    result = provider.request_refund("payment-partial-refund", str(claimed[0]["operation_key"]), claimed[0]["amount_minor"])
    assert result == {"id": "refund-provider-partial-1", "status": "approved"}
    assert json.loads(calls[0].data) == {"amount": 4.0}
