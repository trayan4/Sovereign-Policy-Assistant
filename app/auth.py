"""Validates Keycloak-issued JWTs and resolves them to a clearance level -
replaces the Phase 1 placeholder where the caller just asserted its own
clearance in the request body. A client can no longer claim "cleared" for
itself; it can only present a token Keycloak actually signed for a user
Keycloak actually authenticated as holding that role."""

import os

import jwt
from fastapi import Header, HTTPException
from jwt import PyJWKClient

KEYCLOAK_URL = os.environ.get("KEYCLOAK_URL", "http://keycloak:8080")
KEYCLOAK_REALM = os.environ.get("KEYCLOAK_REALM", "sovereign-policy")
KEYCLOAK_CLIENT_ID = os.environ.get("KEYCLOAK_CLIENT_ID", "sovereign-policy-app")

ISSUER = f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}"
JWKS_URL = f"{ISSUER}/protocol/openid-connect/certs"

# PyJWKClient caches Keycloak's public keys itself and only re-fetches when
# it sees a key id (kid) it doesn't already have cached - so this costs a
# network round trip on an actual key rotation, not on every request.
_jwks_client = PyJWKClient(JWKS_URL)


def _validate(authorization: str | None) -> list[str]:
    """Shared by every auth dependency below: validates signature, issuer,
    expiry, and audience against Keycloak, then returns the roles list out
    of the verified payload - never out of anything the caller could have
    written itself. Raises 401 for anything wrong with the token itself;
    callers decide what to do with the roles (that's an authorization
    question, not an authentication one, and the two shouldn't be
    conflated in one function)."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()

    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=ISSUER,
            audience=KEYCLOAK_CLIENT_ID,
        )
    except Exception:
        # Deliberately generic, and deliberately catching more than just
        # jwt.PyJWTError: a truly malformed (non-JWT) string can fail
        # before jwt's own error types even apply - e.g. in the header
        # decoding get_signing_key_from_jwt does first - and letting that
        # raw exception text reach the caller (a base64/encoding error
        # message, library internals) is an information leak for no
        # benefit; every failure here means the same thing to the caller
        # regardless of which layer actually rejected it.
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    return payload.get("realm_access", {}).get("roles", [])


def get_clearance(authorization: str | None = Header(default=None)) -> str:
    """FastAPI dependency for /ask: any authenticated account gets at
    least "standard" access - this only decides whether confidential
    content is ALSO allowed, it doesn't gate access to asking questions
    at all. Returns "cleared" if the token's roles include cleared_staff,
    otherwise "standard"."""
    roles = _validate(authorization)
    return "cleared" if "cleared_staff" in roles else "standard"


def get_admin(authorization: str | None = Header(default=None)) -> str:
    """FastAPI dependency for the compliance dashboard: query stats and
    the governance-conflict review queue are a DIFFERENT kind of access
    than cleared_staff - being authorized to read confidential policy
    content when asking a question doesn't mean being authorized to view
    org-wide query volume or approve a conflict that changes what every
    future user gets told, and vice versa. A valid token that simply
    lacks the compliance_admin role is a 403 (authenticated, but not
    permitted), not a 401 (not authenticated at all) - the distinction
    matters for a client trying to tell "log in again" apart from
    "this account will never be allowed to do this."""
    roles = _validate(authorization)
    if "compliance_admin" not in roles:
        raise HTTPException(status_code=403, detail="Requires compliance_admin role")
    return "admin"
