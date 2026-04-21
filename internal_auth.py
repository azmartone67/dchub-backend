"""Central helper for validating X-Internal-Key header.

Pattern:
    from internal_auth import is_valid_internal_key
    if is_valid_internal_key(request.headers.get('X-Internal-Key', '')):
        ...  # trusted internal caller

Config:
    Set DCHUB_INTERNAL_KEY and/or DCHUB_SYNC_KEY env vars on Railway (already set).
    Optionally set INTERNAL_WORKER_SECRET for the hmac-style pattern from a15e42c.

Transition:
    LEGACY_OK=True (default): accepts old hardcoded strings too, logs a warning.
    Flip to False after all callers migrated to env-sourced values.
"""
import hmac
import logging
import os

log = logging.getLogger(__name__)

# Legacy hardcoded values — kept only for backward-compat during migration.
# Anyone who had repo access at any point has these. Rotate env values + flip
# LEGACY_OK=False to invalidate.
_LEGACY_KEYS = frozenset(("dchub-internal-2024", "dchub-internal-sync-2026"))

# Flip to "0" in env (or change default here) once all callers send env-sourced values.
LEGACY_OK = os.environ.get("INTERNAL_AUTH_LEGACY_OK", "1") == "1"


def is_valid_internal_key(header_value):
    """Constant-time check of an X-Internal-Key header.

    Returns True if the header matches any configured env-backed secret.
    During migration (LEGACY_OK=True), also accepts the two legacy hardcoded
    strings and logs a deprecation warning so we can audit remaining callers.
    """
    if not header_value:
        return False

    # Env-backed secrets (the correct path). Check all three known env vars.
    for env_var in ("DCHUB_INTERNAL_KEY", "DCHUB_SYNC_KEY", "INTERNAL_WORKER_SECRET"):
        expected = os.environ.get(env_var, "")
        if expected and hmac.compare_digest(str(header_value), expected):
            return True

    # Legacy fallback (logs on hit so we can track remaining hardcoded callers)
    if LEGACY_OK and header_value in _LEGACY_KEYS:
        # Enrich the warning with request context so Phase 2 triage can
        # identify exactly which caller still sends a hardcoded value.
        ctx = "startup/no-request-context"
        try:
            from flask import request, has_request_context
            if has_request_context():
                ua = request.headers.get("User-Agent", "?")[:80]
                ctx = f"{request.method} {request.path} ua={ua}"
        except Exception:
            pass
        log.warning("internal_auth: legacy hardcoded key accepted — migrate caller [%s]", ctx)
        return True

    return False


def get_internal_key_for_client():
    """Return the secret a client should send in its X-Internal-Key header.

    Prefers env var (rotatable); falls back to the legacy hardcoded string
    only during migration so existing clients keep working without changes.
    """
    for env_var in ("DCHUB_INTERNAL_KEY", "DCHUB_SYNC_KEY", "INTERNAL_WORKER_SECRET"):
        v = os.environ.get(env_var, "")
        if v:
            return v
    # Last-resort legacy (remove after all clients migrated)
    return "dchub-internal-sync-2026"
