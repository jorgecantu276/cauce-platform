"""Real-AWS-DynamoDB concurrency validation for the refund lock.

This file intentionally has NO @mock_aws anywhere in it: every test here
issues real boto3 calls against a real, already-deployed DynamoDB table.
It exists to close the gap the pilot readiness audit flagged in what used
to be called test_concurrent_refund_requests_with_different_keys_manual_real_threads
in tests/unit/test_dynamodb_repository.py -- that test was still decorated
with @mock_aws, so RUN_REAL_CONCURRENCY_TESTS=1 only ever raced real Python
threads against Moto's in-memory backend, never actual DynamoDB. That test
(now honestly renamed to
test_concurrent_refund_requests_with_different_keys_moto_thread_stress)
still exists and is still useful as a Moto thread-safety stress test, but it
is not, and was never, a substitute for this.

Double opt-in, on purpose: this is never meant to run in CI, in this
session, or by accident.

    RUN_REAL_AWS_CONCURRENCY_TESTS=1 \
    REAL_DYNAMODB_TABLE_NAME=<an-already-deployed-table-name> \
    AWS_REGION=us-east-1 \
    pytest backend/tests/integration/test_real_dynamodb_refund_concurrency.py -q

Both RUN_REAL_AWS_CONCURRENCY_TESTS=1 and an explicit REAL_DYNAMODB_TABLE_NAME
must be set, or every test in this file is skipped -- there is no default
table name, on purpose, so this can never accidentally target a real table
just because an environment variable happened to already be set for some
other reason. AWS_ENDPOINT_URL_DYNAMODB may additionally be set to point this
at DynamoDB Local instead of real AWS, if a local daemon is preferred over a
deployed sandbox table for this check.

Run this manually after a real sandbox deploy, not as part of routine
verification. It creates and deletes its own rows under a random business_id
and connection_id per run, and cleans them up in a `finally` block -- but it is
still real read/write traffic against a real table, so treat it accordingly.
"""

from datetime import date, datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import os
import sys
import uuid

import boto3
from botocore.exceptions import ClientError
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from payments.dynamodb import DynamoRepository
from payments.models import ProviderPayment
from payments.service import assess_payment


REAL_TABLE_NAME = os.environ.get("REAL_DYNAMODB_TABLE_NAME")
REAL_TESTS_ENABLED = os.environ.get("RUN_REAL_AWS_CONCURRENCY_TESTS") == "1"

pytestmark = pytest.mark.skipif(
    not (REAL_TESTS_ENABLED and REAL_TABLE_NAME),
    reason=(
        "Real DynamoDB concurrency validation, not a CI test and not run by "
        "default: requires BOTH RUN_REAL_AWS_CONCURRENCY_TESTS=1 and an "
        "explicit REAL_DYNAMODB_TABLE_NAME (double opt-in, deliberate -- "
        "this issues real AWS API calls against a real, already-deployed "
        "table). Run manually after a sandbox deploy; see this file's module "
        "docstring for the exact command, and "
        "docs/delivery/2026-09-13-pilot-readiness-audit.md for why the old "
        "Moto-backed 'manual_real_threads' test never actually validated "
        "this."
    ),
)


NOW = datetime(2026, 9, 13, 15, 0, tzinfo=timezone.utc)


def _repository():
    kwargs = {"region_name": os.environ.get("AWS_REGION", "us-east-1")}
    endpoint = os.environ.get("AWS_ENDPOINT_URL_DYNAMODB")
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    resource = boto3.resource("dynamodb", **kwargs)
    return DynamoRepository(REAL_TABLE_NAME, resource=resource)


