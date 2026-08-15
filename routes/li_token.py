"""routes/li_token.py — ONE source for the LinkedIn access token (2026-08-15).

★ WHY THIS MODULE EXISTS. THE SAME BUG HAS NOW LANDED FOUR TIMES.

There are two LinkedIn tokens in this system and they drift:

    LINKEDIN_ACCESS_TOKEN   a Railway env var. Set by hand, months ago. Goes
                            stale or gets revoked silently. Nothing refreshes it.
    the Neon DB row         maintained by the proactive refresh cron
                            (routes/linkedin_token_reset.py) with a
                            refresh_token. Self-sustaining.

Every path that reads the ENV var directly breaks the moment those diverge —
and it breaks SILENTLY, because the paths that read DB-first keep working, so
the system looks alive while individual features die one at a time:

  * 2026-07-31  the auto-publish drain 401'd (EXPIRED_ACCESS_TOKEN, post
                105426) while the DB token sat 13 days from expiry with a
                refresh_token in place. Fixed for content_publisher's four
                publish paths by _li_access_token() — and only those four.
  * 2026-08-15  the LinkedIn IMAGE UPLOAD read the env var while the POST used
                _get_valid_token(). Env token: 401 REVOKED_ACCESS_TOKEN. DB
                token: valid another 50 days. Result — every post published
                and every card was silently dropped (image_attached FALSE on
                all 30 rows of /api/v1/linkedin-quad/status). Fixed in #2718.
  * 2026-08-15  the same revoked env var was ALSO being read directly by the
                comment publisher, the thread publisher, the DM sender, the
                spike responder and marketing_engine's publish_now/repost_now
                — seven more live paths, all 401ing against a healthy DB token.
                That is this module.

The 2026-07-31 fix was correct but scoped to one file, and its guard pinned a
HAND-LISTED set of four functions. Anything added afterwards re-grew the env
read, because nothing said it couldn't. So the durable form is one accessor
plus a guard over EVERY LinkedIn caller — see
tests/test_linkedin_token_single_source.py.

★ FAIL-OPEN, ALWAYS. Every layer falls back rather than returning empty: a
problem reading the DB token must never make publishing darker than the
env-only behaviour it replaced.

★ Legitimate env readers still exist and are ALLOWLISTED in the guard — the
diagnostics whose whole JOB is to report on the env var itself
(linkedin_whoami, integrations_health, linkedin_token_reset,
publisher_status, marketing_engine.linkedin_token_test). Those must keep
reading it directly or they would stop being able to see the drift.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def li_access_token() -> str:
    """The token every LinkedIn API call in routes/ should use.

    Order: DB (self-refreshing) → LINKEDIN_ACCESS_TOKEN env var → ''.

    Delegates to content_publisher._li_access_token(), which is itself
    DB-first via linkedin_poster._get_valid_token(). Imported lazily because
    content_publisher is heavy and imports parts of routes/ back.
    """
    try:
        from content_publisher import _li_access_token
        tok = (_li_access_token() or "").strip()
        if tok:
            return tok
    except Exception as e:  # noqa: BLE001
        logger.warning("[li_token] DB-first lookup failed (%s) — env fallback", e)
    return (os.environ.get("LINKEDIN_ACCESS_TOKEN") or "").strip()


def token_source_drift() -> dict:
    """Diagnostic: are the two sources the same token?

    Reports SHAPE only — lengths and a 6-char prefix — never the secrets.
    `drift=True` means the env var is stale relative to the DB and any path
    still reading it directly is running on a different (probably dead)
    credential. Purely informational; callers decide what to do."""
    env = (os.environ.get("LINKEDIN_ACCESS_TOKEN") or "").strip()
    db = ""
    try:
        from linkedin_poster import _get_valid_token
        db = (_get_valid_token() or "").strip()
    except Exception:  # noqa: BLE001
        pass
    return {
        "env_set": bool(env),
        "db_set": bool(db),
        "env_len": len(env),
        "db_len": len(db),
        "env_prefix": env[:6],
        "db_prefix": db[:6],
        "drift": bool(env and db and env != db),
        "effective_source": "db" if db else ("env" if env else "none"),
    }
