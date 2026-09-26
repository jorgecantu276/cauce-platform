from datetime import date, datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import os
import sys

import boto3
from moto import mock_aws
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from payments.dynamodb import DynamoRepository
from payments.models import PaymentAssessment, ProviderAdjustment, ProviderCheckout, ProviderPayment
from payments.service import assess_payment


NOW = datetime(2026, 9, 13, 15, 0, tzinfo=timezone.utc)
BUSINESS_ID = "11111111-1111-4111-8111-111111111111"
CONNECTION_ID = "22222222-2222-4222-8222-222222222222"


def resource():
    value = boto3.resource("dynamodb", region_name="us-east-1")
    value.create_table(
        TableName="payments", BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": name, "AttributeType": "S"}
            for name in ("PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK", "GSI3PK", "GSI3SK")
        ],
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        GlobalSecondaryIndexes=[
            {"IndexName": "LookupIndex", "KeySchema": [{"AttributeName": "GSI1PK", "KeyType": "HASH"}, {"AttributeName": "GSI1SK", "KeyType": "RANGE"}], "Projection": {"ProjectionType": "ALL"}},
            {"IndexName": "SubjectIndex", "KeySchema": [{"AttributeName": "GSI2PK", "KeyType": "HASH"}, {"AttributeName": "GSI2SK", "KeyType": "RANGE"}], "Projection": {"ProjectionType": "ALL"}},
            {"IndexName": "WorkIndex", "KeySchema": [{"AttributeName": "GSI3PK", "KeyType": "HASH"}, {"AttributeName": "GSI3SK", "KeyType": "RANGE"}], "Projection": {"ProjectionType": "ALL"}},
        ],
    )
    return value


def manifest():
    return {
        "business": {"id": BUSINESS_ID, "displayName": "Cobranza Norte", "folioPrefix": "CN", "branding": {"publicName": "Cobranza Norte", "accent": "#ed684c", "accentHover": "#d8563c", "nav": "#101931", "navAlt": "#172545", "canvas": "#f5f5f2"}},
        "memberships": [{"id": "33333333-3333-4333-8333-333333333333", "subjectId": "user-1", "role": "owner"}],
        "mercadoPago": {"id": CONNECTION_ID, "providerAccountId": "seller-1", "credentialSecretRef": "arn:credentials", "webhookSecretRef": "arn:webhook"},
    }


@mock_aws
def test_manifest_and_charge_link_are_tenant_scoped_and_idempotent():
    repo = DynamoRepository("payments", resource=resource())
    repo.put_tenant_manifest(manifest())
    assert repo.staff_session("user-1")["memberships"] == [{"businessId": BUSINESS_ID, "businessName": "Cobranza Norte", "role": "owner"}]
    customer = repo.create_customer(BUSINESS_ID, "Ana López", "ana@example.test", "customer-request-1")
    assert repo.create_customer(BUSINESS_ID, "Ana López", "ana@example.test", "customer-request-1") == customer
    created = repo.create_charge(BUSINESS_ID, customer["customerId"], 12_500, "MXN", "Anticipo", date(2026, 9, 20), creation_key="charge-request-1", link_token="secure-charge-token")
    assert created["folio"] == "CN-000001"
    charge = repo.find_charge_by_token_digest(__import__("hashlib").sha256(b"secure-charge-token").digest(), NOW)
    assert charge and charge.id == created["chargeId"] and charge.outstanding_minor == 12_500
    assert repo.create_charge(BUSINESS_ID, customer["customerId"], 12_500, "MXN", "Anticipo", date(2026, 9, 20), creation_key="charge-request-1", link_token="secure-charge-token") == created


@mock_aws
def test_customer_list_includes_live_balance_and_open_charge_count():
    repo = DynamoRepository("payments", resource=resource())
    repo.put_tenant_manifest(manifest())
    customer = repo.create_customer(BUSINESS_ID, "Ana López", None, "customer-summary-1")
    first = repo.create_charge(BUSINESS_ID, customer["customerId"], 500, "MXN", "Saldo", date(2026, 9, 20), link_token="customer-summary-token-1")
    second = repo.create_charge(BUSINESS_ID, customer["customerId"], 750, "MXN", "Cancelado", date(2026, 9, 20), link_token="customer-summary-token-2")
    repo.cancel_charge(BUSINESS_ID, second["chargeId"], "33333333-3333-4333-8333-333333333333", "cancel-summary-1", NOW)
    assert repo.list_customers(BUSINESS_ID) == [{
        "customerId": customer["customerId"], "displayName": "Ana López", "email": None,
        "outstandingMinor": 500, "openChargeCount": 1,
    }]
    repo.create_customer(BUSINESS_ID, "Cliente adicional", None, "customer-summary-2")
    customer_page = repo.customer_page(BUSINESS_ID, limit=1)
    assert len(customer_page["items"]) == 1
    assert customer_page["hasMore"] is True
    assert first["chargeId"]
    # A cancelled charge is excluded from the aggregate the same way it is
    # excluded from the per-customer balance above.
    assert repo.business_summary(BUSINESS_ID) == {"openChargeCount": 1, "outstandingMinor": 500}


@mock_aws
def test_business_summary_aggregates_every_open_charge_beyond_the_list_page_limit():
    repo = DynamoRepository("payments", resource=resource())
    repo.put_tenant_manifest(manifest())
    customer = repo.create_customer(BUSINESS_ID, "Ana López", None, "customer-summary-scale")
    for index in range(120):
        repo.create_charge(
            BUSINESS_ID, customer["customerId"], 100, "MXN", "Saldo", date(2026, 9, 20),
            link_token=f"summary-scale-token-{index:04d}",
        )
    # list_charges caps display at 100; business_summary must not inherit that
    # cap -- 120 charges of 100 minor units each must all be counted.
    assert len(repo.list_charges(BUSINESS_ID)) == 100
    assert repo.business_summary(BUSINESS_ID) == {"openChargeCount": 120, "outstandingMinor": 12_000}


@mock_aws
def test_verified_connection_and_checkout_attempt_keep_external_lookup_private():
    repo = DynamoRepository("payments", resource=resource())
    repo.put_tenant_manifest(manifest())
    repo.mark_connection_verified(CONNECTION_ID, NOW)
    connection = repo.active_connection(BUSINESS_ID, "mercado_pago")
    assert connection and connection.id == CONNECTION_ID
    customer = repo.create_customer(BUSINESS_ID, "Ana López", None, "customer-request-2")
    created = repo.create_charge(BUSINESS_ID, customer["customerId"], 500, "MXN", "Saldo", date(2026, 9, 20), link_token="another-secure-token")
    charge = repo.find_charge_by_token_digest(__import__("hashlib").sha256(b"another-secure-token").digest(), NOW)
    attempt, is_new = repo.get_or_create_attempt(charge, connection, "submission-1", NOW)
    assert is_new and repo.payment_context(attempt.id)[0].id == attempt.id


@mock_aws
def test_provider_payment_identity_and_charge_balance_commit_together():
    repo = DynamoRepository("payments", resource=resource(), webhook_connection_id=CONNECTION_ID)
    repo.put_tenant_manifest(manifest())
    repo.mark_connection_verified(CONNECTION_ID, NOW)
    connection = repo.active_connection(BUSINESS_ID, "mercado_pago")
    customer = repo.create_customer(BUSINESS_ID, "Ana López", None, "customer-request-3")
    created = repo.create_charge(BUSINESS_ID, customer["customerId"], 500, "MXN", "Saldo", date(2026, 9, 20), link_token="payment-secure-token")
    charge = repo.find_charge_by_token_digest(__import__("hashlib").sha256(b"payment-secure-token").digest(), NOW)
    attempt, _ = repo.get_or_create_attempt(charge, connection, "submission-3", NOW)
    payment = ProviderPayment("payment-1", attempt.id, "seller-1", "test", 500, "MXN", "approved", NOW, approved_at=NOW)
    outcome = repo.record_payment_observation(repo.payment_context(attempt.id), payment, assess_payment(*repo.payment_context(attempt.id), payment), "event-1", NOW)
    assert outcome.allocation_minor == 500
    assert repo.find_charge_by_token_digest(__import__("hashlib").sha256(b"payment-secure-token").digest(), NOW).outstanding_minor == 0
    assert repo.record_payment_observation(repo.payment_context(attempt.id), payment, assess_payment(*repo.payment_context(attempt.id), payment), "event-1", NOW).allocation_minor == 0


@mock_aws
def test_provider_event_and_outbox_workers_claim_with_a_lease():
    repo = DynamoRepository("payments", resource=resource())
    repo.put_tenant_manifest(manifest())
    captured = repo.capture_provider_event(CONNECTION_ID, "event-lease-1", "payment-1", "payment", {"data": {"id": "payment-1"}}, True, NOW)
    event = repo.claim_provider_events(NOW, NOW.replace(minute=1), 10)[0]
    assert event["id"] == captured["id"] and event["lease_token"]
    repo.fail_provider_event(event["id"], event["lease_token"], "retry", NOW, terminal=False)
    assert repo.claim_provider_events(NOW, NOW.replace(minute=2), 10)[0]["id"] == captured["id"]


@mock_aws
def test_later_refund_snapshot_restores_the_charge_without_deleting_payment():
    repo = DynamoRepository("payments", resource=resource(), webhook_connection_id=CONNECTION_ID)
    repo.put_tenant_manifest(manifest())
    repo.mark_connection_verified(CONNECTION_ID, NOW)
    connection = repo.active_connection(BUSINESS_ID, "mercado_pago")
    customer = repo.create_customer(BUSINESS_ID, "Ana López", None, "customer-request-4")
    repo.create_charge(BUSINESS_ID, customer["customerId"], 500, "MXN", "Saldo", date(2026, 9, 20), link_token="refund-secure-token")
    charge = repo.find_charge_by_token_digest(__import__("hashlib").sha256(b"refund-secure-token").digest(), NOW)
    attempt, _ = repo.get_or_create_attempt(charge, connection, "submission-4", NOW)
    approved = ProviderPayment("payment-refund", attempt.id, "seller-1", "test", 500, "MXN", "approved", NOW, approved_at=NOW, provider_updated_at=NOW)
    context = repo.payment_context(attempt.id)
    repo.record_payment_observation(context, approved, assess_payment(*context, approved), "approved-event", NOW)
    refunded_at = NOW.replace(minute=1)
    refunded = ProviderPayment("payment-refund", attempt.id, "seller-1", "test", 500, "MXN", "refunded", refunded_at, approved_at=NOW, provider_updated_at=refunded_at, adjustments=(ProviderAdjustment("refund-1", "refund", 500, "approved", refunded_at),))
    repo.record_payment_observation(repo.payment_context(attempt.id), refunded, assess_payment(*repo.payment_context(attempt.id), refunded), "refund-event", refunded_at)
    # A delayed older snapshot says "approved" again; provider time, rather
    # than arrival time, prevents it from undoing the confirmed refund.
    repo.record_payment_observation(repo.payment_context(attempt.id), approved, assess_payment(*repo.payment_context(attempt.id), approved), "stale-approved-event", refunded_at)
    value = repo.find_charge_by_token_digest(__import__("hashlib").sha256(b"refund-secure-token").digest(), refunded_at)
    assert value.outstanding_minor == 500 and repo.charge_status(value) == "refunded"


