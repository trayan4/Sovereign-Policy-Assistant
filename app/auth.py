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


def get_clearance(authorization: str | None = Header(default=None)) -> str:
    """FastAPI dependency: validates the bearer token's signature, issuer,
    and expiry against Keycloak, then reads the role out of the verified
    payload - never out of anything the caller could have written itself.
    Returns "cleared" if the token's realm_access.roles includes
    cleared_staff, otherwise "standard". No valid token at all is a 401,
    not a silent "standard" default - a confidential-adjacent question
    should never be answerable by someone who isn't even logged in."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()

    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(token)
        # Signature, issuer, and expiry are all verified - the properties
        # that actually prove "Keycloak issued this, for this realm, and
        # it's still valid." Audience is deliberately not checked: the
        # client isn't configured with an audience mapper yet, so every
        # token carries Keycloak's default ("account") rather than this
        # app's client id. A real deployment should add that mapper and
        # verify audience too - noted here rather than silently skipped.
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=ISSUER,
            options={"verify_aud": False},
        )
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")

    roles = payload.get("realm_access", {}).get("roles", [])
    return "cleared" if "cleared_staff" in roles else "standard"
