"""routes/brain_intake_common.py — MECHANICAL plumbing shared by the brain intakes.

An "intake" turns some other board's verdicts into brain worklist items on the
`/api/v1/heal/findings` bus. `brain_audit_intake` was the first,
`brain_qa_superuser_intake` the second, and each needed the same four
mechanical parts: a brain_state snapshot read/write, an age helper, a TTL
rotation clock, and the rotate-a-window-over-a-capped-list function.

★ WHAT DOES **NOT** BELONG HERE: any rule about what counts as real work.
Every intake's eligibility gate, trust gate and severity ordering stays in its
own module, visible in the file you are reading when you ask "why did this
finding reach the brain?". Sharing the plumbing is maintenance; sharing the
judgement would hide the one thing each intake exists to decide, and would let
a change made for one board silently redefine another. This module knows about
dicts and clocks. It does not know what a finding is.

Currently used by `brain_site_qa_intake`. `brain_audit_intake` and
`brain_qa_superuser_intake` predate it and still carry their own copies;
migrating them is a mechanical follow-up, deliberately not bundled with a
change that adds a new lane.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone


def db_url() -> str | None:
    return (os.environ.get("NEON_DATABASE_URL")
            or os.environ.get("DATABASE_URL"))


def state_get(key: str):
    """Read one brain_state row. Returns None on any failure — callers treat
    that as "no snapshot", which is the safe reading."""
    url = db_url()
    if not url:
        return None
    try:
        import psycopg2
        with psycopg2.connect(url, connect_timeout=5) as conn, \
                conn.cursor() as cur:
            cur.execute(
                "SELECT state_value FROM brain_state WHERE state_key=%s",
                (key,))
            row = cur.fetchone()
            val = row[0] if row else None
            if isinstance(val, str):
                val = json.loads(val or "null")
            return val
    except Exception:
        return None


def state_set(key: str, value) -> bool:
    url = db_url()
    if not url:
        return False
    try:
        import psycopg2
        with psycopg2.connect(url, connect_timeout=5) as conn, \
                conn.cursor() as cur:
            cur.execute(
                """INSERT INTO brain_state (state_key, state_value, updated_at)
                   VALUES (%s, %s::jsonb, NOW())
                   ON CONFLICT (state_key)
                   DO UPDATE SET state_value = EXCLUDED.state_value,
                                 updated_at = NOW()""",
                (key, json.dumps(value)))
            conn.commit()
        return True
    except Exception:
        return False


def age_hours(value) -> float | None:
    """Hours since `value` (ISO string or datetime). None if unparseable.

    ★ Callers must decide what None MEANS for them. It is genuinely "cannot
    tell", and an intake that writes into a persistent deduped backlog should
    treat that as a refusal, not as fresh.
    """
    if value is None or value == "":
        return None
    try:
        if isinstance(value, datetime):
            t = value
        else:
            t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() / 3600.0
    except Exception:
        return None


def cycle_no(ttl_s: int, now_s: float | None = None) -> int:
    """One tick per TTL window — the rotation clock."""
    ttl_s = max(1, int(ttl_s))
    return int((time.time() if now_s is None else now_s) // ttl_s)


def rotate_window(ordered: list, limit: int, cycle: int) -> list:
    """The capped window over an ALREADY-ORDERED list, advancing by `limit`
    each cycle and wrapping.

    ★ WHY THIS EXISTS AT ALL: a fixed sort under a cap is head-of-list
    starvation — the tail never reaches the worklist, no matter how long it
    waits. That is the r78 class that flatlined Layer-5 proposals for twelve
    days and had to be retrofitted into brain_audit_intake after it shipped.
    Every eligible item now gets budget within ceil(n/limit) cycles, while the
    caller's ordering still decides priority WITHIN a window.
    """
    total = len(ordered)
    if limit <= 0 or total == 0:
        return []
    if total <= limit:
        return ordered[:limit]
    start = (int(cycle) * limit) % total
    return (ordered + ordered)[start:start + limit]
