"""
routes/flywheel_master_shell.py — Flywheel Master Shell (2026-07-05).

One pane that live-probes the FIVE flywheel priorities named in the 07-05
health read and scores each PASS/FAIL, so the operator can watch each fix
land and keep the checks as a standing sentinel afterward.

The five lanes (one per priority — the actual back-half-of-the-funnel work):
  1. RETENTION / DURABLE-IDENTITY — the binding constraint. A durable key is
     minted+persisted on first call (claim_api), real agents come back on a
     SECOND day, and mature key-reuse % isn't regressing.
  2. BRAIN DEDUP + FACILITIES BACKLOG — findings dedup to one row per
     (issue,url) (no row-explosion), and the unverified discovery backlog at
     /api/v1/facilities/delta is actually DRAINING (verified moving).
  3. CANONICAL IDENTITY — the north-star reads mcp_calls_identity (fresh,
     non-zero), NOT the deprecated de-looped raw-call metric that produced the
     phantom "-96% collapse" the brain kept citing.
  4. MEDIA DISTRIBUTION — the publishing ARM is alive: a real X/social post
     went out recently (not x_publisher_dead), the monthly-trend send fired
     within cadence (not unsent_3d), and /dc-hub-media isn't multi-second slow.
  5. SEO SLUG-FREEZE — every facility carries a frozen canonical_slug, the
     legacy /facility/<id> 301 points at that STORED slug (not a recompute),
     and the sitemap index is healthy — i.e. the ~8k-page 404/redirect bleed
     is stopped.

Design mirrors the house master-shell pattern (see fixwave_master_shell.py):
admin-gated, killable (FLYWHEEL_DISABLED=1), every probe fail-soft +
timeout-bounded, snapshot row per tick, 30s cache. READ-ONLY / DIAGNOSTIC:
each lane NAMES an actuator but this shell fires NOTHING — the fixes ship via
their own PRs/crons.

Endpoints:
  GET/POST /api/v1/admin/flywheel/master-tick   JSON scoreboard (5 lanes)
  GET      /admin/flywheel                       HTML dashboard (60s refresh)
  GET      /api/v1/admin/flywheel                CF zone-worker bypass alias

Auth: X-Admin-Key header or ?admin_key= vs DCHUB_ADMIN_KEY (falls back to
DCHUB_INTERNAL_KEY) — same gate as fixwave_master_shell / growth_master_shell.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html import escape as _esc

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

flywheel_master_shell_bp = Blueprint("flywheel_master_shell", __name__)

# ── targets ───────────────────────────────────────────────────────────
# Public site surfaces (media page, sitemap, facility redirect) are probed
# through the edge because that's where the leak/regression actually lives.
_EDGE = "https://dchub.cloud"
_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# ── auth / kill ───────────────────────────────────────────────────────

def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("FLYWHEEL_DISABLED") or "").strip() == "1"


# ── helpers ───────────────────────────────────────────────────────────

def _http(url: str, *, timeout: float = 6.0) -> tuple[int, str, int]:
    """Bounded GET. Returns (status, body_text[:200k], elapsed_ms). Never raises."""
    t0 = time.time()
    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", _BROWSER_UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.getcode(), r.read(200_000).decode("utf-8", "replace"), int((time.time() - t0) * 1000)
    except urllib.error.HTTPError as e:
        try:
            txt = e.read(50_000).decode("utf-8", "replace")
        except Exception:
            txt = ""
        return e.code, txt, int((time.time() - t0) * 1000)
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}", int((time.time() - t0) * 1000)


def _http_no_redirect(url: str, *, timeout: float = 6.0) -> tuple[int, str, int]:
    """GET that does NOT follow redirects. Returns (status, Location, ms).
    Used to inspect the /facility/<id> 301 target without chasing it."""
    class _NR(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):  # returning None => raise HTTPError
            return None

    t0 = time.time()
    try:
        opener = urllib.request.build_opener(_NR)
        req = urllib.request.Request(url)
        req.add_header("User-Agent", _BROWSER_UA)
        with opener.open(req, timeout=timeout) as r:
            return r.getcode(), r.headers.get("Location", "") or "", int((time.time() - t0) * 1000)
    except urllib.error.HTTPError as e:
        loc = (e.headers.get("Location", "") if e.headers else "") or ""
        return e.code, loc, int((time.time() - t0) * 1000)
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}", int((time.time() - t0) * 1000)


def _conn():
    """Raw psycopg2 connection (mirrors fixwave._conn). None on failure."""
    try:
        import psycopg2 as _pg
        url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        if not url:
            return None
        c = _pg.connect(url, connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning("[flywheel] db connect failed: %s", e)
        return None


def _scalar(c, sql: str):
    """Fail-soft scalar. None on error (NOT 0 — a probe must tell 'query broke'
    from 'count is zero'). Literal SQL only, NO params tuple: every de-loop /
    regex predicate here carries a literal % or {n} and psycopg2 would try to
    %-substitute an empty tuple (the empty-tuple % trap)."""
    try:
        with c.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
        return row[0] if row else None
    except Exception as e:
        logger.debug("[flywheel] scalar failed: %s -- %s", sql[:80], e)
        try:
            c.rollback()
        except Exception:
            pass
        return None


def _rows(c, sql: str) -> list:
    """Fail-soft fetchall. [] on error. Literal SQL only (see _scalar)."""
    try:
        with c.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()
    except Exception as e:
        logger.debug("[flywheel] rows failed: %s -- %s", sql[:80], e)
        try:
            c.rollback()
        except Exception:
            pass
        return []


def _check(cid: str, name: str, passed, detail: str, ms: int = 0) -> dict:
    # passed: True / False / None (None = indeterminate/gauge, shown as "?")
    return {"id": cid, "name": name, "pass": passed,
            "detail": (detail or "")[:300], "ms": ms}


_CB = lambda: f"_fl={int(time.time())}"  # cache-buster for edge-cached surfaces


# ── lane 1 · retention / durable-identity ─────────────────────────────

def _lane_retention(c) -> list[dict]:
    out = []
    if c is None:
        return [_check("ret_nodb", "retention lane needs db", None, "no db")]

    # 1a — durable-identity mechanism alive: claim_free_key persists a
    # dch_live_ key tagged metadata.source='claim_api'. >0 in 7d = path works.
    mint = _scalar(c, "SELECT COUNT(*) FROM mcp_dev_keys "
                      "WHERE metadata->>'source' = 'claim_api' "
                      "AND created_at >= now() - interval '7 days'")
    out.append(_check("ret_durable_mint", "durable key persisted on first call (7d)",
                      (mint or 0) > 0 if mint is not None else None,
                      f"{mint} claim_api keys minted/7d" if mint is not None else "query failed"))

    # 1b — the RETENTION TRUTH via identity (not rotating IPs): real external
    # agents seen on >=2 distinct days in the last 7d. >=1 = someone came back.
    multiday = _scalar(c, "SELECT count(*) FROM ("
                          " SELECT agent_id FROM mcp_calls_identity"
                          " WHERE is_real_external AND created_at >= now() - interval '7 days'"
                          " GROUP BY agent_id"
                          " HAVING count(DISTINCT date_trunc('day', created_at)) >= 2) t")
    out.append(_check("ret_multiday_return", "real agents returning on a 2nd day (7d)",
                      (multiday or 0) >= 1 if multiday is not None else None,
                      f"{multiday} multi-day agents/7d" if multiday is not None else "query failed"))

    # 1c — mature key-reuse % (30d cohort minted >7d ago). Regression floor 8%
    # (dashboard baseline ~12.8%). This is the r-return signal — watch it climb.
    reuse = _scalar(c, "SELECT ROUND(100.0*COUNT(*) FILTER (WHERE minted_at < now() - interval '7 days'"
                       " AND last_used_at IS NOT NULL"
                       " AND date_trunc('week', last_used_at) > date_trunc('week', minted_at))"
                       " /NULLIF(COUNT(*) FILTER (WHERE minted_at < now() - interval '7 days'),0),1)"
                       " FROM auto_trial_keys WHERE minted_at >= now() - interval '30 days'")
    out.append(_check("ret_key_reuse", "mature key-reuse % >= 8 (30d)",
                      (float(reuse) >= 8.0) if reuse is not None else None,
                      f"{reuse}% mature reuse" if reuse is not None else "no mature cohort yet"))
    return out


# ── lane 2 · brain dedup + facilities backlog ─────────────────────────

def _lane_brain_backlog(c) -> list[dict]:
    out = []
    if c is None:
        return [_check("bl_nodb", "brain/backlog lane needs db", None, "no db")]

    # 2a — no row-explosion: findings dedup to one row per (issue,url) via the
    # canonical upsert. A sane distinct-finding count (7d) = dedup shipped.
    nfind = _scalar(c, "SELECT COUNT(*) FROM brain_findings "
                       "WHERE last_seen >= now() - interval '7 days'")
    out.append(_check("bl_no_row_explosion", "brain findings deduped (no row-explosion, 7d)",
                      (nfind < 2000) if nfind is not None else None,
                      f"{nfind} distinct open/recent findings" if nfind is not None else "query failed"))

    # 2b — GAUGE: max re-emit magnitude. seen_count still climbs because the
    # DETECTORS re-fire per-cycle (per-episode logic not yet shipped). Shown
    # as informational; the runaway quarantine trips at 200.
    maxseen = _scalar(c, "SELECT COALESCE(MAX(seen_count),0) FROM brain_findings "
                         "WHERE last_seen >= now() - interval '7 days'")
    out.append(_check("bl_reemit_gauge", "top detector re-emit count (per-episode logic pending)",
                      None,
                      f"max seen_count = {maxseen} (stateful-detector actuator not yet shipped)"
                      if maxseen is not None else "no seen_count column"))

    # 2c — backlog DRAINING: verified facilities moved >=100 over 7d (mirrors
    # the dedup_pipeline_stalled trigger). No dedup cron => expect RED until
    # /api/v1/admin/dedup/run|drain is put on a schedule.
    latest = _scalar(c, "SELECT verified_count FROM facility_count_snapshots "
                        "ORDER BY snapshot_date DESC LIMIT 1")
    prior = _scalar(c, "SELECT verified_count FROM facility_count_snapshots "
                       "WHERE snapshot_date <= (CURRENT_DATE - 7) "
                       "ORDER BY snapshot_date DESC LIMIT 1")
    if latest is None or prior is None:
        out.append(_check("bl_backlog_draining", "verified facilities moving (>=100/7d)",
                           None, f"snapshots: latest={latest} prior7d={prior}"))
    else:
        moved = int(latest) - int(prior)
        out.append(_check("bl_backlog_draining", "verified facilities moving (>=100/7d)",
                           moved >= 100, f"verified moved {moved:+d} over 7d ({prior}->{latest})"))

    # 2d — GAUGE: current unverified backlog (watch it trend down from ~21,422).
    backlog = _scalar(c, "SELECT COUNT(*) FROM discovered_facilities "
                         "WHERE NOT (merged_at IS NULL AND is_duplicate = 0)")
    out.append(_check("bl_backlog_size", "unverified discovery backlog (trend down)",
                      None, f"{backlog} unverified" if backlog is not None else "query failed"))
    return out


# ── lane 3 · canonical identity ───────────────────────────────────────

def _lane_identity(c) -> list[dict]:
    out = []
    if c is None:
        return [_check("id_nodb", "identity lane needs db", None, "no db")]

    # 3a — ingestion flowing: newest mcp_tool_calls row < 24h (the view's
    # freshness == base-table freshness; no separate ingest job).
    age = _scalar(c, "SELECT EXTRACT(EPOCH FROM (now() - max(created_at))) FROM mcp_tool_calls")
    out.append(_check("id_ingest_fresh", "mcp_tool_calls ingest fresh (<24h)",
                      (float(age) < 86400) if age is not None else None,
                      f"newest row {round(float(age)/3600,1)}h ago" if age is not None else "no rows"))

    # 3b — genuine agent traffic fresh (<48h) — guards against a self-heal
    # firehose masking a stall in real external calls.
    rage = _scalar(c, "SELECT EXTRACT(EPOCH FROM (now() - max(created_at))) "
                      "FROM mcp_calls_identity WHERE is_real_external")
    out.append(_check("id_realext_fresh", "real-external agent traffic fresh (<48h)",
                      (float(rage) < 172800) if rage is not None else None,
                      f"newest real-external {round(float(rage)/3600,1)}h ago" if rage is not None else "none"))

    # 3c — north-star reads the IDENTITY view (distinct agent_id), and we show
    # the deprecated de-looped CALL count beside it so the grain gap that bred
    # the "-96% collapse" phantom is visible. PASS if agents_7d >= 1.
    agents = _scalar(c, "SELECT count(DISTINCT agent_id) FROM mcp_calls_identity "
                        "WHERE created_at >= now() - interval '7 days' "
                        "AND is_public_ip AND is_real_external")
    deloop = None
    try:
        from mcp_calls_deloop import deloop_calls_where
        dw = deloop_calls_where()
        deloop = _scalar(c, "SELECT COUNT(*) FROM mcp_tool_calls "
                            "WHERE created_at >= CURRENT_DATE - INTERVAL '7 days' "
                            "AND created_at < CURRENT_DATE AND " + dw)
    except Exception as e:
        logger.debug("[flywheel] deloop import/probe failed: %s", e)
    detail = (f"agents_7d={agents} (identity view)"
              + (f" · deloop_calls_7d={deloop} — cite AGENTS, not calls" if deloop is not None else ""))
    out.append(_check("id_northstar", "north-star = distinct real agents via mcp_calls_identity",
                      (agents or 0) >= 1 if agents is not None else None, detail))
    return out


# ── lane 4 · media distribution arm ───────────────────────────────────

def _lane_media(c) -> list[dict]:
    out = []

    # 4a — X publisher alive: a real X/social post published < 7d ago. Handles
    # the live schema trap (posted_at=timestamp, published_at=TEXT) by casting
    # ::text and regex-guarding an ISO date before ::timestamp.
    if c is not None:
        xage = _scalar(c,
            "SELECT EXTRACT(EPOCH FROM (now() - MAX(ts)))/86400.0 FROM ("
            " SELECT NULLIF(published_at::text,'')::timestamp AS ts FROM social_media_posts"
            "  WHERE status='published' AND publish_platform IN ('twitter','x')"
            "  AND published_at::text ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}'"
            " UNION ALL"
            " SELECT NULLIF(posted_at::text,'')::timestamp AS ts FROM social_media_posts"
            "  WHERE status='published' AND publish_platform IN ('twitter','x')"
            "  AND posted_at::text ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}') q")
        if xage is None:
            out.append(_check("md_x_publisher", "X publisher alive (post <7d)",
                              False, "no successful X post on record (x_publisher_dead)"))
        else:
            out.append(_check("md_x_publisher", "X publisher alive (post <7d)",
                              float(xage) < 7.0, f"last X post {round(float(xage),1)}d ago"))

        # 4b — monthly-trend send fired within cadence (<35d). Empty => unsent.
        mage = _scalar(c, "SELECT EXTRACT(EPOCH FROM (now() - MAX(sent_at)))/86400.0 "
                          "FROM monthly_outreach_log")
        if mage is None:
            out.append(_check("md_monthly_trend", "monthly-trend send within cadence (<35d)",
                              False, "no monthly outreach on record (monthly_trend_unsent)"))
        else:
            out.append(_check("md_monthly_trend", "monthly-trend send within cadence (<35d)",
                              float(mage) < 35.0, f"last monthly send {round(float(mage),1)}d ago"))
    else:
        out.append(_check("md_x_publisher", "X publisher alive (post <7d)", None, "no db"))
        out.append(_check("md_monthly_trend", "monthly-trend send within cadence (<35d)", None, "no db"))

    # 4c — /dc-hub-media not multi-second slow (brain flagged ~5.2s).
    st, _, ms = _http(f"{_EDGE}/dc-hub-media?{_CB()}", timeout=12.0)
    out.append(_check("md_page_fast", "/dc-hub-media responds < 3s",
                      (st == 200 and ms < 3000) if st else None,
                      f"HTTP {st} · {ms}ms", ms))
    return out


# ── lane 5 · SEO slug-freeze ──────────────────────────────────────────

def _lane_seo_slug(c) -> list[dict]:
    out = []

    # 5a — every sluggable facility carries a frozen canonical_slug (pending=0
    # on BOTH facilities and discovered_facilities).
    if c is not None:
        fpend = _scalar(c, "SELECT COUNT(*) FROM facilities "
                           "WHERE canonical_slug IS NULL AND name IS NOT NULL AND name <> ''")
        dpend = _scalar(c, "SELECT COUNT(*) FROM discovered_facilities "
                           "WHERE canonical_slug IS NULL AND name IS NOT NULL AND name <> ''")
        if fpend is None or dpend is None:
            out.append(_check("seo_slug_frozen", "all facilities have a frozen canonical_slug",
                              None, f"facilities pending={fpend}, discovered pending={dpend}"))
        else:
            out.append(_check("seo_slug_frozen", "all facilities have a frozen canonical_slug",
                              int(fpend) == 0 and int(dpend) == 0,
                              f"pending: facilities={fpend} discovered={dpend}"))
    else:
        out.append(_check("seo_slug_frozen", "all facilities have a frozen canonical_slug", None, "no db"))

    # 5b — LOAD-BEARING: the legacy /facility/<id> 301 must point at the STORED
    # slug, not a recompute-from-name. Sample 3 recent frozen rows; PASS only
    # if all 301 Locations end in the stored canonical_slug.
    if c is not None:
        samples = _rows(c, "SELECT id, canonical_slug FROM facilities "
                           "WHERE canonical_slug IS NOT NULL AND canonical_slug <> '' "
                           "ORDER BY id DESC LIMIT 3")
        if not samples:
            out.append(_check("seo_301_slug_match", "301 target == stored canonical_slug (sampled)",
                              None, "no frozen slugs to sample"))
        else:
            match = 0
            diverged = []
            last_ms = 0
            for fid, slug in samples:
                st, loc, ms = _http_no_redirect(f"{_EDGE}/facility/{fid}", timeout=6.0)
                last_ms = ms
                served = loc.rstrip("/").rsplit("/", 1)[-1].split("?")[0] if loc else ""
                if st in (301, 302, 308) and served == slug:
                    match += 1
                elif st in (301, 302, 308):
                    diverged.append(f"id{fid}: served '{served}' != stored '{slug}'")
                else:
                    diverged.append(f"id{fid}: HTTP {st}")
            out.append(_check("seo_301_slug_match", "301 target == stored canonical_slug (sampled)",
                              match == len(samples),
                              f"{match}/{len(samples)} match" + (" · " + "; ".join(diverged[:2]) if diverged else ""),
                              last_ms))
    else:
        out.append(_check("seo_301_slug_match", "301 target == stored canonical_slug (sampled)", None, "no db"))

    # 5c — sitemap index healthy (has shard/loc entries) — the crawl surface
    # is intact, not a wall of 404/redirects.
    st, body, ms = _http(f"{_EDGE}/sitemap.xml", timeout=8.0)
    nloc = body.count("<loc>") if st == 200 else 0
    healthy = st == 200 and ("<sitemapindex" in body or nloc > 0)
    out.append(_check("seo_sitemap_healthy", "sitemap index healthy (has entries)",
                      healthy if st else None,
                      f"HTTP {st} · {nloc} <loc> entries", ms))
    return out


# ── tick orchestration ────────────────────────────────────────────────
# (key, label, fn, actuator) — actuator is NAMED but never fired (diagnostic).

_LANES = [
    ("retention",     "1 · Retention / durable-identity",   _lane_retention,
     "r-return hook + durable-identity build"),
    ("brain_backlog", "2 · Brain dedup + facilities backlog", _lane_brain_backlog,
     "stateful detector + /api/v1/admin/dedup drain on a schedule"),
    ("identity",      "3 · Canonical identity",              _lane_identity,
     "repoint brain evidence → mcp_calls_identity (drop de-looped calls)"),
    ("media_dist",    "4 · Media distribution arm",          _lane_media,
     "revive X publisher + monthly-trend send-path"),
    ("seo_slug",      "5 · SEO slug-freeze",                 _lane_seo_slug,
     "serve stored canonical_slug from the /facility 301; stop the bleed"),
]

_cache: dict = {"ts": 0.0, "payload": None}
_cache_lock = threading.Lock()
_TICK_TTL = 30.0


def _ensure_snapshots(c) -> None:
    try:
        with c.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS flywheel_snapshots ("
                " id BIGSERIAL PRIMARY KEY,"
                " created_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
                " lanes_pass INT, lanes_total INT, payload JSONB)")
    except Exception as e:
        logger.debug("[flywheel] snapshot ddl skipped: %s", e)


def _run_tick() -> dict:
    c = _conn()
    lanes = []
    for key, label, fn, actuator in _LANES:
        try:
            checks = fn(c)
        except Exception as e:  # a lane must never sink the tick
            checks = [_check(f"{key}_error", "lane crashed", None, str(e)[:200])]
        decided = [ch for ch in checks if ch["pass"] is not None]
        lane_pass = bool(decided) and all(ch["pass"] for ch in decided)
        lanes.append({"lane": key, "label": label, "pass": lane_pass,
                      "actuator": actuator, "checks": checks,
                      "progress": f"{sum(1 for ch in decided if ch['pass'])}/{len(checks)}"})
    payload = {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lanes_pass": sum(1 for l in lanes if l["pass"]),
        "lanes_total": len(lanes),
        "lanes": lanes,
        "note": "read-only DIAGNOSTIC probes; names an actuator per lane but "
                "fires nothing; see routes/flywheel_master_shell.py",
    }
    if c is not None:
        try:
            _ensure_snapshots(c)
            with c.cursor() as cur:
                cur.execute("INSERT INTO flywheel_snapshots (lanes_pass, lanes_total, payload) "
                            "VALUES (%s, %s, %s)",
                            (payload["lanes_pass"], payload["lanes_total"], json.dumps(payload)))
        except Exception as e:
            logger.debug("[flywheel] snapshot insert failed: %s", e)
        try:
            c.close()
        except Exception:
            pass
    return payload


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

@flywheel_master_shell_bp.route("/api/v1/admin/flywheel/master-tick", methods=["GET", "POST"])
def flywheel_master_tick():
    if _disabled():
        return jsonify(ok=False, error="disabled"), 503
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    fresh = (request.args.get("fresh") or "") == "1"
    if fresh:
        with _cache_lock:
            _cache["payload"] = None
    return jsonify(_tick_cached())


@flywheel_master_shell_bp.route("/admin/flywheel", methods=["GET"])
@flywheel_master_shell_bp.route("/api/v1/admin/flywheel", methods=["GET"])
def flywheel_dashboard():
    if _disabled():
        return Response("flywheel disabled", status=503)
    if not _admin_ok():
        return Response("forbidden — X-Admin-Key or ?admin_key=", status=403)
    p = _tick_cached()

    def _chip(v):
        if v is True:
            return '<span style="color:#22c55e">✓</span>'
        if v is False:
            return '<span style="color:#ef4444">✗</span>'
        return '<span style="color:#eab308">?</span>'

    cards = []
    for lane in p["lanes"]:
        rows = "".join(
            f"<tr><td style='padding:4px 8px'>{_chip(ch['pass'])}</td>"
            f"<td style='padding:4px 8px'>{_esc(ch['name'])}</td>"
            f"<td style='padding:4px 8px;color:#94a3b8'>{_esc(ch['detail'])}"
            f"{(' · ' + str(ch['ms']) + 'ms') if ch.get('ms') else ''}</td></tr>"
            for ch in lane["checks"])
        border = "#22c55e" if lane["pass"] else "#334155"
        cards.append(
            f"<div style='background:#0f172a;border:1px solid {border};border-radius:12px;"
            f"padding:16px;margin:12px 0'>"
            f"<div style='font-weight:700;font-size:15px'>{_chip(lane['pass'])} "
            f"{_esc(lane['label'])} <span style='color:#64748b;font-weight:400'>"
            f"({lane['progress']} checks green)</span></div>"
            f"<table style='margin-top:8px;font-size:13px;border-collapse:collapse'>{rows}</table>"
            f"<div style='margin-top:8px;font-size:12px;color:#64748b'>⚡ actuator (not fired): "
            f"{_esc(lane.get('actuator',''))}</div></div>")

    green = p["lanes_pass"] == p["lanes_total"]
    html = (
        "<!doctype html><meta charset='utf-8'>"
        "<meta http-equiv='refresh' content='60'>"
        "<title>Flywheel Master Shell · DC Hub</title>"
        "<body style='background:#020617;color:#e2e8f0;font-family:-apple-system,Segoe UI,"
        "Roboto,sans-serif;max-width:880px;margin:24px auto;padding:0 16px'>"
        f"<h2 style='margin:0 0 4px'>Flywheel Master Shell "
        f"<span style='color:{'#22c55e' if green else '#eab308'}'>"
        f"{p['lanes_pass']}/{p['lanes_total']} lanes green</span></h2>"
        f"<div style='color:#64748b;font-size:12px'>07-05 flywheel priorities · read-only "
        f"DIAGNOSTIC (names an actuator per lane, fires nothing) · 30s tick cache · "
        f"auto-refresh 60s · generated {_esc(p['generated_at'])} · "
        f"JSON: /api/v1/admin/flywheel/master-tick</div>"
        + "".join(cards) + "</body>")
    return Response(html, mimetype="text/html")