@mock_aws
def test_owner_refund_request_is_idempotent_and_worker_claimable():
    repo = DynamoRepository("payments", resource=resource(), webhook_connection_id=CONNECTION_ID)
    repo.put_tenant_manifest(manifest())
    repo.mark_connection_verified(CONNECTION_ID, NOW)
    connection = repo.active_connection(BUSINESS_ID, "mercado_pago")
    customer = repo.create_customer(BUSINESS_ID, "Ana López", None, "customer-request-5")
    repo.create_charge(BUSINESS_ID, customer["customerId"], 500, "MXN", "Saldo", date(2026, 9, 20), link_token="refund-request-token")
    charge = repo.find_charge_by_token_digest(__import__("hashlib").sha256(b"refund-request-token").digest(), NOW)
    attempt, _ = repo.get_or_create_attempt(charge, connection, "submission-5", NOW)
    payment = ProviderPayment("payment-operation", attempt.id, "seller-1", "test", 500, "MXN", "approved", NOW, approved_at=NOW)
    context = repo.payment_context(attempt.id)
    repo.record_payment_observation(context, payment, assess_payment(*context, payment), "approved-operation", NOW)
    first = repo.create_refund_operation(BUSINESS_ID, repo.charge_detail(BUSINESS_ID, charge.id)["payments"][0]["paymentId"], "33333333-3333-4333-8333-333333333333", "refund-operation-1", None, NOW)
    assert repo.create_refund_operation(BUSINESS_ID, repo.charge_detail(BUSINESS_ID, charge.id)["payments"][0]["paymentId"], "33333333-3333-4333-8333-333333333333", "refund-operation-1", None, NOW) == first
    claimed = repo.claim_refund_operations(NOW, NOW.replace(minute=1), 10)
    # Claiming never rewrites status: it stays "requested" both in the
    # claimed copy and in the persisted row. lease_token/lease_expires_at
    # alone stop a second concurrent claim from also picking this operation
    # up (see the second claim_refund_operations call below).
    assert claimed[0]["id"] == first["refundId"] and claimed[0]["status"] == "requested"
    detail = repo.charge_detail(BUSINESS_ID, charge.id)
    assert detail["refundOperations"][0]["refundId"] == first["refundId"]
    assert detail["refundOperations"][0]["status"] == "requested"
    # A second claim attempt while the lease is still live must not reclaim it.
    assert repo.claim_refund_operations(NOW, NOW.replace(minute=1), 10) == []


@mock_aws
def test_owner_can_acknowledge_an_open_payment_review_once():
    repo = DynamoRepository("payments", resource=resource(), webhook_connection_id=CONNECTION_ID)
    repo.put_tenant_manifest(manifest())
    repo.mark_connection_verified(CONNECTION_ID, NOW)
    connection = repo.active_connection(BUSINESS_ID, "mercado_pago")
    customer = repo.create_customer(BUSINESS_ID, "Ana López", None, "customer-request-6")
    repo.create_charge(BUSINESS_ID, customer["customerId"], 500, "MXN", "Saldo", date(2026, 9, 20), link_token="review-secure-token")
    charge = repo.find_charge_by_token_digest(__import__("hashlib").sha256(b"review-secure-token").digest(), NOW)
    attempt, _ = repo.get_or_create_attempt(charge, connection, "submission-6", NOW)
    payment = ProviderPayment("payment-review", attempt.id, "seller-1", "test", 499, "MXN", "approved", NOW, approved_at=NOW)
    context = repo.payment_context(attempt.id)
    repo.record_payment_observation(context, payment, PaymentAssessment(False, "amount_mismatch"), "review-event", NOW)
    review = repo.list_reviews(BUSINESS_ID)[0]
    resolved = repo.resolve_review(BUSINESS_ID, "payment", review["id"], "33333333-3333-4333-8333-333333333333", "review-resolution-1", "acknowledge", "Monto no corresponde al cargo.", NOW)
    assert resolved["outcome"] == "acknowledged_unallocated"
    assert repo.resolve_review(BUSINESS_ID, "payment", review["id"], "33333333-3333-4333-8333-333333333333", "review-resolution-1", "acknowledge", "Monto no corresponde al cargo.", NOW) == resolved


@mock_aws
def test_concurrent_checkout_requests_share_the_single_active_attempt():
    repo = DynamoRepository("payments", resource=resource())
    repo.put_tenant_manifest(manifest())
    repo.mark_connection_verified(CONNECTION_ID, NOW)
    connection = repo.active_connection(BUSINESS_ID, "mercado_pago")
    customer = repo.create_customer(BUSINESS_ID, "Ana López", None, "customer-request-7")
    repo.create_charge(BUSINESS_ID, customer["customerId"], 500, "MXN", "Saldo", date(2026, 9, 20), link_token="concurrent-secure-token")
    charge = repo.find_charge_by_token_digest(__import__("hashlib").sha256(b"concurrent-secure-token").digest(), NOW)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda key: repo.get_or_create_attempt(charge, connection, key, NOW)[0].id, ("submission-7a", "submission-7b")))
    assert len(set(results)) == 1


def _approved_payment_for_refund_lock_tests(repo, link_token, customer_key, payment_id, event_key):
    connection = repo.active_connection(BUSINESS_ID, "mercado_pago")
    customer = repo.create_customer(BUSINESS_ID, "Ana López", None, customer_key)
    repo.create_charge(BUSINESS_ID, customer["customerId"], 500, "MXN", "Saldo", date(2026, 9, 20), link_token=link_token)
    charge = repo.find_charge_by_token_digest(__import__("hashlib").sha256(link_token.encode()).digest(), NOW)
    attempt, _ = repo.get_or_create_attempt(charge, connection, f"submission-{customer_key}", NOW)
    payment = ProviderPayment(payment_id, attempt.id, "seller-1", "test", 500, "MXN", "approved", NOW, approved_at=NOW)
    context = repo.payment_context(attempt.id)
    repo.record_payment_observation(context, payment, assess_payment(*context, payment), event_key, NOW)
    return charge, repo.charge_detail(BUSINESS_ID, charge.id)["payments"][0]["paymentId"]


@mock_aws
def test_two_different_refund_keys_racing_the_same_payment_only_one_wins():
    """Deterministic proof that the *transactional* lock, not the earlier
    fast-path read, is what stops two different Idempotency-Key values from
    both creating an active refund for the same payment.

    This replaces a ThreadPoolExecutor-based version of this test that was
    intermittently flaky in CI (observed ~1 in 5 runs) due to a bug in
    Moto's own in-memory backend under real concurrent transact_write_items
    calls (`RuntimeError: dictionary changed size during iteration` inside
    its internal copy.deepcopy) -- unrelated to the code under test, but
    still a source of non-deterministic CI failures that documenting away
    is not good enough. Patches the one fast-path read
    (`if self._get(..., lock_sk): raise`) that a genuine concurrent caller
    could equally race past if both reads happened at the same instant,
    before either had committed -- forcing both calls to actually reach
    transact_write_items, so it is specifically the transactional
    attribute_not_exists(PK) condition on the REFUND_LOCK item that is
    exercised and asserted on, not the fast path or thread timing. A
    real-thread version validating actual DynamoDB atomicity under genuine
    concurrency is kept separately, for manual runs after a real deploy --
    see test_concurrent_refund_requests_with_different_keys_moto_thread_stress
    below (itself still Moto, not real DynamoDB -- see
    tests/integration/test_real_dynamodb_refund_concurrency.py for that)."""
    repo = DynamoRepository("payments", resource=resource(), webhook_connection_id=CONNECTION_ID)
    repo.put_tenant_manifest(manifest())
    repo.mark_connection_verified(CONNECTION_ID, NOW)
    _, payment_id = _approved_payment_for_refund_lock_tests(repo, "refund-lock-deterministic-token", "customer-refund-lock-det", "payment-refund-lock-det", "approved-refund-lock-det")

    lock_sk = f"REFUND_LOCK#{payment_id}"
    real_get = repo._get
    lock_reads_to_fool = [2]  # one initial fast-path read per caller below

    def fool_the_fast_path(business_id_arg, sk_arg):
        # Simulates both callers' *initial* fast-path read racing before
        # either transaction has committed: claim "no lock yet" for exactly
        # the first two reads of the lock key (one per caller's pre-check),
        # even though the first call's transaction has for real created one
        # by the time the second caller's pre-check runs. Only those two
        # reads are fooled -- the fix's own post-conflict re-read of the
        # lock (used to tell "this same operation_key already won" apart
        # from "a genuinely different, still-unresolved refund") must see
        # the real, committed state, exactly as a real ConsistentRead would
        # moments after the winner's transaction actually commits.
        if sk_arg == lock_sk and lock_reads_to_fool[0] > 0:
            lock_reads_to_fool[0] -= 1
            return None
        return real_get(business_id_arg, sk_arg)

    with patch.object(repo, "_get", side_effect=fool_the_fast_path):
        first = repo.create_refund_operation(BUSINESS_ID, payment_id, "33333333-3333-4333-8333-333333333333", "refund-lock-det-key-a", None, NOW)
        with pytest.raises(ValueError, match="unresolved"):
            repo.create_refund_operation(BUSINESS_ID, payment_id, "33333333-3333-4333-8333-333333333333", "refund-lock-det-key-b", None, NOW)

    charge = repo.find_charge_by_token_digest(__import__("hashlib").sha256(b"refund-lock-deterministic-token").digest(), NOW)
    detail = repo.charge_detail(BUSINESS_ID, charge.id)
    assert len(detail["refundOperations"]) == 1
    assert detail["refundOperations"][0]["refundId"] == first["refundId"]
    # The winning key stays idempotent afterwards (fast path now sees the
    # real lock/refund again, unpatched).
    assert repo.create_refund_operation(BUSINESS_ID, payment_id, "33333333-3333-4333-8333-333333333333", "refund-lock-det-key-a", None, NOW) == first
    with pytest.raises(ValueError, match="unresolved"):
        repo.create_refund_operation(BUSINESS_ID, payment_id, "33333333-3333-4333-8333-333333333333", "refund-lock-det-key-c", None, NOW)


