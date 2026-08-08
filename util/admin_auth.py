"""Strength gate for credentials that satisfy an admin auth check.

Why this exists (2026-08-07)
----------------------------
``routes/jobs_routes._require_admin_key`` built its accepted set as
``[DCHUB_ADMIN_KEY, ADMIN_SECRET]`` and accepted EITHER. On production
``ADMIN_SECRET`` was a guessable product-name-plus-year literal, so every
admin-gated ``/api/jobs/*`` endpoint — 30 of them, including ``backup``,
``db-backup``, ``autopilot`` and ``content-publish`` — was reachable by
anyone who guessed a product name plus the current year. Verified live:
``curl -H "X-API-Key: <the literal>" .../api/jobs/status`` returned 200.

The 2026-07-31 ``DCHUB_ADMIN_KEY`` rotation did not close it, because
``ADMIN_SECRET`` is a *different* env var that nobody thought of as an admin
credential — it is the Cloudflare Worker's secret, copied onto the backend
service and then quietly wired into the backend's accepted set.

The structural lesson is that the accepted set was assembled from whatever env
vars happened to be lying around, with no check on what was in them. So the
fix is not only "delete this one name": every credential that can satisfy an
admin gate now passes ``is_weak_credential`` first, and anything that fails is
dropped *and logged by env-var name*. A weak literal reintroduced into the
environment is therefore inert rather than exploitable.

Fail-closed by design: a dropped credential is not accepted, and if that
leaves the accepted set empty the gate 401s. Locking ourselves out of an admin
endpoint is strictly better than leaving it open to a dictionary guess.

Never log or return the credential VALUE — only the env var name and a reason.
"""

import hashlib
import logging
import os
import re

log = logging.getLogger(__name__)

# 24 is chosen to be comfortably above anything a human types by hand and
# comfortably below every real credential this repo issues (the shortest are
# the 32-hex ADMIN_API_KEY and the 49-char dchub_live_* keys).
MIN_ADMIN_CREDENTIAL_LEN = 24

# A credential containing one of these reads as a hand-typed password rather
# than a generated secret. Presence alone is not disqualifying — dchub_live_*
# keys legitimately contain "dchub" — so the check below only rejects when too
# little entropy remains once the dictionary word is discounted.
_WEAK_TOKENS = (
    "admin", "secret", "password", "passwd", "changeme", "letmein",
    "daily", "test", "demo", "default", "example", "dchub", "token",
)

# Minimum number of DISTINCT characters. Catches padded filler ("aaaa…") that
# clears the length bar without carrying any real entropy.
_MIN_DISTINCT_CHARS = 8

# sha256 of credentials known to be guessable or exposed. Stored as hashes so
# this fence cannot itself leak a working credential into the repo.
_COMPROMISED_SHA256 = frozenset((
    # ADMIN_SECRET on resourceful-essence/dchub-backend, live-exploitable
    # until 2026-08-07. Pinned so it stays rejected even if the length floor
    # is ever lowered.
    "e259f445efd0c1e77d7f682f1bf40c949a742a6d3261e5f097f71671b71ea4b3",
))


def is_weak_credential(value):
    """Return a short reason string if `value` is too weak to guard an admin
    endpoint, or None if it passes.

    Empty/missing returns a reason (fail closed) so callers can treat "no
    credential configured" and "credential rejected" identically.
    """
    v = (value or "").strip()
    if not v:
        return "empty or unset"

    if hashlib.sha256(v.encode("utf-8")).hexdigest() in _COMPROMISED_SHA256:
        return "known-compromised value"

    if len(v) < MIN_ADMIN_CREDENTIAL_LEN:
        return "shorter than %d chars" % MIN_ADMIN_CREDENTIAL_LEN

    if len(set(v)) < _MIN_DISTINCT_CHARS:
        return "fewer than %d distinct characters" % _MIN_DISTINCT_CHARS

    low = v.lower()
    alnum = len(re.sub(r"[^a-z0-9]", "", low))
    for tok in _WEAK_TOKENS:
        if tok in low and (alnum - len(tok)) < MIN_ADMIN_CREDENTIAL_LEN:
            return "dictionary-ish: contains %r with too little entropy around it" % tok

    return None


def accepted_admin_keys(env_names, logger=None):
    """Build the set of admin credentials from `env_names`, dropping weak ones.

    `env_names` is the REVIEWED list of env vars allowed to satisfy an admin
    gate — see tests/test_admin_credential_strength.py, which pins it so a new
    name cannot be added without review.

    Returns a set of accepted values. Rejected credentials are logged at ERROR
    by env-var name and reason, never by value.
    """
    out = logger or log
    keys = set()
    for name in env_names:
        raw = os.environ.get(name, "")
        if not (raw or "").strip():
            continue
        reason = is_weak_credential(raw)
        if reason:
            out.error(
                "ADMIN AUTH: refusing credential from %s (%s) — it is NOT "
                "accepted. Rotate it to a high-entropy value.", name, reason)
            continue
        keys.add(raw.strip())
    return keys
