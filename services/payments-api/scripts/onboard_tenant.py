"""Validate and provision a sandbox pilot tenant from one JSON manifest.

Secrets never belong in this file: use scoped Secrets Manager references.  The
script intentionally creates the connection unverified; the provider-health
verification step must establish that state before checkout can start.
"""

import argparse
import json
import re
import sys
import uuid


def fail(message):
    raise ValueError(message)


def uuid_value(value, name):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{name} must be a UUID") from exc


def manifest_from(path):
    with open(path, encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, dict):
        fail("manifest must be a JSON object")
    business = value.get("business")
    connection = value.get("mercadoPago")
    memberships = value.get("memberships")
    if not isinstance(business, dict) or not isinstance(connection, dict) or not isinstance(memberships, list):
        fail("business, memberships, and mercadoPago are required")
    business_id = uuid_value(business.get("id"), "business.id")
    name = str(business.get("displayName") or "").strip()
    prefix = str(business.get("folioPrefix") or "").strip()
    if not (1 <= len(name) <= 160):
        fail("business.displayName must contain 1-160 characters")
    if not (1 <= len(prefix) <= 12):
        fail("business.folioPrefix must contain 1-12 characters")
    branding = business.get("branding") or {}
    if not isinstance(branding, dict):
        fail("business.branding must be an object")
    colors = {"accent": "#ed684c", "accentHover": "#d8563c", "nav": "#101931", "navAlt": "#172545", "canvas": "#f5f5f2"}
    for field, default in tuple(colors.items()):
        value = str(branding.get(field) or default)
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
            fail(f"business.branding.{field} must be a six-digit hex color")
        colors[field] = value
    public_name = str(branding.get("publicName") or name).strip()
    if not (1 <= len(public_name) <= 160):
        fail("business.branding.publicName must contain 1-160 characters")
    if connection.get("environment") != "test" or connection.get("credentialSource") != "test_credentials":
        fail("only verified sandbox onboarding is supported; live mode is intentionally unavailable")
    connection_id = uuid_value(connection.get("id"), "mercadoPago.id")
    required_refs = ("providerAccountId", "credentialSecretRef", "webhookSecretRef")
    for field in required_refs:
        if not str(connection.get(field) or "").strip():
            fail(f"mercadoPago.{field} is required")
    if not memberships:
        fail("at least one owner membership is required")
    normalized_memberships = []
    owners = 0
    for item in memberships:
        if not isinstance(item, dict):
            fail("memberships must contain objects")
        subject = str(item.get("subjectId") or "").strip()
        role = item.get("role")
        if not subject or len(subject) > 512 or role not in ("owner", "staff"):
            fail("each membership needs subjectId and role owner or staff")
        owners += role == "owner"
        normalized_memberships.append({"id": str(uuid.uuid4()), "subjectId": subject, "role": role})
    if not owners:
        fail("at least one owner membership is required")
    return {
        "business": {"id": business_id, "displayName": name, "folioPrefix": prefix,
                     "branding": {"publicName": public_name, **colors}},
        "memberships": normalized_memberships,
        "mercadoPago": {
            "id": connection_id, "providerAccountId": str(connection["providerAccountId"]),
            "credentialSecretRef": str(connection["credentialSecretRef"]),
            "webhookSecretRef": str(connection["webhookSecretRef"]),
        },
    }


def apply(manifest, table_name):
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../src"))
    from payments.dynamodb import DynamoRepository
    DynamoRepository(table_name).put_tenant_manifest(manifest)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Provision a sandbox payment tenant from a JSON manifest")
    parser.add_argument("manifest", help="path to tenant JSON manifest")
    parser.add_argument("--apply", action="store_true", help="write validated data to DynamoDB")
    parser.add_argument("--table-name", help="DynamoDB table name; required with --apply")
    args = parser.parse_args(argv)
    try:
        manifest = manifest_from(args.manifest)
        if args.apply:
            if not args.table_name:
                fail("--table-name is required with --apply")
            apply(manifest, args.table_name)
            outcome = "provisioned"
        else:
            outcome = "validated"
        print(json.dumps({"outcome": outcome, "businessId": manifest["business"]["id"],
                          "connectionState": "needs_provider_verification",
                          "memberships": len(manifest["memberships"])}, separators=(",", ":")))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"error": str(error)}, separators=(",", ":")), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
