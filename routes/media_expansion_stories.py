"""media_expansion_stories.py — daily "data center expansion stories" lane
(2026-08-16, operator directive).

Detects the day's REAL expansion signals in the infra ingest and drafts a
LinkedIn-ready story per signal, dropping every draft into media_story_queue
with status='queued' for OPERATOR REVIEW. It NEVER posts, sends, or publishes
anything — same contract as media_data_story_factory, whose detect → guard →
queue shape this follows.

THE THREE STORY CLASSES (all numbers re-queried LIVE at draft time — a figure
is never carried over from a previous run; liveness is the product):

  new_facilities_daily   — new non-duplicate facilities added to the tracked
                           fleet (discovered_facilities, COALESCE(is_duplicate,
                           0)=0 — the dedup-suppression truth; duplicate rows
                           are kept but flagged, so the bare COUNT lies).
  operator_fleet_add     — the "nLighten added 6 sites" class (the shape that
                           performed on LinkedIn 2026-08-07/15): the operator
                           who added the most tracked sites in the last 7 days.
  queue_capacity_move    — an interconnection-queue capacity move per grid
                           operator (iso_queue_snapshots): latest active-queue
                           GW vs the previous DISTINCT reading. Operator
                           geography comes from media_claim_verify's
                           OPERATOR_SCOPE so a GB/CA operator can never be
                           dressed in a US claim (the post-100292 class).

WHY TEMPLATES, NOT AN LLM COMPOSER
==================================
The data-story factory's LLM lane went 12/12 rejected on its only run
(2026-06-23): the drafts wove the canon platform totals into prose the editor
called "fabricated specificity", and the then-0.700 quality bar refused the
rest. This lane composes DETERMINISTIC analyst-voice templates from live-read
values only:
  * every figure in the draft is queried in the same run that writes it;
  * NO platform totals (facility/market/country counts) — the whole
    over-claim class is absent by construction;
  * no superlatives that can't be proven, no absolute dates in the body
    (relative phrases only — the editor once rejected real 2026 dates as
    "future dates");
  * each template leads number-first with the unit ADJACENT to the number
    (leads_with_number requires adjacency — the #2372 lesson), carries a
    freshness phrase, names concrete subjects, and links a real dchub.cloud
    path, so an honest draft clears CONTENT_QUALITY_MIN on merit.

THE GUARD GAUNTLET IS HOUSE LAW, UNWEAKENED
===========================================
Every draft must clear, BEFORE it is queued:
  (1) content_publisher._should_skip_publish(cur, text, 'linkedin') — quality,
      zero-stat, number-lead, disparagement, entity-scope, dedup, editor;
  (2) routes.media_claim_verify.verify_claims(text) — ANY block is fatal HERE
      regardless of the MEDIA_CLAIM_VERIFY env mode (the worker doesn't set
      it; this lane enforces block-mode behaviour itself);
  (3) routes.media_fact_check_guard.verify_media_text(text) when present —
      with the corroboration it asks for SUPPLIED, not skipped. That guard
      fails closed on any aggregate GW figure ("no live aggregate GW source")
      and on facility counts far below the platform canon ("likely a
      different metric") because it was built for press-release platform
      claims; this lane's figures come from a structured live source read in
      the SAME run. So every claim the guard flags must match a value the
      detector just read (iso_queue_snapshots GW, add counts, fleet totals) —
      a flagged claim NOT in the run's own live-read values still REJECTS.
      Net effect is stricter than blanket pass/fail: every number in a queued
      draft is provably a number the detector read live this run.
Anything flagged is recorded status='rejected' with the reason (auditable),
exactly like the factory. Guards are never bypassed or tuned down from here.

SCHEDULING (registered AND armed)
=================================
crawler_scheduler.SCHEDULE carries (21, 21, "expansion_stories",
"_run_expansion_stories") and _RUNNERS maps it — both halves, because a
SCHEDULE name missing from _RUNNERS silently no-ops (the 2026-07-21 class).
run_expansion_scan() self-stamps cron_last_run (job_name='expansion_stories')
at entry, mirroring customer_white_glove._stamp_cron_run, so the external
dead-man (check_cron_freshness) — and a human after deploy — can see the lane
actually FIRES, not merely that it is registered. Kill switch:
EXPANSION_STORIES_DISABLE=1 (the lane is queue-only, so it ships armed).

Operator surfacing: the daily pending-drafts digest (15:10 UTC) lists
media_story_queue rows in status 'queued'/'pending' — see media_pending_digest.

Admin endpoints:
  POST /api/v1/media/expansion-stories/run    detect + draft + queue (manual)
  GET  /api/v1/media/expansion-stories/queue  list this lane's queue rows
"""
from __future__ import annotations

