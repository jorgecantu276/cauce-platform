import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from payments.secrets import AwsSecretResolver


class Client:
    def __init__(self):
        self.calls = 0

    def get_secret_value(self, SecretId):
        self.calls += 1
        assert SecretId == "arn:aws:secretsmanager:us-east-1:123:secret:test"
        return {"SecretString": json.dumps({"access_token": "private-value"})}


def test_only_secret_manager_arns_are_accepted_and_values_are_cached():
    client = Client()
    resolver = AwsSecretResolver(client)
    arn = "arn:aws:secretsmanager:us-east-1:123:secret:test"
    assert resolver.field(arn, "access_token") == "private-value"
    assert resolver.field(arn, "access_token") == "private-value"
    assert client.calls == 1
    with pytest.raises(RuntimeError, match="Secrets Manager ARN"):
        resolver.field("private-value", "access_token")