@mock_aws
def test_same_idempotency_key_racing_itself_returns_the_winning_refund_not_unresolved():
    """Deterministic reproduction of the second defect from supervision: two
    concurrent requests sharing the SAME payment_id *and* Idempotency-Key can
    make BOTH the REFUND row (TransactItems index 0) and the REFUND_LOCK row
    (index 1) fail their ConditionalCheckFailed at once -- not just the lock
    -- when the second caller's own initial reads raced in before the first
    caller's transaction had committed. Checking only the lock's
    CancellationReasons entry (the old behavior) would misreport this exact
    case as "another refund operation is unresolved"; the fix must instead
    re-read this operation's own row with ConsistentRead and return the
    already-committed winner exactly, since it is really the same logical
    request."""
    from botocore.exceptions import ClientError as _ClientError
    repo = DynamoRepository("payments", resource=resource(), webhook_connection_id=CONNECTION_ID)
    repo.put_tenant_manifest(manifest())
    repo.mark_connection_verified(CONNECTION_ID, NOW)
    _, payment_id = _approved_payment_for_refund_lock_tests(repo, "refund-self-race-token", "customer-refund-self-race", "payment-refund-self-race", "approved-refund-self-race")

    shared_key = "refund-self-race-shared-key"
    winner = repo.create_refund_operation(BUSINESS_ID, payment_id, "33333333-3333-4333-8333-333333333333", shared_key, None, NOW)

    own_sk = f"REFUND#{payment_id}#" + __import__("hashlib").sha256(f"{payment_id}\n{shared_key}".encode()).hexdigest()
    lock_sk = f"REFUND_LOCK#{payment_id}"
    real_get = repo._get
    stale_reads_to_fool = [2]  # this second caller's own two initial reads: `existing` and the lock fast path

    def stale_initial_read(business_id_arg, sk_arg):
        # The winner's rows are already committed for real by the time this
        # runs -- fooling exactly the first two reads simulates this second
        # caller's own initial reads having raced in a moment earlier, before
        # either row existed yet, forcing it past both fast-path checks and
        # into a genuine TransactWriteItems conflict against the real,
        # already-committed rows.
        if sk_arg in (own_sk, lock_sk) and stale_reads_to_fool[0] > 0:
            stale_reads_to_fool[0] -= 1
            return None
        return real_get(business_id_arg, sk_arg)

    real_transact_write_items = repo.client.transact_write_items
    captured_reasons = {}

    def spy_transact_write_items(**kwargs):
        try:
            return real_transact_write_items(**kwargs)
        except _ClientError as error:
            captured_reasons["codes"] = [reason.get("Code") for reason in error.response.get("CancellationReasons", [])]
            raise

    with patch.object(repo.client, "transact_write_items", side_effect=spy_transact_write_items), \
         patch.object(repo, "_get", side_effect=stale_initial_read):
        result = repo.create_refund_operation(BUSINESS_ID, payment_id, "33333333-3333-4333-8333-333333333333", shared_key, None, NOW)

    # Confirms the exact reproduction: both the REFUND item (index 0) and the
    # REFUND_LOCK item (index 1) failed their condition together.
    assert captured_reasons["codes"][0] == "ConditionalCheckFailed"
    assert captured_reasons["codes"][1] == "ConditionalCheckFailed"
    # Must return the winning refund exactly -- not raise "unresolved".
    assert result == winner
    charge = repo.find_charge_by_token_digest(__import__("hashlib").sha256(b"refund-self-race-token").digest(), NOW)
    assert len(repo.charge_detail(BUSINESS_ID, charge.id)["refundOperations"]) == 1


@mock_aws
@pytest.mark.skipif(
    not os.environ.get("RUN_REAL_CONCURRENCY_TESTS"),
    reason=(
        "Optional Moto thread stress test, NOT a real DynamoDB concurrency "
        "check -- it is still decorated with @mock_aws, so this only ever "
        "races real Python threads against Moto's in-memory backend, never "
        "actual DynamoDB. Moto's TransactWriteItems is not reliably "
        "thread-safe under that (observed ~1 in 5 runs failing on an "
        "internal copy.deepcopy race unrelated to the code under test), "
        "which is exactly what this exercises deliberately "
        "(RUN_REAL_CONCURRENCY_TESTS=1 pytest ...) -- not as a CI gate, and "
        "not a substitute for validating actual DynamoDB atomicity. For "
        "that, see "
        "tests/integration/test_real_dynamodb_refund_concurrency.py, which "
        "has no @mock_aws and its own separate double opt-in against a real "
        "deployed table. See docs/delivery/2026-09-13-pilot-readiness-audit.md."
    ),
)
def test_concurrent_refund_requests_with_different_keys_moto_thread_stress():
    repo = DynamoRepository("payments", resource=resource(), webhook_connection_id=CONNECTION_ID)
    repo.put_tenant_manifest(manifest())
    repo.mark_connection_verified(CONNECTION_ID, NOW)
    _, payment_id = _approved_payment_for_refund_lock_tests(repo, "refund-lock-concurrent-token", "customer-refund-lock", "payment-refund-lock-concurrent", "approved-refund-lock-concurrent")

    def attempt_refund(key):
        try:
            return repo.create_refund_operation(BUSINESS_ID, payment_id, "33333333-3333-4333-8333-333333333333", key, None, NOW)
        except ValueError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt_refund, ("refund-lock-key-a", "refund-lock-key-b")))
    successes = [value for value in outcomes if isinstance(value, dict)]
    failures = [value for value in outcomes if isinstance(value, ValueError)]
    assert len(successes) == 1 and len(failures) == 1
    assert "unresolved" in str(failures[0])
    charge = repo.find_charge_by_token_digest(__import__("hashlib").sha256(b"refund-lock-concurrent-token").digest(), NOW)
    detail = repo.charge_detail(BUSINESS_ID, charge.id)
    assert len(detail["refundOperations"]) == 1
    assert detail["refundOperations"][0]["refundId"] == successes[0]["refundId"]
    # Whichever key won stays idempotent afterwards; the other keeps failing.
    for key in ("refund-lock-key-a", "refund-lock-key-b"):
        try:
            result = repo.create_refund_operation(BUSINESS_ID, payment_id, "33333333-3333-4333-8333-333333333333", key, None, NOW)
            assert result == successes[0]
        except ValueError:
            pass


@mock_aws
def test_refund_review_retry_keeps_the_lock_but_acknowledge_releases_it():
    repo = DynamoRepository("payments", resource=resource(), webhook_connection_id=CONNECTION_ID)
    repo.put_tenant_manifest(manifest())
    repo.mark_connection_verified(CONNECTION_ID, NOW)
    charge, payment_id = _approved_payment_for_refund_lock_tests(repo, "refund-lock-retry-token", "customer-refund-lock-retry", "payment-refund-lock-retry", "approved-refund-lock-retry")

    first = repo.create_refund_operation(BUSINESS_ID, payment_id, "33333333-3333-4333-8333-333333333333", "refund-lock-retry-1", None, NOW)
    claimed = repo.claim_refund_operations(NOW, NOW.replace(minute=1), 10)
    repo.finish_refund_operation(first["refundId"], claimed[0]["lease_token"], "review", NOW, error="provider_rejected")

    # Still locked while the failed refund sits in the review queue, and
    # "review" is not claimable -- its WorkIndex entry (GSI3PK/GSI3SK) was
    # removed, matching its exclusion from claim_refund_operations' statuses.
    with pytest.raises(ValueError):
        repo.create_refund_operation(BUSINESS_ID, payment_id, "33333333-3333-4333-8333-333333333333", "refund-lock-retry-2", None, NOW)
    assert repo.claim_refund_operations(NOW, NOW.replace(minute=2), 10) == []

    review_item = repo.list_reviews(BUSINESS_ID)[0]
    retried = repo.resolve_review(BUSINESS_ID, "refund", review_item["id"], "33333333-3333-4333-8333-333333333333", "resolve-retry-1", "retry", "Reintentar tras corregir credenciales.", NOW)
    assert retried["outcome"] == "queued"
    # A retry re-queues the same operation; it must not release the lock.
    with pytest.raises(ValueError):
        repo.create_refund_operation(BUSINESS_ID, payment_id, "33333333-3333-4333-8333-333333333333", "refund-lock-retry-3", None, NOW)
    assert repo.charge_detail(BUSINESS_ID, charge.id)["refundOperations"][0]["status"] == "requested"
    # And the retry must have restored the WorkIndex entry -- a worker can
    # actually find and reclaim it again, not just see its status change.
    reclaimed = repo.claim_refund_operations(NOW, NOW.replace(minute=3), 10)
    assert len(reclaimed) == 1 and reclaimed[0]["id"] == first["refundId"]


@mock_aws
def test_refund_review_acknowledge_releases_the_lock_for_a_fresh_refund():
    repo = DynamoRepository("payments", resource=resource(), webhook_connection_id=CONNECTION_ID)
    repo.put_tenant_manifest(manifest())
    repo.mark_connection_verified(CONNECTION_ID, NOW)
    charge, payment_id = _approved_payment_for_refund_lock_tests(repo, "refund-lock-ack-token", "customer-refund-lock-ack", "payment-refund-lock-ack", "approved-refund-lock-ack")

    first = repo.create_refund_operation(BUSINESS_ID, payment_id, "33333333-3333-4333-8333-333333333333", "refund-lock-ack-1", None, NOW)
    claimed = repo.claim_refund_operations(NOW, NOW.replace(minute=1), 10)
    repo.finish_refund_operation(first["refundId"], claimed[0]["lease_token"], "review", NOW, error="provider_rejected")
    review_item = repo.list_reviews(BUSINESS_ID)[0]

    acknowledged = repo.resolve_review(BUSINESS_ID, "refund", review_item["id"], "33333333-3333-4333-8333-333333333333", "resolve-ack-1", "acknowledge", "No se pudo completar; se documenta y libera.", NOW)
    assert acknowledged["outcome"] == "acknowledged"
    assert repo.charge_detail(BUSINESS_ID, charge.id)["refundOperations"][0]["status"] == "resolved"

    second = repo.create_refund_operation(BUSINESS_ID, payment_id, "33333333-3333-4333-8333-333333333333", "refund-lock-ack-2", None, NOW)
    assert second["refundId"] != first["refundId"]
    detail = repo.charge_detail(BUSINESS_ID, charge.id)
    assert {item["refundId"] for item in detail["refundOperations"]} == {first["refundId"], second["refundId"]}


@mock_aws
def test_payment_for_an_already_cancelled_charge_is_recorded_without_allocation():
    """assess_payment knows about cancellation directly: no transaction with
    a doomed condition is even attempted."""
    repo = DynamoRepository("payments", resource=resource(), webhook_connection_id=CONNECTION_ID)
    repo.put_tenant_manifest(manifest())
    repo.mark_connection_verified(CONNECTION_ID, NOW)
    connection = repo.active_connection(BUSINESS_ID, "mercado_pago")
    customer = repo.create_customer(BUSINESS_ID, "Ana López", None, "customer-cancel-before")
    created = repo.create_charge(BUSINESS_ID, customer["customerId"], 500, "MXN", "Saldo", date(2026, 9, 20), link_token="cancel-before-token")
    charge = repo.find_charge_by_token_digest(__import__("hashlib").sha256(b"cancel-before-token").digest(), NOW)
    attempt, _ = repo.get_or_create_attempt(charge, connection, "submission-cancel-before", NOW)
    repo.cancel_charge(BUSINESS_ID, created["chargeId"], "33333333-3333-4333-8333-333333333333", "cancel-before-1", NOW)

    context = repo.payment_context(attempt.id)  # re-reads the charge fresh, already cancelled
    payment = ProviderPayment("payment-cancel-before", attempt.id, "seller-1", "test", 500, "MXN", "approved", NOW, approved_at=NOW)
    assessment = assess_payment(*context, payment)
    assert assessment.allocate is False and assessment.review_reason == "charge_cancelled"
    result = repo.record_payment_observation(context, payment, assessment, "event-cancel-before", NOW)
    assert result.allocate is False and result.review_reason == "charge_cancelled" and result.allocation_minor == 0

    detail = repo.charge_detail(BUSINESS_ID, created["chargeId"])
    assert detail["charge"]["outstandingMinor"] == 500 and detail["charge"]["allocatedMinor"] == 0
    assert len(detail["payments"]) == 1 and detail["payments"][0]["reviewReason"] == "charge_cancelled"
    assert any(item["kind"] == "payment" and item["reason"] == "charge_cancelled" for item in repo.list_reviews(BUSINESS_ID))


