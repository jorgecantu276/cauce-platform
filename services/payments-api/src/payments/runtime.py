from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from urllib.parse import quote

from payments.mercadopago import MercadoPagoSandbox
from payments.dynamodb import DynamoRepository
from payments.imports import DEFAULT_IMPORT_ROWS_RETENTION_DAYS, validate_retention_days
from payments.platform import PlatformService
from payments.secrets import AwsSecretResolver
from payments.service import InvalidChargeLink, PaymentFlow
from payments.staff import StaffService


@dataclass(frozen=True)
class WebhookContext:
    connection_id: str
    repository: object
    flow: PaymentFlow
    webhook_secret: str


@dataclass(frozen=True)
class WebhookIngestContext:
    connection_id: str
    repository: object
    webhook_secret: str


class Runtime:
    def __init__(self, environ=None, secret_resolver=None, repository_factory=None,
                 provider_factory=None, clock=None, public_api_base_url=None):
        self.environ = os.environ if environ is None else environ
        self.secret_resolver = secret_resolver or AwsSecretResolver()
        self.repository_factory = repository_factory or DynamoRepository
        self.provider_factory = provider_factory or MercadoPagoSandbox
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.public_api_base_url = public_api_base_url

    def _required(self, name):
        value = self.environ.get(name)
        if not value:
            raise RuntimeError(f"missing runtime configuration: {name}")
        return value

    def _payment_public_base_url(self):
        """Origin for customer-facing links: staff share links and Mercado Pago's
        post-checkout redirect.

        Precedence: the trusted API Gateway request metadata (`request_base_url`,
        set by the Lambda handler from `requestContext`) always wins, so no URL
        needs to be known before the first deploy. `PAYMENT_PUBLIC_BASE_URL` is a
        local-dev-only override for harnesses that build a synthetic event with no
        `requestContext.domainName` (see `work/local_sandbox_server.py`).

        In a real Lambda invocation this resolves to the same origin as
        `_webhook_notification_base_url` today, because everything is served by
        one API Gateway HTTP API with no custom domain or CDN in front. If a
        custom domain/proxy is later added in front of only the customer-facing
        routes, this is the accessor that should start reading that domain from
        config instead. `_webhook_notification_base_url` must keep resolving to
        the raw API Gateway origin, since Mercado Pago calls it directly and
        would not go through that proxy. The env-var fallbacks stay separate
        (`PAYMENT_PUBLIC_BASE_URL` vs `PUBLIC_API_BASE_URL`) so a local harness
        can already exercise two different origins ahead of that change.
        """
        return (self.public_api_base_url or self._required("PAYMENT_PUBLIC_BASE_URL")).rstrip("/")

    def _webhook_notification_base_url(self):
        """Origin Mercado Pago calls directly to deliver webhooks. See the
        docstring on `_payment_public_base_url` for why this is a distinct
        accessor even though it resolves to the same value in production today."""
        return (self.public_api_base_url or self._required("PUBLIC_API_BASE_URL")).rstrip("/")

    def repository(self, webhook_connection_id=None):
        table_name = self._required("PAYMENTS_TABLE_NAME")
        return self.repository_factory(table_name, webhook_connection_id=webhook_connection_id)

    def read_flow(self):
        return PaymentFlow(self.repository(), provider=None, clock=self.clock)

    def staff_service(self):
        secret_arn = self._required("APPLICATION_SECRET_ARN")
        token_secret = self.secret_resolver.field(secret_arn,"link_token_hmac")
        return StaffService(
            self.repository(), token_secret, self._payment_public_base_url(), self.clock
        )

    def _import_rows_retention_days(self):
        raw = self.environ.get("IMPORT_ROWS_RETENTION_DAYS")
        if raw in (None, ""):
            return DEFAULT_IMPORT_ROWS_RETENTION_DAYS
        try:
            return validate_retention_days(int(str(raw).strip()))
        except ValueError as error:
            # Misconfiguration is an operator error, not a bad request.
            raise RuntimeError("invalid IMPORT_ROWS_RETENTION_DAYS configuration") from error

    def platform_service(self):
        return PlatformService(self.repository(), self.clock, retention_days=self._import_rows_retention_days())

    def checkout_flow(self, token):
        repo = self.repository()
        if not isinstance(token, str) or len(token) < 16:
            raise InvalidChargeLink()
        digest = hashlib.sha256(token.encode()).digest()
        charge = repo.find_charge_by_token_digest(digest, self.clock())
        if charge is None:
            raise InvalidChargeLink()
        connection = repo.active_connection(charge.business_id, "mercado_pago")
        if connection is None:
            raise RuntimeError("no verified Mercado Pago connection")
        config = repo.connection_runtime_config(connection.id)
        if config is None:
            raise RuntimeError("Mercado Pago connection is unavailable")
        access_token = self.secret_resolver.field(config.credential_secret_ref, "access_token")
        return_url = (
            self._payment_public_base_url()
            + "/pay/" + quote(token, safe="")
        )
        notification_url = (
            self._webhook_notification_base_url()
            + "/webhooks/mercado-pago/" + quote(connection.id, safe="")
        )
        provider = self.provider_factory(
            access_token,
            connection.provider_account_id,
            credential_source=config.credential_source,
            return_url=return_url,
            notification_url=notification_url,
        )
        return PaymentFlow(repo, provider, clock=self.clock)

    def webhook_ingest_context(self, connection_id):
        repo = self.repository(webhook_connection_id=connection_id)
        config = repo.connection_runtime_config(connection_id)
        if config is None or config.connection.environment != "test":
            raise RuntimeError("verified sandbox connection not found")
        webhook_secret = self.secret_resolver.field(config.webhook_secret_ref, "webhook_secret")
        return WebhookIngestContext(connection_id, repo, webhook_secret)

    def payment_context(self, connection_id):
        repo = self.repository(webhook_connection_id=connection_id)
        config = repo.connection_runtime_config(connection_id)
        if config is None or config.connection.environment != "test":
            raise RuntimeError("verified sandbox connection not found")
        access_token = self.secret_resolver.field(config.credential_secret_ref, "access_token")
        webhook_secret = self.secret_resolver.field(config.webhook_secret_ref, "webhook_secret")
        provider = self.provider_factory(
            access_token,
            config.connection.provider_account_id,
            credential_source=config.credential_source,
        )
        return WebhookContext(
            connection_id=connection_id,
            repository=repo,
            flow=PaymentFlow(repo, provider, clock=self.clock),
            webhook_secret=webhook_secret,
        )

    def webhook_context(self, connection_id):
        """Compatibility alias for callers that need full processing context."""
        return self.payment_context(connection_id)
