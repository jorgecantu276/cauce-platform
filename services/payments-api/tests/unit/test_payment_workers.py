from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from payments.models import (
    Charge, MerchantConnection, PaymentAttempt, ProviderCheckout,
)
from payments.workers import (
    run_operational_health, run_outbox_worker, run_provider_event_worker,
    run_reconciliation_worker, run_refund_worker,
)


NOW = datetime(2026, 9, 11, 18, 0, tzinfo=timezone.utc)


class EventRepository:
    def __init__(self):
        self.failed = []

    def claim_provider_events(self, now, lease_until, limit):
        return [{
            "id":"event-db-1", "merchant_connection_id":"connection-1",
            "provider_event_key":"request-1", "provider_resource_id":"pay-1",
            "event_type":"payment", "lease_token":"lease-1",
            "processing_attempt_count":1,
        }]

    def fail_provider_event(self, *args, **kwargs):
        self.failed.append((args,kwargs))


class Flow:
    def __init__(self):
        self.calls = []
        self.provider = None

    def reconcile_payment(self, event_key, payment_id, expected_connection_id=None):
        self.calls.append((event_key,payment_id,expected_connection_id))
        return type("Assessment", (), {"review_reason":None})()


@dataclass
class Context:
    flow: object
    repository: object = None


class EventRuntime:
    def __init__(self):
        self.repo = EventRepository()
        self.flow = Flow()

    def repository(self):
        return self.repo

    def payment_context(self, connection_id):
        assert connection_id == "connection-1"
        return Context(self.flow)


def test_provider_event_worker_claims_and_processes_authoritatively():
    runtime = EventRuntime()
    result = run_provider_event_worker(runtime,now=NOW)
    assert result == {"claimed":1,"processed":1,"failed":0,"review":0}
    assert runtime.flow.calls == [("request-1","pay-1","connection-1")]
    assert runtime.repo.failed == []


@pytest.mark.parametrize("signature_valid,source", [(True, "owner_review_retry"), (False, "webhook")])
def test_provider_event_worker_rejects_non_internal_payment_review_retry(signature_valid, source):
    runtime = EventRuntime()
    runtime.repo.claim_provider_events = lambda now, lease_until, limit: [{
        "id": "event-db-1", "merchant_connection_id": "connection-1",
        "event_type": "payment_review.retry", "lease_token": "lease-1",
        "signature_valid": signature_valid, "raw_payload": {"source": source, "reviewId": "review-1"},
    }]
    result = run_provider_event_worker(runtime, now=NOW)
    assert result == {"claimed": 1, "processed": 0, "failed": 0, "review": 1}
    assert runtime.flow.calls == []
    assert runtime.repo.failed[0][0][2] == "invalid_internal_retry_event"
    assert runtime.repo.failed[0][1]["terminal"] is True


def test_provider_event_failure_log_is_structured_and_does_not_leak_error_text(caplog):
    runtime = EventRuntime()
    runtime.flow.reconcile_payment = lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("access_token=must-not-appear")
    )
    with caplog.at_level(logging.ERROR, logger="payments.workers"):
        result = run_provider_event_worker(runtime,now=NOW)
    assert result["failed"] == 1
    record = json.loads(caplog.records[-1].getMessage())
    assert record == {
        "attemptCount":1,"connectionId":"connection-1","errorType":"RuntimeError",
        "event":"payment_worker_failure","eventId":"event-db-1","worker":"provider_event",
    }
    assert "must-not-appear" not in caplog.text


class ReconciliationProvider:
    def __init__(self):
        self.expired = []

    def recover_checkout(self, attempt_id):
        return ProviderCheckout(
            "pref-1","https://checkout.example/pref-1",attempt_id,12500,"MXN",
            "seller-1","test",
        )

    def search_payments(self, attempt_id):
        return [
            {"id":"pay-1","updated_at":"2026-09-11T18:01:00Z"},
            {"id":"pay-2","updated_at":"2026-09-11T18:02:00Z"},
        ]

    def expire_checkout(self, preference_id, now):
        self.expired.append((preference_id,now))


class ReconciliationFlow(Flow):
    def __init__(self, repo):
        super().__init__()
        self.provider = ReconciliationProvider()
        self.repository = repo
        self.clock = lambda: NOW

    def _validate_checkout(self, request, checkout):
        assert checkout.external_reference == request.external_reference


class ReconciliationRepository:
    def __init__(self):
        self.events = []
        self.finished = []
        self.ready = []
        self.expired = []
        self.attempt = PaymentAttempt(
            "attempt-1","business-1","charge-1","connection-1","operation-1",
            "submission-1",12500,"MXN","mercado_pago","test","unknown",
        )
        self.connection = MerchantConnection(
            "connection-1","business-1","mercado_pago","seller-1","test",NOW
        )
        self.charge = Charge(
            "charge-1","business-1","customer-1","C-1",12500,12500,"MXN",
            "Anticipo",NOW.date(),
        )

    def claim_reconciliation_attempts(self, now, lease_until, limit):
        return [{
            "id":"attempt-1","merchant_connection_id":"connection-1",
            "status":self.attempt.status,"provider_preference_id":self.attempt.provider_preference_id,
            "recovery_attempt_count":1,
            "recovery_lease_token":"lease-1",
        }]

    def payment_context(self, attempt_id):
        return self.attempt,self.connection,self.charge

    def mark_attempt_ready(self, business_id, attempt_id, checkout, now):
        assert business_id == self.attempt.business_id
        self.ready.append((attempt_id,checkout.preference_id))

    def mark_attempt_expired(self, attempt_id, lease_token, now):
        self.expired.append((attempt_id,lease_token,now))

    def capture_provider_event(self, connection_id, key, resource_id, *args):
        self.events.append((connection_id,key,resource_id))
        return {"status":"accepted"}

    def finish_reconciliation(self, *args):
        self.finished.append(args)