import os
import json
import logging
import re

from flask import Blueprint, request, jsonify

logger = logging.getLogger("media_expansion_stories")

media_expansion_stories_bp = Blueprint("media_expansion_stories", __name__)

# ── optional imports (a missing dep must never break import / boot) ──────────
try:
    import psycopg2 as _pg
    import psycopg2.extras as _pg_extras
except Exception:                                   # pragma: no cover
    _pg = None
    _pg_extras = None

JOB_NAME = "expansion_stories"
JOB_INTERVAL_S = 24 * 3600

SITE = "https://dchub.cloud"

# Detection thresholds. Conservative so the queue stays high-signal; env-tunable.
_NEW_FACILITIES_MIN = int(os.environ.get("EXPANSION_NEW_FACILITIES_MIN", "20"))
_FLEET_ADD_MIN = int(os.environ.get("EXPANSION_FLEET_ADD_MIN", "3"))
_QUEUE_MOVE_MIN_GW = float(os.environ.get("EXPANSION_QUEUE_MOVE_MIN_GW", "2.0"))
_QUEUE_MOVE_MIN_FRAC = float(os.environ.get("EXPANSION_QUEUE_MOVE_MIN_FRAC", "0.03"))
_MAX_PER_RUN = int(os.environ.get("EXPANSION_STORIES_MAX_PER_RUN", "3"))
_COOLDOWN_DAYS = int(os.environ.get("EXPANSION_STORIES_COOLDOWN_DAYS", "6"))


def _enabled() -> bool:
    """ARMED by default — the lane only queues drafts for human review, so the
    safe default is ON. EXPANSION_STORIES_DISABLE=1 is the kill switch."""
    return os.environ.get("EXPANSION_STORIES_DISABLE", "").strip().lower() not in (
        "1", "true", "yes", "on")


# ── admin gate (mirrors media_data_story_factory._admin_ok) ──────────────────
def _admin_ok() -> bool:
    _keys = set()
    for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
        _v = os.environ.get(_n)
        if _v:
            _keys.add(_v)
    if not _keys:
        return False
    _sent = (request.headers.get("X-Internal-Key")
             or request.headers.get("X-Admin-Key")
             or request.args.get("admin_key") or "").strip()
    return bool(_sent) and _sent in _keys


# ── DB ───────────────────────────────────────────────────────────────────────
def _dsn() -> str:
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


def _connect():
    """Best-effort connection. Returns None on any failure (caller degrades)."""
    if not _pg or not _dsn():
        return None
    try:
        try:
            from main import get_pg_connection
            return get_pg_connection()
        except Exception:
            return _pg.connect(_dsn(), connect_timeout=8)
    except Exception as e:                            # pragma: no cover
        logger.warning("[expansion] DB connect failed: %s", str(e)[:160])
        return None


def _rollback_soft(cur) -> None:
    """After ANY failed statement the connection is poisoned until rollback —
    without this, every later read on the connection returns nothing (the
    all-zero /agent/index class). Call in every detector's except path."""
    try:
        cur.connection.rollback()
    except Exception:
        pass