@mock_aws
def test_payment_confirmed_concurrently_with_cancellation_is_recorded_not_lost_or_retried_forever():
    """The specific race: assess_payment ran against a not-yet-cancelled
    snapshot (correctly deciding to allocate), then the charge was cancelled
    before the write transaction committed. The transaction's own condition
    (attribute_not_exists(cancelled_at)) fails and rolls back everything --
    this must not lose the payment evidence or become a doomed infinite
    retry, since that specific condition can never pass again."""
    repo = DynamoRepository("payments", resource=resource(), webhook_connection_id=CONNECTION_ID)
    repo.put_tenant_manifest(manifest())
    repo.mark_connection_verified(CONNECTION_ID, NOW)
    connection = repo.active_connection(BUSINESS_ID, "mercado_pago")
    customer = repo.create_customer(BUSINESS_ID, "Ana López", None, "customer-cancel-race")
    created = repo.create_charge(BUSINESS_ID, customer["customerId"], 500, "MXN", "Saldo", date(2026, 9, 20), link_token="cancel-race-token")
    charge = repo.find_charge_by_token_digest(__import__("hashlib").sha256(b"cancel-race-token").digest(), NOW)
    attempt, _ = repo.get_or_create_attempt(charge, connection, "submission-cancel-race", NOW)

    context = repo.payment_context(attempt.id)
    payment = ProviderPayment("payment-cancel-race", attempt.id, "seller-1", "test", 500, "MXN", "approved", NOW, approved_at=NOW)
    assessment = assess_payment(*context, payment)
    assert assessment.allocate is True  # legitimately decided before the race

    repo.cancel_charge(BUSINESS_ID, created["chargeId"], "33333333-3333-4333-8333-333333333333", "cancel-race-1", NOW)

    result = repo.record_payment_observation(context, payment, assessment, "event-cancel-race", NOW)
    assert result.allocate is False
    assert result.review_reason == "charge_cancelled_after_payment"
    assert result.allocation_minor == 0

    detail = repo.charge_detail(BUSINESS_ID, created["chargeId"])
    assert detail["charge"]["outstandingMinor"] == 500 and detail["charge"]["allocatedMinor"] == 0
    assert len(detail["payments"]) == 1  # evidence preserved, not lost
    assert detail["payments"][0]["reviewReason"] == "charge_cancelled_after_payment"
    assert any(item["kind"] == "payment" and item["reason"] == "charge_cancelled_after_payment" for item in repo.list_reviews(BUSINESS_ID))
    # Idempotent-safe: re-observing the same provider payment afterwards
    # must not create a second payment identity or raise.
    again = repo.record_payment_observation(repo.payment_context(attempt.id), payment, assess_payment(*repo.payment_context(attempt.id), payment), "event-cancel-race-retry", NOW)
    assert again.allocation_minor == 0
    assert len(repo.charge_detail(BUSINESS_ID, created["chargeId"])["payments"]) == 1


@mock_aws
def test_provider_event_review_retry_restores_the_work_index_entry():
    repo = DynamoRepository("payments", resource=resource())
    repo.put_tenant_manifest(manifest())
    captured = repo.capture_provider_event(CONNECTION_ID, "event-review-retry-1", "payment-review-retry", "payment", {"data": {"id": "payment-review-retry"}}, True, NOW)
    claimed = repo.claim_provider_events(NOW, NOW.replace(minute=1), 10)
    assert claimed[0]["id"] == captured["id"]
    repo.fail_provider_event(captured["id"], claimed[0]["lease_token"], "unsupported_event_type", NOW, terminal=True)

    # "review" (paused) is not in claim_provider_events' claimable set, and
    # its WorkIndex entry was removed to match.
    assert repo.claim_provider_events(NOW, NOW.replace(minute=2), 10) == []
    review_item = next(item for item in repo.list_reviews(BUSINESS_ID) if item["kind"] == "provider_event")
    retried = repo.resolve_review(BUSINESS_ID, "provider_event", review_item["id"], "33333333-3333-4333-8333-333333333333", "resolve-event-retry-1", "retry", "Reintentar tras corregir el tipo de evento.", NOW)
    assert retried["outcome"] == "queued"
    # The retry must have restored the WorkIndex entry, not just the status.
    reclaimed = repo.claim_provider_events(NOW, NOW.replace(minute=3), 10)
    assert len(reclaimed) == 1 and reclaimed[0]["id"] == captured["id"]


@mock_aws
def test_claim_provider_events_paginates_past_over_100_not_yet_eligible_items():
    """A single Query page of WorkIndex can be entirely items that are not
    eligible yet (future available_at) without being terminal -- they keep
    their WorkIndex entry (status stays claimable-shaped) but the per-item
    availability filter skips them. Real pending work sitting on a later
    page must still be reachable."""
    repo = DynamoRepository("payments", resource=resource())
    repo.put_tenant_manifest(manifest())
    far_future = NOW.replace(year=NOW.year + 1)
    for index in range(130):
        repo.capture_provider_event(CONNECTION_ID, f"event-pagination-blocker-{index:04d}", f"resource-pagination-blocker-{index:04d}", "payment", {}, True, NOW)
    # One claim call already has to page past a 100-item WorkIndex page to
    # see all 130 candidates.
    blockers = repo.claim_provider_events(NOW, NOW.replace(minute=1), 200)
    assert len(blockers) == 130
    for item in blockers:
        repo.fail_provider_event(item["id"], item["lease_token"], "provider_unavailable", far_future, terminal=False)
    target = repo.capture_provider_event(CONNECTION_ID, "event-pagination-target", "resource-pagination-target", "payment", {}, True, NOW)

    # Every blocker is still claimable-shaped (status "failed", not
    # terminal) and keeps its WorkIndex entry, but is skipped on
    # available_at; the one genuinely eligible item now sits behind all 130
    # of them in GSI3SK order and must still be found.
    claimed = repo.claim_provider_events(NOW, NOW.replace(minute=5), 25)
    assert len(claimed) == 1 and claimed[0]["id"] == target["id"]


@mock_aws
def test_two_new_effective_adjustments_in_one_snapshot_are_applied_correctly():
    """A single provider snapshot can carry more than one new effective
    adjustment at once (e.g. a partial refund and a chargeback observed
    together). _apply_existing_payment_snapshot must not attempt two Update
    operations on the same Charge/Attempt inside one TransactWriteItems call
    (DynamoDB rejects a transaction that touches one item twice), and must
    not restore more balance than was actually allocated."""
    repo = DynamoRepository("payments", resource=resource(), webhook_connection_id=CONNECTION_ID)
    repo.put_tenant_manifest(manifest())
    repo.mark_connection_verified(CONNECTION_ID, NOW)
    connection = repo.active_connection(BUSINESS_ID, "mercado_pago")
    customer = repo.create_customer(BUSINESS_ID, "Ana López", None, "customer-double-adjustment")
    repo.create_charge(BUSINESS_ID, customer["customerId"], 500, "MXN", "Saldo", date(2026, 9, 20), link_token="double-adjustment-token")
    charge = repo.find_charge_by_token_digest(__import__("hashlib").sha256(b"double-adjustment-token").digest(), NOW)
    attempt, _ = repo.get_or_create_attempt(charge, connection, "submission-double-adjustment", NOW)
    approved = ProviderPayment("payment-double-adjustment", attempt.id, "seller-1", "test", 500, "MXN", "approved", NOW, approved_at=NOW)
    context = repo.payment_context(attempt.id)
    repo.record_payment_observation(context, approved, assess_payment(*context, approved), "approved-double-adjustment", NOW)

    later = NOW.replace(minute=1)
    snapshot = ProviderPayment(
        "payment-double-adjustment", attempt.id, "seller-1", "test", 500, "MXN", "refunded", later,
        approved_at=NOW, provider_updated_at=later,
        adjustments=(
            ProviderAdjustment("double-adjustment-refund-1", "refund", 200, "approved", later),
            ProviderAdjustment("double-adjustment-chargeback-1", "chargeback", 300, "approved", later),
        ),
    )
    context2 = repo.payment_context(attempt.id)
    repo.record_payment_observation(context2, snapshot, assess_payment(*context2, snapshot), "event-double-adjustment", later)

    updated_charge = repo.find_charge_by_token_digest(__import__("hashlib").sha256(b"double-adjustment-token").digest(), later)
    assert updated_charge.outstanding_minor == 500
    detail = repo.charge_detail(BUSINESS_ID, charge.id)
    assert detail["charge"]["allocatedMinor"] == 0
    assert len(detail["adjustments"]) == 2


@mock_aws
def test_two_new_adjustments_whose_amounts_exceed_the_allocation_are_capped_not_summed_raw():
    """Two new effective adjustments whose face amounts together exceed what
    was actually allocated (e.g. two independent 400 refund/chargeback
    claims against a 500 allocation) must restore at most the allocated
    amount, never leave allocated_minor negative or outstanding_minor beyond
    the charge's own amount_minor."""
    repo = DynamoRepository("payments", resource=resource(), webhook_connection_id=CONNECTION_ID)
    repo.put_tenant_manifest(manifest())
    repo.mark_connection_verified(CONNECTION_ID, NOW)
    connection = repo.active_connection(BUSINESS_ID, "mercado_pago")
    customer = repo.create_customer(BUSINESS_ID, "Ana López", None, "customer-over-adjustment")
    repo.create_charge(BUSINESS_ID, customer["customerId"], 500, "MXN", "Saldo", date(2026, 9, 20), link_token="over-adjustment-token")
    charge = repo.find_charge_by_token_digest(__import__("hashlib").sha256(b"over-adjustment-token").digest(), NOW)
    attempt, _ = repo.get_or_create_attempt(charge, connection, "submission-over-adjustment", NOW)
    approved = ProviderPayment("payment-over-adjustment", attempt.id, "seller-1", "test", 500, "MXN", "approved", NOW, approved_at=NOW)
    context = repo.payment_context(attempt.id)
    repo.record_payment_observation(context, approved, assess_payment(*context, approved), "approved-over-adjustment", NOW)

    later = NOW.replace(minute=1)
    snapshot = ProviderPayment(
        "payment-over-adjustment", attempt.id, "seller-1", "test", 500, "MXN", "refunded", later,
        approved_at=NOW, provider_updated_at=later,
        adjustments=(
            ProviderAdjustment("over-adjustment-refund-1", "refund", 400, "approved", later),
            ProviderAdjustment("over-adjustment-chargeback-1", "chargeback", 400, "approved", later),
        ),
    )
    context2 = repo.payment_context(attempt.id)
    repo.record_payment_observation(context2, snapshot, assess_payment(*context2, snapshot), "event-over-adjustment", later)

    updated_charge = repo.find_charge_by_token_digest(__import__("hashlib").sha256(b"over-adjustment-token").digest(), later)
    assert updated_charge.outstanding_minor == 500  # never exceeds amount_minor
    detail = repo.charge_detail(BUSINESS_ID, charge.id)
    assert detail["charge"]["allocatedMinor"] == 0  # never goes negative
    assert len(detail["adjustments"]) == 2
    # The reported (provider face-value) amount is kept as-is; only the
    # separately-tracked applied amount is capped to what could actually be
    # restored.
    assert sorted(item["amountMinor"] for item in detail["adjustments"]) == [400, 400]
    applied = sorted(item["effectAppliedMinor"] for item in detail["adjustments"])
    assert applied == [100, 400]
    assert sum(applied) == 500