class ReconciliationRuntime:
    def __init__(self):
        self.repo = ReconciliationRepository()
        self.flow = ReconciliationFlow(self.repo)

    def repository(self):
        return self.repo

    def payment_context(self, connection_id):
        return Context(self.flow,self.repo)


def test_reconciliation_recovers_preference_and_preserves_two_real_payments():
    runtime = ReconciliationRuntime()
    result = run_reconciliation_worker(runtime,now=NOW)
    assert result == {"claimed":1,"paymentsQueued":2,"recovered":1,"failed":0}
    assert runtime.repo.ready == [("attempt-1","pref-1")]
    assert [event[2] for event in runtime.repo.events] == ["pay-1","pay-2"]
    assert len(runtime.repo.finished) == 1


def test_reconciliation_expires_obsolete_provider_preference_before_finishing():
    runtime = ReconciliationRuntime()
    runtime.repo.attempt = replace(
        runtime.repo.attempt,status="expiring",provider_preference_id="pref-old",
        checkout_url="https://checkout.example/pref-old",
    )
    result = run_reconciliation_worker(runtime,now=NOW)
    assert result["failed"] == 0
    assert runtime.flow.provider.expired == [("pref-old",NOW)]
    assert runtime.repo.expired == [("attempt-1","lease-1",NOW)]


class OutboxRepository:
    def __init__(self):
        self.sent = []
        self.failed = []

    def claim_outbox(self, now, lease_until, limit):
        return [{"id":"message-1","business_id":"business-1","topic":"payment_approved",
                 "operation_key":"pay-1","payload":{"chargeId":"charge-1"},
                 "lease_token":"lease-1","attempt_count":1}]

    def notification_details(self, business_id, charge_id):
        return {"email":"customer@example.test"}

    def finish_outbox(self, *args):
        self.sent.append(args)

    def fail_outbox(self, *args):
        self.failed.append(args)


class OutboxRuntime:
    def __init__(self):
        self.repo = OutboxRepository()

    def repository(self):
        return self.repo


class Sender:
    def send(self, message, details):
        assert details["email"] == "customer@example.test"


def test_outbox_delivery_is_completed_after_external_send():
    runtime = OutboxRuntime()
    result = run_outbox_worker(runtime,Sender(),now=NOW)
    assert result == {"claimed":1,"sent":1,"failed":0}
    assert len(runtime.repo.sent) == 1


class RefundRepository:
    def __init__(self):
        self.finished = []
        self.events = []

    def claim_refund_operations(self,now,lease_until,limit):
        return [{"id":"refund-1","business_id":"business-1","payment_id":"payment-1",
                 "merchant_connection_id":"connection-1","operation_key":"refund-request-1",
                 "amount_minor":12500,"full_refund":True,"provider_refund_id":None,
                 "attempt_count":1,"lease_token":"lease-1"}]

    def refund_payment_reference(self,business_id,payment_id):
        return "provider-pay-1"

    def capture_provider_event(self,*args):
        self.events.append(args)
        return {"status":"accepted"}

    def finish_refund_operation(self,*args,**kwargs):
        self.finished.append((args,kwargs))

    def refund_reconciled(self,refund_id,lease_token,provider_refund_id):
        return (refund_id,lease_token,provider_refund_id) == (
            "refund-1","lease-1","provider-refund-1"
        )


class RefundProvider:
    def __init__(self):
        self.calls = []

    def request_refund(self,payment_id,key,amount):
        self.calls.append((payment_id,key,amount))
        return {"id":"provider-refund-1","status":"approved"}


class RefundRuntime:
    def __init__(self):
        self.repo = RefundRepository()
        self.flow = Flow()
        self.flow.provider = RefundProvider()

    def repository(self):
        return self.repo

    def payment_context(self,connection_id):
        return Context(self.flow,self.repo)


def test_refund_worker_reuses_operation_key_and_reconciles_before_completion():
    runtime = RefundRuntime()
    result = run_refund_worker(runtime,now=NOW)
    assert result == {"claimed":1,"completed":1,"retrying":0,"review":0}
    assert runtime.flow.provider.calls == [("provider-pay-1","refund-request-1",None)]
    assert runtime.flow.calls[0][1:] == ("provider-pay-1","connection-1")
    assert runtime.repo.finished[0][0][2] == "completed"


class HealthRepository:
    def __init__(self, health):
        self.health = health

    def operational_health(self, now):
        assert now == NOW
        return self.health


class HealthRuntime:
    def __init__(self, health):
        self.repo = HealthRepository(health)

    def repository(self):
        return self.repo


def test_operational_health_fails_closed_on_stalled_work():
    healthy = {"stalled_provider_events":0,"stalled_attempts":0,
               "stalled_outbox":0,"stalled_refunds":0,"open_reviews":0}
    assert run_operational_health(HealthRuntime(healthy),now=NOW)["healthy"] is True
    unhealthy = {**healthy,"open_reviews":1}
    with pytest.raises(RuntimeError,match="health check failed"):
        run_operational_health(HealthRuntime(unhealthy),now=NOW)
