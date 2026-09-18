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
import json, re, threading
from datetime import datetime, date, timedelta
from flask import Blueprint, jsonify, request
from internal_auth import is_valid_internal_key
import psycopg2, psycopg2.extras

from routes.ai_reach import _conn, _PRIVATE_IP, _INTERNAL_PLAT

ai_reach_rollup_bp = Blueprint("ai_reach_rollup", __name__)

CAP_WEEKS = 16              # how many trailing weeks the /trend endpoint serves
BACKFILL_WEEKS = 12         # trailing weeks recomputed every run (idempotent; self-heals gaps)
_SCAN_CHUNK = 40_000        # id-range chunk per scan statement. Small on purpose: the cost is
                            # heap-fetching ip/platform for every row in the id-range, which under
                            # the 1-replica pool load can blow a big chunk past the timeout. 40K
                            # keeps each statement well under _CHUNK_TIMEOUT_MS even at peak load,
                            # so the backfill completes any time of day (not just the low-load
                            # daily-cron window) — was 200K, which only finished at ~13:00 UTC.
_CHUNK_TIMEOUT_MS = 30_000  # per-statement cap: a chunk that can't finish in 30s fails fast and the
                            # week is skipped (per-week try/except) instead of burning 90s × 14 weeks.
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
                    per_platform          JSONB,
                    computed_at           TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            # idempotent add for the already-deployed table (per_platform feeds
            # /api/v1/ai/reach's per_platform[] so the live endpoint needs no scan).
            cur.execute("ALTER TABLE reach_weekly ADD COLUMN IF NOT EXISTS per_platform JSONB")
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


