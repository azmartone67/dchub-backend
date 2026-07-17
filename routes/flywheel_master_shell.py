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
     (issue,url) (no row-explosion), and the unverified discovery backlog is
     actually DRAINING (verified moving).
  3. CANONICAL IDENTITY — the north-star reads mcp_calls_identity (fresh,
     non-zero), NOT the deprecated de-looped raw-call metric that produced the
     phantom "-96% collapse" the brain kept citing.
  4. MEDIA DISTRIBUTION — the publishing ARM is alive: a real X/social post
     went out recently (not x_publisher_dead), the monthly-trend send fired
     within cadence (not unsent_3d), and the pipeline published SOMETHING in 7d.
  5. SEO SLUG-FREEZE — every facility carries a frozen canonical_slug, the
     legacy /facility/<id> 301 target (recomputed IN-PROCESS via the same
     _canonical_facility_slug the handler uses) still equals the STORED slug,
     and the sitemap has frozen-slug content to emit — i.e. the ~8k-page
     404/redirect bleed is stopped.

★ PURE-DB shell: every probe is a Neon query or an in-process computation —
NO outbound HTTP. (2026-07-06 incident: an earlier revision fetched
/sitemap.xml and /facility/<id> through the public edge, which routed back
into THIS SAME Flask origin and amplified a chronically-hot connection pool
into thread starvation + 502s. A master-shell tick must never issue a
self-request.) Mirrors the house pattern otherwise: admin-gated, killable
(FLYWHEEL_DISABLED=1), fail-soft, snapshot row per tick, 30s cache.
READ-ONLY / DIAGNOSTIC: each lane NAMES an actuator but fires NOTHING.

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
from datetime import datetime, timezone
from html import escape as _esc

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

flywheel_master_shell_bp = Blueprint("flywheel_master_shell", __name__)

# ── targets ───────────────────────────────────────────────────────────
# No network targets: this shell is PURE-DB. See the module docstring for why
# (2026-07-06 self-request/pool-saturation incident). Every lane reads Neon
# via a short-lived raw connection or computes in-process.

# ── auth / kill ───────────────────────────────────────────────────────

def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("FLYWHEEL_DISABLED") or "").strip() == "1"


# ── db helpers ────────────────────────────────────────────────────────