def _approved_and_allocated_payment(repo, link_token, customer_key, payment_id, event_key, amount_minor=500):
    """Shared setup for the adjustment-status-transition tests below: one
    business/customer/charge/attempt with one payment approved and fully
    allocated. Returns (charge, attempt_id, payment_id-in-repo)."""
    connection = repo.active_connection(BUSINESS_ID, "mercado_pago")
    customer = repo.create_customer(BUSINESS_ID, "Ana López", None, customer_key)
    repo.create_charge(BUSINESS_ID, customer["customerId"], amount_minor, "MXN", "Saldo", date(2026, 9, 20), link_token=link_token)
    charge = repo.find_charge_by_token_digest(__import__("hashlib").sha256(link_token.encode()).digest(), NOW)
    attempt, _ = repo.get_or_create_attempt(charge, connection, f"submission-{customer_key}", NOW)
    # Drive the attempt to "ready" (as a real checkout would via
    # mark_attempt_ready) so these tests can actually observe the
    # ready -> expiring transition, not just leave it at "creating" forever.
    checkout = ProviderCheckout(f"preference-{customer_key}", f"https://sandbox.example/{customer_key}", attempt.id, amount_minor, "MXN", "seller-1", "test")
    repo.mark_attempt_ready(BUSINESS_ID, attempt.id, checkout, NOW)
    payment = ProviderPayment(payment_id, attempt.id, "seller-1", "test", amount_minor, "MXN", "approved", NOW, approved_at=NOW)
    context = repo.payment_context(attempt.id)
    repo.record_payment_observation(context, payment, assess_payment(*context, payment), event_key, NOW)
    return charge, attempt.id, repo.charge_detail(BUSINESS_ID, charge.id)["payments"][0]["paymentId"]


def _adjustment_sk(payment_id, provider_adjustment_id):
    import hashlib
    key = hashlib.sha256(str(provider_adjustment_id).encode()).hexdigest()
    return f"ADJUSTMENT#{payment_id}#{key}"


@mock_aws
def test_adjustment_pending_then_approved_restores_balance_exactly_once():
    """The exact reproduction: the SAME provider_adjustment_id observed first
    as "pending" (not effective, no restore) and later as "approved" (now
    effective) must update the stored adjustment and restore the balance --
    the id and its status are not immutable, unlike what
    `if self._get(...): continue` treated them as."""
    repo = DynamoRepository("payments", resource=resource(), webhook_connection_id=CONNECTION_ID)
    repo.put_tenant_manifest(manifest())
    repo.mark_connection_verified(CONNECTION_ID, NOW)
    charge, attempt_id, payment_id = _approved_and_allocated_payment(repo, "adj-transition-token", "customer-adj-transition", "payment-adj-transition", "approved-adj-transition")

    pending_at = NOW.replace(minute=1)
    pending_snapshot = ProviderPayment(
        "payment-adj-transition", attempt_id, "seller-1", "test", 500, "MXN", "approved", pending_at,
        approved_at=NOW, provider_updated_at=pending_at,
        adjustments=(ProviderAdjustment("refund-transition", "refund", 500, "pending", pending_at),),
    )
    context = repo.payment_context(attempt_id)
    repo.record_payment_observation(context, pending_snapshot, assess_payment(*context, pending_snapshot), "event-adj-pending", pending_at)

    detail = repo.charge_detail(BUSINESS_ID, charge.id)
    assert detail["charge"]["outstandingMinor"] == 0
    assert detail["adjustments"][0]["status"] == "pending"
    assert detail["attempts"][0]["status"] == "ready"

    approved_at_2 = NOW.replace(minute=2)
    approved_snapshot = ProviderPayment(
        "payment-adj-transition", attempt_id, "seller-1", "test", 500, "MXN", "refunded", approved_at_2,
        approved_at=NOW, provider_updated_at=approved_at_2,
        adjustments=(ProviderAdjustment("refund-transition", "refund", 500, "approved", approved_at_2),),
    )
    context = repo.payment_context(attempt_id)
    repo.record_payment_observation(context, approved_snapshot, assess_payment(*context, approved_snapshot), "event-adj-approved", approved_at_2)

    detail = repo.charge_detail(BUSINESS_ID, charge.id)
    assert detail["charge"]["outstandingMinor"] == 500
    assert detail["charge"]["allocatedMinor"] == 0
    assert len(detail["adjustments"]) == 1
    assert detail["adjustments"][0]["status"] == "approved"
    assert detail["adjustments"][0]["amountMinor"] == 500
    assert detail["attempts"][0]["status"] == "expiring"


@mock_aws
def test_duplicate_approved_adjustment_observation_is_idempotent():
    repo = DynamoRepository("payments", resource=resource(), webhook_connection_id=CONNECTION_ID)
    repo.put_tenant_manifest(manifest())
    repo.mark_connection_verified(CONNECTION_ID, NOW)
    charge, attempt_id, _ = _approved_and_allocated_payment(repo, "adj-duplicate-token", "customer-adj-duplicate", "payment-adj-duplicate", "approved-adj-duplicate")

    approved_at = NOW.replace(minute=1)
    approved_snapshot = ProviderPayment(
        "payment-adj-duplicate", attempt_id, "seller-1", "test", 500, "MXN", "refunded", approved_at,
        approved_at=NOW, provider_updated_at=approved_at,
        adjustments=(ProviderAdjustment("refund-duplicate", "refund", 500, "approved", approved_at),),
    )
    context = repo.payment_context(attempt_id)
    repo.record_payment_observation(context, approved_snapshot, assess_payment(*context, approved_snapshot), "event-adj-duplicate-1", approved_at)
    assert repo.charge_detail(BUSINESS_ID, charge.id)["charge"]["outstandingMinor"] == 500

    # The exact same "approved" observation arrives again (e.g. a duplicate
    # webhook or reconciliation replay) -- must not restore a second time.
    later = NOW.replace(minute=2)
    context = repo.payment_context(attempt_id)
    repo.record_payment_observation(context, approved_snapshot, assess_payment(*context, approved_snapshot), "event-adj-duplicate-2", later)

    detail = repo.charge_detail(BUSINESS_ID, charge.id)
    assert detail["charge"]["outstandingMinor"] == 500
    assert detail["charge"]["allocatedMinor"] == 0
    assert len(detail["adjustments"]) == 1
    assert detail["adjustments"][0]["amountMinor"] == 500


@mock_aws
def test_rejected_then_approved_restores_balance_exactly_once():
    """Same contract as pending -> approved, starting from a different
    non-effective status."""
    repo = DynamoRepository("payments", resource=resource(), webhook_connection_id=CONNECTION_ID)
    repo.put_tenant_manifest(manifest())
    repo.mark_connection_verified(CONNECTION_ID, NOW)
    charge, attempt_id, _ = _approved_and_allocated_payment(repo, "adj-rejected-token", "customer-adj-rejected", "payment-adj-rejected", "approved-adj-rejected")

    rejected_at = NOW.replace(minute=1)
    rejected_snapshot = ProviderPayment(
        "payment-adj-rejected", attempt_id, "seller-1", "test", 500, "MXN", "approved", rejected_at,
        approved_at=NOW, provider_updated_at=rejected_at,
        adjustments=(ProviderAdjustment("refund-rejected", "refund", 500, "rejected", rejected_at),),
    )
    context = repo.payment_context(attempt_id)
    repo.record_payment_observation(context, rejected_snapshot, assess_payment(*context, rejected_snapshot), "event-adj-rejected-1", rejected_at)
    assert repo.charge_detail(BUSINESS_ID, charge.id)["charge"]["outstandingMinor"] == 0

    approved_at = NOW.replace(minute=2)
    approved_snapshot = ProviderPayment(
        "payment-adj-rejected", attempt_id, "seller-1", "test", 500, "MXN", "refunded", approved_at,
        approved_at=NOW, provider_updated_at=approved_at,
        adjustments=(ProviderAdjustment("refund-rejected", "refund", 500, "approved", approved_at),),
    )
    context = repo.payment_context(attempt_id)
    repo.record_payment_observation(context, approved_snapshot, assess_payment(*context, approved_snapshot), "event-adj-rejected-2", approved_at)

    detail = repo.charge_detail(BUSINESS_ID, charge.id)
    assert detail["charge"]["outstandingMinor"] == 500
    assert detail["adjustments"][0]["status"] == "approved"
    assert detail["adjustments"][0]["amountMinor"] == 500


@mock_aws
def test_older_snapshot_cannot_change_an_adjustment_already_seen_as_newer():
    repo = DynamoRepository("payments", resource=resource(), webhook_connection_id=CONNECTION_ID)
    repo.put_tenant_manifest(manifest())
    repo.mark_connection_verified(CONNECTION_ID, NOW)
    charge, attempt_id, _ = _approved_and_allocated_payment(repo, "adj-stale-token", "customer-adj-stale", "payment-adj-stale", "approved-adj-stale")

    newer_at = NOW.replace(minute=5)
    approved_snapshot = ProviderPayment(
        "payment-adj-stale", attempt_id, "seller-1", "test", 500, "MXN", "refunded", newer_at,
        approved_at=NOW, provider_updated_at=newer_at,
        adjustments=(ProviderAdjustment("refund-stale", "refund", 500, "approved", newer_at),),
    )
    context = repo.payment_context(attempt_id)
    repo.record_payment_observation(context, approved_snapshot, assess_payment(*context, approved_snapshot), "event-adj-stale-1", newer_at)
    assert repo.charge_detail(BUSINESS_ID, charge.id)["charge"]["outstandingMinor"] == 500

    # A stale pending observation, timestamped *before* the approval we
    # already applied, arrives late (out-of-order delivery).
    older_at = NOW.replace(minute=1)
    stale_pending_snapshot = ProviderPayment(
        "payment-adj-stale", attempt_id, "seller-1", "test", 500, "MXN", "approved", older_at,
        approved_at=NOW, provider_updated_at=older_at,
        adjustments=(ProviderAdjustment("refund-stale", "refund", 500, "pending", older_at),),
    )
    context = repo.payment_context(attempt_id)
    repo.record_payment_observation(context, stale_pending_snapshot, assess_payment(*context, stale_pending_snapshot), "event-adj-stale-2", older_at)

    detail = repo.charge_detail(BUSINESS_ID, charge.id)
    assert detail["charge"]["outstandingMinor"] == 500  # not reverted
    assert detail["adjustments"][0]["status"] == "approved"  # not rewritten back to pending
    assert detail["adjustments"][0]["amountMinor"] == 500