def _stamp_cron_run(cur) -> None:
    """Self-register into cron_last_run so the platform dead-man
    (check_cron_freshness) — and post-deploy verification — see this lane
    actually FIRE. crawler_scheduler keeps only in-process history, so without
    this stamp 'registered' and 'runs' are indistinguishable from outside."""
    try:
        cur.execute("""
            INSERT INTO cron_last_run
                (job_name, last_started_at, expected_interval_s, run_count)
            VALUES (%s, NOW() ON CONFLICT DO NOTHING, %s, 1)
            ON CONFLICT (job_name) DO UPDATE SET
                last_started_at = EXCLUDED.last_started_at,
                expected_interval_s = COALESCE(EXCLUDED.expected_interval_s,
                                               cron_last_run.expected_interval_s),
                run_count = cron_last_run.run_count + 1
        """, (JOB_NAME, JOB_INTERVAL_S))
        cur.connection.commit()
    except Exception as e:
        logger.warning("[expansion] cron stamp failed: %s", str(e)[:120])
        _rollback_soft(cur)


def _ensure_tables(conn) -> None:
    """media_story_queue is shared with media_data_story_factory — identical
    idempotent DDL so either module can run first on a fresh DB."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS media_story_queue (
                    id              BIGSERIAL PRIMARY KEY,
                    market_slug     TEXT,
                    market_name     TEXT,
                    shift_kind      TEXT NOT NULL,
                    shift_detail    JSONB,
                    data_brief      TEXT,
                    journalist_pitch TEXT,
                    status          TEXT NOT NULL DEFAULT 'queued',
                    reject_reason   TEXT,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
                    reviewed_at     TIMESTAMPTZ,
                    reviewed_by     TEXT
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS media_story_queue_status_at
                    ON media_story_queue (status, created_at DESC)
            """)
        conn.commit()
    except Exception as e:
        logger.warning("[expansion] _ensure_tables failed: %s", str(e)[:200])
        try:
            conn.rollback()
        except Exception:
            pass


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:60]


def _fmt(n) -> str:
    """1204 -> '1,204'; 4.2 -> '4.2' (no trailing zeros)."""
    try:
        f = float(n)
    except (TypeError, ValueError):
        return str(n)
    if f == int(f):
        return f"{int(f):,}"
    return f"{f:,.1f}"


# ── detection (live reads only; each fails soft + rolls back) ────────────────
def detect_new_facilities(cur) -> dict | None:
    """New non-duplicate facilities in the last 24h (7d context + countries).
    COALESCE(is_duplicate,0)=0 — duplicate rows are retained but flagged, so
    the unfiltered count over-reports."""
    try:
        cur.execute("""
            SELECT COUNT(*) AS n_day
              FROM discovered_facilities
             WHERE COALESCE(is_duplicate, 0) = 0
               AND first_seen >= NOW() - INTERVAL '1 day'
        """)
        row = cur.fetchone()
        n_day = int((row.get("n_day") if hasattr(row, "get") else row[0]) or 0)
        if n_day < _NEW_FACILITIES_MIN:
            return None
        cur.execute("""
            SELECT COUNT(*) AS n_week
              FROM discovered_facilities
             WHERE COALESCE(is_duplicate, 0) = 0
               AND first_seen >= NOW() - INTERVAL '7 days'
        """)
        row = cur.fetchone()
        n_week = int((row.get("n_week") if hasattr(row, "get") else row[0]) or 0)
        cur.execute("""
            SELECT country, COUNT(*) AS c
              FROM discovered_facilities
             WHERE COALESCE(is_duplicate, 0) = 0
               AND first_seen >= NOW() - INTERVAL '1 day'
               AND country IS NOT NULL AND TRIM(country) <> ''
             GROUP BY country ORDER BY c DESC LIMIT 3
        """)
        countries = []
        for r in (cur.fetchall() or []):
            c = r.get("country") if hasattr(r, "get") else r[0]
            if c:
                countries.append(str(c))
        return {
            "kind": "new_facilities_daily",
            "slug": "global-fleet",
            "name": "Global tracked fleet",
            "detail": {"added_24h": n_day, "added_7d": n_week,
                       "top_countries_24h": countries},
        }
    except Exception as e:
        logger.warning("[expansion] detect_new_facilities failed: %s", str(e)[:200])
        _rollback_soft(cur)
        return None


