"""
routes/qa_fixwave_master_shell.py — QA Fix-Wave Master Shell #22 (2026-07-16).

One pane that live-scores the SEVEN QA fix-wave items of 2026-07-16, each as a
lane of checks, so the operator can watch the wave land and keep the checks as
a standing regression sentinel afterward (house master-shell pattern — mirrors
fixwave_master_shell / pillars_master_shell).

The seven lanes (one per QA fix-wave item):
  1. WRI-URL-FRESHNESS    — the presigned R2 URL in WRI_AQUEDUCT_URL expires
                            (X-Amz-Date + X-Amz-Expires); PASS >48h away, WARN
                            <48h, FAIL expired/unparseable. Plus a 2s HEAD
                            (failure tolerated as WARN — SigV4 presigned URLs
                            are method-bound, a GET-signed URL may 403 a HEAD).
  2. GLAMA-BUILD          — the Glama listing for dchub-mcp-server responds 200
                            (empty/stale listing = rebuild; desc = GitHub About).
  3. DETECTOR-DEDUP       — max(seen_count) from brain_findings (informational)
                            + duplicate OPEN brain_strategic_recommendations
                            sharing a fingerprint (uses the fingerprint column
                            from the stateful-detector work when it lands;
                            until then a normalized-title prefix). PASS = 0 dup
                            groups.
  4. VERIFIED-CONSISTENCY — the canonical verified count (NULL-safe fleet
                            predicate COALESCE(is_duplicate,0)=0 over
                            discovered_facilities) vs a per-country breakdown
                            using the SAME predicate; PASS if the breakdown
                            sums back to canonical (±2%), FAIL if the breakdown
                            returns 0 while canonical > 0 (predicate drift).
  5. RETENTION-CANONICAL  — pct_returned_next_week_mature ('mature cross-week
                            return') and pct_reused_30d ('reused at least once
                            30d') computed from the SAME auto_trial_keys SQL
                            the /api/v1/mcp/retention route uses. Informational
                            (PASS showing BOTH correctly labeled) — but FAIL if
                            the two values are identical (re-conflated).
  6. FRONTEND-MAP-GUARD   — /js/new-energy-layers.js on the edge carries the
                            insertButtons map.on guard ('typeof map.on' after
                            'insertButtons', or present ≥3×). Network failure
                            tolerated as WARN.
  7. FRONTEND-LATENCY     — informational: edge GET https://dchub.cloud/dashboard
                            (browser UA, external CF-edge fetch — NOT a
                            self-request); PASS <2s, WARN 2–5s, FAIL >5s.

Design mirrors the house master-shell pattern: admin-gated Flask blueprint,
killable (QA_FIXWAVE_DISABLED=1), every probe fail-soft + timeout-bounded,
45s tick cache. STRICTLY READ-ONLY — pure-DB reads + env parsing + cheap
external probes; it never mutates state, never writes a snapshot row, and
never self-requests the backend (so a slow tick can't dogpile the web pool).

DB access is replica-preferred: main.get_read_connection (the pooled Neon
read-replica ladder — DATABASE_READ_URL / NEON_REPLICA_URL with primary
fallback built in), falling back to the pooled db_utils.get_db wrapper.
No raw psycopg2 connections are opened here.

Each check carries: name, status (pass/fail/warn), value, detail, actuator
(the concrete fix to pull when the check is red).

Endpoints:
  GET/POST /api/v1/admin/qa-fixwave/master-tick   JSON scoreboard (7 lanes)
  GET      /admin/qa-fixwave                       HTML dashboard (60s refresh)
  GET      /api/v1/admin/qa-fixwave                CF zone-worker bypass alias

Auth: X-Admin-Key header or ?admin_key= vs DCHUB_ADMIN_KEY (falls back to
DCHUB_INTERNAL_KEY) — same gate as fixwave_master_shell / pillars_master_shell.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from html import escape as _esc

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

qa_fixwave_master_shell_bp = Blueprint("qa_fixwave_master_shell", __name__)

_EDGE = "https://dchub.cloud"
_GLAMA_URL = "https://glama.ai/api/mcp/v1/servers/azmartone67/dchub-mcp-server"

_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_TICK_TTL = 45.0  # seconds (house range: 30–60s)
_cache: dict = {"ts": 0.0, "payload": None}
_cache_lock = threading.Lock()


# ── auth / kill ───────────────────────────────────────────────────────

def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("QA_FIXWAVE_DISABLED") or "").strip() == "1"


# ── http helper (fail-soft, bounded) ──────────────────────────────────

def _http(url: str, *, method: str = "GET", timeout: float = 5.0,
          ua: str = _BROWSER_UA) -> tuple[int, str, int]:
    """Bounded fetch. Returns (status, body_text[:200k], elapsed_ms).
    Never raises — network failure returns (0, err, ms)."""
    t0 = time.time()
    try:
        req = urllib.request.Request(url, method=method)
        req.add_header("User-Agent", ua)
        req.add_header("Accept", "*/*")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = "" if method == "HEAD" else r.read(200_000).decode("utf-8", "replace")
            return r.getcode(), body, int((time.time() - t0) * 1000)
    except urllib.error.HTTPError as e:
        try:
            txt = e.read(50_000).decode("utf-8", "replace")
        except Exception:
            txt = ""
        return e.code, txt, int((time.time() - t0) * 1000)
    except Exception as e:  # DNS, timeout, TLS, conn-refused
        return 0, f"{type(e).__name__}: {e}", int((time.time() - t0) * 1000)


# ── db (pooled, replica-preferred, READ-ONLY) ─────────────────────────

@contextmanager
def _read_db():
    """Pooled READ-ONLY connection, replica-preferred. Ladder:
      1. main.get_read_connection — the Neon read-replica pool
         (DATABASE_READ_URL / NEON_REPLICA_URL; falls back to primary itself).
      2. db_utils.get_db — the pooled primary wrapper (returns to pool on close).
    Yields None when no DB is reachable — callers must fail-soft (WARN lanes).
    Never opens a raw psycopg2 connection."""
    conn = None
    cleanup = None
    try:
        from main import get_read_connection, return_read_connection
        _c, _src = get_read_connection()
        if _c is not None:
            conn = _c
            cleanup = lambda: return_read_connection(_c, _src)
    except Exception as e:
        logger.debug("[qa-fixwave] read pool unavailable: %s", e)
    if conn is None:
        try:
            from db_utils import get_db
            _w = get_db()
            conn = _w
            cleanup = _w.close  # PGConnectionWrapper.close → putconn
        except Exception as e:
            logger.warning("[qa-fixwave] db connect failed: %s", e)
    try:
        yield conn
    finally:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
            if cleanup is not None:
                try:
                    cleanup()
                except Exception:
                    pass


def _rows(c, sql: str, args: tuple | None = None):
    """Fail-soft fetchall. None on error (NOT [] — a lane must distinguish
    'query broke' from 'no rows'). args=None → literal SQL, no %-substitution
    (the psycopg2 empty-tuple trap)."""
    if c is None:
        return None
    cur = None
    try:
        cur = c.cursor()
        if args is None:
            cur.execute(sql)
        else:
            cur.execute(sql, args)
        return cur.fetchall()
    except Exception as e:
        logger.debug("[qa-fixwave] query failed: %s -- %s", sql[:90], e)
        try:
            c.rollback()
        except Exception:
            pass
        return None
    finally:
        try:
            if cur is not None:
                cur.close()
        except Exception:
            pass


def _scalar(c, sql: str, args: tuple | None = None):
    rows = _rows(c, sql, args)
    if not rows:
        return None
    return rows[0][0]


# ── check shape (name · pass/fail/warn · value · detail · actuator) ───

_STATUS_TO_BOOL = {"pass": True, "fail": False, "warn": None}


def _check(cid: str, name: str, status: str, value, detail: str,
           actuator: str = "", ms: int = 0) -> dict:
    status = status if status in _STATUS_TO_BOOL else "warn"
    return {"id": cid, "name": name, "status": status,
            "pass": _STATUS_TO_BOOL[status],  # tri-state for house-pattern rollup
            "value": value, "detail": (detail or "")[:300],
            "actuator": (actuator or "")[:220], "ms": ms}


def _fmt_int(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "—"


# ── lane 1 · wri-url-freshness ────────────────────────────────────────

_WRI_ACTUATOR = ("re-mint the presigned R2 URL (wri/wri_aqueduct_us_states.json, "
                 "max X-Amz-Expires=604800) and update WRI_AQUEDUCT_URL on Railway")


def _lane_wri(c) -> list[dict]:
    out = []
    url = (os.environ.get("WRI_AQUEDUCT_URL") or "").strip()
    if not url:
        out.append(_check("wri_expiry", "presigned URL expiry >48h away", "fail",
                          None, "WRI_AQUEDUCT_URL env var not set", _WRI_ACTUATOR))
        return out
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    amz_date = (qs.get("X-Amz-Date") or [""])[0]
    amz_exp = (qs.get("X-Amz-Expires") or [""])[0]
    expiry = None
    try:
        signed_at = datetime.strptime(amz_date, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        expiry = signed_at + timedelta(seconds=int(amz_exp))
    except Exception:
        expiry = None
    if expiry is None:
        out.append(_check("wri_expiry", "presigned URL expiry >48h away", "fail",
                          None,
                          f"unparseable X-Amz-Date/X-Amz-Expires (date={amz_date!r}, "
                          f"expires={amz_exp!r})", _WRI_ACTUATOR))
    else:
        hours_left = (expiry - datetime.now(timezone.utc)).total_seconds() / 3600.0
        if hours_left <= 0:
            status = "fail"
        elif hours_left < 48:
            status = "warn"
        else:
            status = "pass"
        out.append(_check("wri_expiry", "presigned URL expiry >48h away", status,
                          f"{hours_left:.1f}h left",
                          f"expires {expiry.isoformat()} (signed {amz_date}, "
                          f"X-Amz-Expires={amz_exp}s)",
                          _WRI_ACTUATOR if status != "pass" else ""))
    st, _body, ms = _http(url, method="HEAD", timeout=2.0)
    out.append(_check("wri_head", "HEAD on the presigned URL answers 200",
                      "pass" if st == 200 else "warn",
                      f"HTTP {st}",
                      "reachable" if st == 200 else
                      ("non-200 tolerated as WARN — SigV4 presigned URLs are "
                       "method-bound; a GET-signed URL can 403 a HEAD"),
                      "", ms))
    return out


# ── lane 2 · glama-build ──────────────────────────────────────────────

def _lane_glama(c) -> list[dict]:
    out = []
    st, body, ms = _http(_GLAMA_URL, timeout=5.0)
    if st == 200:
        detail = "listing live"
        try:
            import json as _json
            j = _json.loads(body)
            attrs = j.get("attributes")
            name = j.get("name") or j.get("slug") or ""
            desc = (j.get("description") or "")[:80]
            detail = f"name={name!r} · attributes={attrs!r} · desc={desc!r}"
        except Exception:
            detail = f"200 but body not JSON ({len(body)}B)"
        out.append(_check("glama_listing", "Glama listing responds 200", "pass",
                          f"HTTP {st}", detail, "", ms))
    elif st == 0:
        out.append(_check("glama_listing", "Glama listing responds 200", "warn",
                          "HTTP 0", f"network failure tolerated as WARN — {body[:120]}",
                          "", ms))
    else:
        out.append(_check("glama_listing", "Glama listing responds 200", "fail",
                          f"HTTP {st}", body[:120],
                          "rebuild the Glama listing (empty=stale→rebuild; "
                          "description = GitHub About)", ms))
    return out


# ── lane 3 · detector-dedup ───────────────────────────────────────────

_OPEN_PROPOSAL_PREDICATE = (
    "COALESCE(status,'') NOT IN "
    "('done','shipped','merged','closed','rejected','superseded','wont_fix','archived','meta')"
)


def _lane_dedup(c) -> list[dict]:
    out = []
    if c is None:
        out.append(_check("dedup_db", "db reachable", "warn", None, "no db connection"))
        return out
    max_seen = _scalar(c, "SELECT MAX(COALESCE(seen_count,0)) FROM brain_findings")
    out.append(_check("dedup_max_seen", "max(seen_count) in brain_findings (informational)",
                      "pass" if max_seen is not None else "warn",
                      _fmt_int(max_seen),
                      "high seen_count = one finding correctly UPSERTED many times "
                      "(dedup working at the findings layer)" if max_seen is not None
                      else "query failed"))
    # fingerprint column from the concurrent stateful-detector work, when it
    # lands; until then a normalized-title prefix is the fingerprint.
    has_fp = _scalar(c,
                     "SELECT COUNT(*) FROM information_schema.columns "
                     "WHERE table_name='brain_strategic_recommendations' "
                     "AND column_name='fingerprint'")
    fp_expr = "fingerprint" if (has_fp or 0) else "LEFT(LOWER(TRIM(title)),120)"
    dup_rows = _rows(c, f"""
        SELECT fp, n FROM (
            SELECT {fp_expr} AS fp, COUNT(*) AS n
              FROM brain_strategic_recommendations
             WHERE {_OPEN_PROPOSAL_PREDICATE}
             GROUP BY 1 HAVING COUNT(*) > 1
        ) d ORDER BY n DESC LIMIT 10""")
    if dup_rows is None:
        out.append(_check("dedup_open_groups", "duplicate OPEN proposal groups == 0",
                          "warn", None, "query failed"))
    else:
        groups = len(dup_rows)
        extra_rows = sum(int(r[1] or 0) - 1 for r in dup_rows)
        top = "; ".join(f"{str(r[0])[:50]!r}×{r[1]}" for r in dup_rows[:3])
        out.append(_check("dedup_open_groups", "duplicate OPEN proposal groups == 0",
                          "pass" if groups == 0 else "fail",
                          f"{groups} dup groups ({extra_rows} extra rows)",
                          (f"fingerprint={'column' if (has_fp or 0) else 'title-prefix(120)'}"
                           + (f" · top: {top}" if top else " · clean")),
                          "" if groups == 0 else
                          "merge/close duplicate open brain_strategic_recommendations "
                          "+ land the stateful-detector fingerprint dedup (upsert, "
                          "don't re-insert)"))
    return out


# ── lane 4 · verified-flag-consistency ────────────────────────────────

# THE canonical NULL-safe fleet/verified predicate (see facilities_by_dims +
# the discovered_facilities fleet-filter rule): COALESCE(is_duplicate,0)=0.
_VERIFIED_PREDICATE = "COALESCE(is_duplicate,0)=0"


def _lane_verified(c) -> list[dict]:
    out = []
    if c is None:
        out.append(_check("verified_db", "db reachable", "warn", None, "no db connection"))
        return out
    canonical = _scalar(c, f"SELECT COUNT(*) FROM discovered_facilities WHERE {_VERIFIED_PREDICATE}")
    out.append(_check("verified_canonical",
                      f"canonical verified count ({_VERIFIED_PREDICATE})",
                      "pass" if (canonical or 0) > 0 else
                      ("warn" if canonical is None else "fail"),
                      _fmt_int(canonical),
                      "live COUNT() over discovered_facilities" if canonical is not None
                      else "query failed",
                      "" if (canonical or 0) > 0 else
                      "verified predicate returns 0 — check is_duplicate backfill"))
    rows = _rows(c, f"""
        SELECT COALESCE(NULLIF(TRIM(country),''),'(unknown)') AS country, COUNT(*) AS n
          FROM discovered_facilities
         WHERE {_VERIFIED_PREDICATE}
         GROUP BY 1 ORDER BY n DESC""")
    if rows is None or canonical is None:
        out.append(_check("verified_breakdown_sum",
                          "per-country breakdown (same predicate) sums to canonical ±2%",
                          "warn", None, "breakdown or canonical query failed"))
        return out
    bsum = sum(int(r[1] or 0) for r in rows)
    n_countries = len(rows)
    top = ", ".join(f"{r[0]}={_fmt_int(r[1])}" for r in rows[:3])
    if bsum == 0 and (canonical or 0) > 0:
        status, why = "fail", ("breakdown returned 0 while canonical > 0 — the "
                               "breakdown predicate has drifted off the NULL-safe canonical")
    else:
        tol = 0.02 * max(canonical or 0, 1)
        status = "pass" if abs(bsum - (canonical or 0)) <= tol else "fail"
        why = f"|{_fmt_int(bsum)} − {_fmt_int(canonical)}| {'≤' if status == 'pass' else '>'} 2%"
    out.append(_check("verified_breakdown_sum",
                      "per-country breakdown (same predicate) sums to canonical ±2%",
                      status,
                      f"sum={_fmt_int(bsum)} over {n_countries} countries",
                      f"{why} · top: {top}",
                      "" if status == "pass" else
                      f"re-align the per-country breakdown to the canonical NULL-safe "
                      f"predicate {_VERIFIED_PREDICATE}"))
    return out


# ── lane 5 · retention-canonical ──────────────────────────────────────

# Mirrors the summary SQL in routes/mcp_retention.py (/api/v1/mcp/retention):
# same table (auto_trial_keys), same mature-cohort rule (minted 8-30d ago so
# every key has had a full subsequent ISO week in which to return).
_RETENTION_SQL = """
    SELECT COUNT(*) AS minted_30d,
           ROUND(100.0*COUNT(*) FILTER (WHERE call_count > 1)/NULLIF(COUNT(*),0),1)
               AS pct_reused_30d,
           COUNT(*) FILTER (WHERE minted_at < now() - interval '7 days')
               AS mature_cohort_30d,
           ROUND(100.0*COUNT(*) FILTER (WHERE minted_at < now() - interval '7 days'
                    AND last_used_at IS NOT NULL
                    AND date_trunc('week', last_used_at) > date_trunc('week', minted_at))
                 /NULLIF(COUNT(*) FILTER (WHERE minted_at < now() - interval '7 days'),0),1)
               AS pct_returned_next_week_mature
      FROM auto_trial_keys
     WHERE minted_at >= now() - interval '30 days'
