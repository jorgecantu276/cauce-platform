import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from payments.platform_auth import platform_role


def _event(groups=None, group_header=None):
    claims = {"sub": "subject-1"}
    if groups is not None:
        claims["cognito:groups"] = groups
    return {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}, "headers": ({"x-platform-role": group_header} if group_header else {})}


def test_no_group_claim_yields_no_platform_role(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_GROUP", "cauce-super-admin")
    assert platform_role(_event()) is None


def test_similar_but_not_exact_group_yields_no_platform_role(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_GROUP", "cauce-super-admin")
    assert platform_role(_event(groups=["cauce-super-admin-helper"])) is None
    assert platform_role(_event(groups=["cauce-super-adminx"])) is None
    assert platform_role(_event(groups=["CAUCE-SUPER-ADMIN"])) is None  # exact, case-sensitive match only


def test_exact_group_match_yields_super_admin(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_GROUP", "cauce-super-admin")
    assert platform_role(_event(groups=["staff", "cauce-super-admin"])) == "super_admin"


def test_group_claim_as_json_encoded_string_is_parsed(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_GROUP", "cauce-super-admin")
    assert platform_role(_event(groups='["cauce-super-admin"]')) == "super_admin"


def test_gateway_bracketed_group_claim_is_parsed_with_exact_matching(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_GROUP", "cauce-super-admin")
    assert platform_role(_event(groups="[staff, cauce-super-admin]")) == "super_admin"
    assert platform_role(_event(groups="[cauce-super-admin-helper]")) is None
    assert platform_role(_event(groups="[staff, cauce-super-adminx]")) is None


def test_unconfigured_admin_group_never_grants_access(monkeypatch):
    monkeypatch.delenv("PLATFORM_ADMIN_GROUP", raising=False)
    assert platform_role(_event(groups=["cauce-super-admin"])) is None


def test_a_role_header_can_never_substitute_for_a_verified_group_claim(monkeypatch):
    # The header is not read anywhere in platform_role -- this proves it,
    # rather than just asserting the function ignores a parameter it never
    # accepted in the first place.
    monkeypatch.setenv("PLATFORM_ADMIN_GROUP", "cauce-super-admin")
    event = _event(group_header="super_admin")
    assert platform_role(event) is None
