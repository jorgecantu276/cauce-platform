"""Platform (superadmin) operations: authorization and orchestration only.

HTTP -> PlatformService -> payments.imports (pure validation) -> DynamoRepository.
platform_transport.py never talks to the repository directly, and never
trusts platformRole from anywhere but the caller-supplied, already-verified
value (see payments/platform_auth.py -- this module never re-derives it
from a raw Lambda event, so it stays independently testable without one).
"""

from datetime import timedelta

from payments import imports
from payments.service import SUBMISSION_KEY_RE


class PlatformForbidden(Exception):
    pass


class PlatformService:
    def __init__(self, repository, clock, *, retention_days=imports.DEFAULT_IMPORT_ROWS_RETENTION_DAYS,
                 apply_rows_per_invocation=imports.APPLY_ROWS_PER_INVOCATION):
        self.repository = repository
        self.clock = clock
        # Rows one apply invocation may process before it returns progress
        # and lets the caller continue (keeps every call inside the Lambda
        # timeout; see the delivery doc).
        self.apply_rows_per_invocation = apply_rows_per_invocation
        # How long the temporary row chunks (customer PII) live before
        # DynamoDB's TTL removes them. Validated here so a bad configuration
        # fails at construction, not on the first import.
        self.retention_days = imports.validate_retention_days(retention_days)

    def authorize(self, subject_id, platform_role):
        # Equality, never substring/prefix matching -- platform_auth.py
        # already enforces that when deriving platform_role from the JWT
        # claim; this only re-checks the final value defensively.
        if not subject_id or platform_role != "super_admin":
            raise PlatformForbidden()

    @staticmethod
    def _idempotency_key(value):
        if not isinstance(value, str) or not SUBMISSION_KEY_RE.fullmatch(value):
            raise ValueError("invalid idempotency key")
        return value

    def validate_import(self, business_id, subject_id, platform_role, idempotency_key, body):
        self.authorize(subject_id, platform_role)
        key = self._idempotency_key(idempotency_key)
        if not isinstance(body, dict):
            raise ValueError("request body must be an object")
        source = imports.validate_source(body.get("source"))
        profile = imports.validate_profile(body.get("profile"))
        records = body.get("records")
        validation = imports.validate_batch(records)
        payload_digest = imports.canonical_digest(source, profile, records)
        row_chunks = imports.chunk_rows(validation["rowResults"])
        now = self.clock()
        return self.repository.create_import_job(
            business_id, subject_id, key, payload_digest, validation["status"],
            source, profile, validation["summary"], validation["issues"], validation["issuesTruncated"],
            row_chunks, now, rows_expires_at=now + timedelta(days=self.retention_days),
        )

    def get_import(self, business_id, subject_id, platform_role, import_id):
        self.authorize(subject_id, platform_role)
        if not import_id:
            raise ValueError("importId is required")
        return self.repository.import_job_detail(business_id, import_id, self.clock())

    def list_imports(self, business_id, subject_id, platform_role, limit, cursor):
        self.authorize(subject_id, platform_role)
        return self.repository.list_import_jobs(business_id, limit, cursor)

    def purge_import(self, business_id, subject_id, platform_role, import_id, idempotency_key):
        """Delete one import's temporary data (rows, results, guards,
        metadata) and keep only minimal, PII-free audit evidence. Authorized
        first, so a non-platform caller learns nothing about whether the
        business or import exists."""
        self.authorize(subject_id, platform_role)
        self._idempotency_key(idempotency_key)
        if not import_id:
            raise ValueError("importId is required")
        return self.repository.purge_import_job(business_id, import_id, subject_id, idempotency_key, self.clock())

    def apply_import(self, business_id, subject_id, platform_role, import_id, idempotency_key):
        """Turn a `validated` import into Customers and Charges, one bounded
        slice per call. The ONLY path that does: validation never writes
        domain state. Superadmin only, authorized before any lookup."""
        self.authorize(subject_id, platform_role)
        key = self._idempotency_key(idempotency_key)
        if not import_id:
            raise ValueError("importId is required")
        return self.repository.apply_import_job(
            business_id, import_id, subject_id, key, self.clock, max_rows=self.apply_rows_per_invocation,
        )
