"""Shared platform-admin authorization, derived only from claims API Gateway
already verified. Used by both staff_transport.py (to expose `platformRole`
on the session response) and platform_transport.py (to actually gate every
`/platform` route) so this sensitive comparison exists in exactly one place.
"""

import json
import os


def platform_role(event):
    """Derive platform access only from claims verified by API Gateway.

    Tenant membership and platform administration are deliberately separate.
    The configured group must match an entire group claim; substrings never
    grant access. Never reads platformRole/role from the request body, query
    string, or any header -- only from requestContext.authorizer.jwt.claims,
    which only API Gateway's JWT authorizer can set.
    """
    expected = os.environ.get("PLATFORM_ADMIN_GROUP", "").strip()
    if not expected:
        return None
    authorizer = (event.get("requestContext") or {}).get("authorizer") or {}
    claims = (authorizer.get("jwt") or {}).get("claims") or {}
    raw = claims.get("cognito:groups", claims.get("groups", []))
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
            groups = decoded if isinstance(decoded, list) else [raw]
        except json.JSONDecodeError:
            # HTTP API JWT authorizers stringify array claims using a
            # bracketed, comma-separated representation such as
            # ``[staff, cauce-super-admin]``. This is not JSON, so parse only
            # that narrow shape and retain exact, case-sensitive matching
            # below. A look-alike or substring can never grant access.
            value = raw.strip()
            if value.startswith("[") and value.endswith("]"):
                groups = [
                    item.strip().strip("'\"")
                    for item in value[1:-1].split(",")
                    if item.strip()
                ]
            else:
                groups = [raw]
    elif isinstance(raw, list):
        groups = raw
    else:
        groups = []
    return "super_admin" if expected in {str(value) for value in groups} else None
