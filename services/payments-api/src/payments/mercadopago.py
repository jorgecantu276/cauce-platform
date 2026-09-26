"""Narrow Mercado Pago Checkout Pro boundary for the first collections slice."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import urllib.error
import urllib.parse
import urllib.request

from payments.models import (
    CheckoutRequest,
    ProviderAdjustment,
    ProviderCheckout,
    ProviderPayment,
)
from payments.service import ProviderOutcomeUnknown


API_BASE = "https://api.mercadopago.com"
IDENTITY_URL = "https://api.mercadolibre.com/users/me"


def _minor_units(value) -> int:
    try:
        minor = Decimal(str(value)) * 100
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise RuntimeError("provider returned an invalid amount") from exc
    if minor != minor.to_integral_value():
        raise RuntimeError("provider returned more than two currency decimals")
    return int(minor)


def _provider_number(amount_minor):
    # Callers backed by boto3.resource's DynamoDB deserialization (e.g. a
    # claimed refund operation's amount_minor) hand back a Decimal, never a
    # native int, regardless of what type was originally written. A whole-
    # number Decimal is accepted and normalized to int; a fractional one is
    # rejected the same as any other invalid amount, since minor units are
    # never fractional.
    if isinstance(amount_minor, bool):
        raise ValueError("amount_minor must be a positive integer")
    if isinstance(amount_minor, Decimal):
        if amount_minor != amount_minor.to_integral_value():
            raise ValueError("amount_minor must be a whole number of minor units")
        amount_minor = int(amount_minor)
    if not isinstance(amount_minor, int) or amount_minor <= 0:
        raise ValueError("amount_minor must be a positive integer")
    value = Decimal(amount_minor) / Decimal(100)
    return int(value) if value == value.to_integral_value() else float(value)


def _parse_time(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


class MercadoPagoSandbox:
    """Checkout Pro client that refuses unverified or live configuration.

    Both test and production tokens may start with APP_USR, so token prefixes
    are deliberately ignored. The operator must identify the credential as
    coming from the dashboard's Test credentials section, and the token owner
    is verified against the configured test seller account before use.
    """

    def __init__(
        self,
        access_token: str,
        expected_account_id: str,
        *,
        credential_source: str,
        return_url: str | None = None,
        notification_url: str | None = None,
        timeout_seconds: int = 8,
        opener=None,
    ):
        if not access_token or not expected_account_id:
            raise ValueError("sandbox token and expected account ID are required")
        if credential_source != "test_credentials":
            raise ValueError("credential must be verified from Test credentials")
        self._access_token = access_token
        self.expected_account_id = str(expected_account_id)
        self.environment = "test"
        self.return_url = return_url
        self.notification_url = notification_url
        self.timeout_seconds = timeout_seconds
        self._opener = opener or urllib.request.urlopen
        self._identity_verified = False

    def _request(self, method, url, payload=None, extra_headers=None):
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Authorization": "Bearer " + self._access_token}
        headers.update(extra_headers or {})
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with self._opener(req, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if method == "POST" and (exc.code == 429 or exc.code >= 500):
                raise ProviderOutcomeUnknown(f"Mercado Pago HTTP {exc.code}") from exc
            raise RuntimeError(f"Mercado Pago HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if method == "POST":
                raise ProviderOutcomeUnknown("Mercado Pago response was uncertain") from exc
            raise RuntimeError("Mercado Pago is temporarily unavailable") from exc

    def request_refund(self, payment_id: str, operation_key: str, amount_minor=None):
        self._ensure_verified()
        if not payment_id or not operation_key:
            raise ValueError("payment ID and refund operation key are required")
        payload = None if amount_minor is None else {
            "amount":_provider_number(amount_minor)
        }
        raw = self._request(
            "POST",
            API_BASE + "/v1/payments/" + urllib.parse.quote(str(payment_id),safe="") + "/refunds",
            payload,
            {"X-Idempotency-Key":operation_key},
        )
        return {
            "id":str(raw.get("id") or ""),
            "status":str(raw.get("status") or "unknown"),
        }

    def expire_checkout(self, preference_id: str, now):
        self._ensure_verified()
        if not preference_id:
            raise ValueError("preference ID is required")
        expiration = now.isoformat(timespec="milliseconds")
        return self._request(
            "PUT",
            API_BASE + "/checkout/preferences/"
            + urllib.parse.quote(str(preference_id),safe=""),
            {"expires":True,"expiration_date_to":expiration},
        )

    def verify_connection(self):
        identity = self._request("GET", IDENTITY_URL)
        actual = str(identity.get("id", ""))
        if actual != self.expected_account_id:
            raise RuntimeError("Mercado Pago credential owner does not match configured seller")
        if "test_user" not in set(identity.get("tags") or []):
            raise RuntimeError("Mercado Pago credential owner is not a verified test user")
        self._identity_verified = True
        return actual

    def _ensure_verified(self):
        if not self._identity_verified:
            self.verify_connection()

    def create_checkout(self, request: CheckoutRequest) -> ProviderCheckout:
        self._ensure_verified()
        if request.environment != "test":
            raise RuntimeError("sandbox adapter cannot create live checkout")
        if request.provider_account_id != self.expected_account_id:
            raise RuntimeError("checkout seller does not match verified credential")
        payload = {
            "items": [{
                "id": request.external_reference,
                "title": request.description,
                "description": request.description,
                "quantity": 1,
                "unit_price": _provider_number(request.amount_minor),
                "currency_id": request.currency,
            }],
            "external_reference": request.external_reference,
            "metadata": {
                "payment_attempt_id": request.external_reference,
                "operation_key": request.operation_key,
            },
        }
        if self.return_url:
            payload["back_urls"] = {
                "success": self.return_url,
                "failure": self.return_url,
                "pending": self.return_url,
            }
            payload["auto_return"] = "approved"
        if self.notification_url:
            payload["notification_url"] = self.notification_url
        raw = self._request("POST", API_BASE + "/checkout/preferences", payload)
        return self._checkout_from_raw(raw, request.external_reference)

    def recover_checkout(self, external_reference: str):
        self._ensure_verified()
        query = urllib.parse.urlencode({"external_reference": external_reference})
        raw = self._request("GET", API_BASE + "/checkout/preferences/search?" + query)
        results = raw.get("elements") or raw.get("results") or []
        if not results:
            return None
        if len(results) != 1:
            raise RuntimeError("multiple provider preferences require operator review")
        return self._checkout_from_raw(results[0], external_reference)

    def search_payments(self, external_reference: str, *, page_size: int = 50):
        """Return every provider payment summary for one persisted attempt.

        Mercado Pago's search endpoint is paginated.  Reconciliation must walk
        every page because a second real payment is evidence, not a duplicate
        to discard.
        """
        self._ensure_verified()
        if not external_reference:
            raise ValueError("external_reference is required")
        offset = 0
        found = []
        while True:
            query = urllib.parse.urlencode({
                "external_reference": external_reference,
                "sort": "date_created",
                "criteria": "asc",
                "limit": page_size,
                "offset": offset,
            })
            raw = self._request("GET", API_BASE + "/v1/payments/search?" + query)
            page = raw.get("results") or []
            for item in page:
                if str(item.get("external_reference") or "") != external_reference:
                    continue
                payment_id = str(item.get("id") or "")
                if payment_id:
                    found.append({
                        "id": payment_id,
                        "updated_at": str(item.get("date_last_updated") or ""),
                    })
            paging = raw.get("paging") or {}
            total = int(paging.get("total") or len(page))
            offset += len(page)
            if not page or offset >= total:
                break
        return found

    def _checkout_from_raw(self, raw, expected_reference):
        items = raw.get("items") or []
        if len(items) != 1:
            raise RuntimeError("provider preference did not contain one charge item")
        item = items[0]
        quantity = int(item.get("quantity", 0))
        amount_minor = _minor_units(item.get("unit_price")) * quantity
        return ProviderCheckout(
            preference_id=str(raw.get("id") or ""),
            # The current Checkout Pro Preferences contract returns init_point
            # as the buyer redirect, including for application test credentials.
            # sandbox_init_point is retained by the provider but can enter an
            # authentication redirect loop with the current test-account flow.
            checkout_url=str(raw.get("init_point") or ""),
            external_reference=str(raw.get("external_reference") or expected_reference),
            amount_minor=amount_minor,
            currency=str(item.get("currency_id") or ""),
            provider_account_id=self.expected_account_id,
            environment="test",
        )

    def get_payment(self, payment_id: str) -> ProviderPayment:
        self._ensure_verified()
        raw = self._request(
            "GET", API_BASE + "/v1/payments/" + urllib.parse.quote(str(payment_id), safe="")
        )
        # This adapter only operates after verify_connection() established the
        # access token belongs to a Mercado Pago test user. Checkout Pro can
        # nevertheless report live_mode=true for a payment made with that test
        # seller and a test buyer (the documented test flow).  Therefore
        # live_mode is retained as raw provider evidence, not used to relabel
        # an otherwise verified sandbox transaction as production. The normal
        # reconciliation checks still bind it to the persisted test connection,
        # exact attempt reference, seller, amount, and currency.
        provider_live_mode = (
            bool(raw.get("live_mode")) if "live_mode" in raw else None
        )
        refunds = tuple(
            ProviderAdjustment(
                provider_adjustment_id=str(refund.get("id")),
                kind="refund",
                amount_minor=_minor_units(refund.get("amount")),
                status=str(refund.get("status") or "unknown"),
                occurred_at=_parse_time(refund.get("date_created")),
            )
            for refund in (raw.get("refunds") or [])
            if refund.get("id") is not None
        )
        adjustments = refunds
        if raw.get("status") == "charged_back":
            effective_refunds = sum(
                item.amount_minor for item in refunds
                if item.status in ("approved","confirmed","completed")
            )
            chargeback_minor = max(
                _minor_units(raw.get("transaction_amount")) - effective_refunds, 0
            )
            if chargeback_minor:
                adjustments = refunds + (
                    ProviderAdjustment(
                        provider_adjustment_id="chargeback:" + str(raw.get("id")),
                        kind="chargeback",
                        amount_minor=chargeback_minor,
                        status="confirmed",
                        occurred_at=_parse_time(raw.get("date_last_updated")),
                    ),
                )
        return ProviderPayment(
            provider_payment_id=str(raw.get("id") or payment_id),
            external_reference=str(raw.get("external_reference") or ""),
            provider_account_id=str(raw.get("collector_id") or ""),
            environment="test",
            amount_minor=_minor_units(raw.get("transaction_amount")),
            currency=str(raw.get("currency_id") or ""),
            status=str(raw.get("status") or "unknown"),
            observed_at=datetime.now(timezone.utc),
            approved_at=_parse_time(raw.get("date_approved")),
            provider_updated_at=_parse_time(raw.get("date_last_updated")),
            provider_live_mode=provider_live_mode,
            adjustments=adjustments,
        )
