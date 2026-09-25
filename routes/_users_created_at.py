"""routes/_users_created_at.py — read users.created_at, which is TEXT.

users.created_at is declared TEXT (main.py `CREATE TABLE IF NOT EXISTS users`)
and holds at least two formats plus NULL:

    '2026-08-08T21:07:43.420312'        bare ISO, written by utcnow() (UTC)
    '2026-07-26 07:42:40.586695+00'     Postgres timestamptz text, with offset
    NULL                                seats provisioned outside signup

So `u.created_at >= NOW() - …` in SQL raises
`operator does not exist: text >= timestamp with time zone`, and so does
`created_at >= %s` with a datetime param (psycopg2 sends a timestamptz literal).
activation_emails' candidate query did exactly that and failed on every sweep
(2026-09-25). A SQL cast is no better: one malformed row makes the whole
statement raise. Filter by date in Python with this parser instead.
"""
from __future__ import annotations

import datetime as _dt


def parse_users_created_at(value) -> _dt.datetime | None:
    """Aware UTC datetime, or None when absent or unparseable.

    A naive value is UTC (its writers used utcnow()). Never raises."""
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        d = value
    else:
        s = str(value).strip()
        if not s:
            return None
        s = s.replace("Z", "+00:00")
        # Postgres renders a whole-hour offset as '+00'; fromisoformat before
        # 3.11 wants '+00:00'. wants '+00:00'. len > 10 keeps a bare date's '-26' intact.
        if len(s) > 10 and s[-3] in "+-" and s[-2:].isdigit() and ":" not in s[-3:]:
            s += ":00"
        try:
            d = _dt.datetime.fromisoformat(s)
        except ValueError:
            return None
    if d.tzinfo is None:
        return d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_dt.timezone.utc)