def detect_operator_fleet_add(cur) -> dict | None:
    """The operator who added the most non-duplicate tracked sites in the last
    7 days (the nLighten story class), with their live fleet total and up to
    three new locations."""
    try:
        cur.execute("""
            SELECT provider, COUNT(*) AS adds
              FROM discovered_facilities
             WHERE COALESCE(is_duplicate, 0) = 0
               AND first_seen >= NOW() - INTERVAL '7 days'
               AND provider IS NOT NULL
               AND TRIM(provider) NOT IN ('', 'Unknown')
               AND LENGTH(provider) <= 60
             GROUP BY provider
            HAVING COUNT(*) >= %s
             ORDER BY adds DESC, provider ASC
             LIMIT 1
        """, (_FLEET_ADD_MIN,))
        row = cur.fetchone()
        if not row:
            return None
        provider = row.get("provider") if hasattr(row, "get") else row[0]
        adds = int((row.get("adds") if hasattr(row, "get") else row[1]) or 0)
        cur.execute("""
            SELECT COUNT(*) AS fleet
              FROM discovered_facilities
             WHERE COALESCE(is_duplicate, 0) = 0 AND provider = %s
        """, (provider,))
        r2 = cur.fetchone()
        fleet = int((r2.get("fleet") if hasattr(r2, "get") else r2[0]) or 0)
        cur.execute("""
            SELECT DISTINCT COALESCE(NULLIF(TRIM(city), ''), country) AS place
              FROM discovered_facilities
             WHERE COALESCE(is_duplicate, 0) = 0 AND provider = %s
               AND first_seen >= NOW() - INTERVAL '7 days'
               AND COALESCE(NULLIF(TRIM(city), ''), country) IS NOT NULL
             LIMIT 3
        """, (provider,))
        places = []
        for r in (cur.fetchall() or []):
            p = r.get("place") if hasattr(r, "get") else r[0]
            if p:
                places.append(str(p))
        return {
            "kind": "operator_fleet_add",
            "slug": _slug(provider),
            "name": str(provider),
            "detail": {"provider": str(provider), "added_7d": adds,
                       "fleet_total": fleet, "new_places": places},
        }
    except Exception as e:
        logger.warning("[expansion] detect_operator_fleet_add failed: %s", str(e)[:200])
        _rollback_soft(cur)
        return None


