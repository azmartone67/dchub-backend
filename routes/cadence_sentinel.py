"""
routes/cadence_sentinel.py — generic cadence dead-man sentinel (2026-07-11).

Built after THREE silent stalls found 07-10/11 that nothing alerted on:
  • Bluesky publisher stalled 29h with 50 approved posts queued
  • Grid-Data shell's gridstatus ingest stuck 7 DAYS (self-heal fired every
    tick, zero re-ingests 07-03 → 07-10)
  • LinkedIn market-verdict queue accumulated 21 failed + 39 backlog while
    sibling LinkedIn paths posted fine

Each stall had a healthy-looking dashboard somewhere; what was missing was a
DEAD-MAN check: "this lane normally produces a row every N hours — has it?"

Design: a REGISTRY of lanes (`LANES`). Each lane declares a freshness probe
(SQL returning age-in-hours of the newest row, or — for TEXT timestamp
columns like social_media_posts.published_at, which holds MIXED formats — a
SQL returning raw text timestamps that we parse defensively in Python) plus
an expected max gap, and optionally a queue probe (depth SQL + threshold).

Stall rules (evaluate_lane — pure logic, unit-tested):
  1. GAP    — newest activity older than max_gap_hours.
  2. QUEUE  — queue depth >= threshold while drain count in the window is
              EXACTLY 0 (an unknown drain count never fires the rule).
A probe that errors reports the lane as UNKNOWN — never a false stall.

One tick (heartbeat-dispatched every ~3h) evaluates every lane and files a
brain finding per stalled lane via routes/brain_findings_writer
.upsert_brain_finding — the CANONICAL writer (hand-rolled INSERTs against
brain_findings fail silently on schema drift). Healthy lanes with a
previously-open cadence finding are upserted to status='resolved' (only when
one is actually open, so seen_count doesn't inflate on every healthy tick).

★ FINDINGS ONLY in v1 — no auto-restart / self-heal actions. The sentinel's
job is to make a stall LOUD, not to guess at a fix.

★ PURE-DB shell: every probe is a Neon query — no outbound HTTP (the
self-request pattern caused the 07-06 flywheel outage). Probe connection is
autocommit; the findings write uses a SEPARATE transaction-mode connection
because upsert_brain_finding is savepoint-wrapped and SAVEPOINT under
autocommit silently no-ops (the all-zeros trap). Disabled state returns 404,
NOT 5xx (a 5xx trips the CF failover breaker → stale Render).

Endpoints:
  GET/POST /api/v1/admin/cadence-sentinel/master-tick   JSON scoreboard
  GET      /admin/cadence-sentinel                      HTML dashboard
  GET      /api/v1/admin/cadence-sentinel               CF zone-worker bypass

Auth: X-Admin-Key header or ?admin_key= vs DCHUB_ADMIN_KEY (→ DCHUB_INTERNAL_KEY).
Kill: CADENCE_SENTINEL_DISABLE=1.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from html import escape as _esc

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

cadence_sentinel_bp = Blueprint("cadence_sentinel", __name__)

FINDING_PREFIX = "cadence_stall_"
DETECTOR = "cadence_sentinel"
DRAIN_WINDOW_HOURS_DEFAULT = 24.0

# ── auth / kill ───────────────────────────────────────────────────────

def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("CADENCE_SENTINEL_DISABLE") or "").strip() == "1"


# ── lane registry ─────────────────────────────────────────────────────
# Every SQL string here is LITERAL-ONLY (executed with no params tuple —
# a stray %s or an empty () would trigger %-substitution, the empty-tuple
# trap). Age probes return HOURS as a scalar. text_ts_sql returns one raw
# text-timestamp column, newest rows first, bounded — parsed in Python
# because social_media_posts.published_at is TEXT with mixed formats.
#
# Lane fields:
#   key / label / why      identity + the stall this lane exists to catch
#   age_sql                scalar age-in-hours of newest activity (optional)
#   text_ts_sql            raw text timestamps, newest-first (optional;
#                          feeds BOTH freshness age and the drain count)
#   max_gap_hours          expected max gap before the GAP rule fires
#   queue_sql              scalar queue depth (optional)
#   queue_threshold        depth at/above which the QUEUE rule can fire
#   drain_window_hours     window for "drained recently" (default 24)

_SMP_BLUESKY = ("(publish_platform IN ('bluesky', 'all') "
                "OR platform = 'bluesky' OR bluesky_uri IS NOT NULL)")
_SMP_LINKEDIN = ("(publish_platform IN ('linkedin', 'all') "
                 "OR platform = 'linkedin')")

LANES = [
    {
        "key": "linkedin_publish",
        "label": "LinkedIn publishes (linkedin_posts)",
        "why": "quad slots 12/16 UTC = 2 posts/day; a silent token or "
               "publisher stall shows as no success rows",
        # failures write rows too (with error/error_message set) — they must
        # NOT reset the dead-man clock, so filter to success-shaped rows.
        "age_sql": (
            "SELECT EXTRACT(EPOCH FROM (now() "
            "- MAX(COALESCE(published_at, posted_at, created_at)))) / 3600.0 "
            "FROM linkedin_posts "
            "WHERE COALESCE(NULLIF(error, ''), NULLIF(error_message, '')) IS NULL "
            "AND COALESCE(status, 'success') NOT IN "
            "('failed', 'error', 'revoked', 'rejected')"),
        "max_gap_hours": 36,
    },
    {
        "key": "smp_other_publish",
        "label": "non-LinkedIn publishes (social_media_posts)",
        "why": "twitter/mastodon/bluesky cross-posts; published_at is TEXT "
               "with mixed formats — parsed defensively",
        "text_ts_sql": (
            "SELECT published_at FROM social_media_posts "
            "WHERE status = 'published' "
            "AND COALESCE(publish_platform, platform) IS NOT NULL "
            "AND COALESCE(publish_platform, platform) <> 'linkedin' "
            "ORDER BY id DESC LIMIT 300"),
        "max_gap_hours": 48,
    },
    {
        "key": "bluesky_publish",
        "label": "Bluesky publishes (social_media_posts)",
        "why": "07-10 stall: publisher dark 29h with 50 approved posts "
               "queued and nothing alerting",
        "text_ts_sql": (
            "SELECT published_at FROM social_media_posts "
            "WHERE status = 'published' AND " + _SMP_BLUESKY +
            " ORDER BY id DESC LIMIT 300"),
        # 6h publisher fires × 3/day cap ⇒ healthy inter-post gaps run to ~18h,
        # and a thin-content day trivially reaches 37-40h — so a 36h GAP trips on
        # cadence, not a stall (this lane's own QUEUE rule stayed silent at the
        # 37.4h alert, i.e. approved depth < 10). Align with the sibling
        # smp_other_publish lane (48h). A REAL jam (approved depth ≥ 10 not
        # draining) is still caught by the QUEUE rule below, which is unchanged.
        "max_gap_hours": 48,
        "queue_sql": (
            "SELECT count(*) FROM social_media_posts "
            "WHERE status = 'approved' AND " + _SMP_BLUESKY),
        "queue_threshold": 10,
        "drain_window_hours": 24,
    },
    {
        "key": "smp_linkedin_queue",
        "label": "LinkedIn approved-queue drain (social_media_posts)",
        "why": "07-10 stall: market-verdict queue accumulated 21 failed + "
               "39 backlog while sibling LinkedIn paths posted fine — so "
               "this lane watches the QUEUE itself, not overall LinkedIn "
               "output (queue-only: no max_gap; verdict posts are "
               "legitimately sparse)",
        "text_ts_sql": (
            "SELECT published_at FROM social_media_posts "
            "WHERE status = 'published' AND " + _SMP_LINKEDIN +
            " ORDER BY id DESC LIMIT 300"),
        "queue_sql": (
            "SELECT count(*) FROM social_media_posts "
            "WHERE status = 'approved' AND " + _SMP_LINKEDIN),
        "queue_threshold": 10,
        "drain_window_hours": 48,
    },
    {
        "key": "grid_ext_ingest",
        "label": "gridstatus ingest (grid_ext_metrics)",
        "why": "07-03→07-10 stall: grid-data shell self-healed every tick "
               "with ZERO re-ingests for 7 days",
        "age_sql": (
            "SELECT EXTRACT(EPOCH FROM (now() - MAX(ingested_at))) / 3600.0 "
            "FROM grid_ext_metrics"),
        "max_gap_hours": 48,
    },
    {
        "key": "grid_ext_breadth",
        "label": "gridstatus per-dataset refresh breadth (grid_ext_metrics)",
        "why": "age of the 3rd-freshest dataset's ingested_at — catches "
               "'one dataset still ingesting, the rest stuck', which the "
               "overall MAX(ingested_at) dead-man cannot see. Top-3 (not "
               "wider): the table also holds one-shot Depth-shell "
               "absorptions (hosting_capacity:*) that legitimately never "
               "refresh; only ~3 dataset families recur (live-calibrated "
               "07-11: 67 dataset_ids, 3 recurring)",
        "age_sql": (
            "SELECT EXTRACT(EPOCH FROM (now() - last_ing)) / 3600.0 FROM ("
            "SELECT MAX(ingested_at) AS last_ing FROM grid_ext_metrics "
            "GROUP BY dataset_id ORDER BY 1 DESC LIMIT 3) t "
            "ORDER BY last_ing ASC LIMIT 1"),
        "max_gap_hours": 72,
    },
    {
        "key": "iso_lmp_ingest",
        "label": "ISO LMP feed (iso_lmp_snapshots)",
        "why": "6-ISO LMP cron fires every 10 min; hours of silence = the "
               "feed loop is dead, not a source hiccup",
        "age_sql": (
            "SELECT EXTRACT(EPOCH FROM (now() - MAX(fetched_at))) / 3600.0 "
            "FROM iso_lmp_snapshots"),
        "max_gap_hours": 6,
    },
    {
        "key": "iso_queue_ingest",
        "label": "ISO interconnection-queue ingest (iso_queue_snapshots)",
        "why": "daily 06:00 UTC ingest; upsert + heartbeat_touch both stamp "
               "ingested_at, so 3 silent days = the ingest path is dead",
        "age_sql": (
            "SELECT EXTRACT(EPOCH FROM (now() - MAX(ingested_at))) / 3600.0 "
            "FROM iso_queue_snapshots"),
        "max_gap_hours": 72,
    },
    {
        "key": "dcpi_daily_snapshots",
        "label": "DCPI daily snapshots (dcpi_daily_snapshots)",
        "why": "per-market daily snapshot rows feed temporal deltas + the "
               "winback digest; captured_at is the write-time stamp",
        "age_sql": (
            "SELECT EXTRACT(EPOCH FROM (now() - MAX(captured_at))) / 3600.0 "
            "FROM dcpi_daily_snapshots"),
        "max_gap_hours": 48,
    },
    {
        "key": "automerge_activity",
        "label": "brain auto-merge activity (brain_automerge_log)",
        "why": "auto-merge went LIVE 07-11 — merge or would_merge rows "
               "should land within 48h; silence means the gate or the "
               "canary loop regressed dark",
        "age_sql": (
            "SELECT EXTRACT(EPOCH FROM (now() - MAX(merged_at))) / 3600.0 "
            "FROM brain_automerge_log"),
        "max_gap_hours": 48,
    },
    {
        "key": "ai_citations",
        "label": "AI citation observations (ai_citations)",
        "why": "citation velocity froze once before (unfrozen 07-10); the "
               "scraper writes observed_at rows on every scan",
        "age_sql": (
            "SELECT EXTRACT(EPOCH FROM (now() - MAX(observed_at))) / 3600.0 "
            "FROM ai_citations"),
        "max_gap_hours": 72,
    },
    # ── r-daily-callout (2026-07-18): the three lanes the July press-stall
    # post-mortem found NOBODY watching. press_releases gained rows daily
    # while the public page froze (that half lives in
    # brain_consistency_radar.check_press_public_surface_stale — this
    # sentinel is pure-DB by design); these cover the DB half + the two
    # publish lanes with no dedicated dead-man.
    {
        "key": "press_generation",
        "label": "press release generation (press_releases)",
        "why": "auto-press targets 18h cadence via press-publisher/run; "
               "2 silent days = the generator lane is dead, not a lull",
        "age_sql": (
            "SELECT EXTRACT(EPOCH FROM (now() - MAX(created_at))) / 3600.0 "
            "FROM press_releases"),
        "max_gap_hours": 48,
    },
    {
        "key": "twitter_publish",
        "label": "X/Twitter publishes (social_media_posts)",
        "why": "bluesky posts daily into the same table, so the generic "
               "smp_other_publish lane can NEVER see a twitter-only stall "
               "(live 07-18: 9 twitter posts in 30d vs 84 bluesky)",
        "text_ts_sql": (
            "SELECT published_at FROM social_media_posts "
            "WHERE status = 'published' "
            "AND COALESCE(publish_platform, platform) = 'twitter' "
            "ORDER BY id DESC LIMIT 300"),
        "max_gap_hours": 120,
    },
    {
        "key": "weekly_digest_send",
        "label": "weekly brain digest email (brain_digest_log)",
        "why": "the operator digest is itself a pipeline; a silent digest "
               "means the visibility loop is dark (weekly cadence, so 10d "
               "before this fires)",
        "age_sql": (
            "SELECT EXTRACT(EPOCH FROM (now() - MAX(sent_at))) / 3600.0 "
            "FROM brain_digest_log"),
        "max_gap_hours": 240,
    },
]


# ── pure logic (unit-tested; no DB, no Flask) ─────────────────────────

_TS_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
)


def parse_ts_defensive(val):
    """Best-effort parse of a mixed-format timestamp into an AWARE UTC
    datetime. social_media_posts.published_at is TEXT written by several
    generations of publishers (isoformat with/without T, with/without
    microseconds, trailing Z, +00/+00:00 offsets, bare dates) — so accept
    broadly and return None for anything unparseable instead of raising."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    if isinstance(val, date):
        return datetime(val.year, val.month, val.day, tzinfo=timezone.utc)
    s = str(val).strip()
    if not s:
        return None
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    # PG-style short offset ('+00' / '-05') → pad to '+00:00' for strptime-
    # free ISO parsing. Only when a time component exists — a bare date
    # like '2026-07-10' also ends in [-]digits and must not be padded.
    if ":" in s and len(s) >= 3 and s[-3] in "+-" and s[-2:].isdigit():
        s = s + ":00"
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    base = s.split("+")[0].strip()
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(base, fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def latest_and_recent(values, now, window_hours):
    """(newest parseable timestamp, count within the last window_hours).
    Unparseable entries are skipped — one junk row must never blind the
    lane. Returns (None, 0) for no parseable timestamps at all."""
    latest = None
    recent = 0
    cutoff = now - timedelta(hours=float(window_hours))
    for v in values:
        dt = parse_ts_defensive(v)
        if dt is None:
            continue
        if latest is None or dt > latest:
            latest = dt
        if dt >= cutoff:
            recent += 1
    return latest, recent


def evaluate_lane(spec, age_hours=None, queue_depth=None, drained_recent=None):
    """The stall decision for one lane. Pure — probes are passed in.

    GAP rule:   age_hours known and > max_gap_hours.
    QUEUE rule: depth known and >= threshold AND drained_recent is EXACTLY
                0 (None = drain unknown → never fires; a probe error must
                not manufacture a stall).
    A lane whose freshness probe returned None is UNKNOWN, not stalled.
    """
    reasons = []
    gap = spec.get("max_gap_hours")
    if gap is not None and age_hours is not None and float(age_hours) > float(gap):
        reasons.append(
            f"gap: newest activity {float(age_hours):.1f}h ago "
            f"(expected max {gap}h)")
    threshold = spec.get("queue_threshold")
    if (spec.get("queue_sql") and threshold is not None
            and queue_depth is not None and int(queue_depth) >= int(threshold)
            and drained_recent == 0):
        window = spec.get("drain_window_hours", DRAIN_WINDOW_HOURS_DEFAULT)
        reasons.append(
            f"queue: {int(queue_depth)} queued >= {int(threshold)} "
            f"with 0 drained in {window}h")
    return {
        "stalled": bool(reasons),
        "reasons": reasons,
        "unknown": (gap is not None and age_hours is None),
        "age_hours": (round(float(age_hours), 2)
                      if age_hours is not None else None),
        "queue_depth": (int(queue_depth) if queue_depth is not None else None),
        "drained_recent": (int(drained_recent)
                           if drained_recent is not None else None),
    }


def validate_lanes(lanes) -> list:
    """Registry shape errors (unit-tested so a bad edit fails CI, not prod)."""
    errors = []
    seen = set()
    for spec in lanes:
        key = spec.get("key") or "<missing>"
        if key in seen:
            errors.append(f"{key}: duplicate key")
        seen.add(key)
        if not spec.get("label"):
            errors.append(f"{key}: missing label")
        has_fresh = bool(spec.get("age_sql") or spec.get("text_ts_sql"))
        has_queue = bool(spec.get("queue_sql"))
        if not has_fresh and not has_queue:
            errors.append(f"{key}: declares no probe at all")
        if spec.get("age_sql") and spec.get("text_ts_sql"):
            errors.append(f"{key}: age_sql and text_ts_sql are exclusive")
        if has_fresh and spec.get("max_gap_hours") is None and not has_queue:
            errors.append(f"{key}: freshness probe without max_gap_hours")
        if spec.get("max_gap_hours") is not None \
                and not float(spec["max_gap_hours"]) > 0:
            errors.append(f"{key}: max_gap_hours must be > 0")
        if has_queue:
            if spec.get("queue_threshold") is None:
                errors.append(f"{key}: queue_sql without queue_threshold")
            if not spec.get("text_ts_sql"):
                errors.append(f"{key}: queue rule needs text_ts_sql to "
                              f"measure the drain")
        for field in ("age_sql", "text_ts_sql", "queue_sql"):
            sql = spec.get(field) or ""
            if "%" in sql:
                errors.append(f"{key}: {field} contains '%' — lane SQL is "
                              f"literal-only (empty-tuple %-trap)")
    return errors


def finding_issue(lane_key: str) -> str:
    return f"{FINDING_PREFIX}{lane_key}"[:200]


def finding_url(lane_key: str) -> str:
    return f"/admin/cadence-sentinel#{lane_key}"


# ── db probes (fail-soft; autocommit; literal SQL only) ───────────────

def _conn():
    try:
        import psycopg2 as _pg
        url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        if not url:
            return None
        c = _pg.connect(url, connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning("[cadence] db connect failed: %s", e)
        return None


def _scalar(c, sql: str):
    """None on error AND on no-rows/NULL — the evaluator treats None as
    UNKNOWN, never as a stall."""
    try:
        with c.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
        return row[0] if row else None
    except Exception as e:
        logger.debug("[cadence] scalar failed: %s -- %s", sql[:80], e)
        try:
            c.rollback()
        except Exception:
            pass
        return None


def _column(c, sql: str):
    """First column of every row; None on ERROR (distinct from [] = genuinely
    empty — an empty publish history plus a full queue IS a stall, but a
    failed query must never be)."""
    try:
        with c.cursor() as cur:
            cur.execute(sql)
            return [r[0] for r in cur.fetchall()]
    except Exception as e:
        logger.debug("[cadence] column failed: %s -- %s", sql[:80], e)
        try:
            c.rollback()
        except Exception:
            pass
        return None


def _probe_lane(c, spec, now):
    """Run a lane's declared probes. Returns kwargs for evaluate_lane."""
    age_hours = None
    drained_recent = None
    if c is not None and spec.get("age_sql"):
        v = _scalar(c, spec["age_sql"])
        try:
            age_hours = float(v) if v is not None else None
        except Exception:
            age_hours = None
    if c is not None and spec.get("text_ts_sql"):
        window = spec.get("drain_window_hours", DRAIN_WINDOW_HOURS_DEFAULT)
        raw = _column(c, spec["text_ts_sql"])
        if raw is not None:
            latest, recent = latest_and_recent(raw, now, window)
            drained_recent = recent
            if latest is not None:
                age_hours = (now - latest).total_seconds() / 3600.0
    queue_depth = None
    if c is not None and spec.get("queue_sql"):
        v = _scalar(c, spec["queue_sql"])
        try:
            queue_depth = int(v) if v is not None else None
        except Exception:
            queue_depth = None
    return {"age_hours": age_hours, "queue_depth": queue_depth,
            "drained_recent": drained_recent}


# ── findings (canonical writer; SEPARATE transaction-mode connection) ─

def _file_findings(lanes_out) -> dict:
    """Upsert one open finding per stalled lane; resolve findings for lanes
    that recovered (only those actually open — resolving on every healthy
    tick would inflate seen_count toward the runaway-quarantine threshold).
    upsert_brain_finding is savepoint-wrapped, and SAVEPOINT under autocommit
    silently no-ops (the all-zeros trap) — so this connection stays in
    transaction mode and commits once at the end."""
    stalled = [l for l in lanes_out if l["stalled"]]
    result = {"filed": 0, "resolved": 0, "skipped": 0}
    conn = None
    try:
        import psycopg2 as _pg
        url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        if not url:
            return result
        conn = _pg.connect(url, connect_timeout=8)  # transaction mode
        from routes.brain_findings_writer import upsert_brain_finding
        with conn.cursor() as cur:
            open_issues = set()
            try:
                # execute WITHOUT a params tuple — literal % in LIKE is safe
                # only when no %-substitution pass runs.
                cur.execute(
                    "SELECT issue FROM brain_findings "
                    "WHERE issue LIKE 'cadence\\_stall\\_%' "
                    "AND COALESCE(status, 'open') NOT IN "
                    "('resolved', 'wont_fix', 'dismissed')")
                open_issues = {r[0] for r in cur.fetchall()}
            except Exception as e:
                logger.debug("[cadence] open-issue probe failed: %s", e)
                try:
                    conn.rollback()
                except Exception:
                    pass
            for lane in stalled:
                detail = (
                    f"{lane['label']} — " + "; ".join(lane["reasons"])
                    + f". age_hours={lane['age_hours']} "
                      f"queue_depth={lane['queue_depth']} "
                      f"drained_recent={lane['drained_recent']}. "
                    + f"why watched: {lane['why']}")
                outcome = upsert_brain_finding(
                    cur,
                    issue=finding_issue(lane["key"]),
                    url=finding_url(lane["key"]),
                    count=1,
                    detail=detail[:2000],
                    detector=DETECTOR,
                    status="open",
                )
                result["filed" if outcome in ("inserted", "updated")
                       else "skipped"] += 1
            for lane in lanes_out:
                if lane["stalled"] or lane.get("unknown"):
                    continue
                if finding_issue(lane["key"]) not in open_issues:
                    continue
                outcome = upsert_brain_finding(
                    cur,
                    issue=finding_issue(lane["key"]),
                    url=finding_url(lane["key"]),
                    count=1,
                    detail=(f"{lane['label']} recovered — "
                            f"age_hours={lane['age_hours']} "
                            f"queue_depth={lane['queue_depth']} "
                            f"drained_recent={lane['drained_recent']}"),
                    detector=DETECTOR,
                    status="resolved",
                )
                if outcome in ("inserted", "updated"):
                    result["resolved"] += 1
        conn.commit()
    except Exception as e:
        logger.warning("[cadence] findings write failed: %s", e)
        try:
            if conn is not None:
                conn.rollback()
        except Exception:
            pass
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
    return result


# ── tick orchestration ────────────────────────────────────────────────

_cache: dict = {"ts": 0.0, "payload": None}
_cache_lock = threading.Lock()
# 10 min — the heartbeat's wide minute windows re-fire in-hour; a longer
# TTL than the registry shell's 30s keeps repeat fires from re-bumping
# stalled findings' seen_count several times an hour.
_TICK_TTL = 600.0


def _ensure_snapshots(c) -> None:
    try:
        with c.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS cadence_sentinel_snapshots ("
                " id BIGSERIAL PRIMARY KEY,"
                " created_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
                " lanes_stalled INT, lanes_total INT, payload JSONB)")
    except Exception as e:
        logger.debug("[cadence] snapshot ddl skipped: %s", e)


def _run_tick() -> dict:
    now = datetime.now(timezone.utc)
    c = _conn()
    lanes_out = []
    for spec in LANES:
        try:
            probes = _probe_lane(c, spec, now)
            verdict = evaluate_lane(spec, **probes)
        except Exception as e:
            verdict = {"stalled": False, "unknown": True, "reasons": [],
                       "age_hours": None, "queue_depth": None,
                       "drained_recent": None}
            logger.warning("[cadence] lane %s crashed: %s",
                           spec.get("key"), e)
        lanes_out.append({
            "key": spec["key"], "label": spec["label"], "why": spec["why"],
            "max_gap_hours": spec.get("max_gap_hours"),
            "queue_threshold": spec.get("queue_threshold"),
            **verdict,
        })
    findings = _file_findings(lanes_out) if c is not None else \
        {"filed": 0, "resolved": 0, "skipped": len(
            [l for l in lanes_out if l["stalled"]])}
    payload = {
        "ok": True,
        "generated_at": now.isoformat(),
        "lanes_total": len(lanes_out),
        "lanes_stalled": sum(1 for l in lanes_out if l["stalled"]),
        "lanes_unknown": sum(1 for l in lanes_out if l.get("unknown")),
        "findings": findings,
        "lanes": lanes_out,
        "note": "dead-man cadence sentinel — FINDINGS ONLY (no auto-restart "
                "in v1); see routes/cadence_sentinel.py",
    }
    if c is not None:
        try:
            _ensure_snapshots(c)
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO cadence_sentinel_snapshots "
                    "(lanes_stalled, lanes_total, payload) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    (payload["lanes_stalled"], payload["lanes_total"],
                     json.dumps(payload)))
        except Exception as e:
            logger.debug("[cadence] snapshot insert failed: %s", e)
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


