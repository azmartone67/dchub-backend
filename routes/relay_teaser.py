"""One withheld number, shown free on the human relay page (r-relay-teaser, 2026-09-24).

A real Grok conversation (owner's account, 2026-09-24) showed the relay link
reaching the human; 1 of 132 Grok relays in 7d was acted on. The page asked for
$10 without showing anything the preview had held back. The owner chose to show
ONE withheld number free on the page.

★ STORED HERE, NOT IN THE TOKEN. The relay token is signed, not encrypted: it is
base64 in a URL the agent already holds, so a value inside it would hand every
agent one gated value per paywall response. The MCP server posts the value here
(internal key) against the token it minted; the page looks it up by that token.
The agent never sees it.

Keyed on the token's signature: unique per minted link, and only a token we
signed can be stored (parse_relay_token). First write wins. Never raises: a
missing table, a DB outage or a bad row means the page renders without the
number, exactly as before.
"""
from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

# A human label ("constraint score", "months to power") and a short value
# ("62", "$41.20/MWh", "18 months"). Rendered escaped anyway; these bound what
# a caller holding the internal key can make the page say.
_LABEL_OK = re.compile(r"[a-z0-9][a-z0-9 ,./()%$&+-]{0,59}")
_VALUE_OK = re.compile(r"[0-9A-Za-z $%.,/+~-]{1,24}")
_DDL_DONE = [False]

_DDL = ("CREATE TABLE IF NOT EXISTS relay_teasers ("
        " token_sig TEXT PRIMARY KEY,"
        " session_id TEXT,"
        " tool TEXT,"
        " label TEXT NOT NULL,"
        " value TEXT NOT NULL,"
        " created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")


def _dsn() -> str:
    return (os.environ.get("DATABASE_URL")
            or os.environ.get("NEON_DATABASE_URL") or "").strip()


def _sig(token: str) -> str:
    return (token or "").rsplit(".", 1)[-1]


def valid_teaser(label, value) -> bool:
    return (isinstance(label, str) and isinstance(value, str)
            and bool(_LABEL_OK.fullmatch(label.strip()))
            and bool(_VALUE_OK.fullmatch(value.strip())))


def store_teaser(token: str, label: str, value: str) -> dict:
    """Store one withheld number for a relay token we minted. Never raises."""
    from routes.human_relay import parse_relay_token
    info = parse_relay_token(token)
    if info is None:
        return {"ok": False, "error": "invalid_token"}
    if not valid_teaser(label, value):
        return {"ok": False, "error": "invalid_teaser"}
    dsn = _dsn()
    if not dsn:
        return {"ok": False, "error": "no_database"}
    try:
        import psycopg2
        conn = psycopg2.connect(dsn, connect_timeout=4)
        try:
            with conn.cursor() as cur:
                if not _DDL_DONE[0]:
                    cur.execute(_DDL)
                    _DDL_DONE[0] = True
                cur.execute(
                    """INSERT INTO relay_teasers (token_sig, session_id, tool, label, value)
                       VALUES (%s, %s, %s, %s, %s) ON CONFLICT (token_sig) DO NOTHING""",
                    (_sig(token), info.get("sid") or None, info.get("tool") or None,
                     label.strip(), value.strip()))
                stored = cur.rowcount == 1
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "stored": stored}
    except Exception as e:  # noqa: BLE001 - a teaser is never worth an error
        logger.warning("relay_teaser store failed: %s", str(e)[:160])
        return {"ok": False, "error": "store_failed"}


def get_teaser(token: str):
    """(label, value) stored for this token, or None. Never raises."""
    dsn = _dsn()
    if not dsn or not token:
        return None
    try:
        import psycopg2
        conn = psycopg2.connect(dsn, connect_timeout=4)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT label, value FROM relay_teasers WHERE token_sig = %s",
                            (_sig(token),))
                row = cur.fetchone()
        finally:
            conn.close()
        if row and valid_teaser(row[0], row[1]):
            return row[0], row[1]
        return None
    except Exception:  # noqa: BLE001 - absent table / outage: render without it
        return None