@mock_aws
def test_concurrent_observations_making_the_same_adjustment_effective_restore_only_once():
    """Deterministic simulation of two racing observations, using a
    controlled fake instead of real threads (real-thread Moto races are
    flaky for reasons unrelated to this code -- see the audit log). One
    caller's read of the adjustment is frozen to what it was *before* a
    second caller committed the pending->approved transition; when the
    frozen-read caller then tries to write, the transaction's own
    ConditionExpression (comparing against that stale, frozen state) must
    reject it -- proving the mechanism that stops two real concurrent
    workers from both restoring the same money."""
    repo = DynamoRepository("payments", resource=resource(), webhook_connection_id=CONNECTION_ID)
    repo.put_tenant_manifest(manifest())
    repo.mark_connection_verified(CONNECTION_ID, NOW)
    charge, attempt_id, payment_id = _approved_and_allocated_payment(repo, "adj-race-token", "customer-adj-race", "payment-adj-race", "approved-adj-race")

    pending_at = NOW.replace(minute=1)
    pending_snapshot = ProviderPayment(
        "payment-adj-race", attempt_id, "seller-1", "test", 500, "MXN", "approved", pending_at,
        approved_at=NOW, provider_updated_at=pending_at,
        adjustments=(ProviderAdjustment("refund-race", "refund", 500, "pending", pending_at),),
    )
    context = repo.payment_context(attempt_id)
    repo.record_payment_observation(context, pending_snapshot, assess_payment(*context, pending_snapshot), "event-adj-race-pending", pending_at)

    # Both "workers" read the adjustment here, while it is still "pending".
    stale_adjustment_snapshot = repo._get(BUSINESS_ID, _adjustment_sk(payment_id, "refund-race"))
    assert stale_adjustment_snapshot["status"] == "pending"

    approved_at = NOW.replace(minute=2)
    approved_snapshot = ProviderPayment(
        "payment-adj-race", attempt_id, "seller-1", "test", 500, "MXN", "refunded", approved_at,
        approved_at=NOW, provider_updated_at=approved_at,
        adjustments=(ProviderAdjustment("refund-race", "refund", 500, "approved", approved_at),),
    )

    # Worker A: reads fresh, applies the transition for real.
    context_a = repo.payment_context(attempt_id)
    repo.record_payment_observation(context_a, approved_snapshot, assess_payment(*context_a, approved_snapshot), "event-adj-race-a", approved_at)
    assert repo.charge_detail(BUSINESS_ID, charge.id)["charge"]["outstandingMinor"] == 500

    # Worker B: its own read of the adjustment happened earlier (frozen to
    # the pre-A "pending" snapshot); it now tries to commit the same
    # transition B believes it is the first to make.
    real_get = repo._get
    def frozen_read(business_id_arg, sk_arg):
        if sk_arg == _adjustment_sk(payment_id, "refund-race"):
            return stale_adjustment_snapshot
        return real_get(business_id_arg, sk_arg)

    with patch.object(repo, "_get", side_effect=frozen_read):
        context_b = repo.payment_context(attempt_id)
        with pytest.raises(RuntimeError):
            repo.record_payment_observation(context_b, approved_snapshot, assess_payment(*context_b, approved_snapshot), "event-adj-race-b", approved_at)

    # Only Worker A's restore ever took effect.
    detail = repo.charge_detail(BUSINESS_ID, charge.id)
    assert detail["charge"]["outstandingMinor"] == 500
    assert detail["charge"]["allocatedMinor"] == 0


@mock_aws
def test_effective_partial_refund_then_effective_adjustment_never_exceeds_allocated_minor():
    """After an effective partial refund already restored part of the
    balance, a later adjustment becoming effective must be capped against
    the Charge's *current* allocated_minor (already reduced by the first
    restore), not the original, never-updated Allocation amount -- or the
    two could together restore more than was ever allocated."""
    repo = DynamoRepository("payments", resource=resource(), webhook_connection_id=CONNECTION_ID)
    repo.put_tenant_manifest(manifest())
    repo.mark_connection_verified(CONNECTION_ID, NOW)
    charge, attempt_id, _ = _approved_and_allocated_payment(repo, "adj-sequential-token", "customer-adj-sequential", "payment-adj-sequential", "approved-adj-sequential")

    first_at = NOW.replace(minute=1)
    first_snapshot = ProviderPayment(
        "payment-adj-sequential", attempt_id, "seller-1", "test", 500, "MXN", "refunded", first_at,
        approved_at=NOW, provider_updated_at=first_at,
        adjustments=(ProviderAdjustment("refund-sequential-1", "refund", 300, "approved", first_at),),
    )
    context = repo.payment_context(attempt_id)
    repo.record_payment_observation(context, first_snapshot, assess_payment(*context, first_snapshot), "event-adj-sequential-1", first_at)
    detail = repo.charge_detail(BUSINESS_ID, charge.id)
    assert detail["charge"]["allocatedMinor"] == 200 and detail["charge"]["outstandingMinor"] == 300

    # A second, independent adjustment claims 400 more -- more than the 200
    # actually still allocated -- observed as a brand-new pending->approved
    # transition in a later call.
    pending_at = NOW.replace(minute=2)
    pending_snapshot = ProviderPayment(
        "payment-adj-sequential", attempt_id, "seller-1", "test", 500, "MXN", "refunded", pending_at,
        approved_at=NOW, provider_updated_at=pending_at,
        adjustments=(
            ProviderAdjustment("refund-sequential-1", "refund", 300, "approved", first_at),
            ProviderAdjustment("chargeback-sequential-2", "chargeback", 400, "pending", pending_at),
        ),
    )
    context = repo.payment_context(attempt_id)
    repo.record_payment_observation(context, pending_snapshot, assess_payment(*context, pending_snapshot), "event-adj-sequential-2", pending_at)
    assert repo.charge_detail(BUSINESS_ID, charge.id)["charge"]["allocatedMinor"] == 200  # still pending, untouched

    approved_at = NOW.replace(minute=3)
    approved_snapshot = ProviderPayment(
        "payment-adj-sequential", attempt_id, "seller-1", "test", 500, "MXN", "refunded", approved_at,
        approved_at=NOW, provider_updated_at=approved_at,
        adjustments=(
            ProviderAdjustment("refund-sequential-1", "refund", 300, "approved", first_at),
            ProviderAdjustment("chargeback-sequential-2", "chargeback", 400, "approved", approved_at),
        ),
    )
    context = repo.payment_context(attempt_id)
    repo.record_payment_observation(context, approved_snapshot, assess_payment(*context, approved_snapshot), "event-adj-sequential-3", approved_at)

    detail = repo.charge_detail(BUSINESS_ID, charge.id)
    assert detail["charge"]["allocatedMinor"] == 0  # never negative
    assert detail["charge"]["outstandingMinor"] == 500  # never exceeds amount_minor
    applied_by_kind = {item["kind"]: item["effectAppliedMinor"] for item in detail["adjustments"]}
    assert applied_by_kind["refund"] == 300  # untouched, already effective
    assert applied_by_kind["chargeback"] == 200  # capped to what was actually left, not its 400 face value
    reported_by_kind = {item["kind"]: item["amountMinor"] for item in detail["adjustments"]}
    assert reported_by_kind["chargeback"] == 400  # reported face value kept, separate from what was applied


@mock_aws
def test_transaction_touches_charge_and_attempt_at_most_once_each():
    """Regression guard for the DynamoDB constraint discovered in the
    previous pass (a TransactWriteItems call cannot reference the same item
    twice): two adjustments becoming effective together, on top of an
    existing effective one, must still produce exactly one Update on the
    Charge and exactly one on the Attempt."""
    repo = DynamoRepository("payments", resource=resource(), webhook_connection_id=CONNECTION_ID)
    repo.put_tenant_manifest(manifest())
    repo.mark_connection_verified(CONNECTION_ID, NOW)
    charge, attempt_id, _ = _approved_and_allocated_payment(repo, "adj-single-update-token", "customer-adj-single-update", "payment-adj-single-update", "approved-adj-single-update")

    pending_at = NOW.replace(minute=1)
    pending_snapshot = ProviderPayment(
        "payment-adj-single-update", attempt_id, "seller-1", "test", 500, "MXN", "approved", pending_at,
        approved_at=NOW, provider_updated_at=pending_at,
        adjustments=(
            ProviderAdjustment("refund-single-update-1", "refund", 200, "pending", pending_at),
            ProviderAdjustment("refund-single-update-2", "refund", 300, "pending", pending_at),
        ),
    )
    context = repo.payment_context(attempt_id)
    repo.record_payment_observation(context, pending_snapshot, assess_payment(*context, pending_snapshot), "event-adj-single-update-1", pending_at)
    assert repo.charge_detail(BUSINESS_ID, charge.id)["charge"]["outstandingMinor"] == 0

    approved_at = NOW.replace(minute=2)
    approved_snapshot = ProviderPayment(
        "payment-adj-single-update", attempt_id, "seller-1", "test", 500, "MXN", "refunded", approved_at,
        approved_at=NOW, provider_updated_at=approved_at,
        adjustments=(
            ProviderAdjustment("refund-single-update-1", "refund", 200, "approved", approved_at),
            ProviderAdjustment("refund-single-update-2", "refund", 300, "approved", approved_at),
        ),
    )
    context = repo.payment_context(attempt_id)
    # Must not raise (a real DynamoDB duplicate-key-in-transaction error
    # would surface here if the fix regressed to one Update per adjustment).
    repo.record_payment_observation(context, approved_snapshot, assess_payment(*context, approved_snapshot), "event-adj-single-update-2", approved_at)

    detail = repo.charge_detail(BUSINESS_ID, charge.id)
    assert detail["charge"]["outstandingMinor"] == 500
    assert detail["charge"]["allocatedMinor"] == 0
    assert detail["attempts"][0]["status"] == "expiring"


@mock_aws
def test_effective_adjustment_reversed_is_flagged_for_review_not_reverted():
    """No safe automatic rule exists for approved -> rejected on an already
    effective adjustment (see the audit log for why); the already-confirmed
    financial effect is retained and the contradiction is routed to review
    instead of being guessed at."""
    repo = DynamoRepository("payments", resource=resource(), webhook_connection_id=CONNECTION_ID)
    repo.put_tenant_manifest(manifest())
    repo.mark_connection_verified(CONNECTION_ID, NOW)
    charge, attempt_id, _ = _approved_and_allocated_payment(repo, "adj-contradiction-token", "customer-adj-contradiction", "payment-adj-contradiction", "approved-adj-contradiction")

    approved_at = NOW.replace(minute=1)
    approved_snapshot = ProviderPayment(
        "payment-adj-contradiction", attempt_id, "seller-1", "test", 500, "MXN", "refunded", approved_at,
        approved_at=NOW, provider_updated_at=approved_at,
        adjustments=(ProviderAdjustment("refund-contradiction", "refund", 500, "approved", approved_at),),
    )
    context = repo.payment_context(attempt_id)
    repo.record_payment_observation(context, approved_snapshot, assess_payment(*context, approved_snapshot), "event-adj-contradiction-1", approved_at)
    assert repo.charge_detail(BUSINESS_ID, charge.id)["charge"]["outstandingMinor"] == 500

    later = NOW.replace(minute=2)
    reversed_snapshot = ProviderPayment(
        "payment-adj-contradiction", attempt_id, "seller-1", "test", 500, "MXN", "refunded", later,
        approved_at=NOW, provider_updated_at=later,
        adjustments=(ProviderAdjustment("refund-contradiction", "refund", 500, "rejected", later),),
    )
    context = repo.payment_context(attempt_id)
    repo.record_payment_observation(context, reversed_snapshot, assess_payment(*context, reversed_snapshot), "event-adj-contradiction-2", later)

    detail = repo.charge_detail(BUSINESS_ID, charge.id)
    # The already-confirmed refund is retained -- not clawed back.
    assert detail["charge"]["outstandingMinor"] == 500
    assert detail["adjustments"][0]["status"] == "rejected"
    assert detail["adjustments"][0]["amountMinor"] == 500
    assert any(item["kind"] == "payment" and item["reason"] == "adjustment_effective_reversed" for item in repo.list_reviews(BUSINESS_ID))