# ── routes (disabled → 404, NEVER 5xx: a 5xx trips the CF failover breaker) ──

@cadence_sentinel_bp.route("/api/v1/admin/cadence-sentinel/master-tick",
                           methods=["GET", "POST"])
def cadence_sentinel_master_tick():
    if _disabled():
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    if (request.args.get("fresh") or "") == "1":
        with _cache_lock:
            _cache["payload"] = None
    return jsonify(_tick_cached())


@cadence_sentinel_bp.route("/admin/cadence-sentinel", methods=["GET"])
@cadence_sentinel_bp.route("/api/v1/admin/cadence-sentinel", methods=["GET"])
def cadence_sentinel_dashboard():
    if _disabled():
        return Response("cadence-sentinel disabled", status=404)
    if not _admin_ok():
        return Response("forbidden — X-Admin-Key or ?admin_key=", status=403)
    p = _tick_cached()

    def _chip(lane):
        if lane["stalled"]:
            return '<span style="color:#ef4444">✗ STALLED</span>'
        if lane.get("unknown"):
            return '<span style="color:#eab308">? unknown</span>'
        return '<span style="color:#22c55e">✓</span>'

    cards = []
    for lane in p["lanes"]:
        border = "#ef4444" if lane["stalled"] else "#334155"
        facts = (f"age {lane['age_hours']}h / max {lane['max_gap_hours']}h"
                 if lane["max_gap_hours"] is not None else "queue-only lane")
        if lane["queue_threshold"] is not None:
            facts += (f" · queue {lane['queue_depth']} (threshold "
                      f"{lane['queue_threshold']}, drained "
                      f"{lane['drained_recent']})")
        reasons = "".join(f"<div style='color:#f87171'>→ {_esc(r)}</div>"
                          for r in lane["reasons"])
        cards.append(
            f"<div id='{_esc(lane['key'])}' style='background:#0f172a;"
            f"border:1px solid {border};border-radius:12px;"
            f"padding:16px;margin:12px 0'>"
            f"<div style='font-weight:700;font-size:15px'>{_chip(lane)} "
            f"{_esc(lane['label'])}</div>"
            f"<div style='margin-top:6px;font-size:13px'>{_esc(facts)}</div>"
            f"{reasons}"
            f"<div style='margin-top:8px;font-size:12px;color:#64748b'>"
            f"{_esc(lane['why'])}</div></div>")

    green = p["lanes_stalled"] == 0
    html = (
        "<!doctype html><meta charset='utf-8'>"
        "<meta http-equiv='refresh' content='120'>"
        "<title>Cadence Sentinel · DC Hub</title>"
        "<body style='background:#020617;color:#e2e8f0;font-family:-apple-system,"
        "Segoe UI,Roboto,sans-serif;max-width:880px;margin:24px auto;padding:0 16px'>"
        f"<h2 style='margin:0 0 4px'>Cadence Dead-Man Sentinel "
        f"<span style='color:{'#22c55e' if green else '#ef4444'}'>"
        f"{p['lanes_stalled']} stalled / {p['lanes_total']} lanes</span></h2>"
        f"<div style='color:#64748b;font-size:12px'>07-11 · findings-only "
        f"(no auto-restart) · files cadence_stall_* to brain_findings · "
        f"10-min tick cache · generated {_esc(p['generated_at'])} · "
        f"JSON: /api/v1/admin/cadence-sentinel/master-tick</div>"
        + "".join(cards) + "</body>")
    return Response(html, mimetype="text/html")
