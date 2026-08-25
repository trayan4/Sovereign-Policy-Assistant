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
        # Signature, issuer, expiry, AND audience are all verified now.
        # Audience matters on its own, separately from signature: it's what
        # stops a token legitimately issued by this same Keycloak, for some
        # OTHER application in the same realm, from being replayed against
        # this one - a valid signature alone doesn't prove the token was
        # ever meant for this app. The client is configured with an
        # audience mapper (see keycloak/realm-export.json) that stamps
        # this app's client id into every token it issues, which is what
        # makes checking it here actually mean something.
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

    roles = payload.get("realm_access", {}).get("roles", [])
    return "cleared" if "cleared_staff" in roles else "standard"
