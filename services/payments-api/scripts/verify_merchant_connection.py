"""Verify a provisioned Mercado Pago sandbox seller before enabling checkout."""

import argparse
from datetime import datetime, timezone
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../src"))

from payments.mercadopago import MercadoPagoSandbox
from payments.dynamodb import DynamoRepository
from payments.secrets import AwsSecretResolver


def verify(connection_id, table_name, resolver=None, provider_factory=MercadoPagoSandbox):
    repository = DynamoRepository(table_name)
    config = repository.connection_verification_config(connection_id)
    if config is None:
        raise ValueError("a sandbox test-credential connection is required")
    resolver = resolver or AwsSecretResolver()
    access_token = resolver.field(config.credential_secret_ref, "access_token")
    provider = provider_factory(access_token, config.connection.provider_account_id,
                                credential_source=config.credential_source)
    provider.verify_connection()
    repository.mark_connection_verified(connection_id, datetime.now(timezone.utc))
    return {"connectionId": connection_id, "state": "verified_test"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Verify a Mercado Pago sandbox seller connection")
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--table-name", required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(verify(args.connection_id,args.table_name),separators=(",", ":")))
        return 0
    except (ValueError, RuntimeError) as error:
        print(json.dumps({"error":str(error)},separators=(",", ":")),file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
