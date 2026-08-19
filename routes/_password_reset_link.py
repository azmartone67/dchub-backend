"""_password_reset_link.py — single source of truth for the 72h
"set your password" link that every onboarding email must carry.

Why this exists
───────────────
The link was minted INLINE inside main.py's new-customer webhook branch and
nowhere else. Every OTHER path that emails a paying customer their key called
``send_welcome_email_sendgrid()`` without ``reset_url`` — notably
``flask_mcp_endpoints.stripe_webhook_mcp``, which is the path taken by anyone
who had a free account BEFORE they paid. Those buyers got a key and no way
into the dashboard.

Measured 2026-08-19: rob@hedmarkholdings.com signed up free on 08-17, bought
Developer on 08-19 at 14:26 UTC, and emailed support at 15:17 UTC asking how
to reset his password — because his welcome email had no link to do it. He had
never logged in (``users.last_login IS NULL``). The asymmetry is the bug: a
customer who buys COLD gets the "Set Your Password & Sign In" button, and a
customer who tries the product first does not. Same product, worse onboarding
for the warmer lead.

One function, two callers. To change the TTL or the URL shape, edit here.

★ This module returns None rather than a URL whose token did not land. See
``mint_reset_url`` — the inline version it replaces could not tell the
difference, because the executor it used never raises.
"""
from __future__ import annotations

import datetime
import logging
import secrets

logger = logging.getLogger("password_reset_link")

# r43-H (2026-05-27): 1h stranded a paying Pro customer (the Carl Braun
# lockout). 72h is the OWASP-recommended ceiling for reset tokens.
RESET_TTL_HOURS = 72

# NOTE the ``.html`` — dchub.cloud 308-redirects /reset-password.html to
# /reset-password and PRESERVES the query string (verified 2026-08-19:
# `curl -sL reset-password.html?token=probe123` → 200 at
# /reset-password?token=probe123). Both shapes work; this one is what every
# already-delivered email in the wild contains, so keep it stable.
RESET_URL_TEMPLATE = "https://dchub.cloud/reset-password.html?token={token}"

_INVALIDATE_SQL = ("UPDATE password_reset_tokens SET used = TRUE "
                   "WHERE user_email = %s AND used = FALSE")
# ON CONFLICT DO NOTHING is required by scripts/regression_lint.py
# (insert-no-on-conflict) and is also the correct semantic here: a collision
# means this token was NOT stored, and mint_reset_url's rowcount check then
# returns None rather than a URL the reset endpoint would reject. Kept as ONE
# string literal because the lint's match is bounded by the closing quote — an
# implicitly-concatenated fragment would put ON CONFLICT outside the match and
# trip the rule despite the SQL being correct.
_INSERT_SQL = """INSERT INTO password_reset_tokens (user_email, token, expires_at)
                 VALUES (%s, %s, %s)
                 ON CONFLICT DO NOTHING"""


def _rowcount(result):
    """_pg_execute returns (rows_affected, fetched_rows); tolerate a plain int
    or None from any other executor a caller injects."""
    if isinstance(result, tuple) and result:
        result = result[0]
    try:
        return int(result or 0)
    except (TypeError, ValueError):
        return 0


def mint_reset_url(email, execute=None):
    """Mint a fresh 72h set-password token for `email`; return its URL or None.

    ★ Returns None — never a URL — when the token did not actually land in
    ``password_reset_tokens``. A URL whose token was never persisted is a DEAD
    link: the customer clicks it, gets "Invalid or expired reset link", and now
    distrusts the next email too. That is strictly worse than a welcome email
    with no button at all, which at least sends them to /forgot-password.

    This matters because ``main._pg_execute`` SWALLOWS every exception and
    returns ``(0, [])`` on failure, so the surrounding ``try/except`` in the
    code this replaces could never fire. The INSERT's rowcount is the only
    evidence the write happened — check it, don't infer it from "no exception".

    `execute` is the (sql, params) -> (rowcount, rows) callable to run against
    Postgres; defaults to ``main._pg_execute``, imported lazily because main
    imports routes and a top-level import would be circular.
    """
    email = (email or "").strip().lower()
    if not email:
        return None

    runner = execute
    if runner is None:
        try:
            from main import _pg_execute as runner
        except Exception as exc:
            logger.warning("[reset-link] no executor available: %s", str(exc)[:120])
            return None

    # A reset token for an address with no users row is a link that cannot
    # work: routes.auth_routes.reset_password does `UPDATE users SET
    # password_hash WHERE email = %s`, which updates 0 rows and STILL returns
    # {'success': True}. The customer would set a password, be told it worked,
    # and then fail to log in. Refuse to mint rather than promise that.
    try:
        _, _rows = runner("SELECT 1 FROM users WHERE LOWER(email) = %s LIMIT 1",
                          (email,), fetch=True)
    except TypeError:
        # An injected executor without a `fetch` kwarg — skip the precondition
        # rather than fail the mint; the caller owns its own executor contract.
        _rows = [(1,)]
    except Exception as exc:
        logger.warning("[reset-link] account check failed for %s: %s", email, str(exc)[:120])
        return None
    if not _rows:
        logger.info("[reset-link] no users row for %s — not minting", email)
        return None

    token = secrets.token_urlsafe(32)
    expires_at = (datetime.datetime.utcnow()
                  + datetime.timedelta(hours=RESET_TTL_HOURS)).isoformat()
    try:
        # Invalidate any outstanding token first so a customer who asks twice
        # can only ever have one live link. Its rowcount is NOT checked — zero
        # is the normal case (nothing outstanding), not a failure.
        runner(_INVALIDATE_SQL, (email,))
        inserted = _rowcount(runner(_INSERT_SQL, (email, token, expires_at)))
    except Exception as exc:
        logger.warning("[reset-link] mint failed for %s: %s", email, str(exc)[:120])
        return None

    if inserted < 1:
        logger.warning("[reset-link] INSERT affected 0 rows for %s — returning "
                       "None rather than a dead link", email)
        return None

    logger.info("[reset-link] set-password link minted for %s (%sh)",
                email, RESET_TTL_HOURS)
    return RESET_URL_TEMPLATE.format(token=token)
