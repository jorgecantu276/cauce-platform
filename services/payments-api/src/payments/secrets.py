"""Secret loading without logging or exposing secret values."""

import json


class AwsSecretResolver:
    def __init__(self, client=None):
        if client is None:
            import boto3
            client = boto3.client("secretsmanager")
        self.client = client
        self._cache = {}

    def object(self, secret_arn):
        if not str(secret_arn).startswith("arn:aws:secretsmanager:"):
            raise RuntimeError("configuration must reference an AWS Secrets Manager ARN")
        if secret_arn not in self._cache:
            response = self.client.get_secret_value(SecretId=secret_arn)
            raw = response.get("SecretString")
            if not raw:
                raise RuntimeError("secret must use SecretString JSON")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise RuntimeError("secret must contain a JSON object")
            self._cache[secret_arn] = value
        return self._cache[secret_arn]

    def field(self, secret_arn, field):
        value = self.object(secret_arn).get(field)
        if not isinstance(value, str) or not value:
            raise RuntimeError(f"secret is missing required field: {field}")
        return value
