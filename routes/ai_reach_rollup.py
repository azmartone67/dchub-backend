"""DC Hub — REACH weekly rollup (2026-06-20).

REACH = distinct EXTERNAL IPs/week = the real number of agent sources, which is
the binding growth constraint (vs loop-inflated request volume). The live
/api/v1/ai/reach (routes/ai_reach.py) scans agent_requests on every cold cache
and is fragile: agent_requests.timestamp is TEXT with no index, so a per-row
cast over millions of rows times out — prior-week external-only counts are
uncomputable live. This module precomputes a durable weekly rollup so reading
reach is O(rows-in-table) and cold-start safe, and it tracks NEW external IPs/wk
(IPs never seen in any prior week) — the acquisition signal we actually want to
grow.

Key tricks (verified against prod):
  * id is monotonic with insert time → a ~23-probe PK binary search resolves any
    week→id boundary in <1s WITHOUT casting the TEXT timestamp.
  * the all-time distinct external-IP universe is tiny (~73), so new_external_ips
    is cheap via a cumulative reach_ip_seen(first_seen_week) table.
  * tables are created with a RAW psycopg2 autocommit conn — db_utils silently
    SKIPS DDL at SKIP_DDL=1 (the default), so CREATE TABLE must bypass it.

Reuses _conn / _PRIVATE_IP / _INTERNAL_PLAT from routes.ai_reach so the rollup
and the live endpoint can never drift.

Routes (registered via ai_reach_rollup_bp in main.py, next to ai_reach):
  POST /api/cron/reach-rollup   -> fire-and-forget recompute (202); never holds a worker
  GET  /api/v1/ai/reach/trend   -> precomputed weeks (cold-start safe, fail-soft 200)
"""
from __future__ import annotations
import re, threading
from datetime import datetime, date, timedelta
from flask import Blueprint, jsonify
import psycopg2, psycopg2.extras

from routes.ai_reach import _conn, _PRIVATE_IP, _INTERNAL_PLAT

ai_reach_rollup_bp = Blueprint("ai_reach_rollup", __name__)

CAP_WEEKS = 16              # how many trailing weeks the /trend endpoint serves
BACKFILL_WEEKS = 12         # trailing weeks recomputed every run (idempotent; self-heals gaps)
_SCAN_CHUNK = 200_000       # id-range chunk per scan statement (dense weeks are ~1M+ wide rows;
                            # a single GROUP BY over a whole week exceeds the statement timeout)
# Filter external/internal in PYTHON on the small grouped output, NOT with a SQL
# regex per row — the SQL `ip !~ regex` over 1M+ dense recent-week rows blew the
# statement timeout and killed the rollup mid-backfill. Same _PRIVATE_IP pattern
# + _INTERNAL_PLAT set → identical result to the live /ai/reach, no drift.
_PRIV_RE = re.compile(_PRIVATE_IP)
_INT_PLAT = set(_INTERNAL_PLAT)
_running = threading.Lock()         # prevents two overlapping recomputes on one replica