@mock_aws
def test_approved_rejected_approved_never_applies_money_twice():
    """The exact reproduction from supervision: a 200 partial refund/
    adjustment against a 500 allocation observed approved -> rejected ->
    approved must restore 200 exactly once, not twice. `status` alone is not
    a safe indicator of whether money has moved: effect_applied_at/
    effect_applied_minor are the one-way marker that must gate it."""
    repo = DynamoRepository("payments", resource=resource(), webhook_connection_id=CONNECTION_ID)
    repo.put_tenant_manifest(manifest())
    repo.mark_connection_verified(CONNECTION_ID, NOW)
    charge, attempt_id, _ = _approved_and_allocated_payment(repo, "adj-flip-token", "customer-adj-flip", "payment-adj-flip", "approved-adj-flip")

    approved_at = NOW.replace(minute=1)
    approved_snapshot = ProviderPayment(
        "payment-adj-flip", attempt_id, "seller-1", "test", 500, "MXN", "approved", approved_at,
        approved_at=NOW, provider_updated_at=approved_at,
        adjustments=(ProviderAdjustment("refund-flip", "refund", 200, "approved", approved_at),),
    )
    context = repo.payment_context(attempt_id)
    repo.record_payment_observation(context, approved_snapshot, assess_payment(*context, approved_snapshot), "event-adj-flip-1", approved_at)
    detail = repo.charge_detail(BUSINESS_ID, charge.id)
    assert detail["charge"]["outstandingMinor"] == 200 and detail["charge"]["allocatedMinor"] == 300

    rejected_at = NOW.replace(minute=2)
    rejected_snapshot = ProviderPayment(
        "payment-adj-flip", attempt_id, "seller-1", "test", 500, "MXN", "approved", rejected_at,
        approved_at=NOW, provider_updated_at=rejected_at,
        adjustments=(ProviderAdjustment("refund-flip", "refund", 200, "rejected", rejected_at),),
    )
    context = repo.payment_context(attempt_id)
    repo.record_payment_observation(context, rejected_snapshot, assess_payment(*context, rejected_snapshot), "event-adj-flip-2", rejected_at)
    detail = repo.charge_detail(BUSINESS_ID, charge.id)
    assert detail["charge"]["outstandingMinor"] == 200 and detail["charge"]["allocatedMinor"] == 300

    approved_again_at = NOW.replace(minute=3)
    approved_again_snapshot = ProviderPayment(
        "payment-adj-flip", attempt_id, "seller-1", "test", 500, "MXN", "approved", approved_again_at,
        approved_at=NOW, provider_updated_at=approved_again_at,
        adjustments=(ProviderAdjustment("refund-flip", "refund", 200, "approved", approved_again_at),),
    )
    context = repo.payment_context(attempt_id)
    repo.record_payment_observation(context, approved_again_snapshot, assess_payment(*context, approved_again_snapshot), "event-adj-flip-3", approved_again_at)

    # INCORRECT (the bug) would be outstandingMinor=400/allocatedMinor=100.
    detail = repo.charge_detail(BUSINESS_ID, charge.id)
    assert detail["charge"]["outstandingMinor"] == 200
    assert detail["charge"]["allocatedMinor"] == 300
    assert detail["adjustments"][0]["status"] == "approved"
    assert detail["adjustments"][0]["effectAppliedMinor"] == 200


@mock_aws
def test_confirmed_pending_completed_never_applies_money_twice():
    """Same contract as approved -> rejected -> approved, using a different
    pair of effective/non-effective statuses (confirmed/completed are both
    effective; pending is not)."""
    repo = DynamoRepository("payments", resource=resource(), webhook_connection_id=CONNECTION_ID)
    repo.put_tenant_manifest(manifest())
    repo.mark_connection_verified(CONNECTION_ID, NOW)
    charge, attempt_id, _ = _approved_and_allocated_payment(repo, "adj-cpc-token", "customer-adj-cpc", "payment-adj-cpc", "approved-adj-cpc")

    confirmed_at = NOW.replace(minute=1)
    confirmed_snapshot = ProviderPayment(
        "payment-adj-cpc", attempt_id, "seller-1", "test", 500, "MXN", "approved", confirmed_at,
        approved_at=NOW, provider_updated_at=confirmed_at,
        adjustments=(ProviderAdjustment("chargeback-cpc", "chargeback", 500, "confirmed", confirmed_at),),
    )
    context = repo.payment_context(attempt_id)
    repo.record_payment_observation(context, confirmed_snapshot, assess_payment(*context, confirmed_snapshot), "event-adj-cpc-1", confirmed_at)
    assert repo.charge_detail(BUSINESS_ID, charge.id)["charge"]["outstandingMinor"] == 500

    pending_at = NOW.replace(minute=2)
    pending_snapshot = ProviderPayment(
        "payment-adj-cpc", attempt_id, "seller-1", "test", 500, "MXN", "approved", pending_at,
        approved_at=NOW, provider_updated_at=pending_at,
        adjustments=(ProviderAdjustment("chargeback-cpc", "chargeback", 500, "pending", pending_at),),
    )
    context = repo.payment_context(attempt_id)
    repo.record_payment_observation(context, pending_snapshot, assess_payment(*context, pending_snapshot), "event-adj-cpc-2", pending_at)
    assert repo.charge_detail(BUSINESS_ID, charge.id)["charge"]["outstandingMinor"] == 500

    completed_at = NOW.replace(minute=3)
    completed_snapshot = ProviderPayment(
        "payment-adj-cpc", attempt_id, "seller-1", "test", 500, "MXN", "approved", completed_at,
        approved_at=NOW, provider_updated_at=completed_at,
        adjustments=(ProviderAdjustment("chargeback-cpc", "chargeback", 500, "completed", completed_at),),
    )
    context = repo.payment_context(attempt_id)
    repo.record_payment_observation(context, completed_snapshot, assess_payment(*context, completed_snapshot), "event-adj-cpc-3", completed_at)

    detail = repo.charge_detail(BUSINESS_ID, charge.id)
    assert detail["charge"]["outstandingMinor"] == 500  # not 1000
    assert detail["charge"]["allocatedMinor"] == 0  # not negative
    assert detail["adjustments"][0]["effectAppliedMinor"] == 500


@mock_aws
def test_contradiction_review_stays_open_until_explicit_resolution():
    """An open contradiction review (approved -> rejected on an already
    effective adjustment) must not be silently cleared by a later,
    unrelated-to-the-contradiction observation of the same payment -- only
    resolve_review's explicit "acknowledge" action may close it, since no
    automatic domain rule exists to decide the contradiction is resolved."""
    repo = DynamoRepository("payments", resource=resource(), webhook_connection_id=CONNECTION_ID)
    repo.put_tenant_manifest(manifest())
    repo.mark_connection_verified(CONNECTION_ID, NOW)
    charge, attempt_id, payment_id = _approved_and_allocated_payment(repo, "adj-review-sticky-token", "customer-adj-review-sticky", "payment-adj-review-sticky", "approved-adj-review-sticky")

    approved_at = NOW.replace(minute=1)
    approved_snapshot = ProviderPayment(
        "payment-adj-review-sticky", attempt_id, "seller-1", "test", 500, "MXN", "approved", approved_at,
        approved_at=NOW, provider_updated_at=approved_at,
        adjustments=(ProviderAdjustment("refund-review-sticky", "refund", 500, "approved", approved_at),),
    )
    context = repo.payment_context(attempt_id)
    repo.record_payment_observation(context, approved_snapshot, assess_payment(*context, approved_snapshot), "event-adj-review-sticky-1", approved_at)

    rejected_at = NOW.replace(minute=2)
    rejected_snapshot = ProviderPayment(
        "payment-adj-review-sticky", attempt_id, "seller-1", "test", 500, "MXN", "approved", rejected_at,
        approved_at=NOW, provider_updated_at=rejected_at,
        adjustments=(ProviderAdjustment("refund-review-sticky", "refund", 500, "rejected", rejected_at),),
    )
    context = repo.payment_context(attempt_id)
    repo.record_payment_observation(context, rejected_snapshot, assess_payment(*context, rejected_snapshot), "event-adj-review-sticky-2", rejected_at)
    assert any(item["reason"] == "adjustment_effective_reversed" for item in repo.list_reviews(BUSINESS_ID))

    # A third, unrelated observation (the same adjustment settling back to
    # "approved", or simply a re-delivered snapshot) must not silently drop
    # the still-open review -- nothing has resolved the contradiction.
    approved_again_at = NOW.replace(minute=3)
    approved_again_snapshot = ProviderPayment(
        "payment-adj-review-sticky", attempt_id, "seller-1", "test", 500, "MXN", "approved", approved_again_at,
        approved_at=NOW, provider_updated_at=approved_again_at,
        adjustments=(ProviderAdjustment("refund-review-sticky", "refund", 500, "approved", approved_again_at),),
    )
    context = repo.payment_context(attempt_id)
    repo.record_payment_observation(context, approved_again_snapshot, assess_payment(*context, approved_again_snapshot), "event-adj-review-sticky-3", approved_again_at)
    detail = repo.charge_detail(BUSINESS_ID, charge.id)
    assert detail["payments"][0]["reviewReason"] == "adjustment_effective_reversed"
    assert any(item["reason"] == "adjustment_effective_reversed" for item in repo.list_reviews(BUSINESS_ID))

    # Only an explicit acknowledge clears it.
    repo.resolve_review(BUSINESS_ID, "payment", payment_id, "33333333-3333-4333-8333-333333333333", "resolve-adj-review-sticky", "acknowledge", "Revisado y aceptado por la propietaria.", NOW)
    detail = repo.charge_detail(BUSINESS_ID, charge.id)
    assert detail["payments"][0]["reviewReason"] is None
    assert not any(item["reason"] == "adjustment_effective_reversed" for item in repo.list_reviews(BUSINESS_ID))