"""


def _lane_retention(c) -> list[dict]:
    out = []
    if c is None:
        out.append(_check("retention_db", "db reachable", "warn", None, "no db connection"))
        return out
    rows = _rows(c, _RETENTION_SQL)
    if not rows:
        out.append(_check("retention_metrics",
                          "both retention metrics computed with correct labels",
                          "warn", None, "query failed (auto_trial_keys)"))
        return out
    minted, pct_reused, mature_n, pct_mature = rows[0]
    value = (f"mature cross-week return = {pct_mature}% (n={_fmt_int(mature_n)}) · "
             f"reused at least once 30d = {pct_reused}% (n={_fmt_int(minted)})")
    identical = (pct_reused is not None and pct_mature is not None
                 and float(pct_reused) == float(pct_mature))
    out.append(_check("retention_metrics",
                      "pct_returned_next_week_mature ≠ pct_reused_30d (labels not re-conflated)",
                      "fail" if identical else "pass",
                      value,
                      ("IDENTICAL values — someone re-conflated the two metrics"
                       if identical else
                       "informational — these are DIFFERENT metrics: 'mature cross-week "
                       "return' (keys minted 8-30d ago that came back a later ISO week) "
                       "vs 'reused at least once 30d' (call_count > 1)"),
                      ("re-split the metrics — same SQL as /api/v1/mcp/retention "
                       "summary (routes/mcp_retention.py)") if identical else ""))
    return out


# ── lane 6 · frontend-map-guard ───────────────────────────────────────

def _lane_map_guard(c) -> list[dict]:
    out = []
    st, body, ms = _http(f"{_EDGE}/js/new-energy-layers.js", timeout=5.0)
    if st == 0:
        out.append(_check("map_guard", "insertButtons guard requires map.on",
                          "warn", "HTTP 0",
                          f"network failure tolerated as WARN — {body[:120]}", "", ms))
        return out
    if st != 200:
        out.append(_check("map_guard", "insertButtons guard requires map.on",
                          "warn", f"HTTP {st}",
                          "non-200 — can't assert guard presence from an error page",
                          "", ms))
        return out
    n_guards = body.count("typeof map.on")
    insert_idx = body.find("insertButtons")
    guard_after = (insert_idx >= 0
                   and body.find("typeof map.on", insert_idx) >= 0)
    ok = guard_after or n_guards >= 3
    out.append(_check("map_guard", "insertButtons guard requires map.on",
                      "pass" if ok else "fail",
                      f"'typeof map.on' ×{n_guards} · after insertButtons={guard_after}",
                      f"{len(body):,}B served from the edge (browser UA)",
                      "" if ok else "deploy the new-energy-layers guard fix", ms))
    return out


# ── lane 7 · frontend-latency ─────────────────────────────────────────

def _lane_latency(c) -> list[dict]:
    out = []
    st, body, ms = _http(f"{_EDGE}/dashboard", timeout=10.0)
    if st == 0:
        # a timeout IS the slow signal; any other network error is indeterminate
        if ms >= 5000:
            out.append(_check("dashboard_latency", "edge /dashboard total <2s",
                              "fail", f"{ms}ms", f"timed out — {body[:100]}",
                              "profile /dashboard render path + CF cache", ms))
        else:
            out.append(_check("dashboard_latency", "edge /dashboard total <2s",
                              "warn", f"{ms}ms",
                              f"network failure tolerated as WARN — {body[:100]}",
                              "", ms))
        return out
    if ms < 2000:
        status, actuator = "pass", ""
    elif ms <= 5000:
        status, actuator = "warn", ""
    else:
        status, actuator = "fail", "profile /dashboard render path + CF cache"
    out.append(_check("dashboard_latency", "edge /dashboard total <2s",
                      status, f"{ms}ms",
                      f"HTTP {st}, {len(body):,}B — external CF-edge fetch "
                      f"(browser UA), not a self-request",
                      actuator, ms))
    return out


# ── tick ──────────────────────────────────────────────────────────────

_LANES = [
    ("wri_url_freshness",   "1 · WRI presigned-URL freshness",        _lane_wri),
    ("glama_build",         "2 · Glama listing build",                _lane_glama),
    ("detector_dedup",      "3 · Detector dedup (open proposals)",    _lane_dedup),
    ("verified_consistency", "4 · Verified-flag consistency",         _lane_verified),
    ("retention_canonical", "5 · Retention metrics canonical labels", _lane_retention),
    ("frontend_map_guard",  "6 · new-energy-layers map.on guard",     _lane_map_guard),
    ("frontend_latency",    "7 · Edge /dashboard latency",            _lane_latency),
]


def _lane_status(checks: list[dict]) -> str:
    statuses = [ch.get("status") for ch in checks]
    if "fail" in statuses:
        return "fail"
    if "warn" in statuses:
        return "warn"
    return "pass"


def _run_tick() -> dict:
    lanes = []
    with _read_db() as c:
        for key, label, fn in _LANES:
            try:
                checks = fn(c)
            except Exception as e:  # a lane must never sink the tick
                checks = [_check(f"{key}_error", "lane crashed", "warn", None,
                                 f"{type(e).__name__}: {str(e)[:180]}")]
            status = _lane_status(checks)
            lanes.append({
                "lane": key, "label": label,
                "status": status,
                "pass": status == "pass",  # house-pattern bool rollup
                "checks": checks,
                "progress": f"{sum(1 for ch in checks if ch['status'] == 'pass')}/{len(checks)}",
            })
    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lanes_pass": sum(1 for l in lanes if l["status"] == "pass"),
        "lanes_warn": sum(1 for l in lanes if l["status"] == "warn"),
        "lanes_fail": sum(1 for l in lanes if l["status"] == "fail"),
        "lanes_total": len(lanes),
        "lanes": lanes,
        "note": ("read-only diagnostic — pure-DB (replica-preferred) + env parse + "
                 "cheap external probes; no self-requests, no writes. "
                 "See routes/qa_fixwave_master_shell.py"),
    }


def _tick_cached() -> dict:
    with _cache_lock:
        if _cache["payload"] is not None and time.time() - _cache["ts"] < _TICK_TTL:
            return _cache["payload"]
    payload = _run_tick()
    with _cache_lock:
        _cache["ts"] = time.time()
        _cache["payload"] = payload
    return payload


# ── routes ────────────────────────────────────────────────────────────

@qa_fixwave_master_shell_bp.route("/api/v1/admin/qa-fixwave/master-tick", methods=["GET", "POST"])
def qa_fixwave_master_tick():
    if _disabled():
        # ★404, never 5xx (2026-08-12): the CF worker's proxyWithRetry reads
        # ANY 5xx from Railway as a dead origin and fails the site over to the
        # stale Render backend. Turning off one diagnostic shell must not be
        # able to do that. See graph_spine_master_shell for the original note.
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    if (request.args.get("fresh") or "") == "1":
        with _cache_lock:
            _cache["payload"] = None
    return jsonify(_tick_cached())


@qa_fixwave_master_shell_bp.route("/admin/qa-fixwave", methods=["GET"])
@qa_fixwave_master_shell_bp.route("/api/v1/admin/qa-fixwave", methods=["GET"])
def qa_fixwave_dashboard():
    if _disabled():
        return Response("qa-fixwave shell disabled", status=404)
    if not _admin_ok():
        return Response("forbidden — X-Admin-Key or ?admin_key=", status=403)
    p = _tick_cached()

    def _chip(status: str) -> str:
        if status == "pass":
            return '<span style="color:#22c55e">✓</span>'
        if status == "fail":
            return '<span style="color:#ef4444">✗</span>'
        return '<span style="color:#eab308">⚠</span>'

    cards = []
    for lane in p["lanes"]:
        rows = []
        for ch in lane["checks"]:
            val = "" if ch.get("value") is None else str(ch["value"])
            act = ch.get("actuator") or ""
            act_html = (f"<div style='color:#f59e0b;font-size:12px;margin-top:2px'>"
                        f"→ {_esc(act)}</div>") if act else ""
            rows.append(
                f"<tr><td style='padding:4px 8px;vertical-align:top'>{_chip(ch['status'])}</td>"
                f"<td style='padding:4px 8px;vertical-align:top'>{_esc(ch['name'])}</td>"
                f"<td style='padding:4px 8px;vertical-align:top;color:#38bdf8'>{_esc(val)}</td>"
                f"<td style='padding:4px 8px;color:#94a3b8'>{_esc(ch['detail'])}"
                f"{(' · ' + str(ch['ms']) + 'ms') if ch.get('ms') else ''}{act_html}</td></tr>")
        border = {"pass": "#22c55e", "fail": "#ef4444"}.get(lane["status"], "#334155")
        cards.append(
            f"<div style='background:#0f172a;border:1px solid {border};border-radius:12px;"
            f"padding:16px;margin:12px 0'>"
            f"<div style='font-weight:700;font-size:15px'>{_chip(lane['status'])} "
            f"{_esc(lane['label'])} <span style='color:#64748b;font-weight:400'>"
            f"({lane['progress']} checks green)</span></div>"
            f"<table style='margin-top:8px;font-size:13px;border-collapse:collapse'>"
            f"{''.join(rows)}</table></div>")

    head_color = ("#22c55e" if p["lanes_pass"] == p["lanes_total"]
                  else ("#ef4444" if p["lanes_fail"] else "#eab308"))
    html = (
        "<!doctype html><meta charset='utf-8'>"
        "<meta http-equiv='refresh' content='60'>"
        "<title>QA Fix-Wave Master Shell · DC Hub</title>"
        "<body style='background:#020617;color:#e2e8f0;font-family:-apple-system,Segoe UI,"
        "Roboto,sans-serif;max-width:960px;margin:24px auto;padding:0 16px'>"
        f"<h2 style='margin:0 0 4px'>QA Fix-Wave Master Shell "
        f"<span style='color:{head_color}'>{p['lanes_pass']}/{p['lanes_total']} lanes green</span>"
        f"<span style='color:#64748b;font-weight:400;font-size:14px'> · "
        f"{p['lanes_warn']} warn · {p['lanes_fail']} fail</span></h2>"
        f"<div style='color:#64748b;font-size:12px'>shell #22 · 2026-07-16 QA fix-wave · "
        f"read-only diagnostic (pure-DB + env + external probes; no self-requests) · "
        f"{int(_TICK_TTL)}s tick cache · auto-refresh 60s · generated {_esc(p['generated_at'])} · "
        f"JSON: /api/v1/admin/qa-fixwave/master-tick (?fresh=1 to force)</div>"
        + "".join(cards) + "</body>")
    return Response(html, mimetype="text/html")