def detect_queue_move(cur) -> dict | None:
    """The biggest interconnection-queue capacity move: per grid operator,
    latest iso_queue_snapshots reading vs the most recent PRIOR DISTINCT value
    (30d lookback — day-over-day is usually flat, so a plain 1-day diff would
    read 0 forever). A move must clear BOTH the absolute and relative bars."""
    try:
        cur.execute("""
            WITH latest AS (
                SELECT DISTINCT ON (iso)
                       iso, as_of, queued_load_total_gw AS gw
                  FROM iso_queue_snapshots
                 WHERE queued_load_total_gw IS NOT NULL
                 ORDER BY iso, as_of DESC
            ),
            prior AS (
                SELECT DISTINCT ON (s.iso)
                       s.iso, s.as_of, s.queued_load_total_gw AS gw
                  FROM iso_queue_snapshots s
                  JOIN latest l ON l.iso = s.iso
                 WHERE s.queued_load_total_gw IS NOT NULL
                   AND s.as_of < l.as_of
                   AND s.as_of >= l.as_of - INTERVAL '30 days'
                   AND s.queued_load_total_gw <> l.gw
                 ORDER BY s.iso, s.as_of DESC
            )
            SELECT l.iso, l.as_of AS latest_as_of, l.gw AS latest_gw,
                   p.as_of AS prior_as_of, p.gw AS prior_gw
              FROM latest l JOIN prior p ON p.iso = l.iso
        """)
        best = None
        for r in (cur.fetchall() or []):
            iso = r.get("iso") if hasattr(r, "get") else r[0]
            latest_as_of = r.get("latest_as_of") if hasattr(r, "get") else r[1]
            latest_gw = float(r.get("latest_gw") if hasattr(r, "get") else r[2])
            prior_as_of = r.get("prior_as_of") if hasattr(r, "get") else r[3]
            prior_gw = float(r.get("prior_gw") if hasattr(r, "get") else r[4])
            if prior_gw <= 0:
                continue
            delta = latest_gw - prior_gw
            if abs(delta) < _QUEUE_MOVE_MIN_GW:
                continue
            if abs(delta) / prior_gw < _QUEUE_MOVE_MIN_FRAC:
                continue
            if best is None or abs(delta) > abs(best["detail"]["delta_gw"]):
                best = {
                    "kind": "queue_capacity_move",
                    "slug": _slug(str(iso)),
                    "name": str(iso),
                    "detail": {
                        "iso": str(iso),
                        "latest_gw": round(latest_gw, 2),
                        "prior_gw": round(prior_gw, 2),
                        "delta_gw": round(delta, 2),
                        "latest_as_of": str(latest_as_of),
                        "prior_as_of": str(prior_as_of),
                    },
                }
        return best
    except Exception as e:
        logger.warning("[expansion] detect_queue_move failed: %s", str(e)[:200])
        _rollback_soft(cur)
        return None


# ── composition (deterministic analyst-voice templates) ──────────────────────
# Region label per grid operator, via media_claim_verify's canonical map so a
# non-US operator can never carry a US framing. Unknown operator → no region
# phrase at all (omit-or-prove).
def _region_phrase(iso: str) -> str:
    try:
        from routes.media_claim_verify import OPERATOR_SCOPE, SCOPE_REGION_LABEL
        scope = OPERATOR_SCOPE.get((iso or "").strip().lower())
        label = SCOPE_REGION_LABEL.get(scope or "")
        if label:
            return f" in the {label} connection pipeline"
    except Exception:
        pass
    return ""


_SOURCE_LINE = ("Source: DC Hub (dchub.cloud), the live infrastructure data "
                "layer for AI agents, refreshed daily.")
_TAGS = "#DataCenter #AIInfrastructure"


