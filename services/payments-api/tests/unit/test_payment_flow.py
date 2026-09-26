from dataclasses import replace
from datetime import date, datetime, timezone
import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from payments.models import (
    Charge,
    MerchantConnection,
    PaymentAttempt,
    ProviderCheckout,
    ProviderPayment,
)
from payments.service import (
    InvalidChargeLink,
    PaymentFlow,
    ProviderOutcomeUnknown,
    assess_payment,
)


NOW = datetime(2026, 9, 11, 18, 0, tzinfo=timezone.utc)


class FakeRepository:
    def __init__(self):
        self.charge = Charge(
            id="charge-1",
            business_id="business-1",
            customer_id="customer-1",
            folio="C-0001",
            amount_minor=12_500,
            outstanding_minor=12_500,
            currency="MXN",
            description="Anticipo de servicio",
            due_date=date(2026, 9, 20),
        )
        self.token_digest = hashlib.sha256(b"secure-charge-token").digest()
        self.connection = MerchantConnection(
            id="connection-1",
            business_id="business-1",
            provider="mercado_pago",
            provider_account_id="seller-test-1",
            environment="test",
            verified_at=NOW,
        )
        self.attempts = {}
        self.unknown = []
        self.observations = []

    def find_charge_by_token_digest(self, digest, now):
        return self.charge if digest == self.token_digest else None

    def active_connection(self, business_id, provider):
        assert business_id == self.charge.business_id
        assert provider == "mercado_pago"
        return self.connection

    def get_or_create_attempt(self, charge, connection, submission_key, now):
        if submission_key in self.attempts:
            return self.attempts[submission_key], False
        attempt = PaymentAttempt(
            id=f"attempt-{len(self.attempts) + 1}",
            business_id=charge.business_id,
            charge_id=charge.id,
            merchant_connection_id=connection.id,
            operation_key=f"operation-{len(self.attempts) + 1}",
            submission_key=submission_key,
            expected_amount_minor=charge.outstanding_minor,
            currency=charge.currency,
            provider=connection.provider,
            environment=connection.environment,
            status="creating",
        )
        self.attempts[submission_key] = attempt
        return attempt, True

    def mark_attempt_ready(self, business_id, attempt_id, checkout, now):
        assert business_id == self.charge.business_id
        for key, attempt in self.attempts.items():
            if attempt.id == attempt_id:
                updated = replace(
                    attempt,
                    status="ready",
                    provider_preference_id=checkout.preference_id,
                    checkout_url=checkout.checkout_url,
                )
                self.attempts[key] = updated
                return updated
        raise AssertionError("attempt not found")

    def mark_attempt_unknown(self, business_id, attempt_id, now):
        assert business_id == self.charge.business_id
        self.unknown.append(attempt_id)
        for key, attempt in self.attempts.items():
            if attempt.id == attempt_id:
                self.attempts[key] = replace(attempt, status="unknown")

    def payment_context(self, external_reference):
        attempt = next((x for x in self.attempts.values() if x.id == external_reference), None)
        return (attempt, self.connection, self.charge) if attempt else None

    def record_payment_observation(self, context, payment, assessment, event_key, now):
        self.observations.append((payment, assessment, event_key))
        if assessment.allocate:
            allocated = min(payment.amount_minor, self.charge.outstanding_minor)
            self.charge = replace(
                self.charge,
                outstanding_minor=self.charge.outstanding_minor - allocated,
            )
            if allocated < payment.amount_minor:
                return replace(assessment, review_reason="extra_payment", allocation_minor=allocated)
            return replace(assessment, allocation_minor=allocated)
        return assessment


class FakeMercadoPago:
    def __init__(self):
        self.create_calls = 0
        self.payment = None
        self.raise_unknown = False
        self.recover_calls = 0

    def create_checkout(self, request):
        self.create_calls += 1
        if self.raise_unknown:
            raise ProviderOutcomeUnknown("connection ended before a response")
        return ProviderCheckout(
            preference_id="pref-1",
            checkout_url="https://sandbox.mercadopago.test/pref-1",
            external_reference=request.external_reference,
            amount_minor=request.amount_minor,
            currency=request.currency,
            provider_account_id=request.provider_account_id,
            environment=request.environment,
        )

    def recover_checkout(self, external_reference):
        self.recover_calls += 1
        return None

    def get_payment(self, payment_id):
        assert self.payment and self.payment.provider_payment_id == payment_id
        return self.payment


def build_flow():
    repo = FakeRepository()
    provider = FakeMercadoPago()
    return PaymentFlow(repo, provider, clock=lambda: NOW), repo, provider


def test_secure_link_exposes_server_controlled_charge_not_buyer_pricing():
    flow, _, _ = build_flow()
    view = flow.get_charge("secure-charge-token")
    assert view == {
        "folio": "C-0001",
        "description": "Anticipo de servicio",
        "dueDate": "2026-09-20",
        "currency": "MXN",
        "amountMinor": 12_500,
        "outstandingMinor": 12_500,
        "status": "pending",
    }


def test_secure_link_exposes_only_the_optional_public_merchant_name():
    flow, repo, _ = build_flow()
    repo.charge = replace(repo.charge, merchant_display_name="Taller Norte")
    assert flow.get_charge("secure-charge-token")["merchantDisplayName"] == "Taller Norte"