def _conn():
    """Raw psycopg2 connection (mirrors fixwave._conn). None on failure.
    Deliberately OUTSIDE the app pool — one short-lived connection per tick,
    opened and closed here, so a tick never checks out a shared pool slot."""
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
    from 'count is zero'). Literal SQL only, NO params tuple: several predicates
    here carry a literal % or {n} regex and psycopg2 would try to %-substitute
    an empty tuple (the empty-tuple % trap)."""
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

    # 1c — mature key-reuse % across BOTH durable cohorts (auto_trial_keys ∪
    # dch_live_ mcp_dev_keys). r-durable-key (2026-07-06): was auto_trial-ONLY,
    # blind to the dch_live_ claim-key cohort agents are told to SAVE — the exact
    # cohort the forward-IP + 30d-window fix targets, so the auto_trial-only read
    # could never reflect that fix landing. UNION both (mirrors mcp_retention.py
    # + routes/backfunnel_master_shell.py). Regression floor 8%.
    reuse = _scalar(c, "WITH k AS ("
                       " SELECT minted_at AS anchor, last_used_at FROM auto_trial_keys"
                       "  WHERE minted_at >= now() - interval '30 days'"
                       " UNION ALL"
                       " SELECT created_at AS anchor, last_used_at FROM mcp_dev_keys"
                       "  WHERE api_key LIKE 'dch_live_%' AND created_at >= now() - interval '30 days')"
                       " SELECT ROUND(100.0*COUNT(*) FILTER (WHERE anchor < now() - interval '7 days'"
                       " AND last_used_at IS NOT NULL"
                       " AND date_trunc('week', last_used_at) > date_trunc('week', anchor))"
                       " /NULLIF(COUNT(*) FILTER (WHERE anchor < now() - interval '7 days'),0),1) FROM k")
    out.append(_check("ret_key_reuse", "mature key-reuse % >= 8 (BOTH cohorts, 30d)",
                      (float(reuse) >= 8.0) if reuse is not None else None,
                      f"{reuse}% mature reuse (auto_trial ∪ dch_live_)" if reuse is not None else "no mature cohort yet"))
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

    # 2b — max re-emit PER EPISODE (stateful-detector layer, 2026-07-17):
    # detectors still re-fire per-cycle, but the canonical writer now absorbs a
    # re-observation into the OPEN episode (last_seen + episode_seen_count only);
    # seen_count/episode_count move once per episode (reopen-after-resolve).
    # Headline = max re-emits absorbed by one CURRENT episode — resets when the
    # episode resolves, so it's bounded by episode lifetime, not all-time (the
    # old accumulator hit 1463). Runaway quarantine still trips at 200
    # consecutive sightings (now fed episode_seen_count — its real semantics).
    maxep = _scalar(c, "SELECT COALESCE(MAX(episode_seen_count),0) FROM brain_findings "
                       "WHERE last_seen >= now() - interval '7 days'")
    # PASS = episode integrity on fresh rows: a finding born after the layer
    # shipped keeps seen_count == its episode count. Pre-fix per-cycle inflation
    # ran ~45/day (would blow past 100 in <3 days); 100+ episodes in 7d = real
    # flap pathology worth a red. Born-before-ship rows carry frozen legacy
    # inflation, so clamp the window to post-ship births (clamp is inert after
    # 2026-07-25 when the rolling 7d window passes it).
    newmax = _scalar(c, "SELECT MAX(seen_count) FROM brain_findings "
                        "WHERE first_seen >= GREATEST(now() - interval '7 days', "
                        "TIMESTAMPTZ '2026-07-18')")
    out.append(_check("bl_reemit_per_episode", "max re-emit per episode (stateful detectors live)",
                      (int(newmax or 0) < 100) if maxep is not None else None,
                      f"max re-emits absorbed by one open episode = {maxep} · "
                      f"max seen_count on post-ship-born rows = {newmax or 0} "
                      f"(counts episodes now, not scan cycles; <100 = healthy)"
                      if maxep is not None else "episode columns not live yet"))

    # 2c — backlog DRAINING (r-probe-fix 2026-07-07): measure real THROUGHPUT —
    # rows actually drained (discovered_facilities.merged_at stamped) in the last
    # 7d; >=100 = healthy. The OLD probe read the 7d DELTA of
    # facility_count_snapshots.verified_count, but verified_count IS the UN-drained
    # REMAINDER (COUNT WHERE merged_at IS NULL AND is_duplicate=0) — a number the
    # drain keeps NEAR ZERO, so a small delta was mis-read as "not moving" and the
    # lane was FALSELY RED. The drain IS firing (GH Actions dchub-jobs.yml
    # auto-approve, ~482/7d; pending queue = 0). This now reflects it.
    drained = _scalar(c, "SELECT COUNT(*) FROM discovered_facilities "
                         "WHERE merged_at::timestamptz >= now() - interval '7 days'")
    out.append(_check("bl_backlog_draining", "facilities DRAINED >=100/7d (real throughput)",
                       (int(drained) >= 100) if drained is not None else None,
                       f"{drained} drained/7d (merged_at stamped)" if drained is not None else "query failed"))

    # 2d — GAUGE (r-probe-fix 2026-07-07): the REAL drainable queue = pending rows
    # awaiting auto-approve (status='pending'). The old gauge counted
    # `NOT (merged_at IS NULL AND is_duplicate=0)` = rows ALREADY PROCESSED
    # (merged OR marked duplicate) and mislabeled ~21,904 DONE rows as "unverified
    # backlog". Show pending + processed honestly.
    pending = _scalar(c, "SELECT COUNT(*) FROM discovered_facilities WHERE status='pending'")
    processed = _scalar(c, "SELECT COUNT(*) FROM discovered_facilities "
                           "WHERE NOT (merged_at IS NULL AND is_duplicate = 0)")
    out.append(_check("bl_backlog_size", "drainable queue (pending) + processed",
                      None, f"{pending} pending in queue · {processed} already processed (merged+dup)"
                      if pending is not None else "query failed"))
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


# ── lane 4 · media distribution arm (pure-DB) ─────────────────────────

def _lane_media(c) -> list[dict]:
    out = []
    if c is None:
        return [_check("md_nodb", "media lane needs db", None, "no db")]

    # Live schema trap: posted_at=timestamp but published_at=TEXT → cast
    # ::text and regex-guard an ISO date before ::timestamp, take MAX of both.
    def _last_post_age_days(platform_pred: str):
        return _scalar(c,
            "SELECT EXTRACT(EPOCH FROM (now() - MAX(ts)))/86400.0 FROM ("
            " SELECT NULLIF(published_at::text,'')::timestamp AS ts FROM social_media_posts"
            "  WHERE status='published'" + platform_pred +
            "  AND published_at::text ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}'"
            " UNION ALL"
            " SELECT NULLIF(posted_at::text,'')::timestamp AS ts FROM social_media_posts"
            "  WHERE status='published'" + platform_pred +
            "  AND posted_at::text ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}') q")

    # 4a — X publisher alive: a real X post published < 7d ago.
    xage = _last_post_age_days(" AND publish_platform IN ('twitter','x')")
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

    # 4c — the pipeline published SOMETHING (any platform) in 7d. Replaces the
    # old /dc-hub-media edge fetch — page latency is a CF-static frontend
    # concern, not measurable (or fixable) from this origin without a
    # self-request. This is the real "arm still moving" signal.
    aage = _last_post_age_days("")
    if aage is None:
        out.append(_check("md_pipeline_publishing", "media pipeline published something (7d)",
                          False, "no successful post on any platform"))
    else:
        out.append(_check("md_pipeline_publishing", "media pipeline published something (7d)",
                          float(aage) < 7.0, f"last publish (any platform) {round(float(aage),1)}d ago"))
    return out


# ── lane 5 · SEO slug-freeze (pure-DB) ────────────────────────────────

def _lane_seo_slug(c) -> list[dict]:
    out = []
    if c is None:
        return [_check("seo_nodb", "slug lane needs db", None, "no db")]

    # 5a — every sluggable facility carries a frozen canonical_slug (pending=0
    # on BOTH facilities and discovered_facilities). This check DELIBERATELY
    # audits the legacy facilities table (comparing it against
    # discovered_facilities), so the legacy source is intentional. lint: legacy-facilities-ok
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

    # 5b — LOAD-BEARING, now IN-PROCESS (no self-request): recompute the 301
    # target with the SAME helper the /facility/<id> handler uses
    # (seo_pages._canonical_facility_slug) and compare to the STORED slug on
    # the row the 301 resolves against (discovered_facilities). Divergence =
    # the handler would 301 to a URL != the frozen slug = duplicate-URL bleed.
    try:
        from routes.seo_pages import _canonical_facility_slug
    except Exception as e:
        _canonical_facility_slug = None
        logger.debug("[flywheel] seo_pages import failed: %s", e)
    if _canonical_facility_slug is None:
        out.append(_check("seo_301_slug_match", "301 recompute == stored canonical_slug (sampled)",
                          None, "seo_pages helper unavailable"))
    else:
        samples = _rows(c, "SELECT provider, name, canonical_slug FROM discovered_facilities "
                           "WHERE canonical_slug IS NOT NULL AND canonical_slug <> '' "
                           "AND name IS NOT NULL ORDER BY id DESC LIMIT 5")
        if not samples:
            out.append(_check("seo_301_slug_match", "301 recompute == stored canonical_slug (sampled)",
                              None, "no frozen slugs to sample"))
        else:
            match, diverged = 0, []
            for provider, name, slug in samples:
                try:
                    recomputed = _canonical_facility_slug(provider, name)
                except Exception as e:
                    recomputed = None
                    logger.debug("[flywheel] slug recompute failed: %s", e)
                if recomputed and recomputed == slug:
                    match += 1
                else:
                    diverged.append(f"'{recomputed}' != stored '{slug}'")
            out.append(_check("seo_301_slug_match", "301 recompute == stored canonical_slug (sampled)",
                              match == len(samples),
                              f"{match}/{len(samples)} match"
                              + (" · " + "; ".join(diverged[:2]) if diverged else "")))

    # 5c — sitemap has content to emit: facilities carry frozen slugs (the
    # sitemap is derived from them). DB proxy for "crawl surface intact" — no
    # HTTP fetch of the (backend-served, expensive) /sitemap.xml.
    slugged = _scalar(c, "SELECT COUNT(*) FROM discovered_facilities "
                         "WHERE canonical_slug IS NOT NULL AND canonical_slug <> ''")
    out.append(_check("seo_sitemap_content", "sitemap has frozen-slug content (>1000 urls)",
                      (slugged > 1000) if slugged is not None else None,
                      f"{slugged} facilities with frozen slugs" if slugged is not None else "query failed"))
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
        "note": "read-only DIAGNOSTIC probes (pure-DB, no self-requests); names "
                "an actuator per lane but fires nothing; see "
                "routes/flywheel_master_shell.py",
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
        # 404 not 503: the CF worker's failover breaker (proxyWithRetry) treats
        # ANY 5xx from Railway as a dead-origin signal and fails the request
        # over to the stale Render backend — and 2 within 10s trip the breaker
        # site-wide for 30s. A kill-switch must NEVER return 5xx. (2026-07-06.)
        return jsonify(ok=False, error="disabled"), 404
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
        # 404 not 503 — see flywheel_master_tick: a 5xx here trips the CF
        # failover breaker and bounces the whole site to stale Render.
        return Response("flywheel disabled", status=404)
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
        f"DIAGNOSTIC (pure-DB, no self-requests; names an actuator per lane, fires nothing) · "
        f"30s tick cache · auto-refresh 60s · generated {_esc(p['generated_at'])} · "
        f"JSON: /api/v1/admin/flywheel/master-tick</div>"
        + "".join(cards) + "</body>")
    return Response(html, mimetype="text/html")
