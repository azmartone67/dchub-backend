"""ONE origin for the WorkOS AuthKit authorization-server URL.

WHY THIS EXISTS (2026-09-01). The AuthKit AS was written out as a literal in
EIGHT places across three repos plus one out-of-repo Cloudflare worker:

    dchub-backend   ai_agent_discovery.py (4 URLs), routes/agent_a2a.py (5),
                    worker.js, tests/test_agent_card_oauth2.py,
                    tests/test_mcp_oauth_workos.py
    dchub-frontend  _worker.js, agent-card.json (3 URLs)
    out-of-repo     the `dchub-oauth-meta` CF worker -- the PRM Claude.ai
                    actually reads

`routes/agent_a2a.py` alone carried BOTH shapes: a hardcoded block at :84-:100
and an env-with-default read at :210. One quantity, two origins, same file.

That matters because the AS is a CONSENSUS value, not a preference. The PRM
advertises it and the gateway validates the token issuer against it. Repoint
one and not the other and `jwtVerify`'s issuer check fails on every token --
OAuth does not degrade, it stops. That is the ERR_JWKS_NO_MATCHING_KEY class
already recorded against the previous domain change.

So the migration to a branded domain (auth.dchub.cloud) must be ONE edit, not
eight synchronised ones. This module is that edit.

WHAT THIS DOES NOT DO: it is a pure refactor. `_DEFAULT` is the value every one
of those sites already carried, so behaviour today is byte-identical whether or
not WORKOS_AUTHKIT_DOMAIN is set. The cutover is then: set the env var on both
Railway services, update the CF worker via the CF API, and re-run the guard.
"""

import os

# The CURRENT production authorization server. This literal is deliberate: an
# unset env var must not silently empty a public discovery document. Every
# other copy in this repo is replaced by a call into this module, and
# tests/test_authkit_one_origin.py fails if a new one appears.
#
# ON CUTOVER: prefer setting WORKOS_AUTHKIT_DOMAIN over editing this line, so
# the change is revertible without a deploy.
_DEFAULT = "https://beloved-stream-52.authkit.app"


def authkit_domain() -> str:
    """The AS origin, normalised. Env wins; `_DEFAULT` is the fallback.

    ``.strip()`` is not cosmetic. WORKOS_API_KEY on dchub-backend is currently
    stored WITH A TRAILING SPACE (79 chars against 78), so whitespace in a
    hand-pasted WorkOS value is a demonstrated failure mode here, not a
    hypothetical one. A trailing space would break the issuer equality check
    that `jwtVerify` performs, and the resulting error names the JWT, not the
    env var. ``routes/agent_a2a.py:210`` did ``.rstrip("/")`` without it.
    """
    raw = os.environ.get("WORKOS_AUTHKIT_DOMAIN") or _DEFAULT
    cleaned = raw.strip().rstrip("/")
    # An env var set to "" or "   " means "unset", not "no authorization
    # server" -- returning empty here would publish a discovery doc with a
    # blank issuer, which is worse than the default.
    return cleaned or _DEFAULT


def authkit_endpoints() -> dict:
    """The four AS URLs every discovery surface in this repo advertises.

    Returned under BOTH naming conventions in use: RFC 8414 snake_case
    (``authorization_endpoint``) and the camelCase the agent-card/marketplace
    surfaces use (``authorizationUrl``). Callers pick; neither can drift from
    the other, which is the point.
    """
    d = authkit_domain()
    return {
        "issuer": d,
        # RFC 8414 / RFC 9728 naming
        "authorization_endpoint": f"{d}/oauth2/authorize",
        "token_endpoint": f"{d}/oauth2/token",
        "registration_endpoint": f"{d}/oauth2/register",
        "authorization_server_metadata": f"{d}/.well-known/oauth-authorization-server",
        # agent-card / OpenAPI-style naming
        "authorizationUrl": f"{d}/oauth2/authorize",
        "tokenUrl": f"{d}/oauth2/token",
        "refreshUrl": f"{d}/oauth2/token",
        "registrationUrl": f"{d}/oauth2/register",
    }


# The scope set WorkOS actually issues. Custom `read:*` scopes were rejected as
# `invalid_scope` on 2026-06-21 and are not coming back; kept here so the
# discovery surfaces cannot disagree about them either.
AUTHKIT_SCOPES = ["openid", "profile", "email", "offline_access"]