def _compute_week(cur, week_start: date, week_end: date) -> dict:
    """One created_at-bounded scan of mcp_tool_calls → REAL-IP counts + per-platform,
    then fold the IP set into reach_ip_seen → new_external_ips (idempotent on re-run).

    r-reach-mcp-source (2026-06-24): source switched from `agent_requests` to
    `mcp_tool_calls`. agent_requests.ip_address only ever held the Railway CGNAT
    proxy IP (100.64/10) — never the real client (no X-Forwarded-For column) — so the
    old reach counted ~16 proxy nodes and the per-platform breakdown collapsed to
    "agents == week total" for every platform (a non-bug over bad data). mcp_tool_calls
    .ip_address IS the real public client IP, the table is small (~1.4K calls/7d) and
    created_at-indexed, so NO chunking / id-binary-search / timeout dance is needed.
    Platform = the canonical PLATFORM_CASE classifier and probes/self-traffic are
    excluded by real_calls_predicate() (the SAME de-loop the funnel uses) → reach ==
    the funnel's unique_ips_7d_real, no drift. The query is INLINED (no bound params)
    because PLATFORM_CASE's ILIKE patterns contain literal % that would collide with
    psycopg2 %-substitution; week_start/week_end are controlled date literals."""
    from mcp_calls_deloop import PLATFORM_CASE, real_calls_predicate
    ips, plats, reqs = set(), set(), 0
    plat_ips: dict = {}   # platform -> set(ip)  → per-platform distinct-agent counts
    plat_reqs: dict = {}  # platform -> request count
    ws, we = week_start.isoformat(), week_end.isoformat()
    # ★★2026-07-27 agent-count audit — this read `mcp_tool_calls` filtered by
    # real_calls_predicate(), which is LOOSER than the canonical identity view.
    # For week 2026-07-20 that published 75 "distinct agents" where the canonical
    # basis gives 67: `is_real_external` additionally excludes scripted clients
    # (python-httpx, curl/, wget, node-fetch, axios, go-http, okhttp, requests/,
    # scrapy, httpie), probe/QA platforms (mcp-probe, value-harness, qa, verify,
    # *audit*, *harness*, *sweep*, sub-3-char platform noise) and known internal
    # IPs. Reading the view instead means reach and /api/v1/stats/live-proof
    # share ONE definition of a real external caller.
    #
    # `AND agent_id IS NOT NULL` drops Cloudflare edge ranges — the view nulls
    # agent_id for them precisely so proxy nodes are not counted as callers
    # (the original "reach counted ~16 proxy nodes" bug).
    #
    # We still collect client_ip, NOT agent_id, because reach_ip_seen holds raw
    # IPs for the new-vs-returning signal; switching that column to hashes would
    # make every historical IP look new. agent_id is md5(client_ip) anyway, so
    # distinctness is identical either way.
    cur.execute(
        "SELECT (" + PLATFORM_CASE.strip() + ") AS plat, client_ip, COUNT(*) AS n "
        "FROM mcp_calls_identity "
        "WHERE created_at >= '" + ws + "'::timestamptz "
        "  AND created_at <  '" + we + "'::timestamptz "
        "  AND is_public_ip AND is_real_external AND agent_id IS NOT NULL "
        "GROUP BY 1, client_ip")
    for plat, ip, n in cur.fetchall():
        if _PRIV_RE.match(ip or ''):      # belt-and-braces; is_public_ip covers this
            continue
        ips.add(ip)
        if plat:
            plats.add(plat)
            plat_ips.setdefault(plat, set()).add(ip)
            plat_reqs[plat] = plat_reqs.get(plat, 0) + int(n or 0)
        reqs += int(n or 0)

    # ★★2026-07-27: distinct_platforms was len(plats) — EVERY distinct platform
    # string, unfiltered. For week 2026-07-20 that published 15, of which six
    # were not platforms at all (`mcp` — the protocol name, and the LARGEST
    # bucket at 1,931 requests — plus mcp-server-validator, smithery connect,
    # unknown, connectors-manager and reviewer-sim, our own test simulator),
    # while `claude`, `claude-code` and `anthropic/claudeai` triple-counted one
    # vendor. That is how /api/v1/ai/reach reported 15 platforms over 7 days
    # while /api/v1/stats/live-proof reported 10 over 30 — an impossible
    # inversion, since a longer window cannot hold fewer platforms.
    # Both endpoints now count through ai_platform_canon so they cannot diverge.
    # The per-platform breakdown below deliberately keeps the RAW ids: the
    # fragmentation only misleads when summed into a headline count.
    from ai_platform_canon import count_platforms
    n_platforms = count_platforms(plats)

    # per-platform breakdown — [{platform_id, agents=distinct-IPs, requests}], top 25.
    per_platform = sorted(
        ({"platform_id": p, "agents": len(s), "requests": plat_reqs.get(p, 0)}
         for p, s in plat_ips.items()),
        key=lambda d: (-d["agents"], -d["requests"]),
    )[:25]

    # Fold this week's IPs into the cumulative seen-set → new_external_ips (weeks are
    # processed ascending, so first_seen_week is honest + re-run-safe).
    if ips:
        psycopg2.extras.execute_values(
            cur,
            "INSERT INTO reach_ip_seen (ip_address, first_seen_week) VALUES %s "
            "ON CONFLICT (ip_address) DO NOTHING",
            [(ip, week_start) for ip in ips],
        )
    # ★★2026-08-17 — new_external_ips EXCEEDED distinct_external_ips EVERY WEEK.
    # This counted every row in reach_ip_seen stamped to the week, which is NOT
    # the same set as the IPs this week actually saw. reach_ip_seen is cumulative
    # and `ON CONFLICT DO NOTHING`, so a first_seen_week stamp is permanent — and
    # the rows stamped during the agent_requests era and the pre-07-27 looser
    # predicate (before `is_real_external` + `agent_id IS NOT NULL` narrowed the
    # population) are still there, still counted, and can never be re-earned.
    # Measured live 2026-08-17 on /api/v1/ai/reach/trend:
    #     week 2026-07-06:  43 distinct IPs, 245 "new"
    #     week 2026-07-13:  81 distinct IPs, 179 "new"
    #     week 2026-05-25:   4 distinct IPs,  33 "new"
    # "New IPs never seen before" cannot exceed "IPs seen" — the field documented
    # as THE acquisition signal was unreadable in both directions (it could not
    # be trusted as a level, and its trend mixed two populations).
    # Bounding the count to THIS week's set makes new <= distinct hold by
    # construction, not by assertion.
    # ★Deliberately conservative: an IP stamped to an earlier week by the legacy
    # writer does not count as new when it first appears under the current
    # predicate. That understates acquisition rather than overstating it, and
    # those stamps are overwhelmingly CF POP / proxy addresses that no real
    # client re-presents. Re-running the rollup self-heals every week inside
    # BACKFILL_WEEKS; weeks older than the window keep their legacy value.
    if ips:
        cur.execute(
            "SELECT COUNT(*) FROM reach_ip_seen "
            "WHERE first_seen_week = %s AND ip_address = ANY(%s)",
            (week_start, list(ips)))
        new_ips = int((cur.fetchone() or [0])[0])
    else:
        new_ips = 0

    cur.execute("""
        INSERT INTO reach_weekly
            (week_start, distinct_external_ips, distinct_platforms,
             new_external_ips, requests, id_lo, id_hi, per_platform, computed_at)
        VALUES (%s, %s, %s, %s, %s, NULL, NULL, %s, NOW() ON CONFLICT DO NOTHING)
        ON CONFLICT (week_start) DO UPDATE SET
            distinct_external_ips = EXCLUDED.distinct_external_ips,
            distinct_platforms    = EXCLUDED.distinct_platforms,
            new_external_ips      = EXCLUDED.new_external_ips,
            requests              = EXCLUDED.requests,
            id_lo                 = NULL,
            id_hi                 = NULL,
            per_platform          = EXCLUDED.per_platform,
            computed_at           = NOW()
    """, (week_start, len(ips), n_platforms, new_ips, reqs, json.dumps(per_platform)))

    return {"week_start": week_start.isoformat(), "distinct_external_ips": len(ips),
            "distinct_platforms": n_platforms, "new_external_ips": new_ips,
            "requests": reqs, "per_platform": per_platform}


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
                cur.execute("SET statement_timeout = '20000'")
                today = datetime.utcnow().date()
                cur_week = _monday(today)
                start_week = cur_week - timedelta(weeks=BACKFILL_WEEKS - 1)
                weeks = []
                w = start_week
                while w <= cur_week:
                    weeks.append(w)
                    w += timedelta(weeks=1)
                # Per-week resilience: one bad week must not abort the backfill (the
                # conn is autocommit → survives a QueryCanceled). Weeks ascending so
                # the cumulative reach_ip_seen set + new_external_ips stay honest.
                results, skipped = [], []
                for wk in weeks:
                    try:
                        results.append(_compute_week(cur, wk, wk + timedelta(weeks=1)))
                    except Exception as e:
                        skipped.append({"week": wk.isoformat(), "error": str(e)[:120]})
                        continue
                return {"ok": True, "weeks_computed": len(results),
                        "weeks_skipped": len(skipped), "skipped": skipped,
                        "window_weeks": len(weeks), "current_week": cur_week.isoformat(),
                        "source": "mcp_tool_calls", "weeks": results}
        finally:
            try: c.close()
            except Exception: pass
    finally:
        _running.release()