def _ensure_tables():
    """Create rollup tables via a RAW autocommit conn (db_utils skips DDL)."""
    c = _conn()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reach_weekly (
                    week_start            DATE PRIMARY KEY,
                    distinct_external_ips INTEGER NOT NULL DEFAULT 0,
                    distinct_platforms    INTEGER NOT NULL DEFAULT 0,
                    new_external_ips      INTEGER NOT NULL DEFAULT 0,
                    requests              BIGINT  NOT NULL DEFAULT 0,
                    id_lo                 BIGINT,
                    id_hi                 BIGINT,
                    computed_at           TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reach_ip_seen (
                    ip_address      TEXT PRIMARY KEY,
                    first_seen_week DATE NOT NULL,
                    first_seen_at   TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        return True
    except Exception:
        return False
    finally:
        try: c.close()
        except Exception: pass


def _monday(d: date) -> date:
    """Monday of d's ISO week (matches Postgres date_trunc('week'))."""
    return d - timedelta(days=d.weekday())


def _parse_ts(s) -> datetime | None:
    """Tolerant parse of the TEXT timestamp to a NAIVE datetime (second precision
    is enough — week boundaries are day-aligned). agent_requests stamps are naive
    UTC-ish; we normalize 'T'->' ' and ','->'.' and take the first 19 chars."""
    if not s:
        return None
    t = str(s).strip().replace("T", " ").replace(",", ".")
    try:
        return datetime.fromisoformat(t[:19])
    except Exception:
        return None


def _id_for_instant(cur, target_dt: datetime, lo: int, hi: int) -> int | None:
    """Smallest id whose timestamp >= target_dt, via PK binary search (no cast).
    Returns None if no row at/after target_dt. ~log2(rows) probes."""
    ans = None
    while lo <= hi:
        mid = (lo + hi) // 2
        cur.execute(
            "SELECT id, timestamp FROM agent_requests WHERE id >= %s ORDER BY id LIMIT 1",
            (mid,),
        )
        r = cur.fetchone()
        if not r:                       # nothing at/after mid
            hi = mid - 1
            continue
        rid, ts = r[0], _parse_ts(r[1])
        if ts is None:                  # unparseable — step past it
            lo = rid + 1
            continue
        if ts >= target_dt:
            ans = rid
            hi = rid - 1                # look for an even earlier qualifying row
        else:
            lo = rid + 1
    return ans


def _compute_week(cur, week_start: date, id_lo: int, id_hi: int) -> dict:
    """One id-bounded GROUP BY scan → counts + the week's external IP set; then
    fold the IP set into reach_ip_seen and derive new_external_ips from
    first_seen_week (idempotent on re-run)."""
    # Scan the week's id-range in bounded CHUNKS, merging in Python. A single
    # GROUP BY over a dense week (~1M+ wide rows, heap fetches for ip/platform)
    # blew the statement timeout; chunking keeps every statement small while the
    # distinct-IP/platform SETS accumulate across chunks. Filter private IPs +
    # internal platforms in Python (same _PRIVATE_IP / _INTERNAL_PLAT as the live
    # /ai/reach — no drift).
    ips, plats, reqs = set(), set(), 0
    cl = id_lo
    while cl <= id_hi:
        ch = min(cl + _SCAN_CHUNK - 1, id_hi)
        cur.execute("""
            SELECT ip_address, platform_id, COUNT(*) AS reqs
            FROM agent_requests
            WHERE id BETWEEN %s AND %s
              AND ip_address IS NOT NULL AND ip_address <> ''
            GROUP BY ip_address, platform_id
        """, (cl, ch))
        for ip, plat, n in cur.fetchall():
            if _PRIV_RE.match(ip or ''):              # private/loopback = internal
                continue
            if (plat or '') in _INT_PLAT:             # internal platform buckets
                continue
            ips.add(ip)
            if plat:
                plats.add(plat)
            reqs += int(n or 0)
        cl = ch + 1

    # Fold this week's IPs into the cumulative seen-set. Only IPs not seen in an
    # EARLIER week get first_seen_week = this week (weeks are processed ascending),
    # so new_external_ips is honest and re-run-safe.
    if ips:
        psycopg2.extras.execute_values(
            cur,
            "INSERT INTO reach_ip_seen (ip_address, first_seen_week) VALUES %s "
            "ON CONFLICT (ip_address) DO NOTHING",
            [(ip, week_start) for ip in ips],
        )
    cur.execute("SELECT COUNT(*) FROM reach_ip_seen WHERE first_seen_week = %s", (week_start,))
    new_ips = int((cur.fetchone() or [0])[0])

    cur.execute("""
        INSERT INTO reach_weekly
            (week_start, distinct_external_ips, distinct_platforms,
             new_external_ips, requests, id_lo, id_hi, computed_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (week_start) DO UPDATE SET
            distinct_external_ips = EXCLUDED.distinct_external_ips,
            distinct_platforms    = EXCLUDED.distinct_platforms,
            new_external_ips      = EXCLUDED.new_external_ips,
            requests              = EXCLUDED.requests,
            id_lo                 = EXCLUDED.id_lo,
            id_hi                 = EXCLUDED.id_hi,
            computed_at           = NOW()
    """, (week_start, len(ips), len(plats), new_ips, reqs, id_lo, id_hi))

    return {"week_start": week_start.isoformat(), "distinct_external_ips": len(ips),
            "distinct_platforms": len(plats), "new_external_ips": new_ips, "requests": reqs}


def run_reach_rollup() -> dict:
    """Recompute the weekly reach rollup over the trailing BACKFILL_WEEKS window,
    processed ascending (so reach_ip_seen builds chronologically and
    new_external_ips is honest). UPSERT-idempotent and self-healing — re-running
    re-derives every window week. Safe to call from the daily cron thread or the
    /api/cron/reach-rollup handler."""
    if not _running.acquire(blocking=False):
        return {"ok": False, "skipped": "already_running"}
    try:
        if not _ensure_tables():
            return {"ok": False, "error": "ensure_tables_failed"}
        c = _conn()
        if c is None:
            return {"ok": False, "error": "no_db"}
        try:
            with c.cursor() as cur:
                cur.execute("SET statement_timeout = '90000'")  # background thread; generous
                cur.execute("SELECT MIN(id), MAX(id) FROM agent_requests")
                minid, maxid = cur.fetchone()
                if not maxid:
                    return {"ok": True, "weeks": [], "note": "no agent_requests rows"}

                # earliest timestamp (for the data's first week)
                cur.execute("SELECT timestamp FROM agent_requests WHERE id = %s", (minid,))
                first_dt = _parse_ts((cur.fetchone() or [None])[0]) or datetime.utcnow()
                today = datetime.utcnow().date()
                cur_week = _monday(today)
                data_first_week = _monday(first_dt.date())

                # Always recompute the full trailing window (UPSERT-idempotent).
                # The per-week scan is now cheap (no SQL regex), so this is ~tens
                # of seconds/day in a background thread and it SELF-HEALS any gap
                # left by a prior partial/timed-out run.
                start_week = max(data_first_week, cur_week - timedelta(weeks=BACKFILL_WEEKS - 1))

                weeks = []
                w = start_week
                while w <= cur_week:
                    weeks.append(w)
                    w += timedelta(weeks=1)

                # NOTE: we deliberately do NOT pre-seed reach_ip_seen from the
                # pre-window range — that DISTINCT+regex scan over millions of
                # rows exceeds the statement timeout. Weeks are processed
                # ASCENDING so the seen-set builds chronologically; the EARLIEST
                # backfilled week's new_external_ips may be slightly inflated
                # (nothing was seeded before it), but the external-IP universe is
                # tiny (~tens) so it self-corrects within a week or two, and every
                # week from the first live run forward is exact (cumulative set).

                # resolve id boundaries for each week start (ascending)
                results = []
                bounds = {}
                for wk in weeks + [cur_week + timedelta(weeks=1)]:
                    bounds[wk] = _id_for_instant(cur, datetime.combine(wk, datetime.min.time()),
                                                 minid, maxid)
                for i, wk in enumerate(weeks):
                    id_lo = bounds.get(wk) or minid
                    nxt = bounds.get(wk + timedelta(weeks=1))
                    id_hi = (nxt - 1) if nxt else maxid
                    if id_hi < id_lo:
                        continue
                    results.append(_compute_week(cur, wk, id_lo, id_hi))

                return {"ok": True, "weeks_computed": len(results),
                        "window_weeks": len(weeks), "current_week": cur_week.isoformat(),
                        "weeks": results}
        finally:
            try: c.close()
            except Exception: pass
    finally:
        _running.release()


@ai_reach_rollup_bp.route("/api/cron/reach-rollup", methods=["POST", "GET"])
def cron_reach_rollup():
    """Fire-and-forget recompute (202). Never holds a worker on the heavy scan —
    a slow synchronous endpoint here would starve the small gunicorn pool."""
    threading.Thread(target=run_reach_rollup, daemon=True, name="reach-rollup").start()
    return jsonify({"ok": True, "started": True,
                    "note": "reach rollup recomputing in background; read /api/v1/ai/reach/trend"}), 202


@ai_reach_rollup_bp.route("/api/v1/ai/reach/trend", methods=["GET"])
def reach_trend():
    """Precomputed weekly reach — cold-start safe (<=CAP_WEEKS-row PK read, no
    agent_requests scan). Fail-soft 200 to match the /ai page contract."""
    out = {"weeks": [], "current": None,
           "note": ("Distinct EXTERNAL IPs/week = real agent reach (not loop-inflated "
                    "volume). new_external_ips = acquisition signal: IPs never seen in any "
                    "prior week.")}
    try:
        if not _ensure_tables():
            out["degraded"] = True
            return jsonify(out), 200
        c = _conn()
        if c is None:
            out["degraded"] = True
            return jsonify(out), 200
        try:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT week_start, distinct_external_ips, distinct_platforms,
                           new_external_ips, requests, computed_at
                    FROM reach_weekly ORDER BY week_start DESC LIMIT %s
                """, (CAP_WEEKS,))
                rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                r["week_start"] = r["week_start"].isoformat() if r["week_start"] else None
                r["computed_at"] = r["computed_at"].isoformat() if r.get("computed_at") else None
            rows.reverse()  # ascending for charting
            out["weeks"] = rows
            out["current"] = rows[-1] if rows else None
        finally:
            try: c.close()
            except Exception: pass
    except Exception:
        out["degraded"] = True
    return jsonify(out), 200
