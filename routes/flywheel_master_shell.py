"""
routes/flywheel_master_shell.py — Flywheel Master Shell (2026-07-05).

One pane that live-probes the flywheel priorities named in the 07-05
health read (five lanes) plus the 07-18 LANE 6 (the funnel's dominant
bottleneck) and scores each PASS/FAIL, so the operator can watch each fix
land and keep the checks as a standing sentinel afterward.

The six lanes (one per priority — the actual back-half-of-the-funnel work):
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
  6. REACH→USAGE CONVERSION (added 2026-07-18) — the funnel's dominant
     bottleneck: of the external AI-platform requests that FIND us (reach,
     ai_daily_stats — includes crawlers indexing for citations, which is WHY
     the absolute % is structurally low), what share becomes REAL de-looped
     tool calls (mcp_tool_calls via mcp_calls_deloop.real_calls_predicate).
     SAME-WINDOW 7d vs 7d — unlike the funnel page's headline 3.2%, which
     divides 30d usage by CUMULATIVE-since-Feb reach. Growth gauge: a check
     fails ONLY on a genuine >20% relative WoW regression, never on the
     absolute level. Both usage series are only meaningful from 2026-07-05
     (identity/de-loop rails shipped then).

★ PURE-DB shell: every probe is a Neon query or an in-process computation —
NO outbound HTTP. (2026-07-06 incident: an earlier revision fetched
/sitemap.xml and /facility/<id> through the public edge, which routed back
into THIS SAME Flask origin and amplified a chronically-hot connection pool
into thread starvation + 502s. A master-shell tick must never issue a
self-request.) Mirrors the house pattern otherwise: admin-gated, killable
(FLYWHEEL_DISABLED=1), fail-soft, snapshot row per tick, 600s cache + stale-while-revalidate
(a 30s cache on a ~10s tick 502'd at the CF edge — see _TICK_TTL).
READ-ONLY / DIAGNOSTIC: each lane NAMES an actuator but fires NOTHING.

Endpoints:
  GET/POST /api/v1/admin/flywheel/master-tick   JSON scoreboard (6 lanes)
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
    # r-honest-gates (2026-07-31): this number's own detail text has declared
    # itself CONTAMINATED since 07-21 (web-map web-UI mints + one-shot QA/probe
    # keys that can never reuse) and pointed at ret_claim_carry as the real
    # leak. A gauge that documents its own noise must not also be a red gate —
    # the 1d/1e decomposition below IS the gate. Kept visible as an
    # informational gauge (the 8% floor stays in the text for trend-watching).
    out.append(_check("ret_key_reuse", "mature key-reuse % (gauge; floor 8; see 1d/1e gates)",
                      None,
                      f"{reuse}% RAW — contaminated by web-map (web-UI) mints + ~90 one-shot QA/probe keys that can never reuse; the GATED reads are ret_claim_carry + ret_claim_activation" if reuse is not None else "no mature cohort yet"))

    # 1d/1e (2026-07-21): the HONEST decomposition (mirrors routes/
    # backfunnel_master_shell.py). The RAW reuse% above is contaminated by web-map
    # web-UI mints + ~90 one-shot QA/probe keys that can never reuse. The real leak
    # is the claim→carry WIRING gap: claim_free_key mints a key but the session's
    # later calls don't carry it (server.mjs binds only on paid-gated tools / only
    # when sessionMeta already has the session). Live: only ~18% of post-claim
    # sessions carried the key. Actuator (SHIPPED): the /track session→key resolver
    # (DCHUB_CLAIM_SESSION_BIND) + the daily reconcile sweep (DCHUB_TRACK_RECONCILE).
    # r-honest-gates (2026-07-31): both checks render from the SHARED module
    # (routes/retention_claim_checks — backfunnel renders from the same one;
    # the twin copies are gone) and gate the POST-FIX cohort only: the 30d
    # window blends claims minted before the activation wiring existed
    # (07-21/22), and that un-fixable history kept the blended gate red long
    # after the fix landed — the rebaseline-artifact class. Pre-fix + blended
    # stay visible in the detail; the split converges to the original check
    # as the window rolls past 08-21.
    from routes.retention_claim_checks import (claim_activation_verdict,
                                               claim_carry_verdict)
    _cc_pass, _cc_detail = claim_carry_verdict(c)
    out.append(_check("ret_claim_carry",
                      "post-claim sessions carry the new key (post-fix cohort)",
                      _cc_pass, _cc_detail))
    _ca_pass, _ca_detail = claim_activation_verdict(c)
    out.append(_check("ret_claim_activation",
                      "claimed keys used after mint (mature post-fix cohort)",
                      _ca_pass, _ca_detail))

    # r-return-hook measurement (2026-07-26 retention wave): the boards named
    # "measure r-return hook effect" as the step before the durable-identity
    # spend. Among real agents active in the PRIOR completed week, split this
    # week's return rate by whether they called get_changes (the hook) that
    # prior week. Informational — it MEASURES the shipped actuator, it does
    # not gate on it (cohorts are small; a rate needs several weeks to firm).
    _rh = _rows(c, "WITH prior AS ("
                   " SELECT ip_address, bool_or(tool_name='get_changes') AS hooked"
                   " FROM mcp_calls_identity"
                   " WHERE is_real_external IS TRUE"
                   " AND created_at >= date_trunc('week', now()) - interval '7 days'"
                   " AND created_at <  date_trunc('week', now())"
                   " GROUP BY 1)"
                   " SELECT COUNT(*) FILTER (WHERE hooked),"
                   " COUNT(*) FILTER (WHERE hooked AND EXISTS("
                   "   SELECT 1 FROM mcp_calls_identity m WHERE m.ip_address=prior.ip_address"
                   "   AND m.is_real_external IS TRUE AND m.created_at >= date_trunc('week', now()))),"
                   " COUNT(*) FILTER (WHERE NOT hooked),"
                   " COUNT(*) FILTER (WHERE NOT hooked AND EXISTS("
                   "   SELECT 1 FROM mcp_calls_identity m WHERE m.ip_address=prior.ip_address"
                   "   AND m.is_real_external IS TRUE AND m.created_at >= date_trunc('week', now())))"
                   " FROM prior")
    r = _rh[0] if _rh else None
    if not r or (int(r[0] or 0) + int(r[2] or 0)) == 0:
        out.append(_check("ret_return_hook", "r-return hook effect (get_changes → return)",
                          None, "no prior-week real agents yet"))
    else:
        hk, hk_ret, nh, nh_ret = (int(x or 0) for x in r)
        hk_pct = round(100.0 * hk_ret / hk, 1) if hk else None
        nh_pct = round(100.0 * nh_ret / nh, 1) if nh else None
        out.append(_check(
            "ret_return_hook", "r-return hook effect (get_changes → return)",
            True,
            f"hooked: {hk_ret}/{hk} returned ({hk_pct}%) · un-hooked: "
            f"{nh_ret}/{nh} ({nh_pct}%) — the measured lift of the shipped "
            f"hook; several weeks of cohorts before reading it as causal"))
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
    # r-drain-floor (2026-07-18): the 100/7d floor assumed a standing backlog.
    # The queue now drains to EMPTY (pending=0), so throughput follows DISCOVERY
    # inflow — 43 drained/7d with nothing left waiting is a healthy pipeline,
    # not a stall (same stale-floor class as the mna 24h SLA false-breach).
    # Pass when the floor is met OR the queue is effectively empty (<25 pending
    # — a partial discovery burst mid-drain shouldn't flip the lane). A real
    # stall now looks like: pending piling up AND low drain — which still fails.
    pending = _scalar(c, "SELECT COUNT(*) FROM discovered_facilities WHERE status='pending'")
    drain_ok = None
    if drained is not None:
        drain_ok = (int(drained) >= 100) or (pending is not None and int(pending) < 25)
    out.append(_check("bl_backlog_draining", "facilities draining (>=100/7d or queue empty)",
                       drain_ok,
                       f"{drained} drained/7d · {pending if pending is not None else '?'} pending "
                       f"(empty queue = inflow-limited, healthy)" if drained is not None else "query failed"))

    # 2d — GAUGE (r-probe-fix 2026-07-07): the REAL drainable queue = pending rows
    # awaiting auto-approve (status='pending'). The old gauge counted
    # `NOT (merged_at IS NULL AND is_duplicate=0)` = rows ALREADY PROCESSED
    # (merged OR marked duplicate) and mislabeled ~21,904 DONE rows as "unverified
    # backlog". Show pending + processed honestly.
    # (pending computed above for the drain check — reused here)
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
        out.append(_check("seo_slug_frozen", "no facility waits >26h for a frozen canonical_slug",
                          None, f"facilities pending={fpend}, discovered pending={dpend}"))
    else:
        # r-honest-gates (2026-07-31): gate on STALE pending, not instantaneous
        # inflow. Ingestion is continuous and the freeze is a cron (now every
        # 6h) — a batch landing between ticks showed pending>0 and read as a
        # broken loop when the loop was merely between beats (07-31: 202 rows
        # landed 90min after a green freeze run and sat red all morning).
        # Stale = older than 26h ≈ four missed ticks = the LOOP is broken.
        # Fresh pending is harmless since serving/sitemap now compose via the
        # freeze builder — the pre-freeze URL equals the post-freeze slug.
        # Age columns are probed (information_schema) because the LIVE tables
        # are the truth, not the repo DDL; no age column ⇒ strict gate as
        # before.
        stale_bits, stale_total, aged = [], 0, True
        for _tbl, _n in (("facilities", fpend), ("discovered_facilities", dpend)):
            _agecol = _scalar(c, "SELECT column_name FROM information_schema.columns "
                                 f"WHERE table_name = '{_tbl}' AND column_name IN "
                                 "('first_seen','created_at') ORDER BY column_name = 'first_seen' DESC LIMIT 1")
            if not _agecol:
                aged = False
                break
            _stale = _scalar(c, f"SELECT COUNT(*) FROM {_tbl} "
                                "WHERE canonical_slug IS NULL AND name IS NOT NULL AND name <> '' "
                                f"AND {_agecol} < now() - interval '26 hours'")
            if _stale is None:
                aged = False
                break
            stale_total += int(_stale)
            stale_bits.append(f"{_tbl}={_stale} stale (of {_n} pending)")
        if aged:
            out.append(_check("seo_slug_frozen", "no facility waits >26h for a frozen canonical_slug",
                              stale_total == 0,
                              " · ".join(stale_bits) + " — fresh pending is awaiting the "
                              "next 6h freeze tick; serving already composes the frozen form"))
        else:
            out.append(_check("seo_slug_frozen", "all facilities have a frozen canonical_slug",
                              int(fpend) == 0 and int(dpend) == 0,
                              f"pending: facilities={fpend} discovered={dpend} "
                              "(no age column found — strict gate)"))

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


# ── lane 6 · reach→usage conversion (the dominant funnel bottleneck) ──

def _wow_pass(cur, prev, rel_floor: float = 0.20):
    """WoW regression gate for GROWTH gauges: fail ONLY on a genuine
    week-over-week RELATIVE drop > rel_floor (default 20%), never on the
    absolute level — reach includes crawlers indexing us for citations, so
    a low conversion % is structural, not an incident. Returns None
    (indeterminate, rendered '?') when either side is missing or the prior
    week is 0 (no base to regress from)."""
    if cur is None or prev is None:
        return None
    try:
        cur_f, prev_f = float(cur), float(prev)
    except (TypeError, ValueError):
        return None
    if prev_f <= 0:
        return None
    return cur_f >= prev_f * (1.0 - rel_floor)


def _wow_str(cur, prev) -> str:
    """'+17.6% WoW' style delta string; 'n/a WoW' when not computable."""
    try:
        if cur is None or prev is None or float(prev) == 0:
            return "n/a WoW"
        return f"{100.0 * (float(cur) - float(prev)) / float(prev):+.1f}% WoW"
    except (TypeError, ValueError):
        return "n/a WoW"


# Agent-grain guard: until zone-worker v4.9.28 (2026-07-10) CF stripped XFF
# on worker subrequests, so agent_id (md5 of the first XFF token) counted the
# worker's ROTATING Cloudflare egress POPs as agents (132 of the 2026-07-05..12
# week's 212; diagnosed 2026-07-19). The canonical regex lives in
# mcp_calls_deloop.CF_POP_IP_REGEX (the view also NULLs agent_id for POP rows);
# lane 6c keeps an explicit client_ip filter as belt-and-braces so the north-
# star gauge stays honest even against a stale view. Fallback mirror below —
# keep byte-identical with mcp_calls_deloop.
try:
    from mcp_calls_deloop import CF_POP_IP_REGEX as _CF_POP_REGEX
except Exception:
    _CF_POP_REGEX = (
        r"^(104\.(1[6-9]|2[0-7])\.|172\.(6[4-9]|7[01])\.|162\.15[89]\."
        r"|141\.101\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\."
        r"|108\.162\.(19[2-9]|2[0-4][0-9]|25[0-5])\."
        r"|173\.245\.(4[89]|5[0-9]|6[0-3])\."
        r"|188\.114\.(9[6-9]|10[0-9]|11[01])\."
        r"|190\.93\.(24[0-9]|25[0-5])\.|197\.234\.24[0-3]\."
        r"|198\.41\.(12[89]|1[3-9][0-9]|2[0-4][0-9]|25[0-5])\."
        r"|131\.0\.7[2-5]\.|103\.21\.24[4-7]\.|103\.22\.20[0-3]\.|103\.31\.[4-7]\.)"
    )


# Fallback mirror of ai_tracking.AI_PLATFORMS keys (the /ai external-AI
# allowlist) so a broken ai_tracking import degrades the lane, not the tick.
_EXT_AI_PLATFORMS_FALLBACK = (
    "chatgpt", "claude", "gemini", "perplexity", "copilot", "grok",
    "deepseek", "cursor", "windsurf", "meta", "cohere", "you", "smithery",
    "groq", "huggingface", "mistral", "webmcp", "kimi", "qwen", "zai",
    "minimax")


def _ext_ai_platform_in_list() -> str:
    """SQL literal IN-list of external-AI platform keys — the SAME allowlist
    the /ai page and funnel_health's reach card use (ai_tracking.AI_PLATFORMS),
    so lane 6's reach can never drift from the funnel's reach definition."""
    try:
        from ai_tracking import AI_PLATFORMS as _AP
        keys = sorted(_AP.keys())
    except Exception as e:
        logger.debug("[flywheel] ai_tracking import failed, using fallback: %s", e)
        keys = sorted(_EXT_AI_PLATFORMS_FALLBACK)
    return ",".join("'" + str(k).replace("'", "''") + "'" for k in keys)


def _lane_reach_usage(c) -> list[dict]:
    out = []
    if c is None:
        return [_check("ru_nodb", "reach→usage lane needs db", None, "no db")]

    # REACH = external AI-platform requests (ai_daily_stats, /ai allowlist).
    # HONEST DENOMINATOR NOTE: this counts EVERY request from an AI-platform
    # UA — overwhelmingly crawlers indexing us for citations, NOT agents in a
    # tool-use loop. That is WHY the conversion % is structurally low; the
    # signal is the TREND, not the level.
    rrow = _rows(c, "SELECT "
                    " SUM(request_count) FILTER (WHERE date >= CURRENT_DATE - 7),"
                    " SUM(request_count) FILTER (WHERE date < CURRENT_DATE - 7) "
                    "FROM ai_daily_stats "
                    "WHERE date >= CURRENT_DATE - 14 "
                    "AND LOWER(platform) IN (" + _ext_ai_platform_in_list() + ")")
    reach_7d, reach_prev = (rrow[0] if rrow else (None, None))

    # USAGE = real external tool calls — the ONE canonical de-loop
    # (mcp_calls_deloop.real_calls_predicate over mcp_tool_calls), byte-
    # identical to the funnel endpoint's tool_calls_7d_real. Series only
    # meaningful from 2026-07-05. Literal % inside — _rows passes no params.
    #
    # BURST ROBUSTNESS (2026-08-10): the WoW PASS gate is computed on a
    # per-(session, day) WINSORIZED sum, capped at _CAP, so a single runaway
    # session (a loop/batch) making hundreds of real calls in one day cannot
    # dominate the 7d total and flip this lane on a one-off flood. Measured: a
    # 2-session, 741-calls/session "datacolo" burst on 08-01 inflated the raw
    # prior week 6,710 -> 8,525 (raw WoW -69% vs burst-adjusted -62%). Gateways
    # are UNTOUCHED — they carry many SMALL sessions (Smithery ~6 calls/session
    # over 116 sessions/day), and a genuine MULTI-day decline still shows. The
    # RAW sum is still displayed (funnel-identical level); only the gate moves.
    _CAP = 100
    use_7d = use_prev = None
    use_7d_w = use_prev_w = None
    try:
        from mcp_calls_deloop import real_calls_predicate
        urow = _rows(c, "SELECT "
                        " COUNT(*) FILTER (WHERE created_at >= now() - interval '7 days'),"
                        " COUNT(*) FILTER (WHERE created_at < now() - interval '7 days') "
                        "FROM mcp_tool_calls "
                        "WHERE created_at >= now() - interval '14 days' "
                        "AND " + real_calls_predicate())
        if urow:
            use_7d, use_prev = urow[0]
        wrow = _rows(c, "WITH sd AS ("
                        " SELECT session_id, created_at::date AS d,"
                        " (created_at >= now() - interval '7 days') AS tw,"
                        " LEAST(COUNT(*), " + str(_CAP) + ") AS capped"
                        " FROM mcp_tool_calls"
                        " WHERE created_at >= now() - interval '14 days'"
                        " AND " + real_calls_predicate() +
                        " GROUP BY 1, 2, 3)"
                        " SELECT SUM(capped) FILTER (WHERE tw),"
                        " SUM(capped) FILTER (WHERE NOT tw) FROM sd")
        if wrow:
            use_7d_w, use_prev_w = wrow[0]
    except Exception as e:
        logger.debug("[flywheel] lane6 deloop probe failed: %s", e)

    # WoW gate prefers the burst-adjusted values; falls back to raw if the
    # winsorized probe was unavailable, so a query slip never loses the check.
    u7 = use_7d_w if use_7d_w is not None else use_7d
    up = use_prev_w if use_prev_w is not None else use_prev

    # 6a — the conversion itself: same-window 7d reach vs 7d real tool calls,
    # WoW-floored. NOT the funnel page's 3.2% (that divides 30d usage by
    # CUMULATIVE-since-Feb reach); this is the honest weekly rate. Note the
    # two series overlap but aren't strictly nested (anonymous/'unknown'-UA
    # real calls aren't in the reach allowlist), so it's a rate, not a share.
    # Displayed % uses RAW usage (funnel-identical); the PASS gate uses the
    # burst-adjusted rate so a one-day flood can't flip it.
    conv = conv_prev = None
    conv_b = conv_prev_b = None
    if reach_7d and use_7d is not None:
        conv = round(100.0 * float(use_7d) / float(reach_7d), 1)
    if reach_prev and use_prev is not None:
        conv_prev = round(100.0 * float(use_prev) / float(reach_prev), 1)
    if reach_7d and u7 is not None:
        conv_b = round(100.0 * float(u7) / float(reach_7d), 1)
    if reach_prev and up is not None:
        conv_prev_b = round(100.0 * float(up) / float(reach_prev), 1)
    out.append(_check(
        "ru_conversion", "reach→usage % (7d same-window) — WoW floor -20%",
        _wow_pass(conv_b, conv_prev_b),
        (f"{conv}% now ({use_7d} real calls / {reach_7d} AI-platform reqs) vs "
         f"{conv_prev}% prior wk (raw {_wow_str(conv, conv_prev)}; burst-adj "
         f"{_wow_str(conv_b, conv_prev_b)}, per-session-day cap {_CAP}) · "
         f"denominator is mostly citation-crawlers — low % is structural, watch "
         f"the trend · funnel page's headline % uses cumulative-since-Feb reach")
        if conv is not None else "reach or usage series unavailable"))

    # 6b — usage volume WoW: real de-looped tool calls, 7d vs prior 7d. PASS on
    # the burst-adjusted (per-session-day-capped) sum; RAW shown for the
    # funnel-identical level.
    out.append(_check(
        "ru_usage_wow", "real tool calls 7d — WoW floor -20%",
        _wow_pass(u7, up),
        (f"{use_7d} real calls/7d vs {use_prev} prior (raw {_wow_str(use_7d, use_prev)}) "
         f"· burst-adjusted {use_7d_w} vs {use_prev_w} ({_wow_str(use_7d_w, use_prev_w)}, "
         f"per-session-day cap {_CAP}) · canonical de-loop · series from 2026-07-05")
        if use_7d is not None else "de-loop probe failed"))

    # 6c — distinct real agents WoW (mcp_calls_identity — the north-star
    # grain, same is_public_ip AND is_real_external filter as lane 3), on
    # the SAME-GRAIN basis: CF-POP client_ips excluded from BOTH windows
    # (see _CF_POP_REGEX — pre-2026-07-10 rows carry rotating CF egress
    # IPs as agent_ids, so the raw WoW compares POP breadth to caller
    # breadth and reads as a fake -33% collapse).
    arow = _rows(c, "SELECT "
                    " count(DISTINCT agent_id) FILTER (WHERE created_at >= now() - interval '7 days'),"
                    " count(DISTINCT agent_id) FILTER (WHERE created_at < now() - interval '7 days') "
                    "FROM mcp_calls_identity "
                    "WHERE is_public_ip AND is_real_external "
                    "AND client_ip !~ '" + _CF_POP_REGEX + "' "
                    "AND created_at >= now() - interval '14 days'")
    ag_7d, ag_prev = (arow[0] if arow else (None, None))
    out.append(_check(
        "ru_agents_wow", "distinct real agents 7d — WoW floor -20%",
        _wow_pass(ag_7d, ag_prev),
        (f"{ag_7d} distinct real agents/7d vs {ag_prev} prior ({_wow_str(ag_7d, ag_prev)}) "
         f"· mcp_calls_identity, is_public_ip AND is_real_external, CF-POP IPs excluded "
         f"(same-grain basis: until worker v4.9.28 on 2026-07-10 the edge stripped XFF, "
         f"so agent_id counted rotating Cloudflare POPs — pre-fix breadth was inflated)")
        if ag_7d is not None else "identity view unavailable"))
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
    ("reach_usage",   "6 · Reach→usage conversion",          _lane_reach_usage,
     "planner/prompts/quickstarts shipped 07-18 — measure their effect here; "
     "next: WebMCP + directory placements"),
]

_cache: dict = {"ts": 0.0, "payload": None}
_cache_lock = threading.Lock()

# ★ WAS 30.0 — a 30-SECOND cache on a ~10-SECOND computation, warmed by a cron
#   that fires ONCE A DAY (cron_heartbeat "flywheel_master_tick_daily", hour 14).
#   So for ~23h59m of every day the cache was cold, every human visit paid the
#   full compute, and the Cloudflare worker gave up first: measured 2026-08-08,
#   edge **502 in 10.2s** while the Railway origin returned **200 in 9.8s**.
#   The page was never down — it was never reachable THROUGH THE EDGE.
_TICK_TTL = 600.0

# How long a stale payload may still be served while a refresh runs behind it.
# Past this the data is too old to show even with a banner.
_STALE_OK = 3600.0

# ★ Guarded by _cache_lock, NOT a bare Event: `if not set: set()` is a
#   check-then-act race — two visitors arriving together can both pass the
#   check before either sets it, and both start a ~10s tick against the same
#   pool. Claim the slot atomically instead.
_refreshing = False


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


def _refresh_async() -> None:
    """Recompute behind a served response. Single-flight: a burst of visitors
    must not start N ten-second ticks against the same pool."""
    global _refreshing
    with _cache_lock:
        if _refreshing:
            return
        _refreshing = True                      # claimed atomically

    def _work():
        global _refreshing
        try:
            payload = _run_tick()
            with _cache_lock:
                _cache["ts"] = time.time()
                _cache["payload"] = payload
        except Exception as e:  # noqa: BLE001
            logger.warning("[flywheel] background refresh failed: %s", e)
        finally:
            with _cache_lock:
                _refreshing = False

    threading.Thread(target=_work, name="flywheel-refresh", daemon=True).start()


def _tick_cached(allow_stale: bool = True) -> dict:
    """Fresh if we have it; otherwise STALE-WHILE-REVALIDATE.

    ★ The old version blocked every caller on a cold cache. With a ~10s tick
      behind a Cloudflare worker that gives up around 10s, that meant the first
      visitor after each TTL expiry got a 502 — and with the old 30s TTL that
      was very nearly every visitor. Serving the previous payload immediately
      and refreshing behind it means a cold cache costs freshness, never
      availability.
    """
    now = time.time()
    with _cache_lock:
        payload, ts = _cache["payload"], _cache["ts"]
    age = now - ts

    if payload is not None and age < _TICK_TTL:
        return payload

    if payload is not None and allow_stale and age < _STALE_OK:
        # Serve what we have, refresh behind it, and SAY it is stale — a page
        # that cannot admit its own staleness is the trap this platform has
        # paid for repeatedly.
        _refresh_async()
        out = dict(payload)
        out["served_stale"] = True
        out["stale_age_seconds"] = int(age)
        return out

    # Nothing cached (cold boot) or too old to show: compute inline. Callers
    # that must not block pass allow_stale=False and get whatever exists.
    payload = _run_tick()
    with _cache_lock:
        _cache["ts"] = time.time()
        _cache["payload"] = payload
    return payload


# ── routes ────────────────────────────────────────────────────────────

# AUTO-REPAIR: duplicate route '/api/v1/admin/flywheel/master-tick' also in tests/test_integrity_shell_and_stale_gate.py:60 — review and remove one
@flywheel_master_shell_bp.route("/api/v1/admin/flywheel/master-tick", methods=["GET", "POST"])
def flywheel_master_tick():
    if _disabled():
        # 404 not 404: the CF worker's failover breaker (proxyWithRetry) treats
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
        # 404 not 404 — see flywheel_master_tick: a 5xx here trips the CF
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

    # ★ 2026-07-24: this page was read as a 4-lane collapse (0 agents, 0 real
    # calls, X publisher dead 11d) while the real flywheel was 4/6 with 75
    # agents. It had been served by the RENDER failover origin off a database
    # frozen at 07-13, at HTTP 200. The CF header x-dc-hub-served-by carried
    # the truth but is invisible in a browser. The origin now states its own
    # identity in the page body. Empty string on a healthy primary.
    try:
        from routes.failover_stale_gate import banner_html
        banner = banner_html()
    except Exception as _bexc:
        logger.debug("[flywheel] origin banner unavailable: %s", _bexc)
        banner = ""

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
        f"<div style='color:#64748b;font-size:12px'>07-05 flywheel priorities + 07-18 lane 6 · read-only "
        f"DIAGNOSTIC (pure-DB, no self-requests; names an actuator per lane, fires nothing) · "
        f"30s tick cache · auto-refresh 60s · generated {_esc(p['generated_at'])} · "
        f"JSON: /api/v1/admin/flywheel/master-tick</div>"
        + banner + "".join(cards) + "</body>")
    return Response(html, mimetype="text/html")
