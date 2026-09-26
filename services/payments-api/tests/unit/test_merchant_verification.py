import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../scripts"))

from verify_merchant_connection import verify


class Connection:
    provider_account_id = "seller-1"


class Config:
    connection = Connection()
    credential_secret_ref = "secret://token"
    credential_source = "test_credentials"


class Repository:
    instance = None

    def __init__(self, dsn):
        self.dsn = dsn
        self.marked = None
        Repository.instance = self

    def connection_verification_config(self, connection_id):
        assert connection_id == "connection-1"
        return Config()

    def mark_connection_verified(self, connection_id, now):
        self.marked = (connection_id, now)


class Resolver:
    def field(self, reference, field):
        assert (reference, field) == ("secret://token", "access_token")
        return "sandbox-token"


class Provider:
    def __init__(self, token, seller_id, *, credential_source):
        assert (token, seller_id, credential_source) == ("sandbox-token", "seller-1", "test_credentials")

    def verify_connection(self):
        return "seller-1"


def test_verification_marks_only_a_provider_verified_sandbox_connection(monkeypatch):
    monkeypatch.setattr("verify_merchant_connection.DynamoRepository", Repository)
    result = verify("connection-1", "payments-test", resolver=Resolver(), provider_factory=Provider)
    assert result == {"connectionId":"connection-1","state":"verified_test"}
    assert Repository.instance.marked[0] == "connection-1"
