from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(frozen=True)
class Charge:
    id: str
    business_id: str
    customer_id: str
    folio: str
    amount_minor: int
    outstanding_minor: int
    currency: str
    description: str
    due_date: date
    merchant_display_name: str | None = None
    public_branding: dict[str, str] | None = None
    cancelled: bool = False


@dataclass(frozen=True)
class MerchantConnection:
    id: str
    business_id: str
    provider: str
    provider_account_id: str
    environment: str
    verified_at: datetime | None


@dataclass(frozen=True)
class MerchantRuntimeConfig:
    connection: MerchantConnection
    credential_secret_ref: str
    webhook_secret_ref: str
    credential_source: str


@dataclass(frozen=True)
class PaymentAttempt:
    id: str
    business_id: str
    charge_id: str
    merchant_connection_id: str
    operation_key: str
    submission_key: str
    expected_amount_minor: int
    currency: str
    provider: str
    environment: str
    status: str
    provider_preference_id: str | None = None
    checkout_url: str | None = None


@dataclass(frozen=True)
class CheckoutRequest:
    external_reference: str
    operation_key: str
    description: str
    amount_minor: int
    currency: str
    provider_account_id: str
    environment: str


@dataclass(frozen=True)
class ProviderCheckout:
    preference_id: str
    checkout_url: str
    external_reference: str
    amount_minor: int
    currency: str
    provider_account_id: str
    environment: str


@dataclass(frozen=True)
class ProviderAdjustment:
    provider_adjustment_id: str
    kind: str
    amount_minor: int
    status: str
    occurred_at: datetime | None = None


@dataclass(frozen=True)
class ProviderPayment:
    provider_payment_id: str
    external_reference: str
    provider_account_id: str
    environment: str
    amount_minor: int
    currency: str
    status: str
    observed_at: datetime
    approved_at: datetime | None = None
    provider_updated_at: datetime | None = None
    provider_live_mode: bool | None = None
    adjustments: tuple[ProviderAdjustment, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class CheckoutResult:
    attempt_id: str
    status: str
    checkout_url: str | None


@dataclass(frozen=True)
class PaymentAssessment:
    allocate: bool
    review_reason: str | None = None
    allocation_minor: int = 0


class PaymentReviewIdentityConflict(Exception):
    """Authoritative retry evidence conflicts with the reviewed payment's identity."""
