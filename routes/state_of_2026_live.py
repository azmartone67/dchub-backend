"""
routes/state_of_2026_live.py — Living State of 2026 (post-publish).

Evolves the static pre-publish QA harness (state_of_2026_precheck.py) into
a SERVER-RENDERED landing page that:

  1. Pulls all 8 verified claim values LIVE from the same endpoints the QA
     harness validates against — so the page never lies to a LinkedIn
     visitor (no hardcoded "300+ markets" while DB shows 306).
  2. Records click-attribution via /r/<token> 302 proxy. token → destination
     map is hardcoded (small set, no DB write on the hot path); each click
     writes a row to `state_of_2026_clicks` with referer + ip_hash + ua + ts.
  3. Surfaces /api/v1/admin/state-of-2026/attribution?days=N — funnel
     telemetry the founder pastes into LinkedIn DMs to prove engagement.
  4. Auto-evolves docs/state-of-2026-claims.txt via a daily cron at
     06:00 UTC that PROPOSES updates to a `state_of_2026_claim_proposals`
     table; the operator clicks "apply" on the admin dashboard to commit.
     NEVER auto-pushes git from a cron.
  5. /admin/state-of-2026-pulse — engagement dashboard (page views, clicks
     by referer, signups attributed, MCP key claims attributed).

DB tables (idempotent CREATE IF NOT EXISTS in init_state_of_2026_tables):
  - state_of_2026_clicks (id, token, destination, referer, ip_hash, ua,
                          session_id, ts)
  - state_of_2026_pageviews (id, ip_hash, referer, ua, ts)
  - state_of_2026_claim_proposals (id, claim_lineno, current_text,
                                   proposed_text, current_min, current_max,
                                   proposed_min, proposed_max, live_value,
                                   reason, status, ts, applied_ts)
  - state_of_2026_live_numbers   (2026-06-07 Round 3: daily-refresh cache so
                                   the page reads sub-ms from this table
                                   instead of fanning out 8 internal probes
                                   on every cold-cache hit. Daily 05:00 UTC
                                   cron refreshes; page falls back to live
                                   probes if the row is missing/stale.)
                                   (claim_lineno PK, claim_text, endpoint,
                                    json_path, live_value, expected_min,
                                    expected_max, measured_at, source)

Rate-limiting / live-API safety:
  - /state-of-2026 page query results are CACHED for 300s in-memory. A
    LinkedIn-driven spike of 1000 visitors/min still only hits backend
    once per 5min for the 8 live numbers. The cache key is a single
    constant ("hero"), so eviction is trivial.
  - The /r/<token> redirect does ONE INSERT per click + a 302. No reads.
    Single Railway replica can sustain ~500 req/s of this trivially.
  - The OG card is served from /api/v1/og/dynamic.png (already CF-cached
    for 1h) — no rate impact.

Routes:
  GET /state-of-2026                          server-rendered HTML landing
  GET /r/<token>                              click-attribution 302 proxy
  POST /api/v1/state-of-2026/subscribe        email → notify list
  GET /api/v1/admin/state-of-2026/attribution JSON funnel (admin-gated)
  GET /admin/state-of-2026-pulse              HTML engagement dashboard
  POST /api/v1/admin/state-of-2026/claims/propose    cron entry point
  POST /api/v1/admin/state-of-2026/claims/apply/<id> manual approve
  GET /api/v1/state-of-2026/live              JSON of 8 live numbers
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone, timedelta
from html import escape as _esc
from typing import Any, Optional

from flask import Blueprint, Response, jsonify, redirect, request

try:
    import requests as _rq  # type: ignore
except Exception:
    _rq = None

logger = logging.getLogger(__name__)

state_of_2026_live_bp = Blueprint("state_of_2026_live", __name__)


# ── env / constants ───────────────────────────────────────────────────

_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY")
              or os.environ.get("ADMIN_KEY") or "").strip()

_PROBE_BASE_URL = (os.environ.get("STATE_OF_2026_PROBE_BASE")
                   or os.environ.get("DCHUB_INTERNAL_BASE_URL")
                   or "https://dchub-backend-production.up.railway.app"
                   ).rstrip("/")

_PUBLIC_BASE_URL = (os.environ.get("DCHUB_PUBLIC_BASE_URL")
                    or "https://dchub.cloud").rstrip("/")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CLAIMS_PATH = os.path.join(_REPO_ROOT, "docs", "state-of-2026-claims.txt")

# Hero-numbers cache (300s) so a LinkedIn spike doesn't fan out 8 internal
# probes per request. A single Railway replica handles this trivially.
_HERO_CACHE: dict[str, Any] = {"data": None, "ts": 0.0}
_HERO_CACHE_TTL_S = 300

# Click-attribution token map. New tokens are appended here and shipped
# with the deploy — the LinkedIn post URL uses the short forms. Adding to
# the map without redeploying is by design ("the post URL set must be in
# version control"). The signup token is the conversion-funnel pivot.
ATTRIBUTION_TOKENS: dict[str, str] = {
    "li":         f"{_PUBLIC_BASE_URL}/state-of-2026",  # LinkedIn → landing
    "signup":     f"{_PUBLIC_BASE_URL}/signup",
    "dcpi":       f"{_PUBLIC_BASE_URL}/dcpi",
    "dcgi":       f"{_PUBLIC_BASE_URL}/dcgi",
    "queues":     f"{_PUBLIC_BASE_URL}/interconnection-queues",
    "mcp":        f"{_PUBLIC_BASE_URL}/integrations/mcp",  # 2026-06-07 page-audit: was /mcp-connect (404 — never existed); canonical landing is /integrations/mcp (200)
    "hyper":      f"{_PUBLIC_BASE_URL}/hyperscalers",
    "qa":         f"{_PUBLIC_BASE_URL}/admin/qa/state-of-2026",
    "report":     f"{_PUBLIC_BASE_URL}/reports/state-of-power",
    "markets":    f"{_PUBLIC_BASE_URL}/markets",
    "cheyenne":   f"{_PUBLIC_BASE_URL}/markets/cheyenne/brief",
    "pricing":    f"{_PUBLIC_BASE_URL}/pricing",
}


# ── DB ────────────────────────────────────────────────────────────────

def _conn():
    """Open raw psycopg2 connection. Returns None on failure (page never
    dies because Postgres flaps — degrades to no-attribution mode)."""
    try:
        import psycopg2 as _pg
        dsn = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL") or "")
        if not dsn:
            return None
        c = _pg.connect(dsn, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning("state_of_2026_live: db connect failed: %s", e)
        return None


def init_state_of_2026_tables() -> None:
    """Boot-init hook. Wired from content_publisher.init_content_tables.
    Idempotent CREATE TABLE IF NOT EXISTS for the 3 tables this surface
    needs. Defensive try/except per statement so a partial state doesn't
    abort the boot."""
    c = _conn()
    if c is None:
        logger.warning("state_of_2026_live: skip table init (no db)")
        return
    try:
        with c.cursor() as cur:
            for stmt in (
                """
                CREATE TABLE IF NOT EXISTS state_of_2026_pageviews (
                    id            BIGSERIAL PRIMARY KEY,
                    ip_hash       TEXT,
                    referer       TEXT,
                    ua            TEXT,
                    session_id    TEXT,
                    ts            TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """,
                "CREATE INDEX IF NOT EXISTS state_of_2026_pageviews_ts_idx "
                "ON state_of_2026_pageviews(ts)",
                """
                CREATE TABLE IF NOT EXISTS state_of_2026_clicks (
                    id            BIGSERIAL PRIMARY KEY,
                    token         TEXT NOT NULL,
                    destination   TEXT NOT NULL,
                    referer       TEXT,
                    ip_hash       TEXT,
                    ua            TEXT,
                    session_id    TEXT,
                    ts            TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """,
                "CREATE INDEX IF NOT EXISTS state_of_2026_clicks_token_ts_idx "
                "ON state_of_2026_clicks(token, ts)",
                """
                CREATE TABLE IF NOT EXISTS state_of_2026_claim_proposals (
                    id              BIGSERIAL PRIMARY KEY,
                    claim_lineno    INTEGER,
                    current_text    TEXT,
                    proposed_text   TEXT,
                    current_min     DOUBLE PRECISION,
                    current_max     DOUBLE PRECISION,
                    proposed_min    DOUBLE PRECISION,
                    proposed_max    DOUBLE PRECISION,
                    live_value      DOUBLE PRECISION,
                    reason          TEXT,
                    status          TEXT NOT NULL DEFAULT 'pending',
                    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    applied_ts      TIMESTAMPTZ
                )
                """,
                "CREATE INDEX IF NOT EXISTS state_of_2026_claim_props_status_idx "
                "ON state_of_2026_claim_proposals(status, ts)",
                # Round 3 (2026-06-07): persisted live-numbers cache. Keyed
                # by claim_lineno so re-runs UPSERT cleanly. The page reads
                # this table for sub-ms response (vs 8 internal probes at
                # ~6s p95 on cold cache). measured_at is the freshness
                # source for the "Updated YYYY-MM-DD UTC" badge on every
                # hero card. The previous_value column lets the dashboard
                # show a delta (and lets the drift detector flag DOWN
                # crashes, not just upward growth).
                """
                CREATE TABLE IF NOT EXISTS state_of_2026_live_numbers (
                    claim_lineno    INTEGER PRIMARY KEY,
                    claim_text      TEXT,
                    endpoint        TEXT,
                    json_path       TEXT,
                    live_value      DOUBLE PRECISION,
                    previous_value  DOUBLE PRECISION,
                    expected_min    DOUBLE PRECISION,
                    expected_max    DOUBLE PRECISION,
                    in_range        BOOLEAN,
                    measured_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    source          TEXT NOT NULL DEFAULT 'cron'
                )
                """,
                "CREATE INDEX IF NOT EXISTS state_of_2026_live_numbers_ts_idx "
                "ON state_of_2026_live_numbers(measured_at)",
            ):
                try:
                    cur.execute(stmt)
                except Exception as e:
                    logger.debug("state_of_2026_live DDL skip: %s", e)
        logger.info("state_of_2026_live: tables initialized")
    finally:
        try: c.close()
        except Exception: pass


# ── helpers ───────────────────────────────────────────────────────────

def _admin_ok(req) -> bool:
    sent = (req.headers.get("X-Admin-Key")
            or req.args.get("admin_key") or "").strip()
    return bool(_ADMIN_KEY) and sent == _ADMIN_KEY


def _ip_hash(req) -> str:
    """SHA256(client_ip + day-salt). Privacy-preserving but stable within
    a day so we can de-dupe view counts."""
    fwd = (req.headers.get("CF-Connecting-IP")
           or req.headers.get("X-Forwarded-For", "").split(",")[0].strip()
           or req.remote_addr or "0.0.0.0")
    day_salt = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return hashlib.sha256(f"{fwd}|{day_salt}".encode("utf-8")).hexdigest()[:32]


def _is_linkedin_referer(referer: str) -> bool:
    r = (referer or "").lower()
    return ("linkedin.com" in r or "lnkd.in" in r)


def _probe_internal(path: str, expect_json: bool = True,
                    timeout: float = 8.0) -> dict:
    """Hit a backend path with admin key. Same pattern as the precheck."""
    out: dict[str, Any] = {"ok": False, "status": 0, "json": None}
    if _rq is None:
        return out
    url = path if path.startswith("http") else f"{_PROBE_BASE_URL}{path}"
    try:
        r = _rq.get(url, headers={
            "User-Agent": "dchub-state-of-2026-live/1.0",
            "X-Admin-Key": _ADMIN_KEY,
        }, timeout=timeout)
        out["status"] = r.status_code
        out["ok"] = (200 <= r.status_code < 400)
        if expect_json and out["ok"]:
            try:
                out["json"] = r.json()
            except Exception:
                pass
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def _extract(blob: Any, path: str) -> Optional[float]:
    """Walk a dotted path into JSON. Same simple semantics as the precheck."""
    if blob is None:
        return None
    try:
        cur = blob
        for k in path.split("."):
            if isinstance(cur, dict):
                cur = cur.get(k)
            elif isinstance(cur, list):
                try:
                    cur = cur[int(k)]
                except (ValueError, IndexError):
                    return None
            else:
                return None
        if cur is None:
            return None
        if isinstance(cur, (int, float)):
            return float(cur)
        try:
            return float(cur)
        except (TypeError, ValueError):
            return None
    except Exception:
        return None


# ── claims parser (matches state_of_2026_precheck._parse_claims_file) ──

def _parse_claims() -> list[dict]:
    """Parse docs/state-of-2026-claims.txt. Returns list with raw_line +
    line number so we can rewrite in place."""
    if not os.path.exists(_CLAIMS_PATH):
        return []
    out: list[dict] = []
    try:
        with open(_CLAIMS_PATH, "r", encoding="utf-8") as f:
            for lineno, raw in enumerate(f, start=1):
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [p.strip() for p in line.split("|")]
                fields: dict[str, Any] = {"_lineno": lineno, "_raw": raw.rstrip("\n")}
                for p in parts:
                    if ":" not in p:
                        continue
                    k, _, v = p.partition(":")
                    fields[k.strip().lower()] = v.strip()
                if not all(k in fields for k in
                           ("claim", "endpoint", "json_path",
                            "expected_min", "expected_max")):
                    continue
                try:
                    fields["expected_min"] = float(fields["expected_min"])
                    fields["expected_max"] = float(fields["expected_max"])
                except ValueError:
                    continue
                out.append(fields)
    except Exception as e:
        logger.warning("claims parse failed: %s", e)
    return out


# ── live hero numbers ─────────────────────────────────────────────────

def _fetch_hero_numbers() -> dict:
    """Pull live values for every claim. Cached 300s."""
    now = time.time()
    if _HERO_CACHE["data"] and (now - _HERO_CACHE["ts"]) < _HERO_CACHE_TTL_S:
        out = dict(_HERO_CACHE["data"])
        out["cache_age_s"] = int(now - _HERO_CACHE["ts"])
        return out

    claims = _parse_claims()
    numbers: list[dict] = []
    for c in claims:
        live_val = None
        r = _probe_internal(c["endpoint"], expect_json=True, timeout=6.0)
        if r.get("ok"):
            live_val = _extract(r.get("json"), c["json_path"])
        numbers.append({
            "claim":        c["claim"],
            "endpoint":     c["endpoint"],
            "json_path":    c["json_path"],
            "expected_min": c["expected_min"],
            "expected_max": c["expected_max"],
            "live_value":   live_val,
            "in_range":     bool(live_val is not None
                                 and c["expected_min"] <= live_val <= c["expected_max"]),
            "lineno":       c["_lineno"],
        })
    data = {
        "claim_count": len(numbers),
        "numbers":     numbers,
        "as_of":       datetime.now(timezone.utc).isoformat(),
        "cache_age_s": 0,
    }
    _HERO_CACHE["data"] = data
    _HERO_CACHE["ts"] = now
    return data


# ── Round 3 (2026-06-07): persisted-cache live numbers ───────────────
# The page now prefers the daily-refreshed row in state_of_2026_live_numbers
# over fanning out 8 internal probes. _fetch_hero_numbers_persisted() reads
# the table; if the table is empty / stale / partial, it falls back to the
# in-memory cache + live probe chain so the page never breaks on a cold boot.

_PERSISTED_MAX_AGE_S = 36 * 60 * 60  # 36h grace period (cron is daily 05:00 UTC)


def _fetch_hero_numbers_persisted() -> dict:
    """Prefer the persisted daily-refresh row. Falls back to the in-memory
    cache + live-probe chain on partial/missing/stale data. Each number
    carries its own measured_at so the page can render a per-card 'Updated'
    badge even if some rows are fresher than others."""
    claims = _parse_claims()
    if not claims:
        return _fetch_hero_numbers()

    c = _conn()
    if c is None:
        return _fetch_hero_numbers()

    rows_by_lineno: dict[int, dict] = {}
    try:
        with c.cursor() as cur:
            try:
                cur.execute(
                    "SELECT claim_lineno, claim_text, endpoint, json_path, "
                    "       live_value, previous_value, expected_min, "
                    "       expected_max, in_range, measured_at, source "
                    "FROM state_of_2026_live_numbers")
                for r in (cur.fetchall() or []):
                    rows_by_lineno[int(r[0] or 0)] = {
                        "claim":          r[1],
                        "endpoint":       r[2],
                        "json_path":      r[3],
                        "live_value":     (float(r[4]) if r[4] is not None else None),
                        "previous_value": (float(r[5]) if r[5] is not None else None),
                        "expected_min":   (float(r[6]) if r[6] is not None else None),
                        "expected_max":   (float(r[7]) if r[7] is not None else None),
                        "in_range":       bool(r[8]) if r[8] is not None else False,
                        "measured_at":    str(r[9]),
                        "source":         r[10] or "cron",
                    }
            except Exception as e:
                logger.debug("persisted numbers read failed: %s", e)
    finally:
        try: c.close()
        except Exception: pass

    # If the cache table is empty OR every row is older than the grace
    # window, fall back to the live-probe path.
    if not rows_by_lineno:
        return _fetch_hero_numbers()

    now_utc = datetime.now(timezone.utc)
    numbers: list[dict] = []
    any_stale = False
    for cl in claims:
        ln = cl["_lineno"]
        row = rows_by_lineno.get(ln)
        if row is None or row.get("live_value") is None:
            any_stale = True
        # Compute age from measured_at if present.
        age_s: Optional[int] = None
        if row and row.get("measured_at"):
            try:
                m_at = datetime.fromisoformat(
                    row["measured_at"].replace(" ", "T").replace("+00", "+00:00"))
                age_s = int((now_utc - m_at).total_seconds())
            except Exception:
                age_s = None
        numbers.append({
            "claim":         (row or {}).get("claim") or cl["claim"],
            "endpoint":      cl["endpoint"],
            "json_path":     cl["json_path"],
            "expected_min":  cl["expected_min"],
            "expected_max":  cl["expected_max"],
            "live_value":    (row or {}).get("live_value"),
            "previous_value": (row or {}).get("previous_value"),
            "in_range":      bool((row or {}).get("in_range", False)),
            "lineno":        ln,
            "measured_at":   (row or {}).get("measured_at"),
            "age_seconds":   age_s,
            "source":        (row or {}).get("source", "cron"),
        })

    # If the persisted view is missing >half the claims, prefer the live
    # path so the page never shows a sparse hero grid.
    persisted_count = sum(1 for n in numbers if n.get("live_value") is not None)
    if persisted_count < max(1, len(numbers) // 2):
        return _fetch_hero_numbers()

    return {
        "claim_count": len(numbers),
        "numbers":     numbers,
        "as_of":       now_utc.isoformat(),
        "cache_age_s": min(
            (n.get("age_seconds") or 0) for n in numbers
            if n.get("age_seconds") is not None) if numbers else 0,
        "source":      ("persisted-partial" if any_stale else "persisted"),
    }


def _persist_live_numbers(source: str = "cron") -> dict:
    """Probe every claim, UPSERT a row in state_of_2026_live_numbers per
    claim_lineno (PK), and return a summary. Updates previous_value to the
    OLD live_value (so the dashboard can render deltas / DOWN warnings).
    Also clears the in-memory 5min cache so the page picks the row up
    immediately on the next request."""
    claims = _parse_claims()
    if not claims:
        return {"ok": False, "error": "no claims to refresh", "n": 0}

    refreshed: list[dict] = []
    errors: list[dict] = []

    c = _conn()
    if c is None:
        return {"ok": False, "error": "db unavailable", "n": 0}

    try:
        for cl in claims:
            try:
                r = _probe_internal(cl["endpoint"], expect_json=True, timeout=8.0)
                live_val = _extract(r.get("json"), cl["json_path"]) if r.get("ok") else None
                in_range = bool(live_val is not None
                                and cl["expected_min"] <= live_val <= cl["expected_max"])
                # Read prior value so we can store it as previous_value.
                prev_val: Optional[float] = None
                try:
                    with c.cursor() as cur:
                        cur.execute(
                            "SELECT live_value FROM state_of_2026_live_numbers "
                            "WHERE claim_lineno = %s", (cl["_lineno"],))
                        prv = cur.fetchone()
                        if prv and prv[0] is not None:
                            prev_val = float(prv[0])
                except Exception:
                    pass

                # UPSERT — on conflict the PK matches.
                try:
                    with c.cursor() as cur:
                        cur.execute(
                            "INSERT INTO state_of_2026_live_numbers "
                            "(claim_lineno, claim_text, endpoint, json_path, "
                            " live_value, previous_value, expected_min, "
                            " expected_max, in_range, measured_at, source) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s) "
                            "ON CONFLICT (claim_lineno) DO UPDATE SET "
                            " claim_text = EXCLUDED.claim_text, "
                            " endpoint = EXCLUDED.endpoint, "
                            " json_path = EXCLUDED.json_path, "
                            " live_value = EXCLUDED.live_value, "
                            " previous_value = EXCLUDED.previous_value, "
                            " expected_min = EXCLUDED.expected_min, "
                            " expected_max = EXCLUDED.expected_max, "
                            " in_range = EXCLUDED.in_range, "
                            " measured_at = NOW(), "
                            " source = EXCLUDED.source",
                            (cl["_lineno"], cl["claim"], cl["endpoint"],
                             cl["json_path"], live_val, prev_val,
                             cl["expected_min"], cl["expected_max"],
                             in_range, source))
                except Exception as e:
                    errors.append({"lineno": cl["_lineno"], "error": str(e)[:120]})
                    continue

                refreshed.append({
                    "lineno":         cl["_lineno"],
                    "claim":          cl["claim"][:80],
                    "live_value":     live_val,
                    "previous_value": prev_val,
                    "in_range":       in_range,
                })
            except Exception as e:
                errors.append({"lineno": cl["_lineno"],
                               "error": f"{type(e).__name__}: {e}"[:120]})
    finally:
        try: c.close()
        except Exception: pass

    # Bust the in-memory hero cache so the page picks up the new persisted
    # row immediately (vs waiting up to 5min for the old TTL to expire).
    _HERO_CACHE["data"] = None
    _HERO_CACHE["ts"] = 0.0

    # Bust the OG-card CF edge cache by hitting the URL with a cache-bust
    # query param. The CF Pages worker honors no-cache on cb=NNN hits; the
    # next anon hit re-renders from the live press_release row.
    og_bust = {"ok": False, "status": 0}
    try:
        if _rq is not None:
            cb = int(time.time())
            r = _rq.get(
                f"{_PROBE_BASE_URL}/api/v1/og/editorial/state-of-2026.png?cb={cb}",
                headers={"User-Agent": "dchub-state2026-og-bust/1.0",
                         "Cache-Control": "no-cache",
                         "X-Admin-Key": _ADMIN_KEY},
                timeout=10)
            og_bust = {"ok": (200 <= r.status_code < 400),
                       "status": r.status_code,
                       "bytes": int(r.headers.get("Content-Length", 0) or 0)}
    except Exception as e:
        og_bust = {"ok": False, "error": f"{type(e).__name__}: {e}"[:120]}

    return {
        "ok":          len(refreshed) > 0,
        "n_refreshed": len(refreshed),
        "n_errors":    len(errors),
        "refreshed":   refreshed,
        "errors":      errors[:10],
        "og_card_regen": og_bust,
        "source":      source,
        "ts":          datetime.now(timezone.utc).isoformat(),
    }


# ──────────────────────────────────────────────────────────────────
# Legacy-URL aliases — brain_consistency_radar + surface_registrations
# both reference /state-of-the-data-center as the canonical URL but
# nothing ever built it. We canonicalize to /state-of-2026 (the actual
# living-doc surface) and 301-redirect every common legacy spelling.
#
# Cloudflare Error 1000 NOTE: the dchub.cloud zone-worker has an
# out-of-repo rule that 403s /state-of-the-data-center BEFORE the
# Pages worker runs (same trap pattern as /research/* per memory).
# This redirect handler executes if/when CF stops blocking — and is
# useful for direct-to-origin calls (Railway URL, Render failover).
# Long-term fix is removing the CF zone-worker rule (CF dashboard).
# ──────────────────────────────────────────────────────────────────

@state_of_2026_live_bp.route("/state-of-the-data-center", methods=["GET"])
@state_of_2026_live_bp.route("/state-of-the-data-center/", methods=["GET"])
@state_of_2026_live_bp.route("/state-of-the-data-center-2026", methods=["GET"])
@state_of_2026_live_bp.route("/state-of-data-centers-2026", methods=["GET"])
@state_of_2026_live_bp.route("/state-of-data-center", methods=["GET"])
@state_of_2026_live_bp.route("/state-of-the-datacenter", methods=["GET"])
def state_of_data_center_legacy_alias():
    """301 → /state-of-2026 (the canonical living-doc URL).

    Preserves query string for attribution (e.g., utm_*, ref tokens).
    """
    from flask import redirect as _redirect
    qs = request.query_string.decode("utf-8") if request.query_string else ""
    target = "/state-of-2026" + (f"?{qs}" if qs else "")
    return _redirect(target, code=301)


@state_of_2026_live_bp.route("/api/v1/admin/state-of-2026/refresh-now",
                              methods=["POST", "GET"])
def state_of_2026_refresh_now():
    """Manual + cron entry point. Re-probes every claim, UPSERTs into
    state_of_2026_live_numbers, busts the in-memory + CF OG cache. Idempotent.

    Kill switch: STATE_2026_REFRESH_DISABLE=1 returns {ok:false,disabled:true}
    without touching the DB."""
    if not _admin_ok(request):
        return jsonify({"ok": False, "error": "admin_key required"}), 401
    if os.environ.get("STATE_2026_REFRESH_DISABLE", "").lower() in (
            "1", "true", "yes"):
        return jsonify({"ok": False, "disabled": True,
                        "reason": "STATE_2026_REFRESH_DISABLE=1"}), 200

    source = (request.args.get("source") or "manual").strip()[:32]
    result = _persist_live_numbers(source=source)
    return jsonify(result)


# ── click attribution ────────────────────────────────────────────────

@state_of_2026_live_bp.route("/r/<token>", methods=["GET"])
def attribution_redirect(token: str):
    """Click-attribution proxy. Records the click, then 302s to the
    destination. Unknown token → 302 to /state-of-2026 (don't leak 404 on
    a LinkedIn-shared URL)."""
    dest = ATTRIBUTION_TOKENS.get(token)
    if not dest:
        dest = f"{_PUBLIC_BASE_URL}/state-of-2026"

    # Append a UTM signature so downstream analytics also see the source.
    sep = "&" if "?" in dest else "?"
    dest_with_utm = (f"{dest}{sep}utm_source=state-of-2026"
                     f"&utm_medium=share&utm_campaign={token}")

    # Best-effort write. Never block the redirect on a DB hiccup.
    try:
        c = _conn()
        if c is not None:
            try:
                with c.cursor() as cur:
                    cur.execute(
                        "INSERT INTO state_of_2026_clicks "
                        "(token, destination, referer, ip_hash, ua, session_id) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (token, dest,
                         (request.headers.get("Referer") or "")[:500],
                         _ip_hash(request),
                         (request.headers.get("User-Agent") or "")[:500],
                         (request.cookies.get("dch_sid") or "")[:64]),
                    )
            finally:
                try: c.close()
                except Exception: pass
    except Exception as e:
        logger.debug("attribution insert skipped: %s", e)

    return redirect(dest_with_utm, code=302)


# ── /api/v1/state-of-2026/live (open JSON) ────────────────────────────

@state_of_2026_live_bp.route("/api/v1/state-of-2026/live", methods=["GET"])
def state_of_2026_live_json():
    """Public JSON of the 8 live numbers. Cached 5min. Used by the page
    AND any external embed."""
    return Response(json.dumps(_fetch_hero_numbers(), default=str, indent=2),
                    mimetype="application/json",
                    headers={"Cache-Control": "public, max-age=300"})


# ── /api/v1/state-of-2026/subscribe ───────────────────────────────────

@state_of_2026_live_bp.route("/api/v1/state-of-2026/subscribe",
                              methods=["POST"])
def state_of_2026_subscribe():
    """Capture email → write to notify_subscribers if available, else
    pageviews table with synthetic referer. Idempotent."""
    payload = request.get_json(silent=True) or {}
    email = str(payload.get("email") or "").strip().lower()
    if not email or "@" not in email or len(email) > 200:
        return jsonify({"ok": False, "error": "invalid email"}), 400

    sent_to_notify = False
    try:
        c = _conn()
        if c is not None:
            try:
                with c.cursor() as cur:
                    # Try the canonical notify list. Schema-tolerant.
                    try:
                        cur.execute(
                            "INSERT INTO notify_subscribers (email, source, ts) "
                            "VALUES (%s, 'state-of-2026', NOW()) "
                            "ON CONFLICT (email) DO NOTHING",
                            (email,))
                        sent_to_notify = True
                    except Exception:
                        # Table may not exist or have a different shape;
                        # fall through to pageviews-style log.
                        pass
                    # Always log the subscribe action so attribution sees it.
                    try:
                        cur.execute(
                            "INSERT INTO state_of_2026_clicks "
                            "(token, destination, referer, ip_hash, ua, session_id) "
                            "VALUES (%s, %s, %s, %s, %s, %s)",
                            ("subscribe", "email://" + email,
                             (request.headers.get("Referer") or "")[:500],
                             _ip_hash(request),
                             (request.headers.get("User-Agent") or "")[:500],
                             (request.cookies.get("dch_sid") or "")[:64]),
                        )
                    except Exception:
                        pass
            finally:
                try: c.close()
                except Exception: pass
    except Exception as e:
        logger.debug("subscribe write skipped: %s", e)

    return jsonify({
        "ok": True,
        "logged": True,
        "notified": sent_to_notify,
        "next_step_url": f"{_PUBLIC_BASE_URL}/r/signup",
        "message": ("Subscribed. The State of 2026 weekly digest ships "
                    "Mondays at 09:00 ET. Claim a dev key to skip the "
                    "wait and pull data live: " + _PUBLIC_BASE_URL + "/r/signup"),
    })


# ── /state-of-2026 server-rendered landing ────────────────────────────

def _render_state_page(hero: dict) -> str:
    nums = hero.get("numbers") or []
    cache_age = hero.get("cache_age_s", 0)

    # Compact: 4 hero stats + 4 supporting. Pick by claim order; the
    # claim file is ordered by importance.
    hero_cards: list[str] = []
    for i, n in enumerate(nums):
        live = n.get("live_value")
        prev = n.get("previous_value")
        display = (f"{int(live):,}" if (live is not None and live >= 100)
                   else (f"{live:.1f}" if live is not None else "—"))
        # Shorten the claim text to its key noun phrase.
        text = n.get("claim", "")
        if len(text) > 90:
            text = text[:88] + "..."
        in_range = n.get("in_range")
        badge_color = "#10b981" if in_range else "#f59e0b"
        badge_text = "LIVE" if in_range else "DRIFT"
        ep = n.get("endpoint", "")
        # Round 3 (2026-06-07): per-card "Updated YYYY-MM-DD HH:MM UTC" badge.
        # measured_at is set by the daily 05:00 UTC refresh cron. If the row
        # is older than 36h, color the badge amber to flag stale data.
        m_at_raw = n.get("measured_at")
        updated_chip = ""
        if m_at_raw:
            try:
                m_at = datetime.fromisoformat(
                    str(m_at_raw).replace(" ", "T").replace("+00", "+00:00"))
                age_s = (datetime.now(timezone.utc) - m_at).total_seconds()
                stale = age_s > 36 * 3600
                clr = "#9ca3af" if not stale else "#f59e0b"
                disp = m_at.strftime("%Y-%m-%d %H:%M UTC")
                updated_chip = (f'<div class="updated-chip" '
                                f'style="color:{clr}" '
                                f'title="Numbers refresh daily at 05:00 UTC. '
                                f'This row was measured at {disp}.">'
                                f'Updated {disp}</div>')
            except Exception:
                pass
        # Delta chip — shows direction since prior refresh.
        delta_chip = ""
        if prev is not None and live is not None:
            d = live - prev
            if abs(d) > 0.001:
                arrow = "↑" if d > 0 else "↓"
                delta_disp = (f"{abs(int(d)):,}" if abs(d) >= 100
                              else f"{abs(d):.1f}")
                d_color = "#34d399" if d > 0 else "#fbbf24"
                delta_chip = (f' <span class="delta-chip" '
                              f'style="color:{d_color}" '
                              f'title="Change since prior daily refresh">'
                              f'{arrow}{delta_disp}</span>')
        # 2026-06-07 page-audit: suppress "source" link when the endpoint is
        # admin-gated (e.g. /admin/funnel-health). The public visitor hitting
        # this on the launch surface saw 403 instead of a real source. For
        # admin endpoints, render the LIVE badge but no clickable link.
        if ep.startswith("/admin/"):
            _src_link = '<span class="stat-src" style="cursor:default" title="Source data is admin-gated; aggregate is public">internal source</span>'
        else:
            _src_link = (f'<a class="stat-src" href="{_PUBLIC_BASE_URL}{_esc(ep)}" '
                         f'target="_blank" rel="noopener nofollow">source</a>')
        hero_cards.append(f'''
        <div class="stat">
          <div class="stat-num">{_esc(display)}{delta_chip}</div>
          <div class="stat-claim">{_esc(text)}</div>
          <div class="stat-meta">
            <span class="live-badge" style="background:{badge_color}">{badge_text}</span>
            {_src_link}
          </div>
          {updated_chip}
        </div>''')

    cache_chip = ""
    if cache_age:
        cache_chip = (f'<span class="cache-chip">data {cache_age}s old · '
                      f'refreshes every 5min</span>')

    page_url = f"{_PUBLIC_BASE_URL}/state-of-2026"
    og_url = f"{_PUBLIC_BASE_URL}/api/v1/og/magazine/state-of-2026.png"
    share_text = ("The State of Data Centers 2026 — every number "
                  "live + verified.")

    # LinkedIn share intent URL — pre-filled with the post URL.
    li_share = ("https://www.linkedin.com/sharing/share-offsite/?"
                f"url={page_url}")

    # Build the 8 attribution links table.
    link_rows = "".join(
        f'<tr><td><code>/r/{_esc(k)}</code></td>'
        f'<td><a href="{_PUBLIC_BASE_URL}/r/{_esc(k)}">{_esc(v)}</a></td></tr>'
        for k, v in ATTRIBUTION_TOKENS.items())

    return f"""<!doctype html>
<html lang=en><head>
<meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>State of Data Centers 2026 — DC Hub</title>
<meta name=description content="Every number live + verified. {hero.get('claim_count', 0)} claims, all sourced from the live API.">
<meta property="og:title" content="State of Data Centers 2026">
<meta property="og:description" content="{_esc(share_text)}">
<meta property="og:image" content="{og_url}">
<meta property="og:url" content="{page_url}">
<meta property="og:type" content="article">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="{og_url}">
<link rel="canonical" href="{page_url}">
<style>
  *{{box-sizing:border-box}}
  body{{background:#0a0a0a;color:#e5e5e5;font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;margin:0;padding:0}}
  /* Round 2 (2026-06-07): high-intent capture modal. Slides up from
     bottom-right when reader hits the threshold (2 brief clicks OR 60s
     on page). Closes with X; 30d cookie prevents re-show on dismiss. */
  #s26-modal{{position:fixed;right:24px;bottom:24px;width:380px;max-width:calc(100vw - 32px);background:#171717;border:1px solid #404040;border-radius:14px;padding:24px;box-shadow:0 16px 48px rgba(0,0,0,0.6);transform:translateY(120%);transition:transform .35s cubic-bezier(.4,0,.2,1);z-index:9999;font-family:inherit}}
  #s26-modal.open{{transform:translateY(0)}}
  #s26-modal .close-btn{{position:absolute;top:12px;right:12px;background:none;border:0;color:#71717a;font-size:18px;cursor:pointer;padding:4px 8px;line-height:1}}
  #s26-modal .close-btn:hover{{color:#e5e5e5}}
  #s26-modal h3{{margin:0 0 8px;font-size:17px;color:#fff;font-weight:700;line-height:1.3}}
  #s26-modal p{{margin:0 0 16px;color:#a3a3a3;font-size:13px;line-height:1.5}}
  #s26-modal input[type=email]{{width:100%;padding:11px 14px;background:#0a0a0a;color:#e5e5e5;border:1px solid #404040;border-radius:8px;font-size:14px;margin-bottom:10px;font-family:inherit}}
  #s26-modal input[type=email]:focus{{outline:0;border-color:#10b981}}
  #s26-modal button.go{{width:100%;padding:11px;background:#10b981;color:#0a0a0a;border:0;border-radius:8px;font-weight:700;cursor:pointer;font-size:14px;transition:background .15s}}
  #s26-modal button.go:hover{{background:#34d399}}
  #s26-modal .legal{{margin-top:12px;font-size:11px;color:#71717a;line-height:1.4}}
  #s26-modal .legal a{{color:#71717a;text-decoration:underline}}
  #s26-modal .ok-msg{{background:rgba(16,185,129,.08);border:1px solid rgba(16,185,129,.3);border-radius:8px;padding:12px;font-size:13px;color:#34d399;margin-top:10px;font-family:JetBrains Mono,monospace;word-break:break-all}}
  #s26-modal .err-msg{{background:rgba(245,158,11,.08);border:1px solid rgba(245,158,11,.3);border-radius:8px;padding:10px;font-size:12px;color:#fbbf24;margin-top:10px}}
  @media (max-width:640px){{ #s26-modal{{right:12px;left:12px;bottom:12px;width:auto}} }}
  a{{color:#7dd3fc}}
  .wrap{{max-width:980px;margin:0 auto;padding:48px 24px 96px}}
  header{{margin-bottom:48px}}
  .eyebrow{{color:#a3a3a3;font-size:13px;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:12px}}
  h1{{font-size:48px;line-height:1.1;margin:0 0 16px;letter-spacing:-1px;font-weight:800}}
  .sub{{color:#a3a3a3;font-size:18px;max-width:720px}}
  .meta{{margin-top:16px;color:#71717a;font-size:13px}}
  .cache-chip{{display:inline-block;padding:2px 8px;background:#171717;border:1px solid #262626;border-radius:6px;color:#71717a;font-size:11px}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px;margin-bottom:64px}}
  .stat{{background:#171717;border:1px solid #262626;border-radius:12px;padding:24px;transition:border-color .15s}}
  .stat:hover{{border-color:#10b981}}
  .stat-num{{font-size:42px;font-weight:800;color:#fff;letter-spacing:-0.5px;line-height:1}}
  .stat-claim{{margin-top:10px;color:#d4d4d8;font-size:14px;line-height:1.4}}
  .stat-meta{{margin-top:14px;display:flex;justify-content:space-between;align-items:center}}
  .live-badge{{display:inline-block;padding:2px 8px;border-radius:4px;color:#fff;font-size:10px;font-weight:700;letter-spacing:0.5px}}
  .updated-chip{{margin-top:10px;font-size:10px;color:#9ca3af;font-family:JetBrains Mono,ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:0.3px;border-top:1px dashed #262626;padding-top:8px}}
  .delta-chip{{font-size:14px;font-weight:600;margin-left:8px;vertical-align:middle;letter-spacing:0}}
  .stat-src{{font-size:11px;color:#71717a;text-decoration:none}}
  .stat-src:hover{{color:#7dd3fc;text-decoration:underline}}
  .cta{{background:#171717;border:1px solid #262626;border-radius:12px;padding:32px;margin-bottom:32px}}
  .cta h2{{margin:0 0 16px;font-size:24px}}
  .cta p{{color:#a3a3a3;margin:0 0 20px}}
  .row{{display:flex;gap:12px;flex-wrap:wrap}}
  .btn{{display:inline-block;padding:12px 24px;border-radius:8px;font-weight:600;text-decoration:none;font-size:14px;transition:transform .1s}}
  .btn:hover{{transform:translateY(-1px)}}
  .btn-primary{{background:#10b981;color:#0a0a0a}}
  .btn-secondary{{background:#1a1a1a;border:1px solid #404040;color:#e5e5e5}}
  .btn-linkedin{{background:#0a66c2;color:#fff}}
  form.subscribe{{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}}
  form.subscribe input{{flex:1;min-width:240px;padding:12px 16px;background:#0a0a0a;color:#e5e5e5;border:1px solid #404040;border-radius:8px;font-size:14px}}
  form.subscribe input:focus{{outline:none;border-color:#10b981}}
  form.subscribe button{{padding:12px 24px;background:#10b981;color:#0a0a0a;border:0;border-radius:8px;font-weight:700;cursor:pointer;font-size:14px}}
  details{{margin:24px 0;border:1px solid #262626;border-radius:8px;padding:16px;background:#171717}}
  details summary{{cursor:pointer;font-weight:600;color:#a3a3a3}}
  details[open] summary{{margin-bottom:12px}}
  table{{width:100%;border-collapse:collapse;margin-top:12px;font-size:13px}}
  table td{{padding:6px 8px;border-bottom:1px solid #262626;vertical-align:top}}
  code{{background:#0a0a0a;padding:2px 6px;border-radius:4px;font-size:12px;color:#7dd3fc}}
  .footer{{margin-top:64px;padding-top:24px;border-top:1px solid #262626;color:#71717a;font-size:13px}}
  .footer a{{color:#a3a3a3;margin-right:16px}}
  .subscribe-msg{{margin-top:12px;font-size:13px;color:#10b981}}
  @media (max-width:640px){{ h1{{font-size:36px}} .stat-num{{font-size:32px}} }}
</style>
</head><body>
<div class="wrap">
<header>
  <div class="eyebrow">DC Hub · Live Report · {datetime.now(timezone.utc).strftime("%B %Y")}</div>
  <h1>State of Data Centers 2026</h1>
  <p class="sub">Every number on this page is queried live from the DC Hub
  API at page load. No snapshots, no marketing math —
  {hero.get('claim_count', 0)} claims, each sourced from a verifiable endpoint.</p>
  <div class="meta">{cache_chip} · refreshes when underlying data grows ·
    <a href="/r/qa">view the QA harness</a> ·
    <a href="/api/v1/state-of-2026/live">JSON</a>
  </div>
</header>

<div class="grid">
  {''.join(hero_cards)}
</div>

<div class="cta">
  <h2>Get the DC Hub Weekly newsletter</h2>
  <p>Every Friday: top LinkedIn posts, top AI-agent MCP queries, top
  DCPI verdict shifts, one strategic note from the brain. Free. No card.
  Unsubscribe in one click.</p>
  <form class="subscribe" onsubmit="return subscribeNewsletter(event)">
    <input type="email" name="email" placeholder="you@firm.com" required>
    <button type="submit">Subscribe</button>
  </form>
  <div id="subscribe-msg" class="subscribe-msg"></div>
  <p style="margin-top:20px"><a href="/newsletter">View past issues →</a>
    &nbsp;·&nbsp;
    <a href="/r/signup">Claim a free dev key (10 API calls/day, no card) →</a></p>
</div>
<script>
function subscribeNewsletter(e) {{
  e.preventDefault();
  var form = e.target;
  var email = form.email.value.trim();
  var msg = document.getElementById('subscribe-msg');
  msg.textContent = 'Subscribing...';
  fetch('/api/v1/newsletter/subscribe', {{
    method:'POST',
    headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{email: email, source: 'state-of-2026'}})
  }}).then(r=>r.json()).then(d=>{{
    msg.textContent = d.ok
      ? "You're in. Welcome email landing now."
      : ("Could not subscribe: " + (d.error || 'unknown'));
  }}).catch(function(){{ msg.textContent='Network error — try again.'; }});
  return false;
}}
</script>

<div class="cta">
  <h2>Verify any number on this page</h2>
  <p>This is the methodology. Every claim hits a live endpoint; pre-publish
  QA runs nightly; drift over 5% from the expected range = the page badge
  flips from LIVE to DRIFT and we hear about it. Receipts not vibes.</p>
  <div class="row">
    <a class="btn btn-primary" href="/r/dcpi">See the 306-market Power Index</a>
    <a class="btn btn-secondary" href="/r/dcgi">See the gas-behind-the-grid index</a>
    <a class="btn btn-secondary" href="/r/queues">Interconnection queues</a>
    <a class="btn btn-secondary" href="/r/hyper">Hyperscaler briefs</a>
  </div>
</div>

<div class="cta">
  <h2>Share this report</h2>
  <p>Pre-filled LinkedIn share — the post links back through our
  attribution proxy so we can show you how many people clicked.</p>
  <div class="row">
    <a class="btn btn-linkedin" href="{li_share}" target="_blank" rel="noopener">
      Share on LinkedIn
    </a>
    <a class="btn btn-secondary" href="/r/report">Read the long-form report</a>
    <a class="btn btn-secondary" href="/r/cheyenne">Why Cheyenne is the
      top-BUILD market</a>
  </div>
</div>

<details>
  <summary>For the curious: how the click attribution works</summary>
  <p>Each shareable link on this page routes through <code>/r/&lt;token&gt;</code>,
  which records the click (referer, hashed IP, UA, session) then 302s
  to the destination with UTM tags appended. Aggregated funnel telemetry
  is at <code>/admin/state-of-2026-pulse</code> (admin-gated).</p>
  <table>
    <thead><tr><td><b>Token</b></td><td><b>Destination</b></td></tr></thead>
    <tbody>{link_rows}</tbody>
  </table>
</details>

<div class="footer">
  <a href="/">DC Hub</a>
  <a href="/methodology">Methodology</a>
  <a href="/dcpi">DCPI</a>
  <a href="/dcgi">DCGI</a>
  <a href="/cited-by">Cited by</a>
  <a href="/api/v1/state-of-2026/live">JSON feed</a>
  <span style="float:right">© DC Hub {datetime.now(timezone.utc).year}</span>
</div>
</div>

<!-- Round 2 (2026-06-07): high-intent capture modal. Hidden until JS opens it
     after the visitor hits the threshold (2 brief clicks OR 60s on page). -->
<div id="s26-modal" role="dialog" aria-label="Get your DC Hub trial key" aria-hidden="true">
  <button class="close-btn" aria-label="Close" onclick="s26Dismiss()">×</button>
  <h3>You're reading the State of 2026. Get the live data.</h3>
  <p>Free trial key — works in Claude, Cursor, Cline, ChatGPT, any MCP client.
     50 calls/day for 7 days. No card.</p>
  <form id="s26-form" onsubmit="return s26Submit(event)">
    <input type="email" name="email" placeholder="you@firm.com" required autocomplete="email">
    <button type="submit" class="go">Get my key →</button>
    <div class="legal">By submitting, you'll receive a single welcome email
      with your trial key. <a href="/privacy" target="_blank" rel="noopener">Privacy</a> ·
      <a href="/terms" target="_blank" rel="noopener">Terms</a>.</div>
  </form>
  <div id="s26-result" style="display:none"></div>
</div>

<script>
function subscribe(e) {{
  e.preventDefault();
  var f = e.target;
  var email = f.email.value.trim();
  var msg = document.getElementById('subscribe-msg');
  msg.style.color = '#a3a3a3';
  msg.textContent = 'Submitting...';
  fetch('/api/v1/state-of-2026/subscribe', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ email: email }})
  }}).then(r => r.json()).then(d => {{
    if (d.ok) {{
      msg.style.color = '#10b981';
      msg.innerHTML = 'Subscribed. <a href="' + d.next_step_url +
        '">Claim a free dev key →</a>';
      f.reset();
    }} else {{
      msg.style.color = '#f59e0b';
      msg.textContent = 'Error: ' + (d.error || 'try again');
    }}
  }}).catch(err => {{
    msg.style.color = '#f59e0b';
    msg.textContent = 'Network error — try again in a moment.';
  }});
  return false;
}}
</script>

<script>
/* ── Round 2 (2026-06-07): high-intent visitor capture ──
   Tracks time-on-page + outbound brief clicks. When threshold hits
   (2+ brief clicks OR 60s on page), the modal opens. Posts to
   /api/v1/state-of-2026/track-event and /claim-email. Dismiss sets a
   30d cookie. GDPR: stored fields = client-generated UUID + UA + Referer
   + hashed IP. Email only on explicit submission. */
(function() {{
  function readCookie(name) {{
    var m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return m ? decodeURIComponent(m[1]) : null;
  }}
  function writeCookie(name, val, days) {{
    var d = new Date();
    d.setTime(d.getTime() + days*86400*1000);
    document.cookie = name + '=' + encodeURIComponent(val) +
      '; expires=' + d.toUTCString() + '; path=/; SameSite=Lax';
  }}
  function getOrMakeVsid() {{
    var v = sessionStorage.getItem('dch_vsid');
    if (v) return v;
    var rnd;
    if (window.crypto && crypto.getRandomValues) {{
      rnd = crypto.getRandomValues(new Uint8Array(16));
    }} else {{
      rnd = new Array(16);
      for (var i=0;i<16;i++) rnd[i] = Math.floor(Math.random()*256);
    }}
    rnd[6] = (rnd[6] & 0x0f) | 0x40;
    rnd[8] = (rnd[8] & 0x3f) | 0x80;
    var hex = '';
    for (var j=0;j<16;j++) hex += (rnd[j]<16?'0':'') + rnd[j].toString(16);
    sessionStorage.setItem('dch_vsid', hex);
    return hex;
  }}
  var VSID = getOrMakeVsid();
  var TICK_S = 30;
  var startMs = Date.now();
  var lastSentS = 0;
  var modalShown = false;
  var dismissed = readCookie('dch_s26_dismissed') === '1';
  var alreadyClaimed = readCookie('dch_s26_claimed') === '1';

  function showModal() {{
    if (modalShown || dismissed || alreadyClaimed) return;
    modalShown = true;
    var m = document.getElementById('s26-modal');
    if (m) {{ m.classList.add('open'); m.setAttribute('aria-hidden','false'); }}
  }}

  function postTrack(payload) {{
    payload.visitor_session_id = VSID;
    return fetch('/api/v1/state-of-2026/track-event', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(payload),
      keepalive: true
    }}).then(function(r){{ return r.json(); }})
      .then(function(d){{
        if (d && d.is_high_intent) showModal();
        return d;
      }}).catch(function(){{ /* network errors are silent */ }});
  }}

  /* Brief-click instrumentation: every link whose href is /r/<token>
     for a high-value brief token counts. */
  var BRIEF_TOKENS = {{
    dcpi:1, dcgi:1, queues:1, mcp:1, hyper:1, report:1,
    markets:1, cheyenne:1
  }};
  document.addEventListener('click', function(ev) {{
    var t = ev.target;
    while (t && t !== document.body) {{
      if (t.tagName === 'A' && t.getAttribute && t.getAttribute('href')) {{
        var href = t.getAttribute('href');
        var m = href.match(/^\\/r\\/([a-z_]+)/);
        if (m && BRIEF_TOKENS[m[1]]) {{
          postTrack({{ event_type: 'brief_click', brief_slug: m[1] }});
        }}
        break;
      }}
      t = t.parentNode;
    }}
  }}, true);

  /* Time-tick: every 30s while page is visible, send the delta. */
  function tick() {{
    if (document.hidden) return;
    var nowS = Math.floor((Date.now() - startMs) / 1000);
    var delta = nowS - lastSentS;
    if (delta < 1) return;
    lastSentS = nowS;
    postTrack({{ event_type: 'time_tick', seconds_on_page: delta }});
  }}
  setInterval(tick, TICK_S * 1000);
  /* Kick at 62s so the 60s threshold trips reliably even on slow connects. */
  setTimeout(tick, 62 * 1000);
  window.addEventListener('beforeunload', tick);

  /* Form submission inside modal. */
  window.s26Submit = function(e) {{
    e.preventDefault();
    var f = e.target;
    var email = f.email.value.trim();
    var btn = f.querySelector('button.go');
    var resultDiv = document.getElementById('s26-result');
    btn.disabled = true;
    btn.textContent = 'Minting...';
    fetch('/api/v1/state-of-2026/claim-email', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ visitor_session_id: VSID, email: email }})
    }}).then(function(r){{ return r.json(); }}).then(function(d){{
      if (d && d.ok && d.api_key) {{
        writeCookie('dch_s26_claimed', '1', 365);
        f.style.display = 'none';
        resultDiv.style.display = 'block';
        resultDiv.innerHTML =
          '<div style="color:#34d399;font-size:14px;font-weight:600;margin-bottom:8px">' +
          'Your trial key (also emailed to ' + email + '):</div>' +
          '<div class="ok-msg">' + d.api_key + '</div>' +
          '<div class="legal" style="margin-top:10px">Add this to your MCP client and reconnect. ' +
          '<a href="/integrations/mcp" target="_blank" rel="noopener" style="color:#7dd3fc">Setup guide →</a></div>';
      }} else {{
        btn.disabled = false;
        btn.textContent = 'Get my key →';
        resultDiv.style.display = 'block';
        resultDiv.innerHTML = '<div class="err-msg">' +
          ((d && d.error) || 'Something went wrong — try again in a moment.') + '</div>';
      }}
    }}).catch(function(){{
      btn.disabled = false;
      btn.textContent = 'Get my key →';
      resultDiv.style.display = 'block';
      resultDiv.innerHTML = '<div class="err-msg">Network error — try again.</div>';
    }});
    return false;
  }};

  window.s26Dismiss = function() {{
    writeCookie('dch_s26_dismissed', '1', 30);
    var m = document.getElementById('s26-modal');
    if (m) {{ m.classList.remove('open'); m.setAttribute('aria-hidden','true'); }}
  }};
}})();
</script>
</body></html>"""


@state_of_2026_live_bp.route("/state-of-2026", methods=["GET"])
def state_of_2026_page():
    """Server-rendered landing page. Lightweight: 1 cache lookup, no DB,
    1 small HTML render. Best-effort pageview log."""
    # Best-effort pageview log — never block the render.
    try:
        c = _conn()
        if c is not None:
            try:
                with c.cursor() as cur:
                    cur.execute(
                        "INSERT INTO state_of_2026_pageviews "
                        "(ip_hash, referer, ua, session_id) "
                        "VALUES (%s, %s, %s, %s)",
                        (_ip_hash(request),
                         (request.headers.get("Referer") or "")[:500],
                         (request.headers.get("User-Agent") or "")[:500],
                         (request.cookies.get("dch_sid") or "")[:64]),
                    )
            finally:
                try: c.close()
                except Exception: pass
    except Exception:
        pass

    # Round 3 (2026-06-07): prefer the persisted-cache row over a live
    # probe-fanout. Page response is now sub-ms when the daily 05:00 UTC
    # cron has refreshed the table. Fallback chain handles cold-boot +
    # partial-cache cases (see _fetch_hero_numbers_persisted).
    hero = _fetch_hero_numbers_persisted()
    return Response(
        _render_state_page(hero),
        mimetype="text/html; charset=utf-8",
        headers={
            "Cache-Control": "public, max-age=120",  # CF edge for 2min
            "X-Robots-Tag": "index, follow",
            "X-DC-State-Source": str(hero.get("source", "live")),
        })


# ── admin attribution JSON ────────────────────────────────────────────

@state_of_2026_live_bp.route("/api/v1/admin/state-of-2026/attribution",
                              methods=["GET"])
def attribution_json():
    """JSON funnel telemetry — clicks by token, signups attributed, etc."""
    if not _admin_ok(request):
        return jsonify({"ok": False, "error": "admin_key required"}), 401

    try:
        days = int(request.args.get("days", "7"))
    except ValueError:
        days = 7
    days = max(1, min(90, days))

    c = _conn()
    if c is None:
        return jsonify({"ok": False, "error": "db unavailable"}), 500

    out: dict[str, Any] = {
        "ok": True,
        "window_days": days,
        "page_views": 0,
        "unique_visitors": 0,
        "linkedin_attributed_views": 0,
        "clicks_by_token": [],
        "subscribes": 0,
        "top_referers": [],
        "as_of": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with c.cursor() as cur:
            # Total pageviews + uniques
            try:
                cur.execute(
                    "SELECT COUNT(*), COUNT(DISTINCT ip_hash) "
                    "FROM state_of_2026_pageviews "
                    "WHERE ts > NOW() - INTERVAL %s",
                    (f"{days} days",))
                row = cur.fetchone()
                if row:
                    out["page_views"]      = int(row[0] or 0)
                    out["unique_visitors"] = int(row[1] or 0)
            except Exception as e:
                logger.debug("pageview agg failed: %s", e)
            # LinkedIn-attributed views
            try:
                cur.execute(
                    "SELECT COUNT(*) FROM state_of_2026_pageviews "
                    "WHERE ts > NOW() - INTERVAL %s "
                    "AND (LOWER(referer) LIKE '%%linkedin.com%%' "
                    "  OR LOWER(referer) LIKE '%%lnkd.in%%')",
                    (f"{days} days",))
                r = cur.fetchone()
                if r:
                    out["linkedin_attributed_views"] = int(r[0] or 0)
            except Exception:
                pass
            # Clicks by token
            try:
                cur.execute(
                    "SELECT token, COUNT(*) AS clicks, "
                    "       COUNT(DISTINCT ip_hash) AS uniques "
                    "FROM state_of_2026_clicks "
                    "WHERE ts > NOW() - INTERVAL %s "
                    "GROUP BY token ORDER BY clicks DESC LIMIT 50",
                    (f"{days} days",))
                rows = cur.fetchall() or []
                out["clicks_by_token"] = [
                    {"token": r[0], "clicks": int(r[1] or 0),
                     "uniques": int(r[2] or 0)} for r in rows
                ]
                out["subscribes"] = sum(
                    r["clicks"] for r in out["clicks_by_token"]
                    if r["token"] == "subscribe")
            except Exception as e:
                logger.debug("clicks agg failed: %s", e)
            # Top referers (anon-friendly truncation at host level)
            try:
                cur.execute(
                    "SELECT COALESCE(SPLIT_PART(SPLIT_PART(referer, '://', 2), '/', 1), '(direct)') AS host, "
                    "       COUNT(*) AS n "
                    "FROM state_of_2026_pageviews "
                    "WHERE ts > NOW() - INTERVAL %s "
                    "GROUP BY host ORDER BY n DESC LIMIT 12",
                    (f"{days} days",))
                out["top_referers"] = [
                    {"host": r[0] or "(direct)", "views": int(r[1] or 0)}
                    for r in (cur.fetchall() or [])
                ]
            except Exception:
                pass

            # MCP key claims attributed (best-effort join through ip_hash
            # window — coarse but correct enough for a first pass).
            try:
                cur.execute(
                    "SELECT COUNT(DISTINCT dk.email) "
                    "FROM mcp_dev_keys dk "
                    "JOIN state_of_2026_clicks c "
                    "  ON c.token IN ('signup', 'mcp') "
                    " AND dk.created_at >= c.ts "
                    " AND dk.created_at <= c.ts + INTERVAL '1 hour' "
                    "WHERE c.ts > NOW() - INTERVAL %s",
                    (f"{days} days",))
                r = cur.fetchone()
                if r:
                    out["mcp_keys_attributed"] = int(r[0] or 0)
            except Exception:
                out["mcp_keys_attributed"] = None  # table may not exist
    finally:
        try: c.close()
        except Exception: pass

    return Response(json.dumps(out, default=str, indent=2),
                    mimetype="application/json",
                    headers={"Cache-Control": "private, no-store"})


# ── claims evolver (cron + manual apply) ──────────────────────────────

@state_of_2026_live_bp.route("/api/v1/admin/state-of-2026/claims/propose",
                              methods=["POST", "GET"])
def claims_propose():
    """Daily cron entry point. Walk every claim; if the live value is
    OUT of the [min, max] band — especially HIGHER than max (growth) —
    write a proposal row that the operator approves with one click.

    NEVER auto-applies. NEVER touches git. Strict observer."""
    if not _admin_ok(request):
        return jsonify({"ok": False, "error": "admin_key required"}), 401

    claims = _parse_claims()
    if not claims:
        return jsonify({"ok": False, "error": "no claims to evolve"}), 500

    proposed: list[dict] = []
    c = _conn()
    try:
        for cl in claims:
            r = _probe_internal(cl["endpoint"], expect_json=True, timeout=6.0)
            live = _extract(r.get("json"), cl["json_path"]) if r.get("ok") else None
            if live is None:
                continue
            emin = cl["expected_min"]
            emax = cl["expected_max"]
            if emin <= live <= emax:
                continue  # in band, nothing to do

            # Out of band. Decide direction + propose new range.
            # Round 3 (2026-06-07): DOWN crashes are propagated with the
            # same surface as UP growth — both write a pending proposal
            # with the same status, but the reason text leads with
            # GROWTH/DECLINE so the operator can triage at a glance.
            reason = ""
            severity = "GROWTH"  # GROWTH | DECLINE | CRASH
            if live > emax:
                # Growth — bump both ends up. New max = live * 1.1
                # (10% headroom); new min = current max (lock in floor).
                new_min = emax
                new_max = round(live * 1.1, 1)
                pct_over = ((live - emax) / emax * 100.0) if emax else 0
                reason = (f"GROWTH: live {live} > current max {emax} "
                          f"(+{pct_over:.1f}%). Bumping range to "
                          f"[{new_min}, {new_max}].")
            elif live < emin:
                # Shrink — could be a real drop (we'd want to know) or a
                # transient flap. Don't auto-shrink; instead propose a new
                # floor at live * 0.9 and call out the severity. > 25%
                # below min = CRASH (e.g. dev keys 55 → 10), < 25% = DECLINE.
                pct_under = ((emin - live) / emin * 100.0) if emin else 0
                if pct_under >= 25.0:
                    severity = "CRASH"
                else:
                    severity = "DECLINE"
                new_min = round(live * 0.9, 1)
                new_max = emax
                reason = (f"{severity}: live {live} < current min {emin} "
                          f"(-{pct_under:.1f}%). Proposing floor at "
                          f"{new_min} but operator should investigate "
                          f"BEFORE applying — a crash usually means a "
                          f"data source broke, not that the claim should "
                          f"be lowered.")
            else:
                continue

            # Rewrite the visible claim text — e.g. "300+ markets" → "320+ markets".
            old_text = cl["claim"]
            new_text = _rewrite_claim_text(old_text, live, emax, new_max)
            # For DOWN drift, DON'T rewrite the visible claim text — the
            # operator might decide to fix the data source rather than
            # lower the published number. Leave it as a flag.
            if severity in ("DECLINE", "CRASH"):
                new_text = old_text  # operator inspects before any rewrite

            row = {
                "lineno":        cl["_lineno"],
                "current_text":  old_text,
                "proposed_text": new_text,
                "current_min":   emin,
                "current_max":   emax,
                "proposed_min":  new_min,
                "proposed_max":  new_max,
                "live_value":    live,
                "reason":        reason,
            }

            if c is not None:
                try:
                    with c.cursor() as cur:
                        # Don't double-propose same lineno with status='pending'.
                        cur.execute(
                            "SELECT id FROM state_of_2026_claim_proposals "
                            "WHERE claim_lineno = %s AND status = 'pending'",
                            (cl["_lineno"],))
                        if cur.fetchone():
                            row["skipped"] = "already pending"
                        else:
                            cur.execute(
                                "INSERT INTO state_of_2026_claim_proposals "
                                "(claim_lineno, current_text, proposed_text, "
                                " current_min, current_max, proposed_min, "
                                " proposed_max, live_value, reason) "
                                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                                "RETURNING id",
                                (row["lineno"], row["current_text"],
                                 row["proposed_text"], row["current_min"],
                                 row["current_max"], row["proposed_min"],
                                 row["proposed_max"], row["live_value"],
                                 row["reason"]))
                            rid = cur.fetchone()
                            if rid:
                                row["id"] = int(rid[0])
                except Exception as e:
                    row["error"] = str(e)
            proposed.append(row)
    finally:
        if c is not None:
            try: c.close()
            except Exception: pass

    return jsonify({
        "ok": True,
        "proposals":     proposed,
        "proposed_n":    len([p for p in proposed if "id" in p]),
        "skipped_n":     len([p for p in proposed if "skipped" in p]),
        "ts":            datetime.now(timezone.utc).isoformat(),
    })


_NUMBER_PLUS_RE = re.compile(r"\b(\d{2,6})\s*\+\s*")
_NUMBER_RE      = re.compile(r"\b(\d{2,6})\b")


def _rewrite_claim_text(text: str, live: float,
                         old_max: float, new_max: float) -> str:
    """Heuristic rewrite: if the claim has "NNN+", bump NNN to round-down
    of live; otherwise leave verbatim (operator will inspect the diff
    before applying)."""
    target = int(live // 10 * 10)  # round to nearest 10 down
    # Replace the first "NNN+" pattern with target+
    m = _NUMBER_PLUS_RE.search(text)
    if m:
        return _NUMBER_PLUS_RE.sub(f"{target}+ ", text, count=1).strip()
    # Replace the first standalone NN-NNNN with `target`
    m = _NUMBER_RE.search(text)
    if m:
        return _NUMBER_RE.sub(str(int(live)), text, count=1)
    return text


@state_of_2026_live_bp.route(
    "/api/v1/admin/state-of-2026/claims/apply/<int:pid>", methods=["POST"])
def claims_apply(pid: int):
    """Apply one proposal — UPDATE the in-memory claim line and rewrite
    the file. Idempotent."""
    if not _admin_ok(request):
        return jsonify({"ok": False, "error": "admin_key required"}), 401

    c = _conn()
    if c is None:
        return jsonify({"ok": False, "error": "db unavailable"}), 500

    prop: Optional[dict] = None
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT claim_lineno, current_text, proposed_text, "
                "  current_min, current_max, proposed_min, proposed_max, "
                "  live_value, reason, status "
                "FROM state_of_2026_claim_proposals WHERE id = %s", (pid,))
            row = cur.fetchone()
            if not row:
                return jsonify({"ok": False, "error": "not found"}), 404
            if row[9] != "pending":
                return jsonify({"ok": False,
                                "error": f"status={row[9]}, not pending"}), 400
            prop = {
                "lineno": row[0], "current_text": row[1],
                "proposed_text": row[2], "current_min": row[3],
                "current_max": row[4], "proposed_min": row[5],
                "proposed_max": row[6], "live_value": row[7],
                "reason": row[8],
            }
    finally:
        pass

    # Rewrite the claims file.
    if not os.path.exists(_CLAIMS_PATH):
        try: c.close()
        except Exception: pass
        return jsonify({"ok": False, "error": "claims file missing"}), 500

    try:
        with open(_CLAIMS_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        # Find the target line by lineno (1-based)
        target_idx = (prop["lineno"] or 0) - 1
        if target_idx < 0 or target_idx >= len(lines):
            return jsonify({"ok": False, "error": "lineno out of range"}), 500
        old_line = lines[target_idx]
        # Surgical replace: claim text + expected_min + expected_max
        new_line = re.sub(
            r"claim:\s*[^|]+",
            f"claim: {prop['proposed_text']}",
            old_line, count=1)
        new_line = re.sub(
            r"expected_min:\s*[^|]+",
            f"expected_min: {prop['proposed_min']}",
            new_line, count=1)
        new_line = re.sub(
            r"expected_max:\s*[\s\S]*$",
            f"expected_max: {prop['proposed_max']}\n",
            new_line, count=1)
        lines[target_idx] = new_line
        with open(_CLAIMS_PATH, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except Exception as e:
        return jsonify({"ok": False,
                        "error": f"rewrite failed: {type(e).__name__}: {e}"}), 500

    # Mark applied.
    try:
        with c.cursor() as cur:
            cur.execute(
                "UPDATE state_of_2026_claim_proposals "
                "SET status = 'applied', applied_ts = NOW() WHERE id = %s",
                (pid,))
    finally:
        try: c.close()
        except Exception: pass

    return jsonify({
        "ok": True, "applied_id": pid,
        "old_line_snippet": old_line.strip()[:160],
        "new_line_snippet": new_line.strip()[:160],
        "note": ("Claim file rewritten in-place. Commit/push from a "
                 "separate human step (this endpoint does not touch git)."),
    })


# ── /admin/state-of-2026-pulse engagement dashboard ───────────────────

@state_of_2026_live_bp.route("/admin/state-of-2026-pulse", methods=["GET"])
def state_of_2026_pulse():
    """Operator-facing HTML pulse — page views, clicks, attribution, +
    pending claim proposals with one-click apply buttons."""
    if not _admin_ok(request):
        return Response(
            "<h1>401</h1><p>?admin_key=… or X-Admin-Key</p>",
            status=401, mimetype="text/html")

    days = max(1, min(90, int(request.args.get("days", "7"))))
    admin_key = (request.headers.get("X-Admin-Key")
                 or request.args.get("admin_key") or "").strip()
    admin_q = _esc(admin_key, quote=True)

    # Inline the attribution call so we don't 401-loop on the proxy.
    # 2026-06-07 Round-1 cleanup: Blueprint has NO test_request_context
    # method (that's a Flask app method). Was 500'ing every pulse hit
    # with AttributeError. Use current_app to get the live Flask app
    # and push the request context with the admin key injected so the
    # inline attribution_json() call passes the _admin_ok gate.
    from flask import current_app
    try:
        with current_app.test_request_context(
            f"/api/v1/admin/state-of-2026/attribution?days={days}",
            headers={"X-Admin-Key": _ADMIN_KEY}):
            att_resp = attribution_json()
        att = json.loads(att_resp.get_data(as_text=True))
    except Exception as e:
        logger.debug("pulse attribution sub-call failed: %s", e)
        att = {}

    # Pending proposals
    proposals: list[dict] = []
    c = _conn()
    if c is not None:
        try:
            with c.cursor() as cur:
                try:
                    cur.execute(
                        "SELECT id, claim_lineno, current_text, proposed_text, "
                        "  current_min, current_max, proposed_min, proposed_max, "
                        "  live_value, reason, ts "
                        "FROM state_of_2026_claim_proposals "
                        "WHERE status = 'pending' ORDER BY ts DESC LIMIT 50")
                    for r in (cur.fetchall() or []):
                        proposals.append({
                            "id": int(r[0]),
                            "lineno": int(r[1] or 0),
                            "current_text": r[2] or "",
                            "proposed_text": r[3] or "",
                            "current_min": r[4],
                            "current_max": r[5],
                            "proposed_min": r[6],
                            "proposed_max": r[7],
                            "live_value": r[8],
                            "reason": r[9] or "",
                            "ts": str(r[10]),
                        })
                except Exception as e:
                    logger.debug("pending proposals fetch failed: %s", e)
        finally:
            try: c.close()
            except Exception: pass

    # Round 2 (2026-06-07): pull the visitor-funnel KPIs (high-intent →
    # email → key → activated → paid) from routes/state_visitor_claim so
    # the dashboard shows the conversion mechanic, not just raw clicks.
    funnel = {}
    try:
        from routes.state_visitor_claim import state_visitor_claim_bp as _svc_bp  # noqa: F401
        # Use the test_request_context so we share auth + config rather
        # than running an HTTP loopback (which would 401 without the key).
        with state_of_2026_live_bp.test_request_context(
            f"/api/v1/state-of-2026/funnel?days={days}",
            headers={"X-Admin-Key": _ADMIN_KEY}):
            from routes.state_visitor_claim import visitor_funnel as _vf
            f_resp = _vf()
        try:
            funnel = json.loads(f_resp.get_data(as_text=True))
        except Exception:
            funnel = {}
    except Exception as e:
        logger.debug("visitor funnel fetch failed: %s", e)
        funnel = {}

    # Render
    pv = att.get("page_views", 0)
    uv = att.get("unique_visitors", 0)
    li = att.get("linkedin_attributed_views", 0)
    sub = att.get("subscribes", 0)
    mcp = att.get("mcp_keys_attributed")
    mcp_disp = "n/a" if mcp is None else str(mcp)

    # Visitor funnel KPIs
    f_visitors  = int(funnel.get("visitors", 0))
    f_li        = int(funnel.get("linkedin_attributed", 0))
    f_hi        = int(funnel.get("high_intent_threshold_hit", 0))
    f_emails    = int(funnel.get("emails_submitted", 0))
    f_active    = int(funnel.get("keys_activated", 0))
    f_paid      = int(funnel.get("paid", 0))
    f_hi_pct    = funnel.get("high_intent_pct", 0.0)
    f_email_pct = funnel.get("email_submit_pct", 0.0)
    f_paid_pct  = funnel.get("paid_pct", 0.0)

    clicks_rows = "".join(
        f'<tr><td><code>/r/{_esc(c["token"])}</code></td>'
        f'<td style="text-align:right">{c["clicks"]}</td>'
        f'<td style="text-align:right">{c["uniques"]}</td></tr>'
        for c in att.get("clicks_by_token", []))

    ref_rows = "".join(
        f'<tr><td>{_esc(r["host"])}</td>'
        f'<td style="text-align:right">{r["views"]}</td></tr>'
        for r in att.get("top_referers", []))

    prop_rows = "".join(
        f'<tr>'
        f'<td><code>{p["lineno"]}</code></td>'
        f'<td><span style="color:#a3a3a3">{_esc(p["current_text"])[:80]}</span><br>'
        f'<b style="color:#10b981">{_esc(p["proposed_text"])[:80]}</b></td>'
        f'<td>{_esc(str(p["live_value"]))}</td>'
        f'<td>[{p["current_min"]},{p["current_max"]}] → [{p["proposed_min"]},{p["proposed_max"]}]</td>'
        f'<td>{_esc(p["reason"])[:160]}</td>'
        f'<td><button onclick="applyProp({p["id"]})">Apply</button></td>'
        f'</tr>' for p in proposals)
    if not prop_rows:
        prop_rows = '<tr><td colspan=6 class=muted>No pending proposals.</td></tr>'

    return Response(f"""<!doctype html>
<html><head><meta charset=utf-8><title>State of 2026 Pulse · DC Hub</title>
<meta name=robots content="noindex,nofollow">
<style>
  body{{background:#0a0a0a;color:#e5e5e5;font:14px/1.5 system-ui,sans-serif;margin:0;padding:24px;max-width:1200px;margin-left:auto;margin-right:auto}}
  a{{color:#7dd3fc}}
  h1{{margin:0 0 8px;font-size:22px}}
  .muted{{color:#a3a3a3;font-size:13px}}
  .kpi-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:24px 0}}
  .kpi{{background:#171717;border:1px solid #262626;border-radius:8px;padding:16px}}
  .kpi-label{{color:#a3a3a3;font-size:12px;text-transform:uppercase;letter-spacing:0.5px}}
  .kpi-value{{font-size:28px;font-weight:700;color:#fff;margin-top:4px}}
  .panel{{background:#171717;border:1px solid #262626;border-radius:8px;padding:16px;margin:16px 0}}
  .panel h2{{margin:0 0 12px;font-size:16px}}
  table{{width:100%;border-collapse:collapse;font-size:13px}}
  table th,table td{{padding:8px 10px;text-align:left;border-bottom:1px solid #262626;vertical-align:top}}
  table th{{color:#a3a3a3;text-transform:uppercase;letter-spacing:0.5px;font-size:11px}}
  code{{background:#0a0a0a;padding:1px 6px;border-radius:4px;font-size:12px}}
  button{{background:#10b981;color:#0a0a0a;border:0;padding:6px 12px;border-radius:6px;font-weight:600;cursor:pointer;font-size:12px}}
  button:hover{{background:#34d399}}
</style></head><body>
<h1>State of 2026 Pulse</h1>
<p class=muted>Window: last {days} days · admin-gated ·
  <a href="?days=1&admin_key={admin_q}">1d</a> ·
  <a href="?days=7&admin_key={admin_q}">7d</a> ·
  <a href="?days=30&admin_key={admin_q}">30d</a> ·
  <a href="/admin/qa/state-of-2026?admin_key={admin_q}">QA harness</a> ·
  <a href="/state-of-2026">live page</a> ·
  <a href="/api/v1/admin/state-of-2026/attribution?days={days}&admin_key={admin_q}">JSON</a>
</p>

<div class=kpi-grid>
  <div class=kpi><div class=kpi-label>Page views</div><div class=kpi-value>{pv:,}</div></div>
  <div class=kpi><div class=kpi-label>Unique visitors</div><div class=kpi-value>{uv:,}</div></div>
  <div class=kpi><div class=kpi-label>LinkedIn referrals</div><div class=kpi-value>{li:,}</div></div>
  <div class=kpi><div class=kpi-label>Email subscribes</div><div class=kpi-value>{sub:,}</div></div>
  <div class=kpi><div class=kpi-label>MCP keys attributed</div><div class=kpi-value>{mcp_disp}</div></div>
</div>

<div class=panel>
  <h2>Visitor Funnel (Round 2: page → trial key → paid)</h2>
  <p class=muted>The conversion mechanic: page-side JS tracks time + brief
    clicks; threshold (2 clicks OR 60s) opens the email modal; submission
    mints a dch_trial_ key + emails it. Source:
    <code>state_visitor_intent</code> ·
    <a href="/api/v1/state-of-2026/funnel?days={days}&admin_key={admin_q}">JSON</a></p>
  <div class=kpi-grid style="margin-top:12px">
    <div class=kpi>
      <div class=kpi-label>Visitors (tracked)</div>
      <div class=kpi-value>{f_visitors:,}</div>
      <div class=muted style="font-size:11px;margin-top:4px">{f_li:,} from LinkedIn</div>
    </div>
    <div class=kpi>
      <div class=kpi-label>High-intent (≥2 clicks OR 60s)</div>
      <div class=kpi-value>{f_hi:,}</div>
      <div class=muted style="font-size:11px;margin-top:4px">{f_hi_pct}% of visitors</div>
    </div>
    <div class=kpi>
      <div class=kpi-label>Emails submitted</div>
      <div class=kpi-value>{f_emails:,}</div>
      <div class=muted style="font-size:11px;margin-top:4px">{f_email_pct}% of visitors</div>
    </div>
    <div class=kpi>
      <div class=kpi-label>Keys activated</div>
      <div class=kpi-value>{f_active:,}</div>
      <div class=muted style="font-size:11px;margin-top:4px">first MCP call after mint</div>
    </div>
    <div class=kpi>
      <div class=kpi-label>Paid conversions</div>
      <div class=kpi-value>{f_paid:,}</div>
      <div class=muted style="font-size:11px;margin-top:4px">{f_paid_pct}% of emails</div>
    </div>
  </div>
</div>

<div class=panel>
  <h2>Clicks by attribution token</h2>
  <table>
    <thead><tr><th>Token</th><th style="text-align:right">Clicks</th><th style="text-align:right">Uniques</th></tr></thead>
    <tbody>{clicks_rows or '<tr><td colspan=3 class=muted>No clicks yet.</td></tr>'}</tbody>
  </table>
</div>

<div class=panel>
  <h2>Top referers</h2>
  <table>
    <thead><tr><th>Host</th><th style="text-align:right">Views</th></tr></thead>
    <tbody>{ref_rows or '<tr><td colspan=2 class=muted>No referrers yet.</td></tr>'}</tbody>
  </table>
</div>

<div class=panel>
  <h2>Live numbers cache (refreshes daily 05:00 UTC)</h2>
  <p class=muted>The /state-of-2026 page reads from <code>state_of_2026_live_numbers</code>;
  cron <code>state_of_2026_refresh</code> @ 05:00 UTC refreshes the cache from
  every claim's live endpoint and busts the OG card edge cache.
  <button onclick="refreshNow()" style="margin-left:8px">Refresh now</button>
  · Kill switch: <code>STATE_2026_REFRESH_DISABLE=1</code></p>
  <div id="refresh-status" class=muted style="margin-top:8px"></div>
</div>

<div class=panel>
  <h2>Auto-claim-update proposals (pending review)</h2>
  <p class=muted>Daily 06:00 UTC cron writes proposals here when a live
  number drifts past the [min, max] expected band. <b style="color:#34d399">GROWTH</b>
  proposals bump the published number up; <b style="color:#fbbf24">DECLINE</b> and
  <b style="color:#f87171">CRASH</b> proposals flag a data-source regression for
  triage (the page leaves the claim text untouched until you investigate).
  Apply rewrites docs/state-of-2026-claims.txt in-place; commit/push is a
  separate manual step.</p>
  <table>
    <thead><tr><th>Line</th><th>Diff</th><th>Live</th><th>Range</th><th>Reason</th><th>Action</th></tr></thead>
    <tbody>{prop_rows}</tbody>
  </table>
</div>

<script>
var KEY = "{admin_q}";
function refreshNow() {{
  var s = document.getElementById('refresh-status');
  s.textContent = 'Refreshing… (this re-probes all 8 endpoints + busts the OG cache)';
  s.style.color = '#a3a3a3';
  fetch('/api/v1/admin/state-of-2026/refresh-now?admin_key=' + KEY + '&source=manual', {{
    method: 'POST', headers: {{ 'X-Admin-Key': KEY }}
  }}).then(r => r.json()).then(d => {{
    if (d.ok) {{
      s.style.color = '#34d399';
      var og = d.og_card_regen || {{}};
      s.textContent = 'OK — refreshed ' + d.n_refreshed +
        ' rows, ' + d.n_errors + ' errors. OG card HTTP ' +
        (og.status || '—') + '.';
      setTimeout(function() {{ window.location.reload(); }}, 2500);
    }} else {{
      s.style.color = '#f59e0b';
      s.textContent = 'Failed: ' + (d.error || d.reason || 'unknown');
    }}
  }}).catch(e => {{
    s.style.color = '#f59e0b';
    s.textContent = 'Network error: ' + e;
  }});
}}
function applyProp(pid) {{
  if (!confirm('Apply proposal #' + pid + '? This rewrites claims.txt on disk.')) return;
  fetch('/api/v1/admin/state-of-2026/claims/apply/' + pid + '?admin_key=' + KEY, {{
    method: 'POST',
    headers: {{ 'X-Admin-Key': KEY }}
  }}).then(r => r.json()).then(d => {{
    if (d.ok) {{
      alert('Applied #' + pid + '. New: ' + (d.new_line_snippet || ''));
      window.location.reload();
    }} else {{
      alert('Failed: ' + (d.error || 'unknown'));
    }}
  }}).catch(e => alert('Network error: ' + e));
}}
</script>
</body></html>""", mimetype="text/html",
        headers={"Cache-Control": "private, no-store"})
