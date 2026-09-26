from datetime import datetime, timezone
import hashlib
import re

from payments.models import (
    Charge,
    CheckoutRequest,
    CheckoutResult,
    MerchantConnection,
    PaymentAssessment,
    PaymentAttempt,
    PaymentReviewIdentityConflict,
    ProviderCheckout,
    ProviderPayment,
)


SUBMISSION_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


class InvalidChargeLink(Exception):
    pass


class ProviderOutcomeUnknown(Exception):
    """The provider may have accepted the request, but no response was received."""


def assess_payment(
    attempt: PaymentAttempt | None,
    connection: MerchantConnection | None,
    charge: Charge | None,
    payment: ProviderPayment,
) -> PaymentAssessment:
    """Decide whether authoritative provider facts may satisfy a charge.

    The caller must still persist the payment even when this returns a review
    reason. Allocation must occur under the same database transaction and row
    lock as payment insertion.
    """
    if attempt is None or connection is None or charge is None:
        return PaymentAssessment(False, "unknown_attempt")
    if attempt.business_id != charge.business_id or connection.business_id != charge.business_id:
        return PaymentAssessment(False, "tenant_mismatch")
    if payment.external_reference != attempt.id:
        return PaymentAssessment(False, "unknown_attempt")
    if payment.provider_account_id != connection.provider_account_id:
        return PaymentAssessment(False, "account_mismatch")
    if payment.environment != connection.environment:
        return PaymentAssessment(False, "environment_mismatch")
    if payment.currency != attempt.currency or payment.currency != charge.currency:
        return PaymentAssessment(False, "currency_mismatch")
    if payment.amount_minor != attempt.expected_amount_minor:
        return PaymentAssessment(False, "amount_mismatch")
    if payment.status != "approved":
        return PaymentAssessment(False)
    if charge.cancelled:
        # Every other fact about this observation checks out -- it is a real,
        # approved payment for the right attempt/account/amount -- but the
        # charge it would satisfy was cancelled. The caller must still
        # persist the payment and route it to review; it must not be
        # allocated to a cancelled charge's balance.
        return PaymentAssessment(False, "charge_cancelled")
    return PaymentAssessment(True)