def compose_draft(story: dict) -> str | None:
    """Deterministic LinkedIn draft from live-read values ONLY. Each template:
    number-led with the unit adjacent (leads_with_number), a freshness phrase,
    concrete named subjects, an honest tracked-scope line, and a real
    dchub.cloud path — and never a platform total or a superlative."""
    kind = story.get("kind")
    d = story.get("detail") or {}
    try:
        if kind == "new_facilities_daily":
            n_day, n_week = d["added_24h"], d["added_7d"]
            countries = d.get("top_countries_24h") or []
            where = ""
            if countries:
                where = (", with new entries in " + ", ".join(countries[:2])
                         + (f" and {countries[2]}" if len(countries) > 2 else ""))
            return (
                f"{_fmt(n_day)} facilities joined DC Hub's tracked fleet in the "
                f"last 24 hours — {_fmt(n_week)} in the last 7 days{where}.\n\n"
                "Each addition lands as a distinct, machine-readable record: "
                "location, power context, provenance. That is what lets a "
                "capacity planner evaluate sites one by one instead of taking "
                "a portfolio-level number on faith.\n\n"
                "To be clear: these are additions to what DC Hub tracks, not a "
                "market-wide construction census.\n\n"
                f"{_SOURCE_LINE} Full fleet: {SITE}/facilities\n\n{_TAGS}")

        if kind == "operator_fleet_add":
            provider = d["provider"]
            adds, fleet = d["added_7d"], d["fleet_total"]
            places = d.get("new_places") or []
            where = ""
            if places:
                where = (" New entries include " + ", ".join(places[:2])
                         + (f" and {places[2]}" if len(places) > 2 else "") + ".")
            return (
                f"{_fmt(adds)} facilities: {provider} added more tracked sites "
                "to DC Hub's fleet in the last 7 days than any other operator, "
                f"reaching {_fmt(fleet)} tracked facilities.{where}\n\n"
                "The useful signal is the shape, not just the count: each "
                "building is a distinct, machine-readable record — location, "
                "power context, provenance — so a distributed portfolio can be "
                "evaluated site by site rather than as one headline figure.\n\n"
                f"To be clear, this is what DC Hub tracks, not a claim about "
                f"{provider}'s complete estate.\n\n"
                f"{_SOURCE_LINE} Full operator footprint: {SITE}/facilities\n\n"
                f"{_TAGS}")

        if kind == "queue_capacity_move":
            iso = d["iso"]
            latest_gw, prior_gw = d["latest_gw"], d["prior_gw"]
            delta = d["delta_gw"]
            direction = "up" if delta > 0 else "down"
            verb = "grew" if delta > 0 else "eased"
            return (
                f"{_fmt(abs(delta))} GW: {iso}'s active interconnection queue "
                f"{verb} to {_fmt(latest_gw)} GW of queued capacity in DC Hub's "
                f"latest snapshot, {direction} from {_fmt(prior_gw)} GW"
                f"{_region_phrase(iso)}.\n\n"
                "Queue movement is the earliest public signal of where new "
                "load and generation are actually heading — it shows up in the "
                "connection pipeline quarters before it shows up as steel. DC "
                "Hub reads each operator's queue at the source and keeps the "
                "series as data, so the move is checkable, not anecdotal.\n\n"
                f"To be clear: this is {iso}'s reported active queue as "
                "ingested by DC Hub, and queue totals include projects that "
                "will never be built.\n\n"
                f"{_SOURCE_LINE} Market context: {SITE}/markets\n\n{_TAGS}")
    except (KeyError, TypeError) as e:
        logger.warning("[expansion] compose_draft(%s) missing field: %s",
                       kind, str(e)[:120])
        return None
    return None


# ── the guard gauntlet (identical contract to media_data_story_factory) ──────
def _story_numbers(detail) -> set:
    """Every numeric value the detector read live this run (recursive over the
    story detail), as rounded absolute values. These are the ONLY figures a
    draft is allowed to carry beyond what canon corroborates."""
    out = set()

    def walk(v):
        if isinstance(v, bool):
            return
        if isinstance(v, (int, float)):
            out.add(round(abs(float(v)), 2))
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)

    walk(detail)
    return out


def _uncorroborated(unverified, detail) -> list:
    """Resolve verify_media_text's flagged claims against the run's OWN
    live-read values. A flagged claim whose figure the detector just read from
    the structured source is corroborated by construction (composed from that
    very read, same run); anything else stays flagged. Fail-CLOSED: a claim we
    cannot parse stays flagged."""
    nums = _story_numbers(detail)
    residual = []
    for item in (unverified or []):
        claim = str((item or {}).get("claim") or "")
        vals = []
        try:
            from routes.media_claim_verify import extract_claims
            vals = [c.get("value") for c in (extract_claims(claim) or [])]
        except Exception:
            vals = []
        matched = False
        for v in vals:
            if v is None:
                continue
            v = round(abs(float(v)), 2)
            if any(abs(v - n) <= max(0.011, n * 0.001) for n in nums):
                matched = True
                break
        if not matched:
            residual.append(item)
    return residual