def _delete_all_items_for_business(repo, business_id, connection_id):
    """Best-effort cleanup: this table is real and possibly shared with
    other real data, so this test must never leave rows behind, pass or
    fail. Deletes this run's business partition and its connection identity
    only when that identity still belongs to this business."""
    pk = f"BUSINESS#{business_id}"
    try:
        response = repo.table.query(KeyConditionExpression="PK = :pk", ExpressionAttributeValues={":pk": pk})
        with repo.table.batch_writer() as batch:
            for item in response.get("Items", []):
                batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
    finally:
        try:
            repo.table.delete_item(
                Key={"PK": f"CONNECTION_IDENTITY#{connection_id}", "SK": "OWNER"},
                ConditionExpression="business_id = :business_id",
                ExpressionAttributeValues={":business_id": business_id},
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
    # PAYMENT_IDENTITY#... owner rows live under their own hashed PK, not the
    # business partition -- find them via the payments just deleted above by
    # re-deriving nothing (they hold no reverse pointer), so this is the one
    # known gap: an owner row for a payment created by this test can outlive
    # it. It carries no financial data (business_id + payment_id only) and
    # its SK is a fixed random per-run UUID-derived value, so it can never
    # collide with a future run's data.


def _setup_business_with_refundable_payment(repo, business_id, connection_id, payment_id):
    manifest = {
        "business": {"id": business_id, "displayName": "Real Concurrency Check", "folioPrefix": "RC", "branding": {}},
        "memberships": [{"id": str(uuid.uuid4()), "subjectId": "real-concurrency-owner", "role": "owner"}],
        "mercadoPago": {"id": connection_id, "providerAccountId": "seller-real-concurrency", "credentialSecretRef": "arn:credentials", "webhookSecretRef": "arn:webhook"},
    }
    repo.put_tenant_manifest(manifest)
    repo.mark_connection_verified(connection_id, NOW)
    connection = repo.active_connection(business_id, "mercado_pago")
    customer = repo.create_customer(business_id, "Cliente de Prueba Real", None, str(uuid.uuid4()))
    link_token = str(uuid.uuid4())
    repo.create_charge(business_id, customer["customerId"], 500, "MXN", "Saldo de prueba real", date(2026, 9, 20), link_token=link_token)
    import hashlib
    charge = repo.find_charge_by_token_digest(hashlib.sha256(link_token.encode()).digest(), NOW)
    attempt, _ = repo.get_or_create_attempt(charge, connection, str(uuid.uuid4()), NOW)
    payment = ProviderPayment(payment_id, attempt.id, "seller-real-concurrency", "test", 500, "MXN", "approved", NOW, approved_at=NOW)
    context = repo.payment_context(attempt.id)
    repo.record_payment_observation(context, payment, assess_payment(*context, payment), str(uuid.uuid4()), NOW)
    return charge, repo.charge_detail(business_id, charge.id)["payments"][0]["paymentId"]


def test_real_dynamodb_two_different_refund_keys_racing_the_same_payment_only_one_wins():
    """The same guarantee as
    tests/unit/test_dynamodb_repository.py::test_two_different_refund_keys_racing_the_same_payment_only_one_wins,
    but against a real DynamoDB table with genuine concurrent threads -- no
    Moto, no patched fast-path reads. Exactly one of two concurrent
    create_refund_operation calls with different Idempotency-Key values for
    the same payment must win; the other must see "another refund operation
    is unresolved", using the real REFUND_LOCK item's real
    attribute_not_exists(PK) condition under real network-level concurrency."""
    repo = _repository()
    business_id, connection_id, payment_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    membership_id = str(uuid.uuid4())
    try:
        charge, stored_payment_id = _setup_business_with_refundable_payment(repo, business_id, connection_id, payment_id)

        def attempt_refund(key):
            try:
                return repo.create_refund_operation(business_id, stored_payment_id, membership_id, key, None, NOW)
            except ValueError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt_refund, ("real-concurrency-key-a", "real-concurrency-key-b")))

        successes = [value for value in outcomes if isinstance(value, dict)]
        failures = [value for value in outcomes if isinstance(value, ValueError)]
        assert len(successes) == 1, f"expected exactly one winner, got {outcomes!r}"
        assert len(failures) == 1
        assert "unresolved" in str(failures[0])
        detail = repo.charge_detail(business_id, charge.id)
        assert len(detail["refundOperations"]) == 1
        assert detail["refundOperations"][0]["refundId"] == successes[0]["refundId"]
    finally:
        _delete_all_items_for_business(repo, business_id, connection_id)