class PaymentFlow:
    """Small domain slice for secure charge links and Mercado Pago checkout."""

    def __init__(self, repository, provider, clock=None):
        self.repository = repository
        self.provider = provider
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _digest_token(token: str) -> bytes:
        if not isinstance(token, str) or len(token) < 16:
            raise InvalidChargeLink()
        return hashlib.sha256(token.encode("utf-8")).digest()

    def _charge(self, token: str) -> Charge:
        charge = self.repository.find_charge_by_token_digest(
            self._digest_token(token), self.clock()
        )
        if charge is None:
            raise InvalidChargeLink()
        return charge

    def get_charge(self, token: str) -> dict:
        charge = self._charge(token)
        status = (
            self.repository.charge_status(charge)
            if hasattr(self.repository,"charge_status")
            else ("paid" if charge.outstanding_minor == 0 else "pending")
        )
        public_charge = {
            "folio": charge.folio,
            "description": charge.description,
            "dueDate": charge.due_date.isoformat(),
            "currency": charge.currency,
            "amountMinor": charge.amount_minor,
            "outstandingMinor": charge.outstanding_minor,
            "status": status,
        }
        if charge.merchant_display_name:
            public_charge["merchantDisplayName"] = charge.merchant_display_name
        if charge.public_branding:
            public_charge["branding"] = charge.public_branding
        return public_charge

    def start_checkout(self, token: str, submission_key: str) -> CheckoutResult:
        if not isinstance(submission_key, str) or not SUBMISSION_KEY_RE.fullmatch(submission_key):
            raise ValueError("submission key must be 8-128 safe characters")
        charge = self._charge(token)
        if charge.outstanding_minor <= 0:
            return CheckoutResult("", "paid", None)

        connection = self.repository.active_connection(charge.business_id, "mercado_pago")
        if (
            connection is None
            or connection.environment != "test"
            or connection.verified_at is None
        ):
            raise RuntimeError("a verified sandbox Mercado Pago connection is required")

        attempt, created = self.repository.get_or_create_attempt(
            charge, connection, submission_key, self.clock()
        )
        if not created:
            if attempt.status == "expiring":
                return CheckoutResult(attempt.id,"recovering",None)
            if not created and attempt.status == "ready":
                return CheckoutResult(attempt.id, "ready", attempt.checkout_url)
            if not created and attempt.status == "unknown":
                recovered = self.provider.recover_checkout(attempt.id)
                if recovered is not None:
                    request = CheckoutRequest(
                        external_reference=attempt.id,
                        operation_key=attempt.operation_key,
                        description=charge.description,
                        amount_minor=attempt.expected_amount_minor,
                        currency=attempt.currency,
                        provider_account_id=connection.provider_account_id,
                        environment=connection.environment,
                    )
                    self._validate_checkout(request, recovered)
                    ready = self.repository.mark_attempt_ready(
                        attempt.business_id, attempt.id, recovered, self.clock()
                    )
                    return CheckoutResult(ready.id, "ready", ready.checkout_url)
                return CheckoutResult(attempt.id, "recovering", None)
            if not created and attempt.status == "creating":
                return CheckoutResult(attempt.id, "recovering", None)

        request = CheckoutRequest(
            external_reference=attempt.id,
            operation_key=attempt.operation_key,
            description=charge.description,
            amount_minor=attempt.expected_amount_minor,
            currency=attempt.currency,
            provider_account_id=connection.provider_account_id,
            environment=connection.environment,
        )
        try:
            checkout = self.provider.create_checkout(request)
        except ProviderOutcomeUnknown:
            self.repository.mark_attempt_unknown(attempt.business_id, attempt.id, self.clock())
            return CheckoutResult(attempt.id, "recovering", None)

        self._validate_checkout(request, checkout)
        ready = self.repository.mark_attempt_ready(attempt.business_id, attempt.id, checkout, self.clock())
        return CheckoutResult(ready.id, "ready", ready.checkout_url)

    @staticmethod
    def _validate_checkout(request: CheckoutRequest, checkout: ProviderCheckout) -> None:
        expected = (
            request.external_reference,
            request.amount_minor,
            request.currency,
            request.provider_account_id,
            request.environment,
        )
        actual = (
            checkout.external_reference,
            checkout.amount_minor,
            checkout.currency,
            checkout.provider_account_id,
            checkout.environment,
        )
        if actual != expected or not checkout.preference_id or not checkout.checkout_url:
            raise RuntimeError("provider checkout response did not match the persisted attempt")

    def reconcile_payment(
        self,
        event_key: str,
        provider_payment_id: str,
        expected_connection_id: str | None = None,
    ) -> PaymentAssessment:
        """Fetch authoritative payment facts and record them durably.

        Webhook signature validation and durable event capture belong at the
        transport/repository boundary. A redirect must never call this method.
        """
        payment = self.provider.get_payment(provider_payment_id)
        context = self.repository.payment_context(payment.external_reference)
        if (
            context is not None
            and expected_connection_id is not None
            and context[1].id != expected_connection_id
        ):
            context = None
        if context is None:
            assessment = assess_payment(None, None, None, payment)
        else:
            assessment = assess_payment(*context, payment)
        return self.repository.record_payment_observation(
            context, payment, assessment, event_key, self.clock()
        )

    def reconcile_review_payment(self, event_key, provider_payment_id, review_id, business_id,
                                 expected_connection_id):
        """Recheck one reviewed payment using the provider's current facts."""
        payment = self.provider.get_payment(provider_payment_id)
        if payment.provider_payment_id != provider_payment_id:
            raise PaymentReviewIdentityConflict("provider returned a different payment")
        context = self.repository.payment_context(payment.external_reference)
        if context is not None and (
            context[1].id != expected_connection_id
            or context[0].business_id != business_id
            or context[1].business_id != business_id
            or context[2].business_id != business_id
        ):
            raise PaymentReviewIdentityConflict("provider payment context belongs to another connection or business")
        assessment = assess_payment(*context, payment) if context else assess_payment(None, None, None, payment)
        return self.repository.reassess_payment_review(
            business_id, review_id, expected_connection_id, context, payment,
            assessment, event_key, self.clock(),
        )