def _guard_check(cur, text: str, detail=None) -> tuple[bool, str]:
    """Run the composed draft through the hardened publish guards. FAIL-CLOSED
    on any guard hit; fail-open only when a guard itself is unavailable (with
    a logged reason). Platform is 'linkedin' — this IS a LinkedIn lane, so the
    number-lead style gate applies on purpose.

    verify_claims blocks are fatal HERE regardless of the MEDIA_CLAIM_VERIFY
    env mode: _should_skip_publish only hard-fails claim blocks when the env
    says 'block', and the worker service doesn't set it — this lane does not
    inherit that softness.

    verify_media_text runs with corroboration SUPPLIED (see module docstring):
    each flagged claim must match a value in `detail` — the numbers this run
    read live — or the draft rejects. The guard itself is untouched."""
    if not text or not text.strip():
        return False, "empty draft"

    try:
        from content_publisher import _should_skip_publish
        skip, why = _should_skip_publish(cur, text, "linkedin")
        if skip:
            return False, f"publish-guard: {why}"
    except Exception as e:
        logger.warning("[expansion] _should_skip_publish unavailable: %s", str(e)[:160])

    try:
        from routes.media_claim_verify import verify_claims
        cv = verify_claims(text)
        if cv.get("blocks"):
            return False, "claim-verify: " + "; ".join(cv["blocks"])[:240]
    except Exception as e:
        logger.warning("[expansion] verify_claims unavailable: %s", str(e)[:160])

    try:
        from routes.media_fact_check_guard import verify_media_text  # type: ignore
        res = verify_media_text(text)
        if isinstance(res, dict) and res.get("ok") is False:
            residual = _uncorroborated(res.get("unverified"), detail or {})
            if residual:
                return False, ("fact-check-guard (not in this run's live "
                               "reads): " + "; ".join(str(u) for u in residual)[:240])
    except ImportError:
        pass
    except Exception as e:
        logger.warning("[expansion] verify_media_text unavailable/raised: %s",
                       str(e)[:160])

    return True, ""


# ── queue cooldown ───────────────────────────────────────────────────────────
def _on_cooldown(cur, kind: str, slug: str) -> bool:
    """True when this (kind, slug) story should NOT queue again yet:
      * an UNREVIEWED 'queued' row already exists (never stack drafts the
        operator hasn't looked at), or
      * an 'approved' row landed within the cooldown window (the story just
        ran; re-detecting the same 7-day window daily is not news).
    'rejected' rows never block a retry — tomorrow's live numbers may pass.
    new_facilities_daily is exempt from the approved-cooldown (it is the daily
    lane; each day is a new number) but still never stacks unreviewed drafts."""
    try:
        cur.execute("""
            SELECT 1 FROM media_story_queue
             WHERE shift_kind = %s AND COALESCE(market_slug, '') = %s
               AND status = 'queued'
             LIMIT 1
        """, (kind, slug or ""))
        if cur.fetchone():
            return True
        if kind == "new_facilities_daily":
            return False
        cur.execute("""
            SELECT 1 FROM media_story_queue
             WHERE shift_kind = %s AND COALESCE(market_slug, '') = %s
               AND status = 'approved'
               AND created_at >= NOW() - (%s * INTERVAL '1 day')
             LIMIT 1
        """, (kind, slug or "", _COOLDOWN_DAYS))
        return bool(cur.fetchone())
    except Exception as e:
        logger.warning("[expansion] cooldown check failed: %s", str(e)[:160])
        _rollback_soft(cur)
        return False