@ai_reach_rollup_bp.route("/api/cron/reach-rollup", methods=["POST", "GET"])
def cron_reach_rollup():
    """Fire-and-forget recompute (202). Never holds a worker on the heavy scan —
    a slow synchronous endpoint here would starve the small gunicorn pool."""
    if not is_valid_internal_key(request.headers.get("X-Internal-Key") or request.headers.get("X-Admin-Key")):
        return jsonify({"error": "unauthorized"}), 401
    threading.Thread(target=run_reach_rollup, daemon=True, name="reach-rollup").start()
    return jsonify({"ok": True, "started": True,
                    "note": "reach rollup recomputing in background; read /api/v1/ai/reach/trend"}), 202


def mark_partial_weeks(rows: list, this_week: str) -> list:
    """PURE. Stamp `partial` (+ `coverage_hours` on the partial row) in place.

    ★★2026-08-17 — THE IN-PROGRESS WEEK WAS INDISTINGUISHABLE FROM A COLLAPSE.
    `weeks[]` ends with the CURRENT ISO week, which holds only the hours elapsed
    when the rollup last ran. Read on Monday 2026-08-17 at 23:49Z, the endpoint
    served week 2026-08-17 = 0 agents / 0 calls (computed 02:53Z, ~3h into the
    week) directly after a complete week of 72 — a chart-ready 100% cliff, every
    Monday, forever. A prior session already mis-read exactly this as a real
    93% collapse (08-05: week 08-03 = 6 agents right after a complete 85).

    The row was always honest; the LABEL was missing. Each week now declares
    whether it is complete, and the partial one declares how much of itself the
    number actually covers."""
    for r in rows or []:
        r["partial"] = (r.get("week_start") == this_week)
        if r["partial"]:
            ca = _parse_ts(r.get("computed_at"))
            ws = _parse_ts(r.get("week_start"))
            # Hours of the week INSIDE the measurement, not hours since it
            # began: a stale compute covers less, and saying so is the point.
            r["coverage_hours"] = (
                round(max(0.0, (ca - ws).total_seconds() / 3600.0), 1)
                if ca and ws else None)
    return rows


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
                           new_external_ips, requests, per_platform, computed_at
                    FROM reach_weekly ORDER BY week_start DESC LIMIT %s
                """, (CAP_WEEKS,))
                rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                r["week_start"] = r["week_start"].isoformat() if r["week_start"] else None
                r["computed_at"] = r["computed_at"].isoformat() if r.get("computed_at") else None
                pp = r.get("per_platform")
                if isinstance(pp, str):
                    try: r["per_platform"] = json.loads(pp)
                    except Exception: r["per_platform"] = []
            rows.reverse()  # ascending for charting
            # ★★2026-08-17 — THE IN-PROGRESS WEEK WAS INDISTINGUISHABLE FROM A
            # COLLAPSE. weeks[] ends with the CURRENT ISO week, which holds only
            # the hours elapsed when the rollup last ran. Read on Monday
            # 2026-08-17 at 23:49Z this served week 2026-08-17 = 0 agents /
            # 0 calls (computed 02:53Z, ~3h into the week) directly after a
            # complete week of 72 — a chart-ready 100% cliff, every Monday,
            # forever. A prior session already mis-read this as a real 93%
            # collapse (08-05: week 08-03 = 6 agents after a complete 85).
            # The row is honest; it was the LABEL that was missing. Each week
            # now declares whether it is complete, and how much of itself the
            # number actually covers.
            mark_partial_weeks(rows, _monday(datetime.utcnow().date()).isoformat())
            out["weeks"] = rows
            out["current"] = rows[-1] if rows else None
            # The last week whose number is final. Quote THIS one; `current` is
            # a live partial and will move under any reader.
            out["latest_complete"] = next(
                (r for r in reversed(rows) if not r["partial"]), None)
            out["note"] += (
                " weeks[] ENDS WITH THE IN-PROGRESS WEEK (partial=true, "
                "coverage_hours = hours of that week measured so far) — chart or "
                "quote `latest_complete` instead, or a Monday read renders as a "
                "collapse to zero. For fixed complete ISO weeks on the canonical "
                "basis, prefer /api/v1/reports/weekly-series.")
        finally:
            try: c.close()
            except Exception: pass
    except Exception:
        out["degraded"] = True
    return jsonify(out), 200