@mock_aws
def test_concurrent_observations_racing_the_reject_then_approve_transition_cannot_double_apply():
    """Two concurrent workers, one of them working off a stale read from
    before the adjustment was ever marked effective, must not both be able
    to flip effect_applied_at. This adjustment id is *first* observed as
    "rejected" (never effective, effect_applied_at unset) rather than
    "pending", exercising the same not-yet-applied->first-effective race the
    reject/re-approve defect was specifically about, on a distinct starting
    status from the existing pending->approved race test."""
    repo = DynamoRepository("payments", resource=resource(), webhook_connection_id=CONNECTION_ID)
    repo.put_tenant_manifest(manifest())
    repo.mark_connection_verified(CONNECTION_ID, NOW)
    charge, attempt_id, payment_id = _approved_and_allocated_payment(repo, "adj-race2-token", "customer-adj-race2", "payment-adj-race2", "approved-adj-race2")

    rejected_at = NOW.replace(minute=1)
    rejected_snapshot = ProviderPayment(
        "payment-adj-race2", attempt_id, "seller-1", "test", 500, "MXN", "approved", rejected_at,
        approved_at=NOW, provider_updated_at=rejected_at,
        adjustments=(ProviderAdjustment("refund-race2", "refund", 200, "rejected", rejected_at),),
    )
    context = repo.payment_context(attempt_id)
    repo.record_payment_observation(context, rejected_snapshot, assess_payment(*context, rejected_snapshot), "event-adj-race2-1", rejected_at)

    # Both "workers" read the adjustment here, while it is "rejected" and has
    # never been effective yet.
    stale_adjustment_snapshot = repo._get(BUSINESS_ID, _adjustment_sk(payment_id, "refund-race2"))
    assert stale_adjustment_snapshot["status"] == "rejected"
    assert not stale_adjustment_snapshot.get("effect_applied_at")

    approved_at = NOW.replace(minute=2)
    approved_snapshot = ProviderPayment(
        "payment-adj-race2", attempt_id, "seller-1", "test", 500, "MXN", "approved", approved_at,
        approved_at=NOW, provider_updated_at=approved_at,
        adjustments=(ProviderAdjustment("refund-race2", "refund", 200, "approved", approved_at),),
    )

    # Worker A: reads fresh, applies the first-ever effective transition for
    # real.
    context_a = repo.payment_context(attempt_id)
    repo.record_payment_observation(context_a, approved_snapshot, assess_payment(*context_a, approved_snapshot), "event-adj-race2-a", approved_at)
    detail = repo.charge_detail(BUSINESS_ID, charge.id)
    assert detail["charge"]["outstandingMinor"] == 200 and detail["charge"]["allocatedMinor"] == 300

    # Worker B: its own read happened earlier (frozen to the pre-A
    # "rejected, not yet applied" snapshot); it now tries to commit the same
    # transition B believes it is the first to make.
    real_get = repo._get
    def frozen_read(business_id_arg, sk_arg):
        if sk_arg == _adjustment_sk(payment_id, "refund-race2"):
            return stale_adjustment_snapshot
        return real_get(business_id_arg, sk_arg)

    with patch.object(repo, "_get", side_effect=frozen_read):
        context_b = repo.payment_context(attempt_id)
        with pytest.raises(RuntimeError):
            repo.record_payment_observation(context_b, approved_snapshot, assess_payment(*context_b, approved_snapshot), "event-adj-race2-b", approved_at)

    # Only Worker A's restore ever took effect -- money moved exactly once.
    detail = repo.charge_detail(BUSINESS_ID, charge.id)
    assert detail["charge"]["outstandingMinor"] == 200
    assert detail["charge"]["allocatedMinor"] == 300


@mock_aws
def test_reported_amount_can_be_corrected_after_effect_applied_without_changing_applied_amount():
    """A provider can later correct the face amount it reports for an
    already-effective adjustment (e.g. a currency-rounding fix) without that
    ever changing how much was actually restored to the charge -- the
    reported and applied amounts are tracked separately precisely so a
    correction like this can never move money a second time."""
    repo = DynamoRepository("payments", resource=resource(), webhook_connection_id=CONNECTION_ID)
    repo.put_tenant_manifest(manifest())
    repo.mark_connection_verified(CONNECTION_ID, NOW)
    charge, attempt_id, _ = _approved_and_allocated_payment(repo, "adj-correction-token", "customer-adj-correction", "payment-adj-correction", "approved-adj-correction")

    approved_at = NOW.replace(minute=1)
    approved_snapshot = ProviderPayment(
        "payment-adj-correction", attempt_id, "seller-1", "test", 500, "MXN", "approved", approved_at,
        approved_at=NOW, provider_updated_at=approved_at,
        adjustments=(ProviderAdjustment("refund-correction", "refund", 200, "approved", approved_at),),
    )
    context = repo.payment_context(attempt_id)
    repo.record_payment_observation(context, approved_snapshot, assess_payment(*context, approved_snapshot), "event-adj-correction-1", approved_at)
    detail = repo.charge_detail(BUSINESS_ID, charge.id)
    assert detail["charge"]["outstandingMinor"] == 200
    assert detail["adjustments"][0]["amountMinor"] == 200
    assert detail["adjustments"][0]["effectAppliedMinor"] == 200

    corrected_at = NOW.replace(minute=2)
    corrected_snapshot = ProviderPayment(
        "payment-adj-correction", attempt_id, "seller-1", "test", 500, "MXN", "approved", corrected_at,
        approved_at=NOW, provider_updated_at=corrected_at,
        adjustments=(ProviderAdjustment("refund-correction", "refund", 250, "approved", corrected_at),),
    )
    context = repo.payment_context(attempt_id)
    repo.record_payment_observation(context, corrected_snapshot, assess_payment(*context, corrected_snapshot), "event-adj-correction-2", corrected_at)

    detail = repo.charge_detail(BUSINESS_ID, charge.id)
    # The reported face value follows the provider's correction...
    assert detail["adjustments"][0]["amountMinor"] == 250
    # ...but the amount actually applied to the balance never changes again.
    assert detail["adjustments"][0]["effectAppliedMinor"] == 200
    assert detail["charge"]["outstandingMinor"] == 200
    assert detail["charge"]["allocatedMinor"] == 300


@mock_aws
def test_create_customer_conflict_retry_is_bounded_not_infinite():
    """P5: create_customer used to recurse without limit on every
    conditional conflict. A sustained conflict (e.g. real throttling, not
    just a one-shot race that resolves on the next read) must give up after
    a bounded number of attempts with a clear error, not recurse forever."""
    from botocore.exceptions import ClientError as _ClientError
    repo = DynamoRepository("payments", resource=resource())
    repo.put_tenant_manifest(manifest())
    always_conflicting = _ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "boom"}}, "TransactWriteItems")
    with patch.object(repo.client, "transact_write_items", side_effect=always_conflicting):
        with pytest.raises(RuntimeError, match="repeatedly"):
            repo.create_customer(BUSINESS_ID, "Ana Bound", None, "customer-bound-retry")


@mock_aws
def test_create_refund_operation_non_lock_conflict_retry_is_bounded():
    """The other half of P5 for create_refund_operation specifically: a
    sustained conflict that is *not* the REFUND_LOCK item (so the immediate,
    non-retried ValueError path does not apply) must still give up after a
    bounded number of attempts, not recurse forever."""
    from botocore.exceptions import ClientError as _ClientError
    repo = DynamoRepository("payments", resource=resource(), webhook_connection_id=CONNECTION_ID)
    repo.put_tenant_manifest(manifest())
    repo.mark_connection_verified(CONNECTION_ID, NOW)
    _, payment_id = _approved_payment_for_refund_lock_tests(repo, "refund-bound-retry-token", "customer-refund-bound-retry", "payment-refund-bound-retry", "approved-refund-bound-retry")
    # Index 0 (the refund item itself) is the one that "fails", not index 1
    # (the lock) -- simulating sustained throttling/contention unrelated to
    # another unresolved refund, which the lock-specific fast path must not
    # intercept.
    always_conflicting = _ClientError(
        {"Error": {"Code": "TransactionCanceledException", "Message": "boom"},
         "CancellationReasons": [{"Code": "ConditionalCheckFailed"}, {"Code": "None"}, {"Code": "None"}]},
        "TransactWriteItems",
    )
    with patch.object(repo.client, "transact_write_items", side_effect=always_conflicting):
        with pytest.raises(RuntimeError, match="repeatedly"):
            repo.create_refund_operation(BUSINESS_ID, payment_id, "33333333-3333-4333-8333-333333333333", "refund-bound-retry-key", None, NOW)


@mock_aws
def test_mark_attempt_ready_never_consults_the_gsi_at_all():
    """R30, resolved rather than merely mitigated: mark_attempt_ready now
    takes business_id (every real caller already has it on the
    PaymentAttempt it just created or fetched) and resolves the attempt with
    a direct, strongly consistent PK/SK GET on the base table -- not a
    LookupIndex (GSI) query, which is never eligible for ConsistentRead and
    was the actual source of the transient-miss risk the older, weaker
    retry-based mitigation only papered over. Proven here by making
    `_lookup` (the GSI path) raise if it is ever called at all -- and
    mark_attempt_ready must still succeed."""
    repo = DynamoRepository("payments", resource=resource())
    repo.put_tenant_manifest(manifest())
    repo.mark_connection_verified(CONNECTION_ID, NOW)
    connection = repo.active_connection(BUSINESS_ID, "mercado_pago")
    customer = repo.create_customer(BUSINESS_ID, "Ana López", None, "customer-no-gsi")
    repo.create_charge(BUSINESS_ID, customer["customerId"], 500, "MXN", "Saldo", date(2026, 9, 20), link_token="no-gsi-token")
    charge = repo.find_charge_by_token_digest(__import__("hashlib").sha256(b"no-gsi-token").digest(), NOW)
    attempt, _ = repo.get_or_create_attempt(charge, connection, "submission-no-gsi", NOW)

    def gsi_must_not_be_called(identity):
        raise AssertionError(f"mark_attempt_ready must not query the GSI, got _lookup({identity!r})")

    checkout = ProviderCheckout("preference-no-gsi", "https://sandbox.example/no-gsi", attempt.id, 500, "MXN", "seller-1", "test")
    with patch.object(repo, "_lookup", side_effect=gsi_must_not_be_called):
        ready = repo.mark_attempt_ready(BUSINESS_ID, attempt.id, checkout, NOW)
    assert ready.status == "ready"


@mock_aws
def test_mark_attempt_ready_fails_promptly_with_exactly_one_read_when_genuinely_missing():
    """An attempt id that never resolves under the given business_id (a real
    bug, a stale id, or a foreign id from a different business) fails with
    exactly one strongly consistent read -- there is nothing to retry
    against a direct PK/SK GET, unlike the GSI-based lookup this replaced."""
    repo = DynamoRepository("payments", resource=resource())
    repo.put_tenant_manifest(manifest())
    checkout = ProviderCheckout("preference-missing", "https://sandbox.example/missing", "11111111-1111-4111-8111-111111111111", 500, "MXN", "seller-1", "test")
    reads = []
    real_get = repo._get
    def counting_get(business_id_arg, sk_arg):
        reads.append((business_id_arg, sk_arg))
        return real_get(business_id_arg, sk_arg)
    with patch.object(repo, "_get", side_effect=counting_get):
        with pytest.raises(RuntimeError, match="different preference"):
            repo.mark_attempt_ready(BUSINESS_ID, "11111111-1111-4111-8111-111111111111", checkout, NOW)
    assert len(reads) == 1