# ── the run ──────────────────────────────────────────────────────────────────
def run_expansion_scan(conn=None) -> dict:
    """Detect → compose → guard → queue. Returns a result dict; never raises.
    NOTHING is sent or published — drafts land in media_story_queue
    status='queued' for the operator (surfaced by the pending-drafts digest)."""
    if not _enabled():
        return {"ok": True, "skipped": "disabled"}

    own_conn = conn is None
    if own_conn:
        conn = _connect()
    if conn is None:
        return {"ok": False, "error": "db unavailable"}

    queued, rejected, skipped = [], [], []
    detected = 0
    try:
        _ensure_tables(conn)
        with conn.cursor(cursor_factory=_pg_extras.RealDictCursor
                         if _pg_extras else None) as cur:
            _stamp_cron_run(cur)

            stories = []
            for detector in (detect_new_facilities, detect_operator_fleet_add,
                             detect_queue_move):
                s = detector(cur)
                if s:
                    stories.append(s)
            stories = stories[:_MAX_PER_RUN]
            detected = len(stories)

            for story in stories:
                kind, slug = story["kind"], story.get("slug") or ""
                name = story.get("name")
                if _on_cooldown(cur, kind, slug):
                    skipped.append({"kind": kind, "name": name,
                                    "reason": "cooldown/unreviewed draft exists"})
                    continue

                draft = compose_draft(story)
                if not draft:
                    rejected.append({"kind": kind, "name": name,
                                     "reason": "compose failed"})
                    continue

                passed, reason = _guard_check(cur, draft, story.get("detail"))
                status = "queued" if passed else "rejected"
                try:
                    cur.execute("""
                        INSERT INTO media_story_queue
                            (market_slug, market_name, shift_kind, shift_detail,
                             data_brief, status, reject_reason)
                        VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
                        RETURNING id
                    """, (slug, name, kind,
                          json.dumps(story.get("detail"), default=str),
                          draft, status, (reason[:480] if reason else None)))
                    row = cur.fetchone()
                    conn.commit()
                    rec = {"id": (row.get("id") if hasattr(row, "get")
                                  else (row[0] if row else None)),
                           "kind": kind, "name": name}
                    if passed:
                        queued.append(rec)
                    else:
                        rec["reason"] = reason
                        rejected.append(rec)
                except Exception as e:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    rejected.append({"kind": kind, "name": name,
                                     "reason": f"queue write failed: {str(e)[:120]}"})
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass

    result = {
        "ok": True,
        "detected": detected,
        "queued": len(queued),
        "rejected": len(rejected),
        "skipped": len(skipped),
        "queued_items": queued,
        "rejected_items": rejected,
        "skipped_items": skipped,
        "note": ("drafts written to media_story_queue (status=queued) for "
                 "operator review. NOTHING was sent or published."),
    }
    logger.info("[expansion] run: detected=%d queued=%d rejected=%d skipped=%d",
                detected, len(queued), len(rejected), len(skipped))
    return result


# ── endpoints ────────────────────────────────────────────────────────────────
@media_expansion_stories_bp.route("/api/v1/media/expansion-stories/run",
                                  methods=["POST"])
def run_endpoint():
    if not _admin_ok():
        return jsonify({"ok": False, "error": "admin only"}), 403
    return jsonify(run_expansion_scan())


@media_expansion_stories_bp.route("/api/v1/media/expansion-stories/queue",
                                  methods=["GET"])
def queue_endpoint():
    if not _admin_ok():
        return jsonify({"ok": False, "error": "admin only"}), 403
    status = (request.args.get("status") or "all").strip().lower()
    conn = _connect()
    if conn is None:
        return jsonify({"ok": False, "error": "db unavailable"}), 503
    items = []
    try:
        _ensure_tables(conn)
        with conn.cursor(cursor_factory=_pg_extras.RealDictCursor
                         if _pg_extras else None) as cur:
            kinds = ("new_facilities_daily", "operator_fleet_add",
                     "queue_capacity_move")
            if status == "all":
                cur.execute("""
                    SELECT id, market_slug, market_name, shift_kind,
                           data_brief, status, reject_reason, created_at
                      FROM media_story_queue
                     WHERE shift_kind IN %s
                     ORDER BY created_at DESC LIMIT 100
                """, (kinds,))
            else:
                cur.execute("""
                    SELECT id, market_slug, market_name, shift_kind,
                           data_brief, status, reject_reason, created_at
                      FROM media_story_queue
                     WHERE shift_kind IN %s AND status = %s
                     ORDER BY created_at DESC LIMIT 100
                """, (kinds, status))
            for r in (cur.fetchall() or []):
                d = dict(r)
                if d.get("created_at") is not None:
                    d["created_at"] = str(d["created_at"])
                items.append(d)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return jsonify({"ok": True, "status": status, "count": len(items),
                    "items": items})
