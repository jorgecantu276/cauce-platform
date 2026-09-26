import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from health.app import handler


def test_health_returns_200_ok():
    event = {"requestContext": {"http": {"method": "GET"}}}
    response = handler(event, None)
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"status": "ok"}
