import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from common.cors import allowed_origin_header


def test_echoes_origin_when_it_matches_a_registered_business_hostname():
    with patch("common.cors.db.get_business_by_hostname", return_value={"businessId": "biz-1"}):
        assert allowed_origin_header("https://pay.tacoselsol.mx") == {"Access-Control-Allow-Origin": "https://pay.tacoselsol.mx"}


def test_returns_empty_for_an_unregistered_origin():
    with patch("common.cors.db.get_business_by_hostname", return_value=None):
        assert allowed_origin_header("https://not-a-real-business.example") == {}


def test_returns_empty_for_a_missing_origin_header():
    assert allowed_origin_header(None) == {}