def test_invalid_charge_link_reveals_nothing():
    flow, _, _ = build_flow()
    with pytest.raises(InvalidChargeLink):
        flow.get_charge("wrong-token")


def test_duplicate_submission_resumes_same_attempt_and_checkout():
    flow, _, provider = build_flow()
    first = flow.start_checkout("secure-charge-token", "browser-request-123")
    second = flow.start_checkout("secure-charge-token", "browser-request-123")
    assert first.attempt_id == second.attempt_id
    assert second.checkout_url == first.checkout_url
    assert provider.create_calls == 1


def test_uncertain_provider_response_is_not_retried_as_a_new_checkout():
    flow, repo, provider = build_flow()
    provider.raise_unknown = True
    result = flow.start_checkout("secure-charge-token", "browser-request-123")
    again = flow.start_checkout("secure-charge-token", "browser-request-123")
    assert result.status == again.status == "recovering"
    assert provider.create_calls == 1
    assert provider.recover_calls == 1
    assert repo.unknown == [result.attempt_id]


def approved_payment(attempt_id="attempt-1", payment_id="pay-1", **overrides):
    values = dict(
        provider_payment_id=payment_id,
        external_reference=attempt_id,
        provider_account_id="seller-test-1",
        environment="test",
        amount_minor=12_500,
        currency="MXN",
        status="approved",
        observed_at=NOW,
        approved_at=NOW,
        adjustments=(),
    )
    values.update(overrides)
    return ProviderPayment(**values)


def test_verified_approved_payment_allocates_and_updates_outstanding_balance():
    flow, repo, provider = build_flow()
    started = flow.start_checkout("secure-charge-token", "browser-request-123")
    provider.payment = approved_payment(started.attempt_id)
    result = flow.reconcile_payment("event-1", "pay-1")
    assert result.allocate is True
    assert result.allocation_minor == 12_500
    assert flow.get_charge("secure-charge-token")["status"] == "paid"
    assert repo.observations[0][2] == "event-1"


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"provider_account_id": "wrong-seller"}, "account_mismatch"),
        ({"environment": "live"}, "environment_mismatch"),
        ({"amount_minor": 100}, "amount_mismatch"),
        ({"currency": "USD"}, "currency_mismatch"),
        ({"external_reference": "other-attempt"}, "unknown_attempt"),
    ],
)
def test_invalid_provider_facts_record_payment_for_review_without_allocating(override, reason):
    flow, repo, provider = build_flow()
    started = flow.start_checkout("secure-charge-token", "browser-request-123")
    provider.payment = approved_payment(started.attempt_id, **override)
    result = flow.reconcile_payment("event-1", "pay-1")
    assert result.allocate is False
    assert result.review_reason == reason
    assert repo.charge.outstanding_minor == 12_500
    assert len(repo.observations) == 1


def test_second_real_payment_is_recorded_and_flagged_not_discarded():
    flow, repo, provider = build_flow()
    started = flow.start_checkout("secure-charge-token", "browser-request-123")
    provider.payment = approved_payment(started.attempt_id, "pay-1")
    flow.reconcile_payment("event-1", "pay-1")
    provider.payment = approved_payment(started.attempt_id, "pay-2")
    result = flow.reconcile_payment("event-2", "pay-2")
    assert result.review_reason == "extra_payment"
    assert result.allocation_minor == 0
    assert [p.provider_payment_id for p, _, _ in repo.observations] == ["pay-1", "pay-2"]


def test_assessment_never_treats_browser_redirect_as_provider_confirmation():
    flow, _, _ = build_flow()
    flow.start_checkout("secure-charge-token", "browser-request-123")
    assert flow.get_charge("secure-charge-token")["status"] == "pending"


def test_unverified_or_live_connection_cannot_start_sandbox_checkout():
    flow, repo, _ = build_flow()
    repo.connection = replace(repo.connection, verified_at=None)
    with pytest.raises(RuntimeError, match="verified sandbox"):
        flow.start_checkout("secure-charge-token", "browser-request-123")
    repo.connection = replace(repo.connection, verified_at=NOW, environment="live")
    with pytest.raises(RuntimeError, match="verified sandbox"):
        flow.start_checkout("secure-charge-token", "browser-request-124")


def test_assessment_is_pure_and_requires_approved_status():
    flow, repo, _ = build_flow()
    attempt, _ = repo.get_or_create_attempt(repo.charge, repo.connection, "request-1", NOW)
    result = assess_payment(
        attempt,
        repo.connection,
        repo.charge,
        approved_payment(attempt.id, status="pending"),
    )
    assert result.allocate is False
    assert result.review_reason is None


def test_webhook_connection_cannot_allocate_another_connections_attempt():
    flow, repo, provider = build_flow()
    started = flow.start_checkout("secure-charge-token", "browser-request-123")
    provider.payment = approved_payment(started.attempt_id)
    result = flow.reconcile_payment(
        "event-1", "pay-1", expected_connection_id="different-connection"
    )
    assert result.allocate is False
    assert result.review_reason == "unknown_attempt"
    assert repo.charge.outstanding_minor == 12_500
