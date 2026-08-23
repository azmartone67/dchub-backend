"""DCPI — Data Center Power Index (phase 108).

Two scores per US data-center market, recomputed daily:

  CONSTRAINT SCORE      0..100   high = avoid (queue wait, grid stress)
  EXCESS POWER SCORE    0..100   high = opportunity (the contrarian play)

The Excess Power Score is the differentiator — surfaces stranded capacity,
curtailed renewables, retiring-plant interconnection, behind-the-meter
industrial headroom — power that buyers don't know exists.

Endpoints:
  GET  /dcpi                          public dashboard (US heatmap)
  GET  /dcpi/<market>                 deep-dive page for one market
  GET  /api/v1/dcpi/scores            JSON of all current scores
  GET  /api/v1/dcpi/scores/<market>   detailed scoring for one market
  GET  /api/v1/dcpi/movers            top movers (24h, 7d, 30d)
  GET  /dcpi/og/<market>.svg          1200x630 social card
  POST /api/v1/dcpi/recompute         admin/cron — recompute all scores
  GET  /dcpi/press                    press kit page
"""

from __future__ import annotations
import os, json, math, datetime
from typing import Optional, Any
from flask import Blueprint, request, jsonify, Response, render_template_string
import psycopg2
import psycopg2.extras


# Phase 223: defensive round helper


# Phase 225: decorator that returns the fallback page on ANY exception
from functools import wraps
def _safe_dcpi_page(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            return _phase225_dcpi_error_page(str(e))
    return wrapper


def _safe_round(v, digits=1):
    """Safely round a value that might be None or non-numeric."""
    if v is None: return 0.0
    try: return round(float(v), digits)
    except (TypeError, ValueError): return 0.0




# === Phase 213: dynamic market list (use /api/v1/markets/list for full 132) ===
def _dcpi_dynamic_markets():
    """Returns list of market dicts {slug, name, cities, state, country}.
    Pulls from internal markets API. Falls back to MARKET_ALIASES if API fails.
    """
    import os, urllib.request, json
    try:
        # r33-Q+port-fix (2026-05-22): default was localhost:8000 but the
        # app binds $PORT=8080. Same one-digit typo class as the
        # Inspector→L22 bug. With :8000 this self-call always failed →
        # DCPI fell back to MARKET_ALIASES instead of the live 132-market
        # list. Caught by the new regression-guard CI check.
        base = os.environ.get("DCHUB_INTERNAL_API", "http://localhost:8080")
        # Use enterprise key to bypass tier-gate
        ent_key = os.environ.get("DCHUB_ENT_KEY", "ent_internal_dcpi_scorer")
        req = urllib.request.Request(
            f"{base}/api/v1/markets/list",
            headers={"X-API-Key": ent_key, "User-Agent": "dcpi-scorer/2.0"}
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
        markets_raw = data.get("data") or data.get("markets") or []
        out = []
        for m in markets_raw:
            out.append({
                "slug": m.get("id"),
                "name": m.get("name"),
                "cities": m.get("cities") or [m.get("name")],
                "state": m.get("state"),
                "country": m.get("country", "US"),
                "facility_count": m.get("facility_count", 0),
                "pipeline_mw": m.get("pipeline_mw_total", 0),
                "operational_mw": m.get("total_power_mw", 0),
                "avg_kwh_usd": m.get("avg_kwh_price_usd"),
            })
        return out
    except Exception as e:
        import logging
        logging.warning(f"_dcpi_dynamic_markets fetch failed: {e}")
        return None


dcpi_bp = Blueprint("dcpi", __name__)

# ---------------------------------------------------------------------------
# Metro → City slug aliases (was a local in /dcpi/<slug> HTML route; r47.43
# promoted to module level so the API endpoint can apply the same mapping).
#
# Why: /markets/* canonicalizes to METRO slugs (northern-virginia,
# dallas-fort-worth, silicon-valley) but market_power_scores keys on the
# dominant CITY (ashburn, dallas, santa-clara). Without this map, the natural
# URL transform /markets/<metro> → /dcpi/<metro> 404s.
#
# Pre-r47.43 we had northern-virginia and dallas-fort-worth covered but had
# silently dropped silicon-valley — caught by the pre-CBRE DCPI sweep.
# Both call sites (HTML page handler + API endpoint) now read from this
# single dict, so future additions are one-edit fixes.
# ---------------------------------------------------------------------------
# r-twin-unpublish (2026-07-28): the table moved to util/market_aliases so
# dchub_self_heal can import it WITHOUT importing this module (which builds
# MARKETS at import time — a live DB query). Re-exported, not redefined:
# a hand-copied second table is the bug class fixed in util/iso_taxonomy
# the same day. routes/site_valuation_engine.py imports this name too.
from util.market_aliases import DCPI_METRO_ALIASES  # noqa: E402,F401

# r-status-taxonomy (2026-07-29): the operational/pipeline vocabulary lives in
# ONE module for the same reason DCPI_METRO_ALIASES does — every hand-copied
# status literal in this repo drifted, and the copy in this file was the one
# governing the published index. Fragments are built once at import (pure
# string work over module constants — no DB, no I/O, no % in the output).
from util.status_taxonomy import (  # noqa: E402
    operational_sql as _status_operational_sql,
    pipeline_sql as _status_pipeline_sql,
    unclassified_sql as _status_unclassified_sql,
    basis as _status_basis,
)
_SQL_OP_STATUS = _status_operational_sql()
_SQL_PIPE_STATUS = _status_pipeline_sql()
_SQL_UNK_STATUS = _status_unclassified_sql()

# r-sat-dedup (2026-08-08): duplicate-visibility for the market saturation
# FOOTPRINT, defined ONCE because it is interpolated into two sibling queries
# (US city+state, intl city-pooled) that sit 14 lines apart and must never
# describe different row sets. That divergence is the repeat bug class in
# gather_metrics_for_market: r-declone-2 found the country predicate applied to
# only one branch, and r-status-taxonomy found the unfiltered-op_mw bug in BOTH
# and had to fix them twice. One name, two call sites, no third definition.
#
# r-universe-dedup (2026-08-08): now THREE call sites — _load_markets_dynamic's
# city_stats CTE interpolates the same name. The constant keeps its FOOTPRINT
# name because the sibling test pins that symbol, but its scope is general: this
# is THE duplicate-visibility rule for every discovered_facilities aggregate in
# this module. A fourth aggregate must reference it, not re-type the literal.
#
# ★ The pointer ALONE, never is_duplicate. `is_duplicate` is a suppression bit
# that also drops the row from counts and the sitemap, and 3,286 twin rows carry
# a pointer while staying UNflagged (measured 2026-08-08) — scoping on the flag
# would leave exactly those double-counted while dropping 1,510 flagged-but-
# UNpointed rows that are not twins at all. Same predicate as the page facility
# list (r-list-dedup), routes/facilities_by_dims.py and routes/d1_sync.py.
_SQL_FOOTPRINT_DEDUP = "AND duplicate_of_id IS NULL"



def _canonical_first(slug, candidates):
    """Reorder lookup candidates so a known alias target is tried FIRST.

    r-twin-unpublish (2026-07-28). Both /dcpi/<slug> and
    /api/v1/dcpi/scores/<slug> built `candidates = [slug, …aliases]`, so an
    alias key that still had its OWN row matched itself and the alias branch
    never ran. r-twin-dedup drops those redundant slugs from the scoring
    universe but their rows stayed behind, published and frozen — on
    2026-07-28 all seven (northern-virginia, dallas-fort-worth,
    silicon-valley, cheyenne-wy, columbus-oh, the-dalles-or, washington)
    were serving scores 9 days stale with iso_type NULL, while their
    canonical twins were current.

    Unpublishing alone does NOT fix the page: neither lookup filters on
    `published`, so the stale row would still be found and served. Promoting
    the canonical target is what makes /dcpi/northern-virginia 301 to
    /dcpi/ashburn and the API return ashburn's row.

    Scoped deliberately: only slugs that are KEYS of DCPI_METRO_ALIASES move.
    Every other market resolves exactly as before, published or not.
    """
    target = DCPI_METRO_ALIASES.get((slug or "").lower())
    if not target:
        return candidates
    out = [target] + [c for c in candidates if c != target]
    return out

# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------
def _conn():
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        raise RuntimeError("DATABASE_URL not set")
    return psycopg2.connect(db, sslmode="require")


_TABLES_READY = False
def _ensure_tables():
    # r66: this ran on nearly every DCPI call (9 sites) from BOTH replicas, and
    # the `ADD COLUMN data_basis_json` below takes an AccessExclusiveLock — so the
    # concurrent ALTERs deadlocked constantly ([dcpi-fallback] deadlock detected).
    # Run the idempotent DDL once per process, serialized across replicas with a
    # transaction-scoped advisory lock so a second caller waits instead of deadlocking.
    global _TABLES_READY
    if _TABLES_READY:
        return
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(572341001)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS market_power_scores (
                id              SERIAL PRIMARY KEY,
                market_slug     TEXT NOT NULL,
                market_name     TEXT NOT NULL,
                state           TEXT,
                iso             TEXT,
                latitude        REAL,
                longitude       REAL,

                constraint_score    REAL,
                excess_power_score  REAL,
                time_to_power_months REAL,

                queue_capacity_mw    REAL,
                queue_wait_months    REAL,
                reserve_margin_pct   REAL,
                gen_additions_12mo_mw REAL,
                curtailment_pct      REAL,
                stranded_capacity_mw REAL,
                emergency_count_30d  INT,

                top_risks_json         JSONB,
                top_opportunities_json JSONB,

                verdict         TEXT,                  -- BUILD | CAUTION | AVOID
                tier_required   TEXT DEFAULT 'free',   -- top-line free, county data Pro

                computed_at     TIMESTAMPTZ DEFAULT NOW(),
                trend_30d       JSONB                  -- recent score history
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_mps_slug ON market_power_scores(market_slug)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_mps_computed ON market_power_scores(computed_at DESC)")
        # r65 (2026-06-02): provenance label column. Records whether a market's
        # power metrics came from a live source vs the static iso_defaults /
        # slug_overrides. Additive + nullable so it cannot affect any existing
        # row, score, verdict or writer. Surfaced read-only on /dcpi outputs.
        cur.execute("ALTER TABLE market_power_scores "
                    "ADD COLUMN IF NOT EXISTS data_basis_json JSONB")
        # r-iso-taxonomy (2026-07-28): says WHICH KIND of thing `iso` names —
        # 'RTO' (organised market, has an interconnection queue), 'BA'
        # (balancing authority, no market) or 'REGION' (NERC footprint).
        # The column has always mixed all three (PJM + TVA + WECC sat in it
        # interchangeably) and callers could not tell them apart, so an agent
        # would pull PJM's queue for Charlotte — Duke territory, no RTO.
        # Additive + nullable: no existing row, score, verdict or writer
        # changes, and readers that ignore it behave exactly as before.
        cur.execute("ALTER TABLE market_power_scores "
                    "ADD COLUMN IF NOT EXISTS iso_type TEXT")
        # r-ws3-signal-tier (2026-07-28): per-market SIGNAL QUALITY of the score.
        # Today a market whose every score input came from the hardcoded
        # iso_defaults dict is published with exactly the same confidence as one
        # driven by live interconnect_queue + planned_generators + grid_telemetry
        # reads — nothing distinguishes them. The LOW_SIGNAL verdict cannot cover
        # this: it is written by dchub_self_heal's strict matrix only when a score
        # is exactly 0, which the iso_defaults guarantee never happens (measured
        # 2026-07-28: 0 of 310 published markets carry it).
        # 'full' | 'partial' | 'low' — rule in gather_metrics_for_market.
        # NULL means "the writer of this row did not record one" (rows predating
        # this column + every row written by lite_recompute). Readers MUST
        # surface NULL as unknown, never as 'low' — coercing it would invent a
        # measurement. Additive + nullable: no existing row, score, verdict or
        # writer changes, and readers that ignore it behave exactly as before.
        cur.execute("ALTER TABLE market_power_scores "
                    "ADD COLUMN IF NOT EXISTS signal_tier TEXT")
        # r-ws3-methodology (2026-07-29): which VERSION of the method produced
        # this row. market_power_scores is UPDATE-in-place and the only history
        # is dcpi_daily_snapshots, so a methodology change restates the entire
        # back series implicitly. Measured: on 2026-07-25 the local-granularity
        # terms landed and phoenix's published excess jumped 34.8 -> 62.8 in a
        # day; a subscriber had no way to learn the grid had not changed.
        # Without this column a restatement is indistinguishable from a market
        # move. Additive + nullable, same recipe as signal_tier: NULL means the
        # row predates version stamping (or was written by a path that does not
        # stamp) — readers surface NULL as unknown, never as the current
        # version, which would backdate a claim we cannot make.
        cur.execute("ALTER TABLE market_power_scores "
                    "ADD COLUMN IF NOT EXISTS method_version TEXT")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS dcpi_runs (
                id           SERIAL PRIMARY KEY,
                started_at   TIMESTAMPTZ DEFAULT NOW(),
                finished_at  TIMESTAMPTZ,
                markets_scored INT,
                error_count  INT,
                source       TEXT,                     -- cron | manual | api
                notes        TEXT
            )
        """)
        # ──────────────────────────────────────────────────────────────────
        # Phase 268 (2026-05-29) — daily DCPI snapshot table for movers
        #
        # The /api/v1/dcpi/movers and /api/v1/dcpi/trending endpoints had
        # a `week_ago` CTE that SELECTed from market_power_scores WHERE
        # computed_at < NOW() - INTERVAL '7 days'. But every writer to
        # that table is UPDATE-in-place (UNIQUE on market_slug, enforced
        # in Phase 215), so each slug has exactly ONE row with a recent
        # computed_at. The week_ago CTE returned 0 rows for every slug,
        # so excess_delta_7d was always 0, sorted arbitrarily, output
        # meaningless.
        #
        # Fix: persist a daily snapshot per market into a NEW table,
        # `dcpi_daily_snapshots`, and have movers read prev_excess from
        # (snapshot_date < CURRENT_DATE - 7) rows. Snapshot written by
        # the existing facility-snapshot-daily.yml cron (one more curl).
        #
        # Why a new table name (not market_power_scores_history): the
        # legacy *_history table was created by dchub_self_heal.py via
        # `LIKE market_power_scores INCLUDING ALL` which inherits the
        # UNIQUE(market_slug) constraint — incompatible with per-day
        # snapshots. New table avoids the migration risk.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS dcpi_daily_snapshots (
                id                  SERIAL PRIMARY KEY,
                snapshot_date       DATE NOT NULL,
                market_slug         TEXT NOT NULL,
                market_name         TEXT,
                excess_power_score  REAL,
                constraint_score    REAL,
                verdict             TEXT,
                captured_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_dcpi_daily_snapshots_day_slug
                ON dcpi_daily_snapshots(snapshot_date, market_slug)
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS ix_dcpi_daily_snapshots_slug_date "
                    "ON dcpi_daily_snapshots(market_slug, snapshot_date DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_dcpi_daily_snapshots_date "
                    "ON dcpi_daily_snapshots(snapshot_date DESC)")
        # r-ws3-methodology (2026-07-29): dcpi_daily_snapshots is the OFFICIAL
        # DCPI series — the only per-day history that exists. Stamping the
        # method version on each point is what lets /history label a step
        # change as a restatement rather than a market move. Additive +
        # nullable; every existing point keeps NULL, which is honest (nobody
        # recorded a version when they were written).
        cur.execute("ALTER TABLE dcpi_daily_snapshots "
                    "ADD COLUMN IF NOT EXISTS method_version TEXT")
        # r67 (2026-06-02): the scorer now reads the latest live grid_telemetry
        # row per ISO/zone (iso_grid_adapters.py writes it; the iso-data-pull
        # cron populates it every 20 min). That lookup is
        #   WHERE iso=%s [AND zone=%s] ORDER BY observed_at DESC LIMIT 1
        # so it wants (iso, zone, observed_at DESC). iso_grid_adapters.ensure_schema()
        # already creates ix_grid_telemetry_iso_zone_ts, but that DDL only runs
        # when the pull cron fires; if the scorer's recompute happens first (or
        # the adapter module never loads in this process) the index would be
        # absent and the per-market lookup would seq-scan grid_telemetry on every
        # one of 300+ markets. Create it here too — IF NOT EXISTS makes it a
        # no-op when the adapter already made it, and it shares this function's
        # run-once flag + pg_advisory_xact_lock so it's idempotent + deadlock-safe.
        # CREATE TABLE IF NOT EXISTS guards the (rare) case where the cron has
        # never run, so grid_telemetry doesn't yet exist — same column shape as
        # iso_grid_adapters._SCHEMA_DDL (verified live via /api/v1/admin/schema).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS grid_telemetry (
                id                 BIGSERIAL PRIMARY KEY,
                iso                TEXT NOT NULL,
                zone               TEXT,
                observed_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                online_gen_mw      REAL,
                load_mw            REAL,
                headroom_mw        REAL,
                reserve_margin_pct REAL,
                fuel_mix           JSONB DEFAULT '{}'::jsonb,
                source             TEXT
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS ix_grid_telemetry_iso_zone_ts "
                    "ON grid_telemetry (iso, zone, observed_at DESC)")
        c.commit()
    _TABLES_READY = True  # idempotent DDL done for this process — skip on later calls


# ─────────────────────────────────────────────────────────────────────
# r67 (2026-06-02) — LIVE grid-headroom read.
#
# iso_grid_adapters.py pulls real per-ISO online-generation vs. load every
# 20 min (iso-data-pull.yml cron) and stores it in grid_telemetry. Until now
# the scorer never read it: compute_excess_power_score fell straight to the
# hardcoded `reserve_margin_pct or 12`. This wires the live read in.
#
# WHAT THE LIVE ROW ACTUALLY CONTAINS (verified live 2026-06-02 against the
# Railway origin /api/v1/iso/sample/<iso> for MISO/NYISO/CAISO/SPP):
#   online_gen_mw, load_mw, headroom_mw=(gen-load)   → populated, REAL
#   reserve_margin_pct                               → NULL (the public
#       adapters never compute it — they only carry gen/load/headroom)
# So the live signal we have is the INSTANTANEOUS OPERATING headroom of
# currently-online generation: headroom_mw / load_mw. That is a *different
# physical quantity* from the NERC PLANNING reserve margin that
# compute_excess_power_score's s_reserve term is calibrated for (12%→0,
# 25%→100, i.e. installed-capacity margin over peak). It legitimately runs
# slightly negative when a region imports power (all 4 ISOs were -0.1%..-14%
# at verification time because online gen excludes imports).
#
# HONEST SYNTHESIS (never a fabricated number):
#   live_op_reserve_pct = headroom_mw / load_mw * 100      (real, signed)
# We use it as a *live delta* on the modeled planning anchor: a grid running
# tighter-than-balanced (negative op-headroom) shades the planning reserve
# DOWN; a grid running looser (positive surplus) shades it UP. The modeled
# iso_default stays the centred anchor (balanced grid → modeled value) so a
# single 5-min snapshot can't impersonate a planning-reserve study, but the
# LIVE reading genuinely moves the score in the correct direction. Bounded to
# ±LIVE_DELTA_CAP_PCT so a transient spike can't dominate. The raw live
# fields + provenance are stamped into data_basis so the basis is auditable.
#
# Cross-process safe: read-only SELECT on its own short-lived connection, one
# row via the (iso, zone, observed_at DESC) index. Stale rows (older than
# LIVE_TELEMETRY_MAX_AGE_MIN) are ignored so we never present a dead feed as
# live — we fall back to the modeled estimate, honestly labelled.
# ─────────────────────────────────────────────────────────────────────
LIVE_TELEMETRY_MAX_AGE_MIN = 180   # 3h — pull cron runs every 20 min; 9 misses = stale
LIVE_DELTA_CAP_PCT = 6.0           # max pp the live op-reserve delta can move the anchor
# DCPI iso codes whose telemetry zone is the ISO-wide system aggregate the
# adapters emit (iso == zone). Verified live: MISO/MISO, NYISO/NYCA,
# CAISO/CAISO, SPP/SPP. We match on iso only (latest row, any zone) so a future
# multi-zone adapter still resolves to the freshest system reading.
_TELEMETRY_ISO_CODES = frozenset({"ERCOT", "CAISO", "PJM", "MISO",
                                  "NYISO", "ISONE", "SPP"})

# Per-ISO read cache. A recompute scores 300+ markets and many share an ISO
# (dozens of CAISO/PJM markets); without this each one would re-run the same
# single-row SELECT, hammering the 1-replica Neon pool (the exact pattern behind
# the documented backend flapping). Only 7 keys max; 60s TTL is far under the
# 20-min pull cadence so freshness within a recompute is unaffected. Stores the
# resolved dict (or None) so misses are cached too.
_TELEMETRY_CACHE: dict = {}
_TELEMETRY_CACHE_TTL_S = 60

# Per-STATE interconnection-queue depth cache. Same rationale as the telemetry
# cache above: a recompute scores 300+ markets and many share a state, so
# without this each one re-runs the same aggregate against interconnect_queue
# (5,441 rows) and hammers the 1-replica pool. Keyed by upper-cased state, 60s
# TTL, misses cached (None) too. See _state_queue_depth.
_QUEUE_STATE_CACHE: dict = {}
_QUEUE_STATE_CACHE_TTL_S = 60


def _state_queue_depth(state: str):
    """Real active interconnection-queue depth for a US/CA state, or None.

    Returns {'active_n', 'active_mw'} aggregated over non-terminal projects in
    interconnect_queue (the LIVE table, refreshed daily), or None when the
    state is missing/unmatched or the read fails. Never fabricates — a miss
    falls the caller through to the per-ISO modeled anchor.

    ★ 2026-07-24: replaces a block that queried a MISSPELLED table
    (`interconnection_queue`) with malformed SQL inside a swallow-all
    try/except, so it threw on every call and every market silently fell to
    its ISO anchor — the root cause of the 145/317 identical-score collapse
    the integrity shell (#25) flags in lane 3.
    """
    if not state:
        return None
    key = state.strip().upper()
    if not key:
        return None
    import time as _t
    now = _t.time()
    hit = _QUEUE_STATE_CACHE.get(key)
    if hit is not None and (now - hit[0]) < _QUEUE_STATE_CACHE_TTL_S:
        return hit[1]
    result = None
    _c = None
    try:
        _c = _conn()
        with _c.cursor() as cur:
            # Non-terminal projects only — exclude completed/dead rows so the
            # depth reflects genuine pending contention for grid capacity.
            cur.execute(
                """
                SELECT COUNT(*) AS active_n,
                       COALESCE(SUM(capacity_mw), 0) AS active_mw
                  FROM interconnect_queue
                 WHERE UPPER(state) = %s
                   AND COALESCE(queue_status,'') !~*
                       'commercial operation|withdrawn|suspended|terminated|cancel'
                """,
                (key,),
            )
            row = cur.fetchone()
        if row and (row[0] or 0) > 0:
            result = {"active_n": int(row[0]), "active_mw": float(row[1] or 0)}
    except Exception:
        result = None  # fall through to the modeled anchor; never fabricate
    finally:
        if _c is not None:
            try:
                _c.close()
            except Exception:
                pass
    _QUEUE_STATE_CACHE[key] = (now, result)
    return result


# Per-STATE near-term generation-additions cache. Same rationale as the queue
# cache above: a recompute scores 300+ markets sharing ~48 states, so cache the
# per-state aggregate (planned_generators, 2,311 rows) 60s to protect the
# 1-replica pool. Keyed by upper-cased state, misses cached (None) too. See
# _state_gen_additions.
_GEN_ADD_STATE_CACHE: dict = {}
_GEN_ADD_STATE_CACHE_TTL_S = 60


def _state_gen_additions(state: str):
    """Real near-term (<=12mo) generation additions for a US state, in MW, or None.

    Sums capacity_mw over planned_generators (EIA-860M planned + under-
    construction fleet) whose planned online date falls within the next 12
    months AND whose status is a GENUINE near-term addition -- under
    construction or approvals-received / construction-complete-not-yet-in-
    service: EIA status codes (U), (V), (T), (TS). Regulatory-pending rows
    ((P), (L)) are excluded: their online dates are aspirational, not committed.
    Returns None when the state is missing/unmatched or the read fails -- the
    caller then keeps gen_additions at its default (0), never a fabricated value.

    ★ 2026-07-24: replaces a block that queried `capacity_pipeline WHERE iso=%s`.
    That table has NO `iso` column and NO completion_date -- it tracks DATA-
    CENTER capacity by region/market (operators: Meta, Google, Equinix ...), i.e.
    DEMAND, not generation SUPPLY -- so the query threw on every call inside a
    swallow-all try and gen_additions_12mo_mw was 0 for every market, starving
    s_additions (20% of the excess-power score). This is the excess-side sibling
    of the interconnect_queue constraint fix (f6e8984f). planned_generators is
    the correct generation-SUPPLY source (real planned_year/planned_month online
    dates, per-state coverage across 47 states). The scoring FORMULA is unchanged
    -- only the input it reads.
    """
    if not state:
        return None
    key = state.strip().upper()
    if not key:
        return None
    import time as _t
    now = _t.time()
    hit = _GEN_ADD_STATE_CACHE.get(key)
    if hit is not None and (now - hit[0]) < _GEN_ADD_STATE_CACHE_TTL_S:
        return hit[1]
    result = None
    _c = None
    try:
        _c = _conn()
        with _c.cursor() as cur:
            # planned_month/planned_year are numeric; (year*12 + month) is a
            # sortable month index. Window = [now, now+12] months. The status
            # test uses a regex (~), NOT LIKE, to avoid the psycopg2
            # '%'-as-placeholder trap and to pin only the genuine near-term
            # EIA codes. NULL year/month/status rows drop out of the filters.
            cur.execute(
                """
                SELECT COALESCE(SUM(capacity_mw), 0) AS add_mw
                  FROM planned_generators
                 WHERE UPPER(state) = %s
                   AND status ~ '^\\((U|V|T|TS)\\)'
                   AND (planned_year * 12 + planned_month) BETWEEN
                         (EXTRACT(YEAR  FROM NOW())::int * 12
                          + EXTRACT(MONTH FROM NOW())::int)
                     AND (EXTRACT(YEAR  FROM NOW())::int * 12
                          + EXTRACT(MONTH FROM NOW())::int + 12)
                """,
                (key,),
            )
            row = cur.fetchone()
        if row and (row[0] or 0) > 0:
            result = float(row[0])
    except Exception:
        result = None  # keep the default; never fabricate
    finally:
        if _c is not None:
            try:
                _c.close()
            except Exception:
                pass
    _GEN_ADD_STATE_CACHE[key] = (now, result)
    return result


def _latest_grid_telemetry(iso: str) -> Optional[dict]:
    """Return the most-recent live grid_telemetry row for an ISO as a dict, or
    None when there is no fresh row. Read-only, fail-safe (never raises into
    the scorer). Only rows newer than LIVE_TELEMETRY_MAX_AGE_MIN qualify, so a
    dead/paused feed degrades to the modeled estimate instead of masquerading
    as live. Cached 60s per ISO so a full recompute issues at most one read per
    ISO per minute (1-replica pool protection)."""
    if not iso or iso not in _TELEMETRY_ISO_CODES:
        return None
    import time as _t
    _now = _t.time()
    _hit = _TELEMETRY_CACHE.get(iso)
    if _hit and (_now - _hit[0]) < _TELEMETRY_CACHE_TTL_S:
        return _hit[1]
    row = None
    _c = None
    try:
        _c = _conn()
        with _c.cursor() as cur:
            cur.execute(
                """
                SELECT iso, zone, observed_at, online_gen_mw, load_mw,
                       headroom_mw, reserve_margin_pct, source,
                       EXTRACT(EPOCH FROM (NOW() - observed_at)) / 60.0 AS age_min
                  FROM grid_telemetry
                 WHERE iso = %s
                   AND observed_at >= NOW() - (%s || ' minutes')::interval
                 ORDER BY observed_at DESC
                 LIMIT 1
                """,
                (iso, str(int(LIVE_TELEMETRY_MAX_AGE_MIN))),
            )
            row = cur.fetchone()
    except Exception:
        # grid_telemetry may not exist yet (cron never ran) or DB hiccup —
        # the modeled fallback path handles it. Never fabricate. Do NOT cache a
        # transient error as a None miss (a brief DB blip shouldn't suppress
        # live data for the next 60s); just return None for this call.
        return None
    finally:
        # Explicit close: _conn() is a raw psycopg2 connection whose `with`-exit
        # commits but does NOT close. This runs in the per-cycle scorer, and
        # DB-pool saturation was a prior flapping cause — so never leak it.
        if _c is not None:
            try:
                _c.close()
            except Exception:
                pass
    result: Optional[dict] = None
    if row:
        iso_v, zone_v, observed_at, gen, load, headroom, rmpct, source, age_min = row
        result = {
            "iso": iso_v, "zone": zone_v, "observed_at": observed_at,
            "online_gen_mw": (float(gen) if gen is not None else None),
            "load_mw": (float(load) if load is not None else None),
            "headroom_mw": (float(headroom) if headroom is not None else None),
            "reserve_margin_pct": (float(rmpct) if rmpct is not None else None),
            "source": source,
            "age_min": (round(float(age_min), 1) if age_min is not None else None),
        }
    # Cache the successful read (row or genuine empty) for the TTL window.
    _TELEMETRY_CACHE[iso] = (_now, result)
    return result


def _live_operating_reserve_pct(tel: dict) -> Optional[float]:
    """Real instantaneous operating-reserve % from a telemetry row, signed.

    Prefers an explicit reserve_margin_pct if the adapter ever supplies one
    (none of the public adapters do today); otherwise derives it from the
    measured headroom_mw / load_mw. Returns None if neither is computable —
    NEVER a fabricated value."""
    if tel is None:
        return None
    rmpct = tel.get("reserve_margin_pct")
    if rmpct is not None:
        return float(rmpct)
    headroom = tel.get("headroom_mw")
    load = tel.get("load_mw")
    if headroom is not None and load and load > 0:
        return (float(headroom) / float(load)) * 100.0
    return None


def _reserve_margin_with_live(modeled_anchor: Optional[float],
                              tel: Optional[dict]) -> tuple[Optional[float], Optional[float]]:
    """Blend the modeled planning-reserve anchor with the LIVE operating-reserve
    reading. Returns (effective_reserve_margin_pct, live_op_reserve_pct).

    - live_op_reserve_pct is the real measured signal (signed, may be negative)
      or None when telemetry can't yield it.
    - effective_reserve_margin_pct is what the scorer should use: the modeled
      anchor shaded by the live deviation from a balanced grid (0% op-headroom),
      capped at ±LIVE_DELTA_CAP_PCT so one 5-min snapshot can't impersonate a
      planning study. When there's no live signal we return the anchor unchanged
      (caller then labels it modeled). When there's no anchor either, we return
      the live value alone rather than inventing one."""
    live_op = _live_operating_reserve_pct(tel) if tel else None
    if live_op is None:
        return modeled_anchor, None
    # Live deviation from a balanced grid, clamped so it adjusts (not replaces)
    # the calibrated planning anchor.
    delta = max(-LIVE_DELTA_CAP_PCT, min(LIVE_DELTA_CAP_PCT, live_op))
    if modeled_anchor is None:
        # No modeled anchor for this ISO/region — use the live reading directly
        # (clamped to a sane planning-reserve floor of 0; we never publish a
        # negative planning reserve, but we keep the true live_op for the basis).
        return max(0.0, live_op), live_op
    effective = float(modeled_anchor) + delta
    return max(0.0, effective), live_op


# Phase 268 (2026-05-29) — snapshot writer + backfill bootstrap. Both
# guarded by pg_try_advisory_lock so even with multiple gunicorn workers
# (or transient 2-replica states) only one runs at a time. The backfill
# is idempotent: it no-ops if dcpi_daily_snapshots already has rows.
_DCPI_SNAPSHOT_LOCK_ID = 268052901  # arbitrary stable int for advisory lock
_DCPI_BACKFILL_LOCK_ID = 268052902

def _try_advisory_lock(cur, lock_id: int) -> bool:
    try:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (lock_id,))
        return bool((cur.fetchone() or [False])[0])
    except Exception:
        return False

def _advisory_unlock(cur, lock_id: int) -> None:
    try:
        cur.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))
        cur.fetchone()
    except Exception:
        pass


def write_dcpi_snapshot() -> dict:
    """Persist today's market_power_scores snapshot into dcpi_daily_snapshots.

    Idempotent per (snapshot_date, market_slug) via ON CONFLICT upsert.
    Leader-gated via pg_try_advisory_lock so concurrent calls are safe.
    Called by the daily cron (facility-snapshot-daily.yml).
    """
    _ensure_tables()
    out: dict = {"ok": False}
    try:
        with _conn() as c, c.cursor() as cur:
            if not _try_advisory_lock(cur, _DCPI_SNAPSHOT_LOCK_ID):
                return {"ok": True, "skipped": "another_writer_holds_lock",
                        "rows_inserted": 0}
            try:
                cur.execute("""
                    INSERT INTO dcpi_daily_snapshots
                        (snapshot_date, market_slug, market_name,
                         excess_power_score, constraint_score, verdict,
                         method_version)
                    SELECT CURRENT_DATE, market_slug, market_name,
                           excess_power_score, constraint_score, verdict,
                           method_version
                      FROM market_power_scores
                     WHERE COALESCE(published, true) = true
                    ON CONFLICT (snapshot_date, market_slug) DO UPDATE
                       SET market_name        = EXCLUDED.market_name,
                           excess_power_score = EXCLUDED.excess_power_score,
                           constraint_score   = EXCLUDED.constraint_score,
                           verdict            = EXCLUDED.verdict,
                           method_version     = EXCLUDED.method_version,
                           captured_at        = NOW()
                """)
                rows = cur.rowcount
                c.commit()
                out = {"ok": True, "rows_inserted": int(rows),
                        "snapshot_date": datetime.date.today().isoformat()}
            finally:
                _advisory_unlock(cur, _DCPI_SNAPSHOT_LOCK_ID)
    except Exception as e:
        out = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    return out


def backfill_dcpi_snapshots_if_empty() -> dict:
    """One-time bootstrap: if dcpi_daily_snapshots is empty, seed it with
    today's market_power_scores rows. This gives movers a baseline of
    "today" so the table isn't NULL-tolerant forever — real 7d deltas
    show up 7 days after this bootstrap.

    Safe to call repeatedly: the empty-check guards against re-running,
    and the advisory lock guards against multi-worker races.
    """
    _ensure_tables()
    out: dict = {"ok": False, "backfilled": False}
    try:
        with _conn() as c, c.cursor() as cur:
            if not _try_advisory_lock(cur, _DCPI_BACKFILL_LOCK_ID):
                return {"ok": True, "skipped": "another_writer_holds_lock",
                        "backfilled": False}
            try:
                cur.execute("SELECT COUNT(*) FROM dcpi_daily_snapshots")
                existing = int((cur.fetchone() or [0])[0] or 0)
                if existing > 0:
                    out = {"ok": True, "backfilled": False,
                            "existing_rows": existing,
                            "reason": "table_already_populated"}
                else:
                    # First-ever bootstrap. Seed with today's scores so
                    # movers has a baseline to subtract from (deltas will
                    # all be 0 today, real deltas start day 8).
                    cur.execute("""
                        INSERT INTO dcpi_daily_snapshots
                            (snapshot_date, market_slug, market_name,
                             excess_power_score, constraint_score, verdict)
                        SELECT CURRENT_DATE, market_slug, market_name,
                               excess_power_score, constraint_score, verdict
                          FROM market_power_scores
                         WHERE COALESCE(published, true) = true
                        ON CONFLICT (snapshot_date, market_slug) DO NOTHING
                    """)
                    rows = cur.rowcount
                    c.commit()
                    out = {"ok": True, "backfilled": True,
                            "rows_inserted": int(rows),
                            "snapshot_date": datetime.date.today().isoformat()}
            finally:
                _advisory_unlock(cur, _DCPI_BACKFILL_LOCK_ID)
    except Exception as e:
        out = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    return out


# ---------------------------------------------------------------------------
# Market universe — extend as we go
# ---------------------------------------------------------------------------
_MARKETS_HARDCODED = [
    # (slug, display name, state, ISO, lat, lon)
    ("northern-virginia",   "Northern Virginia",      "VA", "PJM",   38.95, -77.45),
    ("dallas-fort-worth",   "Dallas–Fort Worth",      "TX", "ERCOT", 32.78, -96.80),
    ("phoenix",             "Phoenix",                "AZ", "WECC",  33.45, -112.07),
    ("atlanta",             "Atlanta",                "GA", "SERC",  33.75, -84.39),
    ("chicago",             "Chicago",                "IL", "PJM",   41.88, -87.63),
    ("silicon-valley",      "Silicon Valley",         "CA", "CAISO", 37.40, -121.95),
    ("santa-clara",         "Santa Clara",            "CA", "CAISO", 37.35, -121.96),
    ("new-york",            "New York Metro",         "NY", "NYISO", 40.71, -74.01),
    ("seattle",             "Seattle",                "WA", "WECC",  47.61, -122.33),
    ("portland-or",         "Portland",               "OR", "WECC",  45.51, -122.68),
    ("central-washington",  "Central Washington",     "WA", "WECC",  47.10, -120.30),  # excess hydro
    ("columbus-oh",         "Columbus",               "OH", "PJM",   39.96, -83.00),
    ("salt-lake-city",      "Salt Lake City",         "UT", "WECC",  40.76, -111.89),
    ("kansas-city",         "Kansas City",            "MO", "SPP",   39.10, -94.58),
    ("minneapolis",         "Minneapolis",            "MN", "MISO",  44.98, -93.27),
    ("austin",              "Austin",                 "TX", "ERCOT", 30.27, -97.74),
    ("houston",             "Houston",                "TX", "ERCOT", 29.76, -95.37),
    ("nashville",           "Nashville",              "TN", "TVA",   36.16, -86.78),
    ("denver",              "Denver",                 "CO", "WECC",  39.74, -104.99),
    ("las-vegas",           "Las Vegas",              "NV", "WECC",  36.17, -115.14),
    ("memphis",             "Memphis",                "TN", "MISO",  35.15, -90.05),
    ("st-louis",            "St. Louis",              "MO", "MISO",  38.63, -90.20),
    # The contrarian set — markets the Excess Power Score should highlight
    ("williston-nd",        "Williston, ND",          "ND", "MISO",  48.15, -103.62),
    ("cheyenne-wy",         "Cheyenne, WY",           "WY", "WECC",  41.14, -104.82),
    ("midland-tx",          "Midland–Odessa",         "TX", "ERCOT", 31.99, -102.07),
    ("appalachia-coal",     "Appalachia (Retiring Coal)", "WV", "PJM", 38.50, -81.50),
    ("the-dalles-or",       "The Dalles, OR",         "OR", "WECC",  45.59, -121.17),
    ("pacific-nw-rural",    "Rural Pacific NW",       "OR", "WECC",  44.50, -120.00),
    ("rural-spp",           "Rural SPP",              "KS", "SPP",   38.50, -98.50),
    ("upper-michigan",      "Upper Peninsula MI",     "MI", "MISO",  46.50, -87.50),
    # r57 (2026-05-25): International expansion. 15 markets across UK,
    # EU, APAC, Canada. State codes use country-region pairs so the
    # downstream UI can group by sovereign. ISOs are real grid operators
    # registered upstream: NGESO (UK), EirGrid (IE), ENTSOE-* (EU),
    # NORDPOOL (Nordic), TEPCO/KEPCO (JP), AEMO (AU), EMA (SG), IESO
    # (Ontario), HQ (Québec), BCH (British Columbia).
    #
    # iso_defaults gets matching entries below so the scorer doesn't
    # silently fall back to WECC and emit fake US-style verdicts.
    ("london",              "London",                 "UK", "NGESO",     51.51,  -0.13),
    ("manchester",          "Manchester",             "UK", "NGESO",     53.48,  -2.24),
    ("dublin",              "Dublin",                 "IE", "EirGrid",   53.35,  -6.26),
    ("frankfurt",           "Frankfurt",              "DE", "ENTSOE-DE", 50.11,   8.68),
    ("amsterdam",           "Amsterdam",              "NL", "ENTSOE-NL", 52.37,   4.90),
    ("paris",               "Paris",                  "FR", "ENTSOE-FR", 48.86,   2.35),
    ("marseille",           "Marseille",              "FR", "ENTSOE-FR", 43.30,   5.37),
    ("stockholm",           "Stockholm",              "SE", "NORDPOOL",  59.33,  18.06),
    ("tokyo",               "Tokyo",                  "JP", "TEPCO",     35.68, 139.69),
    ("osaka",               "Osaka",                  "JP", "KEPCO",     34.69, 135.50),
    ("sydney",              "Sydney",                 "AU", "AEMO",     -33.87, 151.21),
    ("melbourne",           "Melbourne",              "AU", "AEMO",     -37.81, 144.96),
    ("singapore",           "Singapore",              "SG", "EMA",        1.35, 103.82),
    ("toronto",             "Toronto",                "ON", "IESO",      43.65, -79.38),
    ("montreal",            "Montréal",               "QC", "HQ",        45.50, -73.57),
    ("vancouver",           "Vancouver",              "BC", "BCH",       49.28,-123.12),

    # r71 (2026-06-06): coverage expansion — DCPI 233→290+.
    # ── US tier-2/3 (Tier 2 Coverage) ────────────────────────────────────
    # All US cities in markets we know are AI/DC-relevant but missed by the
    # >=3-facility threshold of _load_markets_dynamic. Scored from the live
    # ISO data we already ingest (ERCOT/PJM/MISO/SPP/WECC/etc.) — verdict
    # is reliable; per-market overrides (slug_overrides below) tighten the
    # ones with public planning data.
    ("boise",               "Boise",                  "ID", "WECC",  43.62, -116.21),
    ("tulsa",               "Tulsa",                  "OK", "SPP",   36.15,  -95.99),
    ("oklahoma-city",       "Oklahoma City",          "OK", "SPP",   35.47,  -97.52),
    ("albuquerque",         "Albuquerque",            "NM", "WECC",  35.08, -106.65),
    ("birmingham",          "Birmingham",             "AL", "SOCO",  33.52,  -86.81),
    ("wichita",             "Wichita",                "KS", "SPP",   37.69,  -97.34),
    ("omaha",               "Omaha",                  "NE", "SPP",   41.26,  -95.93),
    ("spokane",             "Spokane",                "WA", "WECC",  47.66, -117.43),
    ("tucson",               "Tucson",                "AZ", "WECC",  32.22, -110.93),
    ("buffalo",             "Buffalo",                "NY", "NYISO", 42.89,  -78.88),
    ("hartford",            "Hartford",               "CT", "ISONE", 41.76,  -72.67),
    ("charleston-sc",       "Charleston, SC",         "SC", "SOCO",  32.78,  -79.93),
    ("knoxville",           "Knoxville",              "TN", "TVA",   35.96,  -83.92),
    ("lexington-ky",        "Lexington",              "KY", "PJM",   38.04,  -84.50),
    ("madison",             "Madison",                "WI", "MISO",  43.07,  -89.40),
    ("des-moines",          "Des Moines",             "IA", "MISO",  41.59,  -93.62),
    ("sioux-falls",         "Sioux Falls",            "SD", "MISO",  43.55,  -96.73),
    ("bismarck",            "Bismarck",               "ND", "MISO",  46.81, -100.78),
    ("reno",                "Reno",                   "NV", "WECC",  39.53, -119.81),
    ("pittsburgh",          "Pittsburgh",             "PA", "PJM",   40.44,  -79.99),
    ("cleveland",           "Cleveland",              "OH", "PJM",   41.50,  -81.69),
    ("indianapolis",        "Indianapolis",           "IN", "MISO",  39.77,  -86.16),
    ("little-rock",         "Little Rock",            "AR", "MISO",  34.75,  -92.29),
    ("jackson-ms",          "Jackson",                "MS", "MISO",  32.30,  -90.18),
    ("anchorage",           "Anchorage",              "AK", "WECC",  61.22, -149.90),

    # ── US territories + DC (Tier 2 Coverage) ────────────────────────────
    # DC has its own grid (Pepco/PJM), the Caribbean/Pacific territories
    # run isolated island grids. ISO key 'TERRITORY' triggers the
    # iso_defaults fallback to WECC-ish neutral params; per-slug overrides
    # set their known characteristics (DC is in PJM, etc.).
    ("dc",                  "Washington, DC",         "DC", "PJM",   38.91,  -77.04),
    ("san-juan",            "San Juan",               "PR", "PREPA", 18.47,  -66.10),
    ("guam",                "Guam",                   "GU", "GPA",   13.50, 144.79),
    ("virgin-islands",      "US Virgin Islands",      "VI", "WAPA",  18.34,  -64.93),

    # ── Canada (extra provinces) ─────────────────────────────────────────
    ("calgary",             "Calgary",                "AB", "AESO",  51.05, -114.07),
    ("edmonton",            "Edmonton",               "AB", "AESO",  53.55, -113.49),
    ("winnipeg",            "Winnipeg",               "MB", "MH",    49.90,  -97.14),
    ("ottawa",              "Ottawa",                 "ON", "IESO",  45.42,  -75.70),
    ("quebec-city",         "Québec City",            "QC", "HQ",    46.81,  -71.20),

    # ── EU expansion (ISO data already ingested via ENTSO-E) ─────────────
    # All ISOs in ENTSOE-* family; mapped to per-country defaults below.
    ("madrid",              "Madrid",                 "ES", "ENTSOE-ES", 40.42,  -3.70),
    ("barcelona",           "Barcelona",              "ES", "ENTSOE-ES", 41.39,   2.17),
    ("milan",               "Milan",                  "IT", "ENTSOE-IT", 45.46,   9.19),
    ("rome",                "Rome",                   "IT", "ENTSOE-IT", 41.90,  12.50),
    ("munich",              "Munich",                 "DE", "ENTSOE-DE", 48.14,  11.58),
    ("berlin",              "Berlin",                 "DE", "ENTSOE-DE", 52.52,  13.40),
    ("rotterdam",           "Rotterdam",              "NL", "ENTSOE-NL", 51.92,   4.48),
    ("copenhagen",          "Copenhagen",             "DK", "NORDPOOL",  55.68,  12.57),
    ("oslo",                "Oslo",                   "NO", "NORDPOOL",  59.91,  10.75),
    ("helsinki",            "Helsinki",               "FI", "NORDPOOL",  60.17,  24.94),
    ("warsaw",              "Warsaw",                 "PL", "ENTSOE-PL", 52.23,  21.01),
    ("vienna",              "Vienna",                 "AT", "ENTSOE-AT", 48.21,  16.37),
    ("brussels",            "Brussels",               "BE", "ENTSOE-BE", 50.85,   4.35),
    ("lisbon",              "Lisbon",                 "PT", "ENTSOE-PT", 38.72,  -9.14),
    ("zurich",              "Zurich",                 "CH", "ENTSOE-CH", 47.37,   8.54),
    ("athens",              "Athens",                 "GR", "ENTSOE-GR", 37.98,  23.73),
    ("prague",              "Prague",                 "CZ", "ENTSOE-CZ", 50.08,  14.44),
    ("edinburgh",           "Edinburgh",              "UK", "NGESO",     55.95,  -3.19),

    # ── APAC expansion ──────────────────────────────────────────────────
    ("seoul",               "Seoul",                  "KR", "KEPCO-KR",  37.57, 126.98),
    ("busan",               "Busan",                  "KR", "KEPCO-KR",  35.18, 129.08),
    ("mumbai",              "Mumbai",                 "IN", "POSOCO",    19.08,  72.88),
    ("hyderabad",           "Hyderabad",              "IN", "POSOCO",    17.39,  78.49),
    ("chennai",             "Chennai",                "IN", "POSOCO",    13.08,  80.27),
    ("bangalore",           "Bangalore",              "IN", "POSOCO",    12.97,  77.59),
    ("jakarta",             "Jakarta",                "ID", "PLN",       -6.21, 106.85),
    ("hong-kong",           "Hong Kong",              "HK", "CLP",       22.30, 114.17),
    ("taipei",              "Taipei",                 "TW", "TAIPOWER",  25.03, 121.57),
    ("bangkok",             "Bangkok",                "TH", "EGAT",      13.76, 100.50),
    ("kuala-lumpur",        "Kuala Lumpur",           "MY", "TNB",        3.14, 101.69),
    ("manila",              "Manila",                 "PH", "NGCP",      14.60, 120.98),
    ("ho-chi-minh-city",    "Ho Chi Minh City",       "VN", "EVN",       10.82, 106.63),
    ("auckland",            "Auckland",               "NZ", "TPM",      -36.85, 174.76),
    ("perth",               "Perth",                  "WA", "AEMO",     -31.95, 115.86),
    ("brisbane",            "Brisbane",               "QL", "AEMO",     -27.47, 153.03),

    # ── r-str-coverage (2026-08-07): four published markets DCPI could not
    # score ──────────────────────────────────────────────────────────────
    # Each is a real metro with a real operator, and NONE of them can arrive
    # via _load_markets_dynamic — that loader filters country='US'.
    #   - johor: Malaysia's Sedenak/Kulai cluster. TNB (Peninsular Malaysia),
    #     the same operator as kuala-lumpur. It sits ~17 km from singapore
    #     and is NOT its twin: different sovereign, different grid, different
    #     queue. That distance is an international border, not a suburb.
    #   - batam: Indonesian free-trade island (Nongsa). Runs its OWN ISOLATED
    #     grid — bright PLN Batam — not the Java–Bali system jakarta sits on.
    #     It therefore gets its own ISO key instead of inheriting PLN's
    #     national anchors, which describe a grid it is not connected to.
    #   - pune: Maharashtra, POSOCO like mumbai. ★state='IN' collides with
    #     Indiana. Safe only because _normalize_us_isos gates on the CURRENT
    #     iso label, not the state code (see its GUARD note) — POSOCO is not
    #     in _US_DCPI_ISOS, so the row is never rewritten to MISO. Do not
    #     "simplify" that guard to a state lookup.
    #   - queretaro: Mexico's data-centre hub and the first MEXICAN market in
    #     DCPI. CENACE is Mexico's system operator; in central Mexico the
    #     binding constraint is transmission, not generation.
    #
    #     ★CORRECTION (2026-08-07, same day): an earlier version of this
    #     comment called queretaro "the FIRST Latin American market in DCPI
    #     (the set had zero LatAm rows before today)". That was WRONG, and the
    #     way it was wrong is worth keeping. `barueri` and `osasco` — two
    #     Greater-São-Paulo municipalities — were ALREADY scored, at 45.9 and
    #     45.8. They were missed because the check grepped
    #     _MARKETS_HARDCODED, and they do not live here: they arrive from
    #     _load_markets_dynamic, so no amount of reading this file could have
    #     found them.
    #     ★To answer "is city X already in DCPI", query the LIVE SCORED
    #     UNIVERSE (/api/v1/dcpi/scores at the origin), never these tuples.
    #     Same class as the registration-vs-routable trap.
    #     ★Related and unfixed: barueri and osasco publish with NO grid
    #     operator at all (their page titles carry no "· <op> grid" segment),
    #     which means iso is empty and their planning anchors fall through
    #     iso_defaults to the WECC default — Western-US parameters on the
    #     Brazilian grid, whose operator is ONS. queretaro is therefore the
    #     first LatAm market with a REAL operator anchor, which is the only
    #     "first" worth claiming. Fixing those two is a separate change.
    ("johor",               "Johor",                  "MY", "TNB",         1.49, 103.74),
    ("batam",               "Batam",                  "ID", "PLN-BATAM",   1.08, 104.03),
    ("pune",                "Pune",                   "IN", "POSOCO",     18.52,  73.86),
    ("queretaro",           "Querétaro",              "MX", "CENACE",     20.59, -100.39),

    # r-orphan-geography (2026-07-30): pin two markets the orphan re-adopter
    # kept re-publishing with US-doppelgänger geography. Neither can come
    # from the dynamic loader (their discovered_facilities rows are all
    # country ZA / CA, which the `country='US'` filter excludes), so their
    # corrupted market_power_scores row was the ONLY source tuple and the
    # daily recompute wrote the corruption back every run:
    #   - johannesburg: 80 facilities, ALL South Africa (Gauteng — sibling
    #     city of midrand, like ashburn/sterling). The row carried
    #     state='GA' (Gauteng abbreviated, but US-state-shaped), so ISO
    #     normalization stamped SOCO and a geocode-era backfill placed it
    #     at Johannesburg CALIFORNIA (35.37, -117.63 — a Mojave town of
    #     ~170 people). state GP + empty iso mirror midrand's convention;
    #     _normalize_us_isos leaves both alone (GP is not in STATE_ISO).
    #   - markham: 27 facilities, ALL Ontario (Greater Toronto — Cologix
    #     TOR4, Digital Realty YYZ10, Centersquare YYZ2). The row said
    #     NY/NYISO at the Markham HAMLET in upstate New York
    #     (42.84, -75.23). ON/IESO, same as toronto/ottawa.
    # Hardcoded rows beat the orphan re-adopter in _build_markets_list, so
    # the recompute now rewrites the corrected fields daily instead of the
    # corrupted ones. Pinned by tests/test_dcpi_orphan_geography.py.
    ("johannesburg",        "Johannesburg",           "GP", "",       -26.20,  28.05),
    ("markham",             "Markham",                "ON", "IESO",    43.86, -79.34),
]

# r-portland-canon (2026-08-02): (cleaned city slug, state) → (slug, name)
# overrides for dynamic city markets whose bare-city slug ALREADY MEANS a
# different market everywhere else. Portland, ME cleared the >=3-facilities
# bar and the LOWER(city) rule minted it as bare 'portland' named 'Portland'
# — but 'portland' is Portland, OREGON on every other surface (the hardcoded
# 'portland-or' row's display name, main.py market vocab, the curated
# /markets/portland page). The name collision cross-wired the deep-dive
# name-match resolver nightly. Mint Maine state-suffixed and name-qualified
# instead (the 'Cheyenne, WY' / 'Williston, ND' convention). The recompute
# self-heal below renames any pre-existing bare row so this cannot leave a
# stale twin behind (st.-louis pattern, 702a7bd0).
#
# r-aurora-canon (2026-08-02): same mechanism, INVERTED outcome. Aurora IL
# (22 fac / 158 MW, Chicago metro) and Aurora CO (12 fac / 51 MW, Denver
# metro) both clear the >=3-facilities bar and both land inside the loader's
# `LIMIT 200`, so the loader emitted TWO groups under slug 'aurora' and the
# per-slug `UPDATE ... WHERE market_slug=%s` scoring loop kept only the one
# written LAST. Ordering is facility_count DESC, so the SMALLER city always
# writes last and wins the slug — which is why the single live row said CO
# while 76% of the MW behind it is Illinois (the /markets/aurora brief read
# "29 facilities, 209 MW" with CyrusOne — an ILLINOIS operator — as its top
# operator, under a Colorado label).
#
# Unlike Portland there was NO corroborating surface: no hardcoded aurora-il
# / aurora-co market, no curated /markets/aurora page, no main.py vocab entry,
# and main.MARKET_ALIASES claims 'Aurora' for BOTH 'chicago' and 'denver'.
# Owner decision (2026-08-02): bare 'aurora' MEANS ILLINOIS — it is ~2x the
# facilities and ~3x the MW, and it preserves most of what the already-indexed
# URL says. Colorado is minted state-suffixed.
#
# NOTE the inversion vs portland, and do not "fix" it by symmetry: bare
# 'aurora' stays a REAL, published market here, so it must NOT get a
# DCPI_METRO_ALIASES entry, must NOT enter REDUNDANT_TWIN_SLUGS, and
# 'aurora-co' must NOT be a MARKETS_CANONICAL_REDIRECT source — it is its own
# indexable market page. Bare 'portland' was retired because a hardcoded
# 'portland-or' row already owned Oregon; Aurora has no such twin to fold into.
_CITY_MARKET_DISAMBIGUATION = {
    ("portland", "ME"): ("portland-me", "Portland, ME"),
    ("aurora", "CO"): ("aurora-co", "Aurora, CO"),
}


# Phase 214: try dynamic 132-market list first, fall back to hardcoded 30
def _load_markets_dynamic():
    """Phase 215: direct Postgres query — no internal API auth dance.
    Returns list matching the structure of _MARKETS_HARDCODED.
    """
    import os, psycopg2
    try:
        url = os.environ.get("DATABASE_URL")
        if not url:
            return None
        conn = psycopg2.connect(url, connect_timeout=8)
        with conn.cursor() as cur:
            # All US cities with >=3 facilities + dominant state
            #
            # r-universe-dedup (2026-08-08): this CTE had NO duplicate-visibility
            # predicate, and unlike the saturation footprint it does not merely
            # mis-score a market — it decides WHICH CITIES ARE MARKETS AT ALL.
            # 9,459 of 24,859 discovered_facilities rows (38%) carry a
            # duplicate_of_id, so a twin-heavy city read up to 10x its real size:
            #   ADMISSION  `HAVING COUNT(*) >= 3` is the published bar for
            #     becoming a scored DCPI market. goose-creek SC counted 12 rows
            #     against fewer than 3 real buildings — admitted on twins alone.
            #   CROWD-OUT  `ORDER BY facility_count DESC LIMIT 200` is a fixed
            #     cap, so padding is zero-sum. Measured on the live replica, 22
            #     real markets were displaced off the 200 by twin-inflated ones,
            #     including mount-pleasant WI (3,600 MW) and abilene TX (3,100 MW)
            #     — two of the largest AI-era campuses in the country, absent
            #     from the scored set because ashburn was counted 308 instead of
            #     163. Same shape as r-list-dedup, where twins ate a LIMIT 50.
            #   CENTROID   the percentile_cont median is taken over the SAME
            #     rows, so duplicated coordinates re-weight it. 36 markets move
            #     >0.5 km and chattanooga moves 21.4 km. That centroid is what
            #     gather_metrics_for_market hands to _local_infra_metrics, whose
            #     25/40/60 km boxes feed constraint (<= +6) and excess (<= +8) —
            #     so this is a scored INPUT, not just a label. It is written
            #     back via `latitude=COALESCE(%s, latitude)`, where the new
            #     value wins, so the correction lands on the next recompute.
            # The op_mw/pipeline_mw columns are ALSO double-counted, but the
            # tuple branch below discards them — they are dead output today.
            # Deduping them anyway keeps the row honest if a branch ever reads it.
            #
            # No market LEAVES the scored universe: every displaced slug is
            # already in market_power_scores, and _load_scored_orphans re-adopts
            # anything ever scored. Verified before shipping, not assumed.
            cur.execute(f"""
                WITH city_stats AS (
                    SELECT
                        LOWER(city) AS slug,
                        city AS name,
                        state,
                        COUNT(*) AS facility_count,
                        COALESCE(SUM(power_mw), 0) AS op_mw,
                        COALESCE(SUM(power_mw) FILTER (WHERE status IN ('construction','planned','permitting','Under Construction','Planned')), 0) AS pipeline_mw,
                        -- r-market-coords (2026-07-06): market centroid from the
                        -- market's own facilities so market_power_scores.lat/lng
                        -- stops being NULL (feeds facility_profile nearest-metro
                        -- fallback + market_brief proximity). MEDIAN not AVG: some
                        -- discovered_facilities rows carry bad coords (e.g. a few
                        -- TX rows sit in the Gulf), and a mean gets dragged to open
                        -- water — median lands on the true metro center. The
                        -- recompute writes these via COALESCE, so they populate
                        -- NULLs + survive future NULL recomputes.
                        percentile_cont(0.5) WITHIN GROUP (ORDER BY latitude)
                            FILTER (WHERE latitude IS NOT NULL AND latitude BETWEEN -90 AND 90) AS lat,
                        percentile_cont(0.5) WITHIN GROUP (ORDER BY longitude)
                            FILTER (WHERE longitude IS NOT NULL AND longitude BETWEEN -180 AND 180) AS lon
                    FROM discovered_facilities
                    WHERE city IS NOT NULL AND city != ''
                      AND state IS NOT NULL AND state != ''
                      AND LENGTH(state) = 2
                      AND state ~ '^[A-Z]{{2}}$'
                      AND (country = 'US' OR country = 'USA')
                      {_SQL_FOOTPRINT_DEDUP}
                    GROUP BY LOWER(city), city, state
                    HAVING COUNT(*) >= 3
                )
                SELECT slug, name, state, facility_count, op_mw, pipeline_mw, lat, lon
                FROM city_stats
                ORDER BY facility_count DESC
                LIMIT 200;
            """)
            rows = cur.fetchall()
        conn.close()

        if not rows:
            return None

        # Adapt to the hardcoded MARKETS structure (list of dicts)
        if isinstance(_MARKETS_HARDCODED, list) and _MARKETS_HARDCODED:
            sample = _MARKETS_HARDCODED[0]
            if isinstance(sample, dict):
                keys = list(sample.keys())
                # Map our DB rows to the same dict shape
                out = []
                for r in rows:
                    slug, name, state, fac, op_mw, pipe_mw, lat, lon = r
                    # r-portland-canon (2026-08-02): state-suffix a city whose
                    # bare slug is another market's name (portland → ME twin).
                    _clean = slug.replace(" ", "-").replace(",", "").replace(".", "")
                    _clean, name = _CITY_MARKET_DISAMBIGUATION.get(
                        (_clean, state), (_clean, name))
                    d = {}
                    for k in keys:
                        if k == "slug": d[k] = _clean
                        elif k == "name": d[k] = name
                        elif k == "state": d[k] = state
                        elif k == "country": d[k] = "US"
                        elif k == "cities": d[k] = [name]
                        elif k in ("facility_count", "fac"): d[k] = int(fac)
                        elif k in ("operational_mw", "op_mw", "total_mw"): d[k] = float(op_mw)
                        elif k in ("pipeline_mw", "pipeline_mw_total"): d[k] = float(pipe_mw)
                        elif k in ("latitude", "lat"): d[k] = float(lat) if lat is not None else sample.get(k)
                        elif k in ("longitude", "lon", "lng"): d[k] = float(lon) if lon is not None else sample.get(k)
                        else: d[k] = sample.get(k)  # default from hardcoded
                    out.append(d)
                return out
            elif isinstance(sample, str):
                # List of slug strings
                # r-period-slug (2026-07-06): strip periods too — 'St. Louis'
                # → 'st-louis' (was 'st.-louis', a soft-404 dup of the canonical).
                return [r[0].replace(" ", "-").replace(",", "").replace(".", "") for r in rows]
            elif isinstance(sample, tuple):
                # Phase ZZ (2026-05-16) — CRITICAL FIX. _MARKETS_HARDCODED is
                # a list of 6-tuples (slug, name, state, iso, lat, lon) but
                # this branch was MISSING, so the function fell through to
                # `return None` on EVERY call. Result: MARKETS = the 30
                # hardcoded markets only, never the 200+ dynamic ones; the
                # daily recompute refreshed only 30 of 276 DCPI markets,
                # leaving 246 frozen at 3-5 days stale. /api/v1/dcpi/scores
                # showed median age 5.1 days as a direct consequence. Auto-
                # press kept writing about Cheyenne because it was one of
                # the few markets with fresh data.
                #
                # Now: emit tuples in the canonical shape. iso is resolved
                # via iso_taxonomy (the common case). lat/lon are the
                # median facility centroid (r-market-coords 2026-07-06) so the
                # recompute can COALESCE real coords into market_power_scores
                # (was hardcoded None,None → 198/317 markets sat NULL). None
                # still passes through harmlessly for the rare coord-less group.
                from util.iso_taxonomy import resolve_iso as _resolve_iso
                out_tuples = []
                for r in rows:
                    slug, name, state, fac, op_mw, pipe_mw, lat, lon = r
                    # r-period-slug (2026-07-06): strip periods too — 'St. Louis'
                    # → 'st-louis' (was 'st.-louis', a soft-404 dup of canonical).
                    clean_slug = slug.replace(" ", "-").replace(",", "").replace(".", "")
                    # r-portland-canon (2026-08-02): state-suffix a city whose
                    # bare slug is another market's name (portland → ME twin).
                    # BEFORE the iso resolve, so the slug-keyed override table
                    # can't hand Maine an Oregon grid.
                    clean_slug, name = _CITY_MARKET_DISAMBIGUATION.get(
                        (clean_slug, state), (clean_slug, name))
                    # r-iso-taxonomy (2026-07-28): resolve on the SLUG first,
                    # not the state alone — Kansas City (Evergy/SPP) and
                    # St. Louis (Ameren/MISO) are both "MO" and a state-only
                    # lookup is wrong for one of them no matter what it returns.
                    iso = _resolve_iso(clean_slug, state)
                    lat = float(lat) if lat is not None else None
                    lon = float(lon) if lon is not None else None
                    out_tuples.append((clean_slug, name, state, iso, lat, lon))
                return out_tuples
        return None
    except Exception as e:
        import logging
        logging.warning(f"_load_markets_dynamic direct DB failed: {e}")
        return None


# r-split-state-iso (2026-07-28): per-MARKET iso overrides, applied AFTER
# _state_to_iso() in _load_markets_dynamic().
#
# _state_to_iso() gives a whole state one ISO. For a state whose metros sit in
# different RTOs that cannot be right at any state value, and the damage is not
# cosmetic: the dynamic loader's derived iso WINS over _MARKETS_HARDCODED on a
# slug collision, so a market someone already recorded correctly by hand gets
# silently overwritten on the next recompute. Downstream, agents read this
# field literally and pull that RTO's interconnection queue.
#
# Keyed on the cleaned slug the loader builds (LOWER(city), spaces -> dashes).
# Add an entry ONLY for a metro whose serving utility contradicts its state
# default — not to paper over a state default that is simply wrong (fix the
# state instead, as NC was below).
# r-iso-taxonomy (2026-07-28): the override table moved to
# util/iso_taxonomy.MARKET_ISO_OVERRIDES, alongside the state defaults it
# overrides — keeping the two halves of one decision in two files is how the
# maps drifted in the first place. Re-exported (not redefined) so this name
# stays importable and tests/test_dcpi_state_iso.py keeps working.
#
# The Kansas City entry that lived here is preserved there verbatim, and the
# rest of the Evergy Metro Missouri side (north-kansas-city, lees-summit,
# independence…) was added with it: north-kansas-city was live with iso=MISO
# on the same wrong state default, so fixing only kansas-city would have left
# one half of the metro on the wrong grid.
from util.iso_taxonomy import MARKET_ISO_OVERRIDES as _MARKET_ISO_OVERRIDES  # noqa: E402


def _state_to_iso(state: str) -> str:
    """Map a US state code to its dominant grid operator.

    r-iso-taxonomy (2026-07-28): the map that used to live inline here is
    now util/iso_taxonomy.STATE_ISO. It was one of four divergent copies in
    the tree and the only *wrong* one (NC→PJM, MO→MISO, MI→PJM, SC→SOCO),
    and because the daily recompute writes `iso` unconditionally it was the
    copy that governed every served row. See util/iso_taxonomy for why.

    Kept as a thin wrapper: routes/gas_intelligence.py imports it by name.
    Prefer iso_taxonomy.resolve_iso(slug, state) in new code — this
    signature cannot see the market slug, so it cannot resolve split
    states (Kansas City vs St. Louis both being MO).
    """
    from util.iso_taxonomy import resolve_iso
    return resolve_iso(state=state)


# r-declone-2 (2026-07-17): US-grid ISO codes (incl. territories, whose state
# codes stay US-style). Used by gather_metrics_for_market to pick the local-
# footprint match grain: US markets match discovered_facilities on city+state
# (the aurora-CO vs aurora-IL problem); any other ISO is international and
# matches on city across non-US rows, because intl `state` spellings in
# discovered_facilities are free-text ('QLD'/'WAS'/'Maharashtra'/''…).
_US_DCPI_ISOS = frozenset({
    "CAISO", "ERCOT", "NYISO", "ISONE", "PJM", "MISO", "SPP", "WECC",
    "TVA", "SOCO", "SERC", "FRCC", "PREPA", "GPA", "WAPA",
})

# r-namesake (2026-08-07): the 50 states + DC (from util/iso_taxonomy.STATE_ISO,
# the SoT for state->grid) plus the territories DCPI scores US-style. This is a
# LAST-RESORT tiebreaker in _is_intl_market, never a first test: several of
# these two-letter codes are also non-US subdivisions (DE is Delaware AND
# Deutschland, WA is Washington AND Western Australia, GA is Georgia AND
# Gauteng), which is why the ISO label is consulted first.
try:
    from util.iso_taxonomy import STATE_ISO as _STATE_ISO_FOR_CODES
    _US_STATE_CODES = frozenset(_STATE_ISO_FOR_CODES) | {"PR", "GU", "VI"}
except Exception:                                    # pragma: no cover
    _US_STATE_CODES = frozenset()


def _place_label(market_name: str | None, state: str | None) -> str:
    """'Cheyenne, WY' — appending the state only when it isn't already there.

    r-iso-taxonomy (2026-07-28): the JSON-LD Dataset block built
    spatialCoverage.name as f"{market_name}, {state}" unconditionally, but
    seven markets carry the state inside market_name already, so
    /dcpi/cheyenne-wy published "Cheyenne, WY, WY" to Google and to every
    agent reading the Schema.org markup. Also hit: williston-nd,
    the-dalles-or, charleston-sc, dc ("Washington, DC, DC") and
    upper-michigan ("Upper Peninsula MI, MI").

    Fixed here rather than by rewriting the seven market_name values: the
    concat is the actual defect, and normalising the names would leave the
    next 'City, ST'-shaped row to reintroduce it.
    """
    name = (market_name or "").strip()
    st = (state or "").strip()
    if not st:
        return name
    if not name:
        return st
    # Already ends with the state, with or without the comma
    # ('Cheyenne, WY' / 'Upper Peninsula MI').
    tail = name[-len(st):].upper()
    if tail == st.upper():
        boundary = name[:-len(st)].rstrip()
        if boundary.endswith(",") or boundary != name[:-len(st)]:
            return name
    return f"{name}, {st}"


def _log_sat(v: float, ceiling: float) -> float:
    """Log-scaled 0..1 saturation term: ln(1+v)/ln(1+ceiling), clipped.

    r-declone-2 (2026-07-17): replaces the linear v/ceiling terms whose
    output differences between real small metros (5 vs 13 facilities)
    were smaller than the 0.1 rounding of every published score — the
    direct cause of ISO-cloned DCPI rows surviving r-declone."""
    try:
        v = max(0.0, float(v or 0))
        return _clip(math.log1p(v) / math.log1p(ceiling), 0.0, 1.0)
    except Exception:
        return 0.0


# r57 (2026-05-25): Splice the international markets in even when the
# dynamic loader succeeds. The dynamic loader filters
# `country = 'US' OR country = 'USA'` so it never returns the new UK/
# EU/APAC/CA set. Without this splice the daily recompute would still
# only score US markets after r57 ships.
_INTL_ISO_LABELS = frozenset((
                      # OG intl set (r57)
                      "NGESO", "EirGrid", "ENTSOE-DE", "ENTSOE-NL",
                      "ENTSOE-FR", "NORDPOOL", "TEPCO", "KEPCO",
                      "AEMO", "EMA", "IESO", "HQ", "BCH",
                      # r71 (2026-06-06) coverage expansion: EU
                      "ENTSOE-ES", "ENTSOE-IT", "ENTSOE-PL",
                      "ENTSOE-AT", "ENTSOE-BE", "ENTSOE-PT",
                      "ENTSOE-CH", "ENTSOE-GR", "ENTSOE-CZ",
                      # r71: APAC
                      "KEPCO-KR", "POSOCO", "PLN", "CLP",
                      "TAIPOWER", "EGAT", "TNB", "NGCP",
                      "EVN", "TPM",
                      # r-str-coverage (2026-08-07): batam's isolated island
                      # grid + Mexico's system operator. ★A market whose ISO
                      # is missing from THIS list is silently never scored —
                      # the splice is what puts non-US rows into the
                      # recompute universe at all. Adding a tuple above
                      # without adding its ISO here is a no-op.
                      "PLN-BATAM", "CENACE",
                      # r71: Canada extras
                      "AESO", "MH",
                      # r71: US territories (NOT in dynamic loader b/c
                      # discovered_facilities country='US' filter doesn't
                      # cover PR/GU/VI consistently; treat as intl for
                      # the merge but their state codes (DC/PR/GU/VI)
                      # stay US-style)
                      "PREPA", "GPA", "WAPA",
))

_INTL_MARKETS = [m for m in _MARKETS_HARDCODED
                  if isinstance(m, tuple) and len(m) >= 4
                  and m[3] in _INTL_ISO_LABELS]


# ── r-namesake (2026-08-07) ────────────────────────────────────────────────
# THE NAMESAKE CLASS. Three published markets described TWO cities at once:
# manchester was UK/NGESO in _MARKETS_HARDCODED but shipped as NH/ISONE at
# (42.97, -71.47); dublin shipped as OH/PJM while its facility list was
# entirely Irish (AWS EU-West-1, Meta Clonee, Equinix DB); vienna shipped as
# VA/PJM with a list that MIXED Ashburn and Wien (NTT VIE1, Arelion Wien Sud).
#
# MECHANISM — two independent holes, both needed for the page to look the way
# it did:
#   1. _load_markets_dynamic is US-ONLY by construction (its WHERE carries
#      `country = 'US' OR country = 'USA'`), and _build_markets_list lets a
#      dynamic row WIN every slug collision. Three US facilities in
#      Manchester NH clear its `HAVING COUNT(*) >= 3` bar, so a US namesake
#      quietly redefined a curated international market's state, ISO and
#      coordinates on every recompute. The curated tuple was never consulted.
#   2. The market-scoped facility queries had no country predicate at all, so
#      "the facilities in this market" meant "every facility on earth whose
#      city string matches", regardless of which country the market is in.
#
# Hole 1 is closed by _is_intl_market below; hole 2 by _market_country_scope.
# Neither is a per-market pin — johannesburg and markham were pinned as tuples
# in r-orphan-geography and the class still produced three more instances.
# ── r-jsonld-country (2026-08-08) ────────────────────────────────────────────
# The DCPI page's schema.org Dataset hardcoded addressCountry "US" for every
# market, so 61 non-US markets — Tokyo, Singapore, Frankfurt, São Paulo —
# asserted they were in the United States. JSON-LD is the channel AI engines
# lift VERBATIM into cited answers, so this is the highest-fidelity wrong claim
# on the site.
#
# The market's own `state` field cannot be trusted to name the country: for a
# non-US market it holds whatever the registry recorded, which is a country
# code for most (DE, MY, MX) but a SUBDIVISION for several — perth 'WA'
# (Western Australia), brisbane 'QL', the Canadian provinces, johannesburg 'GP'
# (Gauteng). 'GP' is itself a real ISO-3166 code, for GUADELOUPE, so a
# state-as-country fallback does not merely fail on Johannesburg, it confidently
# relocates it. The grid OPERATOR label is the reliable key, so it is used first.
_ISO_LABEL_COUNTRY = {
    "AEMO": "AU", "TPM": "NZ",
    "AESO": "CA", "BCH": "CA", "HQ": "CA", "IESO": "CA", "MH": "CA",
    "CENACE": "MX", "CLP": "HK", "EGAT": "TH", "EMA": "SG",
    "ENTSOE-AT": "AT", "ENTSOE-BE": "BE", "ENTSOE-CH": "CH",
    "ENTSOE-CZ": "CZ", "ENTSOE-DE": "DE", "ENTSOE-ES": "ES",
    "ENTSOE-FR": "FR", "ENTSOE-GR": "GR", "ENTSOE-IT": "IT",
    "ENTSOE-NL": "NL", "ENTSOE-PL": "PL", "ENTSOE-PT": "PT",
    "EVN": "VN", "EIRGRID": "IE", "KEPCO": "JP", "TEPCO": "JP",
    "KEPCO-KR": "KR", "NGCP": "PH", "PLN": "ID", "PLN-BATAM": "ID",
    "POSOCO": "IN", "TAIPOWER": "TW", "TNB": "MY",
    # ★ NGESO is Great Britain and the ISO-3166 alpha-2 for the United Kingdom
    # is GB, not UK. The registry records state='UK' for london/manchester/
    # edinburgh, which is NOT a country code — another reason the label decides.
    "NGESO": "GB",
    # NORDPOOL is deliberately ABSENT: it spans SE/DK/NO/FI, so no single
    # country follows from the label and the market's own code must decide.
}

# Markets whose grid operator is unregistered (iso ''), so the label lookup
# cannot fire. Kept explicit rather than inferred — johannesburg's 'GP' is the
# exact case a state fallback gets wrong.
_MARKET_COUNTRY_BY_SLUG = {"johannesburg": "ZA"}

# The only subdivision-free codes trusted as a last resort. Every entry is a
# country whose markets record their own ISO-3166 alpha-2 in `state` and whose
# operator label spans several countries (the Nord Pool members).
_STATE_AS_COUNTRY_OK = frozenset({"SE", "DK", "NO", "FI"})

# Non-standard country spellings the registry uses in `state`. These are the
# country, not a subdivision of it, so addressRegion must NOT repeat them —
# "addressCountry: GB, addressRegion: UK" reads as a region called UK.
_COUNTRY_ALIASES = {"UK": "GB", "USA": "US"}


def _dcpi_place(s, slug=None):
    """PURE. schema.org Place for one DCPI market's spatialCoverage.

    addressRegion is only emitted for a market whose `state` really is a
    subdivision of the country named — for a non-US market it may hold a
    country code (DE, MY), and "addressRegion: DE, addressCountry: DE" is
    noise at best. addressCountry is omitted entirely when unresolvable: a
    Place with no country is honest, a Place in the wrong country is not.
    """
    state = (s.get("state") or "").strip()
    iso = (s.get("iso") or "").strip()
    country = _market_country(state, iso, slug)
    place = {"@type": "Place",
             "name": _place_label(s.get("market_name"), state)}
    if country:
        place["addressCountry"] = country
        _st = state.upper()
        if state and _st != country.upper() and _COUNTRY_ALIASES.get(_st) != country.upper():
            place["addressRegion"] = state
    elif state:
        place["addressRegion"] = state
    return place


def _market_country(state, iso, slug=None):
    """ISO-3166 alpha-2 country for a DCPI market, or None when it cannot be
    determined. NEVER guesses: an unknown market yields None and the caller
    omits the country rather than asserting one.

    Resolution order — operator label, explicit slug, then the market's own
    code but only from a vetted set. See _ISO_LABEL_COUNTRY for why `state` is
    not trustworthy on its own.
    """
    _iso = (iso or "").strip().upper()
    _state = (state or "").strip().upper()
    if not _is_intl_market((None, None, _state, _iso)):
        # US market. The territories carry their own ISO-3166 code and are more
        # precisely themselves than "US" (schema.org accepts either).
        return _state if _state in _US_TERRITORY_CODES else "US"
    if _iso in _ISO_LABEL_COUNTRY:
        return _ISO_LABEL_COUNTRY[_iso]
    if slug and slug in _MARKET_COUNTRY_BY_SLUG:
        return _MARKET_COUNTRY_BY_SLUG[slug]
    if _state in _STATE_AS_COUNTRY_OK:
        return _state
    return None


def _is_intl_market(row) -> bool:
    """True when a (slug, name, state, iso, lat, lon) tuple describes a market
    OUTSIDE the United States.

    Order matters, and each rung exists because the rung below it is wrong for
    some real market:
      1. A US grid label settles it — PREPA/GPA/WAPA are in BOTH this set and
         _INTL_ISO_LABELS (r71 bundled the territories into the intl splice for
         merge purposes only), and they are US.
      2. A registered international grid label settles it — munich and berlin
         carry state='DE', which is also Delaware, and perth carries state='WA',
         which is also Washington. Their ENTSOE-DE / AEMO labels are what make
         them unambiguous. This is the same collision _normalize_us_isos guards.
      3. Otherwise fall back to the state code. johannesburg is the case that
         needs it: iso='' (midrand convention, no registered operator) with
         state='GP', which is not a US state.
    """
    if not (isinstance(row, tuple) and len(row) >= 4):
        return False
    iso = (row[3] or "").strip().upper()
    state = (row[2] or "").strip().upper()
    if iso in _US_DCPI_ISOS:
        return False
    if iso in _INTL_ISO_LABELS:
        return True
    return bool(state) and state not in _US_STATE_CODES


def _live_state_reads_allowed(state: str | None, iso: str | None) -> bool:
    """May this market read the US-only, STATE-keyed live tables?

    ★★★ THE BUG THIS CLOSES (measured live 2026-08-08, each one byte-identical
    to its US twin, which is the proof):

        Pune       state='IN'  ->  INDIANA's interconnection queue.
                   queue_capacity_mw 35,229.5 and gen_additions 1,666.5 were
                   IDENTICAL to Indianapolis's.
        Batam      state='ID'  ->  IDAHO's planned generation.
                   gen_additions_12mo_mw 400.0 was IDENTICAL to Boise's.
        Querétaro  state='MX'  ->  1,272.8 MW of CAISO-queue wind farms in
                   Tecate, Baja California — a WECC-synchronous grid ~1,900 km
                   away that interconnects to California, not to CENACE's SIN.
        Johor      state='MY'  ->  no US collision, so it correctly published
                   nulls and signal_tier='low'. Johor is the control case that
                   proves the mechanism.
        Also confirmed: perth WA->Washington, berlin/frankfurt/munich
        DE->Delaware, mumbai/chennai/bangalore/hyderabad IN->Indiana.

    `interconnect_queue` and `planned_generators` are US tables keyed on a
    2-letter USPS STATE code. A non-US market's `state` field holds a 2-letter
    ISO-3166 COUNTRY code. The two namespaces collide and the queries carry no
    country predicate, so `WHERE UPPER(state) = 'IN'` returned Indiana to an
    Indian market.

    ★ Why an allow-list of US state codes is NOT the fix: 'IN' and 'ID' ARE
    real US states, so the two worst cases sail straight through one. The
    predicate has to be about the MARKET's country, not the code's shape —
    which is exactly what _is_intl_market already decides, so this DELEGATES
    to it rather than introducing a second country test that could drift.

    ★ Why this is worse than the /dcpi/manchester namesake bug: that one
    mis-populated a display list. This one contaminates the SCORE — queue depth
    drives queue_wait_months (30% of the constraint weight) and gen additions
    carry 20% of the excess weight — and it inflated signal_tier from 'low' to
    'partial', so the wrong-country read made the market look BETTER evidenced
    than it was.

    The empty-state check is a precondition of the QUERY, not a country
    judgement: with nothing to key on, a state-keyed read is a no-op, so it is
    refused rather than issued.
    """
    if not (state or "").strip():
        return False
    return not _is_intl_market((None, None, state, iso))


# r-namesake-territory (2026-08-07): the US territories carry their OWN
# ISO-3166 code in discovered_facilities.country — 'PR', not 'US'. DCPI scores
# them US-style (state PR/GU/VI, ISO PREPA/GPA/WAPA), and the r71 comment on
# _INTL_MARKETS already warned that "discovered_facilities country='US' filter
# doesn't cover PR/GU/VI consistently". Measured: an IN ('US','USA') scope cut
# /dcpi/san-juan from 19 facilities to 2, dropping Claro Puerto Rico, Critical
# Hub and EdgeUno SJU1 — all genuinely in San Juan, all country='PR'.
_US_TERRITORY_CODES = frozenset({"PR", "GU", "VI", "AS", "MP"})

# How far from its own centre a market's footprint may plausibly reach, used
# ONLY to disprove an unknown-country row — never to make a claim. ~5 degrees
# of latitude is ~550 km, comfortably wider than any metro and still 2 orders
# of magnitude tighter than a continent.
_MARKET_RADIUS_DEG = 5.0


def _market_country_scope(iso, state, lat=None, lon=None):
    """(sql_fragment, params) restricting `discovered_facilities` rows to the
    country of ONE market. THE single definition of "this market's facilities"
    — every market-scoped facility query in this module ANDs it on, and
    tests/test_dcpi_market_country_scope.py fails the build if one doesn't.

    US markets: the row must claim the US, or the market's own territory code
    (san-juan accepts 'PR', guam 'GU', virgin-islands 'VI'). An UNKNOWN country
    (NULL or '') is still credited — a real chunk of the US fleet was ingested
    before the column existed, and DCPI's standing honesty rule is that a
    market is never penalised for absent data — but ONLY when nothing disproves
    it. A row whose own coordinates sit implausibly far from the market is
    disproved and dropped: that is the leak the old `country IS NULL OR
    country=''` clause left open for every 2-letter subdivision code the US
    shares with somewhere else (WA is also Western Australia, ON Ontario, GA
    Gauteng, SA South Australia).

    The disproof is MARKET-RELATIVE, not a North America bounding box. The box
    was the first cut of this and it was wrong twice over: Guam sits at 144.79E,
    outside any box that also contains the mainland, and a box that held both
    would span half the planet and disprove nothing. Distance from the market
    is what the check actually means.

    Rows with no coordinates, and markets with no coordinates, skip the
    distance test entirely — fail-open, exactly as before this function
    existed, so neither can shrink a legitimate footprint.

    International markets: the row must NOT claim the US. City-level pooling is
    deliberate and predates this function — intl `state` spellings in
    discovered_facilities are free-text ('QLD'/'WAS'/'Maharashtra'/'') and are
    not a filterable grain. Excluding US rows is the honest half we do have,
    and it is exactly what keeps Manchester NH out of Manchester UK.
    """
    if _is_intl_market((None, None, state, iso)):
        return (" AND UPPER(COALESCE(country, '')) NOT IN ('US', 'USA')", [])

    _st = (state or "").strip().upper()
    _ok = ["US", "USA"] + ([_st] if _st in _US_TERRITORY_CODES else [])
    _in = ", ".join(["%s"] * len(_ok))

    try:
        _lat, _lon = float(lat), float(lon)
    except (TypeError, ValueError):
        _lat = _lon = None
    if _lat is None or (not _lat and not _lon):     # (0,0) = coords unknown
        # No market centre to measure against — credit unknown country, as the
        # pre-r-namesake code did unconditionally.
        return (f" AND (UPPER(COALESCE(country, '')) IN ({_in})"
                f"      OR COALESCE(country, '') = '')", list(_ok))

    import math
    _d = _MARKET_RADIUS_DEG
    _dlon = _d / max(0.2, abs(math.cos(math.radians(_lat))))
    return (
        f" AND (UPPER(COALESCE(country, '')) IN ({_in})"
        "      OR (COALESCE(country, '') = ''"
        "          AND (latitude IS NULL OR longitude IS NULL"
        "               OR (latitude BETWEEN %s AND %s"
        "                   AND longitude BETWEEN %s AND %s))))",
        list(_ok) + [_lat - _d, _lat + _d, _lon - _dlon, _lon + _dlon],
    )


def _load_scored_orphans(known_slugs):
    """r-dcpi-orphan (2026-07-17): re-adopt markets that were ALREADY scored but
    have fallen out of the recompute universe.

    THE BUG THIS CLOSES (measured 2026-07-17): market_power_scores held 316 rows
    while MARKETS covered 277 -> 41 ORPHANS frozen at 2026-07-03, still served
    publicly by /dcpi and /dcpi/history for 14 days. They were INVISIBLE: every
    dcpi-daily.yml chunk reported errors:0 and every run was green, because an
    orphan is not FAILING -- it is simply never visited. The only signal was
    SUM(markets_scored)=277 != 316 rows (and /api/v1/dcpi/freshness stale_7d=41).
    LESSON: errors:0 on a chunked job proves nothing about COVERAGE.

    The universe can shrink for several independent reasons: _load_markets_dynamic
    caps at LIMIT 200 ORDER BY facility_count DESC, so a market that slips past
    rank 200 drops out (the is_duplicate fleet filter reshuffling facility counts
    is the likely 07-03 trigger); the HAVING COUNT(*) >= 3 threshold can drop one;
    a canonical-slug change or a removal from _MARKETS_HARDCODED does the same.
    Rather than chase each trigger, make orphaning STRUCTURALLY IMPOSSIBLE: once a
    market has been scored it stays in the universe and keeps refreshing.

    Fail-soft: returns [] on any error, so a DB blip can never shrink MARKETS.
    Shape matches _MARKETS_HARDCODED: (slug, name, state, iso, lat, lon).
    """
    import os, psycopg2
    try:
        url = os.environ.get("DATABASE_URL")
        if not url:
            return []
        conn = psycopg2.connect(url, connect_timeout=8)
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT market_slug, market_name, state, iso, latitude, longitude
                      FROM market_power_scores
                     WHERE market_slug IS NOT NULL AND market_slug <> ''
                """)
                rows = cur.fetchall() or []
        finally:
            try:
                conn.close()
            except Exception:
                pass
        out = []
        for r in rows:
            slug = r[0]
            if not slug or slug in known_slugs:
                continue
            out.append((slug, r[1] or slug, r[2], r[3], r[4], r[5]))
        if out:
            print(f"[dcpi] r-dcpi-orphan: re-adopted {len(out)} already-scored "
                  f"market(s) missing from MARKETS", flush=True)
        return out
    except Exception as e:
        print(f"[dcpi] r-dcpi-orphan: orphan scan failed (non-fatal): {e}", flush=True)
        return []


def _dedup_market_twins(markets):
    """r-twin-dedup (2026-07-19): collapse the same market defined under two
    slugs (e.g. 'cheyenne' + 'cheyenne-wy', 'washington' + 'dc') so the recompute
    scores ONE canonical row per market. Without this, the exact-slug merge kept
    both and the orphan re-adopter re-published the twin forever, duplicating it
    in every ranked list. Any redundant slug that is a key of DCPI_METRO_ALIASES
    (canonical picks reference-informed there) is dropped from the scoring
    universe; its already-scored row is unpublished separately, and the alias +
    the no-published-filter single-market lookup keep direct links resolving.
    Order-preserving; only drops entries whose slug is a known alias-key AND
    whose canonical target is also present in the list (never orphans a market).
    NEVER raises."""
    try:
        canon_present = set()
        for m in markets:
            s = m[0] if isinstance(m, tuple) else (m.get("slug") if hasattr(m, "get") else None)
            if s:
                canon_present.add(s)
        out = []
        dropped = []
        for m in markets:
            s = m[0] if isinstance(m, tuple) else (m.get("slug") if hasattr(m, "get") else None)
            target = DCPI_METRO_ALIASES.get((s or "").lower())
            if s and target and target in canon_present:
                dropped.append(s)          # a twin whose canonical is present
                continue
            out.append(m)
        if dropped:
            print(f"[dcpi] r-twin-dedup: dropped {len(dropped)} alias-twin slugs "
                  f"from the scoring universe: {dropped}", flush=True)
        return out
    except Exception as e:
        print(f"[dcpi] r-twin-dedup: skipped (non-fatal): {e}", flush=True)
        return markets


def _normalize_us_isos(markets):
    """r-iso-taxonomy (2026-07-28): re-resolve `iso` on US markets through
    util/iso_taxonomy so the hardcoded rows and the dynamic rows can't
    disagree about the same grid.

    Needed because the two sources drifted apart: _MARKETS_HARDCODED said
    kansas-city was SPP (right) while the dynamic loader's state map said
    MISO (wrong) — and dynamic wins on slug collisions, so the wrong value
    is what shipped. Normalising both through one resolver removes the
    class of bug rather than the instance.

    GUARD — gate on the CURRENT label, not the state code. Intl markets
    reuse two-letter codes that collide with US states: Munich and Berlin
    carry state='DE', which is also Delaware. Keying off `state` alone
    would rewrite Munich from ENTSOE-DE to PJM. A row is only eligible if
    its existing iso is already a US-grid label (or empty), which no intl
    market ever satisfies.

    Order-preserving. NEVER raises — a market list is worth more than a
    perfectly-normalised one.
    """
    try:
        from util.iso_taxonomy import resolve_iso
        out, changed = [], []
        for m in markets:
            if not (isinstance(m, tuple) and len(m) >= 4):
                out.append(m)
                continue
            slug, name, state, iso = m[0], m[1], m[2], m[3]
            cur = (iso or "").upper().strip()
            if cur and cur not in _US_DCPI_ISOS:
                out.append(m)          # international — never touch
                continue
            new_iso = resolve_iso(slug, state, default=iso)
            if new_iso and new_iso != iso:
                changed.append(f"{slug}({state}) {iso}->{new_iso}")
                m = (slug, name, state, new_iso) + tuple(m[4:])
            out.append(m)
        if changed:
            print(f"[dcpi] r-iso-taxonomy: corrected {len(changed)} market "
                  f"ISO labels: {changed}", flush=True)
        return out
    except Exception as e:
        print(f"[dcpi] r-iso-taxonomy: normalization skipped "
              f"(non-fatal): {e}", flush=True)
        return markets


def _build_markets_list():
    """r57: always-includes-intl market list builder. Tries the dynamic
    US loader, then unions on the international set. Falls back to
    pure hardcoded if dynamic fails.

    r71 (2026-06-06): expanded to union the FULL `_MARKETS_HARDCODED`
    set (not just `_INTL_MARKETS`) so the US tier-2/3 + territory rows
    we added at the bottom of the hardcoded list also survive the
    merge. The dynamic loader's slugs still win on collisions, so this
    only adds markets that the discovered_facilities `>=3 facilities`
    threshold misses (Boise, Tulsa, Bismarck, DC, San Juan, etc.)."""
    dyn = _load_markets_dynamic()
    if dyn:
        # r-namesake (2026-08-07): a dynamic row may NOT redefine an
        # international market. _load_markets_dynamic only ever emits US rows
        # (its WHERE is `country = 'US' OR country = 'USA'`), so a dynamic
        # collision with a curated intl slug is by definition a different city
        # that happens to share a name — never richer data about the same one.
        # Letting it win is how manchester UK/NGESO shipped as Manchester NH
        # ISONE, dublin IE as Dublin OH, and vienna AT as Vienna VA.
        # US-vs-US collisions still go to the dynamic row: there it really is
        # the same market with live coords + the r-iso-taxonomy resolution.
        _intl_hardcoded = {m[0]: m for m in _MARKETS_HARDCODED
                           if isinstance(m, tuple) and len(m) >= 4
                           and _is_intl_market(m)}
        _hijacked = sorted({(m[0] if isinstance(m, tuple) else m.get("slug"))
                            for m in dyn} & set(_intl_hardcoded))
        if _hijacked:
            dyn = [m for m in dyn
                   if (m[0] if isinstance(m, tuple) else m.get("slug"))
                   not in _intl_hardcoded]
            print(f"[dcpi] r-namesake: dropped {len(_hijacked)} US namesake "
                  f"row(s) shadowing an international market: {_hijacked}",
                  flush=True)
        # Avoid dupes by slug (dynamic loader could pick up a slug that
        # collides with the hardcoded set).
        dyn_slugs = {m[0] if isinstance(m, tuple) else m.get("slug")
                      for m in dyn}
        # Union ALL hardcoded markets (intl + new US tier-2 + territories),
        # not just the strict intl filter. Dynamic-loader rows still win
        # on slug collisions — they have richer fields and live data.
        merged = list(dyn) + [m for m in _MARKETS_HARDCODED
                                if isinstance(m, tuple)
                                and len(m) >= 4
                                and m[0] not in dyn_slugs]
        # r-dcpi-orphan (2026-07-17): a market that was EVER scored must never
        # silently drop out of the recompute universe. See _load_scored_orphans.
        _merged_slugs = {m[0] if isinstance(m, tuple) else m.get("slug")
                         for m in merged}
        merged += _load_scored_orphans(_merged_slugs)
        # r-twin-dedup (2026-07-19): applied AFTER orphans so the orphan
        # re-adopter can't smuggle a redundant twin back into the universe.
        merged = _dedup_market_twins(merged)
        # r-iso-taxonomy (2026-07-28): last, so orphan re-adopted rows and
        # hardcoded rows are normalised too — not just the dynamic ones.
        return _normalize_us_isos(merged)
    return _normalize_us_isos(_dedup_market_twins(_MARKETS_HARDCODED))


MARKETS = _build_markets_list()



# ---------------------------------------------------------------------------
# Scoring formulas
# ---------------------------------------------------------------------------
def _clip(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


# ---------------------------------------------------------------------------
# r-local-granularity (2026-07-25): market-LOCAL infrastructure terms.
#
# WHY: every scorer input is ISO- or STATE-level (queue depth, reserve margin,
# curtailment...), so markets sharing a state carried byte-identical
# (constraint, excess) pairs — allen/fort-worth/irving/abilene all 53.4/57.8,
# and the intl default row made barueri/bologna/midrand (three continents!)
# identical at 41.1/44.8. 101/317 markets shared a pair on 07-24 (integrity
# shell #25 dc_pairs check). These terms read the market's OWN infrastructure
# within a radius of its coordinates:
#   substations        (HIFLD ~127k, US-only)  -> grid ACCESS within 40 km
#   gem_power operating (GEM ~128k, GLOBAL)    -> deliverable generation, 60 km
#   discovered_facilities canonical fleet      -> DC competition within 25 km
#
# ADDITIVE-ONLY + BOUNDED (<= +8 excess, <= +6 constraint) + FAIL-SOFT: no
# coords / no rows / DB error -> zero adjustment. A market must never be
# PENALIZED for absent data (HIFLD is US-only) — the same honesty rule as
# local_saturation's "no_local_facility_footprint" label. Deterministic: same
# rows in, same score out; no noise. Verdict thresholds are untouched — this
# breaks input DUPLICATION (the integrity lane's diagnosis), it does not
# relabel identical inputs.
#
# ── r-radius-dedup (2026-08-08): local_dc_count keyed the WRONG duplicate ────
# The 25 km DC-competition COUNT below scoped duplicate visibility on
# `COALESCE(is_duplicate, 0) = 0`. Visibility in this repo is `duplicate_of_id`
# ALONE (routes/facility_profile_page.py keys the canonical on the pointer;
# routes/facilities_by_dims.py, routes/d1_sync.py and the market facility list
# at ~line 7819 all scope on it). Measured on the live table 2026-08-08 over
# 24,859 discovered_facilities rows, the flag key was wrong in BOTH directions:
#   * 3,286 rows carry a duplicate_of_id while staying UNflagged — every one of
#     those was counted a SECOND time, as competition against itself.
#   * 1,510 rows are flagged with NO pointer. That is a keeperless suppression
#     (see repair_dedup_keeper_election.py), not a twin — a real facility that
#     was being dropped from the count entirely.
# Net effect was inflation: of 316 markets, 253 counted a different number of
# facilities under the two keys. Replaying the real scorer over the whole
# universe with ONLY this predicate toggled moved constraint_score for 147
# markets — 132 down, 15 up (those are the flagged-but-unpointed rows coming
# back: spokane 4 -> 7 facilities, +0.5 points) — mean |delta| 0.47, max 1.5.
# ZERO verdicts flipped. 106 of the changed markets already sat at or above the
# 40-facility ceiling on BOTH keys, where the term saturates and inflation is
# invisible; the defect lives below the ceiling, which is where a 25 km radius
# puts most markets.
#
# ★ Why this was not caught by r-list-dedup (#2386, e7af3252), which fixed the
# same bug class in this file hours earlier: that PR's test only asserts over
# queries that are market-NAME-scoped AND row-rendering. This is a RADIUS
# aggregate, so it matched neither gate and was explicitly scoped out. A bbox
# COUNT is a third shape and needed its own test —
# tests/test_dcpi_local_dc_count_dedup.py.
#
# The published methodology string in util/dcpi_method.py (LOCAL_INFRA_TERMS,
# served at /api/v1/dcpi/methodology) named the old predicate verbatim, so it
# moved in the same commit. Score movement is recorded in REVISIONS there, per
# that module's published versioning rule.
#
# The predicate comes from _SQL_FOOTPRINT_DEDUP (r-sat-dedup, #2403) rather than
# being spelled again here — that constant's own comment asks for "one name, two
# call sites, no third definition", and this is the third call site. Note the
# clause ORDER changed to put the interpolated fragment last: the constant is an
# `AND ...` fragment, so it cannot sit where the old predicate did (first, right
# after WHERE). Same rows either way; measured identical counts before and after
# the restructure.
# ---------------------------------------------------------------------------
_LOCAL_INFRA_CACHE: dict = {}


def _local_infra_metrics(lat, lon) -> dict:
    """Bounded bbox counts of the market's own grid/generation/DC footprint.
    Returns zero-dict on ANY failure — callers can always .get() safely."""
    out = {"local_substation_count": 0, "local_max_kv": 0.0,
           "local_gen_mw": 0.0, "local_dc_count": 0}
    try:
        latf, lonf = float(lat), float(lon)
    except (TypeError, ValueError):
        return dict(out)
    if not latf and not lonf:          # (0, 0) = coords unknown
        return dict(out)
    key = (round(latf, 3), round(lonf, 3))
    hit = _LOCAL_INFRA_CACHE.get(key)
    if hit is not None:
        return dict(hit)
    import math
    coslat = max(0.2, abs(math.cos(math.radians(latf))))
    d_sub, d_gen, d_dc = 40.0 / 111.0, 60.0 / 111.0, 25.0 / 111.0
    try:
        with _conn() as c, c.cursor() as cur:
            # SET LOCAL inside the implicit tx — a plain SET does not stick
            # on Neon's pooled endpoint.
            cur.execute("SET LOCAL statement_timeout = 3500")
            cur.execute(
                f"""SELECT
                     (SELECT COUNT(*) FROM substations
                       WHERE lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s),
                     (SELECT COALESCE(MAX(voltage_kv), 0) FROM substations
                       WHERE lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s),
                     (SELECT COALESCE(SUM(capacity_mw), 0) FROM gem_power
                       WHERE status = 'operating'
                         AND lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s),
                     -- r-radius-dedup: the POINTER, never the flag — from the
                     -- SAME constant the saturation footprint uses, so this
                     -- file holds ONE definition of the rule, not three.
                     (SELECT COUNT(*) FROM discovered_facilities
                       WHERE latitude  BETWEEN %s AND %s
                         AND longitude BETWEEN %s AND %s
                         {_SQL_FOOTPRINT_DEDUP})""",
                (latf - d_sub, latf + d_sub,
                 lonf - d_sub / coslat, lonf + d_sub / coslat,
                 latf - d_sub, latf + d_sub,
                 lonf - d_sub / coslat, lonf + d_sub / coslat,
                 latf - d_gen, latf + d_gen,
                 lonf - d_gen / coslat, lonf + d_gen / coslat,
                 latf - d_dc, latf + d_dc,
                 lonf - d_dc / coslat, lonf + d_dc / coslat))
            row = cur.fetchone() or (0, 0, 0, 0)
        out = {"local_substation_count": int(row[0] or 0),
               "local_max_kv": round(float(row[1] or 0), 1),
               "local_gen_mw": round(float(row[2] or 0), 1),
               "local_dc_count": int(row[3] or 0)}
    except Exception as _e:
        print(f"[dcpi] local-infra lookup failed (neutral 0s): {_e}", flush=True)
        return dict(out)
    _LOCAL_INFRA_CACHE[key] = dict(out)
    return dict(out)


# ── r-ws3-methodology (2026-07-29) ──────────────────────────────────────
# Every weight, ceiling, threshold and scoring-time default below now comes
# from util/dcpi_method.py, which is ALSO what /api/v1/dcpi/methodology emits.
# One object, two consumers — so a published weight and a scoring weight
# cannot silently disagree. They did: the static /dcpi/methodology page
# published a five-term excess formula whose terms exist nowhere in this
# repo, and a NEUTRAL verdict band derive_verdict cannot emit.
#
# This is an extract-to-constant refactor ONLY. The arithmetic, the operand
# order and the float literals are unchanged, so every score and verdict is
# byte-identical — pinned by tests/test_dcpi_methodology.py, which reproduces
# real published rows from the constants.
#
# Module-level import (not function-local) on purpose: util/dcpi_method has no
# dependencies beyond the stdlib, and the same unguarded style is already used
# for util.market_aliases and util.iso_taxonomy above. A lazy import inside the
# scorer would run on every market of every recompute.
from util.dcpi_method import (                       # noqa: E402
    DCPI_METHOD_VERSION,
    CONSTRAINT_INPUT_DEFAULTS as _C_DEF,
    CONSTRAINT_CEILINGS as _C_CEIL,
    CONSTRAINT_WEIGHTS as _C_W,
    CONSTRAINT_EMERGENCY_POINTS_PER_EVENT as _C_EMERG_PTS,
    CONSTRAINT_LOCAL_COMPETITION_BONUS as _C_LOCAL_BONUS,
    EXCESS_INPUT_DEFAULTS as _E_DEF,
    EXCESS_CEILINGS as _E_CEIL,
    EXCESS_WEIGHTS as _E_W,
    EXCESS_RESERVE_FLOOR_PCT as _E_RES_FLOOR,
    EXCESS_RESERVE_SPAN_PCT as _E_RES_SPAN,
    EXCESS_LOCAL_GRID_BONUS as _E_LOCAL_BONUS,
    LOCAL_GRID_SUBSTATION_CEILING as _LG_SUB_CEIL,
    LOCAL_GRID_SUBSTATION_POINTS as _LG_SUB_PTS,
    LOCAL_GRID_KV_POINTS as _LG_KV_PTS,
    LOCAL_GRID_GEN_CEILING as _LG_GEN_CEIL,
    LOCAL_GRID_GEN_POINTS as _LG_GEN_PTS,
    VERDICT_BANDS as _V_BANDS,
    VERDICT_FALLBACK as _V_FALLBACK,
    COMPOSITE_WEIGHTS as _CO_W,
    COMPOSITE_TTP_CAP_MONTHS as _CO_TTP_CAP,
    COMPOSITE_VERDICT_MULTIPLIERS as _CO_MULT,
    COMPOSITE_DEFAULT_MULTIPLIER as _CO_MULT_DEFAULT,
    SIGNAL_TIER as _METHOD_SIGNAL_TIER,
)

# r-provenance-writer (2026-08-08): the one definition of "write a scored
# market row". Shared with routes/dcpi_freshness_watchdog.py — see that
# module's docstring for why three hand-copies existed and what they dropped.
from util.dcpi_score_row import (                     # noqa: E402
    upsert_scored_market,
    LITE_MAY_NOT_CLOBBER_FULL,
)

# r-ws3-methodology (2026-07-29): ONE definition of the signal-tier basis
# string. It was hand-copied into four readers, and all four copies published
# the same FALSE claim: that an unrecorded tier may mean the row "was written
# by the lite recompute path". POST /api/v1/dcpi/lite-recompute iterates
# MARKETS, which holds tuples, and raises AttributeError on every market inside
# its own swallow-all `except` — it has written ZERO rows. A confidently wrong
# reason is worse than no reason, so name only what is true.
_SIGNAL_TIER_BASIS_RECORDED = "live_adapter_count_at_score_time"
_SIGNAL_TIER_BASIS_UNRECORDED = (
    "unrecorded: this row predates signal tiering, or was last written before "
    "the current recompute — tier unknown, NOT low")


def _signal_tier_basis(tier) -> str:
    """Why this row's signal_tier reads the way it does. NEVER implies 'low'."""
    return (_SIGNAL_TIER_BASIS_RECORDED if tier
            else _SIGNAL_TIER_BASIS_UNRECORDED)


def _attach_method_version(out: dict, row) -> dict:
    """Publish method_version ONLY when the row actually carries the column.

    An endpoint whose SELECT never asked for method_version would otherwise
    emit `null`, which a reader would read as "no version was recorded" when
    the truth is "this endpoint did not look". Absent means absent; null means
    the writer recorded none. Those are different claims, so they get
    different representations.
    """
    try:
        if isinstance(row, dict) and "method_version" in row:
            out["method_version"] = row.get("method_version") or None
            out["method_doc"] = "/api/v1/dcpi/methodology"
    except Exception:
        pass
    return out


def compute_constraint_score(metrics: dict) -> float:
    """High score = MORE constrained (avoid). 0..100.

    Weights/ceilings: util.dcpi_method. Published: /api/v1/dcpi/methodology.
    """
    queue_wait_m = float(metrics.get("queue_wait_months") or _C_DEF["queue_wait_months"])
    reserve_pct  = float(metrics.get("reserve_margin_pct") or _C_DEF["reserve_margin_pct"])
    emergencies  = int(metrics.get("emergency_count_30d") or _C_DEF["emergency_count_30d"])
    demand_yoy   = float(metrics.get("demand_growth_yoy_pct") or _C_DEF["demand_growth_yoy_pct"])

    # Wait > 36 months is critical
    s_wait = _clip((queue_wait_m / _C_CEIL["queue_wait_months"]) * 100, 0, 100)
    # Reserve < 13% is critical (NERC standard)
    s_reserve = _clip((1 - (reserve_pct / _C_CEIL["reserve_margin_pct"])) * 100, 0, 100)
    # NOTE: emergency_count_30d is NEVER assigned anywhere in this module, so
    # this term is a structural zero for every market at every signal tier —
    # i.e. 20% of every constraint score. Published as a known limitation
    # rather than quietly carried.
    s_emerg = _clip(emergencies * _C_EMERG_PTS, 0, 100)
    s_demand = _clip((demand_yoy / _C_CEIL["demand_growth_yoy_pct"]) * 100, 0, 100)

    base = (_C_W["queue_wait"]*s_wait + _C_W["reserve_margin"]*s_reserve
            + _C_W["emergencies"]*s_emerg + _C_W["demand_growth"]*s_demand)
    # r-local-granularity: local DC density competes for the same feeders and
    # queue positions — a bounded (<= +6) bump. Zero when the key is absent,
    # so callers that never gathered local terms score byte-identically.
    s_local_comp = _clip((float(metrics.get("local_dc_count") or 0) / _C_CEIL["local_dc_count"]) * 100, 0, 100)
    return round(_clip(base + _C_LOCAL_BONUS * s_local_comp, 0, 100) or 0, 1)


def compute_excess_power_score(metrics: dict) -> float:
    """High score = MORE excess available (build here). 0..100.

    The contrarian metric — what nobody else publishes.
    """
    reserve_pct       = float(metrics.get("reserve_margin_pct") or _E_DEF["reserve_margin_pct"])
    gen_additions_mw  = float(metrics.get("gen_additions_12mo_mw") or _E_DEF["gen_additions_12mo_mw"])
    curtailment_pct   = float(metrics.get("curtailment_pct") or _E_DEF["curtailment_pct"])
    queue_approval    = float(metrics.get("queue_approval_rate_pct") or _E_DEF["queue_approval_rate_pct"])
    stranded_mw       = float(metrics.get("stranded_capacity_mw") or _E_DEF["stranded_capacity_mw"])
    btm_headroom_mw   = float(metrics.get("btm_headroom_mw") or _E_DEF["btm_headroom_mw"])

    # Reserve above the floor counts as a bonus, spread over the span
    s_reserve  = _clip(((reserve_pct - _E_RES_FLOOR) / _E_RES_SPAN) * 100, 0, 100)
    # 5000+ MW additions in 12mo = 100
    s_additions = _clip((gen_additions_mw / _E_CEIL["gen_additions_12mo_mw"]) * 100, 0, 100)
    # 10%+ curtailment = a LOT of wasted power
    s_curtail  = _clip((curtailment_pct / _E_CEIL["curtailment_pct"]) * 100, 0, 100)
    s_approval = _clip(queue_approval, 0, 100)
    # 1000+ MW of stranded capacity = max signal
    s_strand   = _clip((stranded_mw / _E_CEIL["stranded_capacity_mw"]) * 100, 0, 100)
    s_btm      = _clip((btm_headroom_mw / _E_CEIL["btm_headroom_mw"]) * 100, 0, 100)

    base = (_E_W["reserve_margin"]*s_reserve + _E_W["gen_additions"]*s_additions
            + _E_W["curtailment"]*s_curtail + _E_W["queue_approval"]*s_approval
            + _E_W["stranded"]*s_strand + _E_W["btm_headroom"]*s_btm)
    # r-local-granularity: the market's OWN grid access — substation density
    # (40 km), HV class, and deliverable local generation (60 km, GLOBAL
    # gem_power so intl markets differentiate too). Bounded <= +8, additive
    # only, zero when keys are absent or no rows exist.
    _subs = float(metrics.get("local_substation_count") or 0)
    _kv = float(metrics.get("local_max_kv") or 0)
    _gen = float(metrics.get("local_gen_mw") or 0)
    # HV class points: first threshold met wins (345kV then 230kV), 0 below.
    _kv_pts = 0.0
    for _kv_threshold, _kv_award in _LG_KV_PTS:
        if _kv >= _kv_threshold:
            _kv_pts = _kv_award
            break
    s_local_grid = _clip((_subs / _LG_SUB_CEIL) * _LG_SUB_PTS
                         + _kv_pts
                         + (_gen / _LG_GEN_CEIL) * _LG_GEN_PTS, 0, 100)
    return round(_clip(base + _E_LOCAL_BONUS * s_local_grid, 0, 100), 1)


def derive_verdict(constraint: float, excess: float) -> str:
    """BUILD / CAUTION / AVOID. Bands: util.dcpi_method.VERDICT_BANDS.

    There is deliberately NO 'NEUTRAL' band. The static /dcpi/methodology page
    published one for months; it was never reachable from this function, which
    is why 67% of published markets carried a verdict that page could not
    produce.
    """
    for _label, _band in _V_BANDS:
        if excess >= _band["excess_min"] and constraint <= _band["constraint_max"]:
            return _label
    return _V_FALLBACK


def derive_composite_score(excess, constraint, ttp_months, verdict=None):
    """Single-number 0-100 ranking score derived from the three published
    DCPI components, with a verdict-aware quality multiplier.

    r41-dcpi-composite (2026-05-25): added so AI agents calling
    /api/v1/dcpi/scores can sort markets by a single sortable value
    without having to recombine the components themselves. The verdict
    field (BUILD/CAUTION/AVOID/LOW_SIGNAL) gives the headline, the
    composite gives the rank.

    r41.1 (2026-05-25): added verdict multiplier. Without it, a market
    with missing data showing as constraint=0/ttp=0 (i.e. LOW_SIGNAL)
    scored 80.8 — above legitimate BUILD markets at 73.1, because the
    formula couldn't distinguish 'no constraints' from 'no data'. The
    verdict layer already knows which markets are trustworthy, so we
    use it as the quality gate:

      BUILD:      1.00 (full weight — trusted, actionable)
      CAUTION:    0.85 (slight discount — trusted but bordered)
      AVOID:      0.60 (penalty — known issues)
      LOW_SIGNAL: 0.35 (heavy penalty — data integrity unknown)
      else/null:  1.00 (no verdict yet — neutral, trust components)

    Weights match the verdict thresholds:
      excess_power_score:   60% (primary signal in derive_verdict)
      (100 - constraint):   30% (lower constraint = better)
      time-to-power factor: 10% (capped at 60 months)
    """
    # r-ws3-methodology: weights + multipliers now come from util.dcpi_method,
    # the same object /api/v1/dcpi/methodology publishes. routes/dcpi_explain.py
    # still HAND-COPIES this multiplier table under a comment reading "If that
    # changes, update here too" — that copy should import from here as well.
    e = float(excess or 0)
    c = float(constraint or 0)
    t = min(float(ttp_months or 0), _CO_TTP_CAP)
    raw = ((e * _CO_W["excess"])
           + ((100 - c) * _CO_W["inverse_constraint"])
           + ((1 - t / _CO_TTP_CAP) * 100 * _CO_W["time_to_power"]))
    multiplier = _CO_MULT.get((verdict or '').upper(), _CO_MULT_DEFAULT)
    composite = raw * multiplier
    return round(max(0.0, min(100.0, composite)), 1)


# ─── Phase SS DCPI v2 components ───────────────────────────────────
# Two additional 0..100 scores that complement the v1 excess/constraint
# duo. Computed on demand (no schema change) so v1 consumers keep
# working unchanged; v2 consumers opt in via /api/v1/dcpi/scores/<slug>/v2
# and the `recommend_market` MCP tool surfaces them in risk_flags.

def compute_water_risk_score(metrics: dict) -> float:
    """High = more water stress = worse for cooling-heavy DC builds. 0..100.

    Inputs (any may be missing — degrades to neutral 50):
        water_stress_index    1..5 USGS scale  (5 = extreme)
        drought_pct           0..100, % of state area in drought
        cooling_water_avail   m³/day available for industrial use (optional)
    """
    stress  = metrics.get("water_stress_index")
    drought = metrics.get("drought_pct")
    avail   = metrics.get("cooling_water_avail")

    parts, weights = [], []
    if stress is not None:
        # USGS 1..5 → 0..100 (1=>0, 5=>100)
        parts.append(_clip(((float(stress) - 1) / 4.0) * 100, 0, 100))
        weights.append(0.55)
    if drought is not None:
        parts.append(_clip(float(drought), 0, 100))
        weights.append(0.30)
    if avail is not None:
        # 100k m³/day = no penalty; <10k m³/day = max penalty.
        a = float(avail)
        scarcity = 1.0 - _clip(a / 100_000.0, 0, 1)
        parts.append(_clip(scarcity * 100, 0, 100))
        weights.append(0.15)

    if not parts:
        return 50.0   # neutral — no signal
    total_w = sum(weights)
    return round(sum(p * w for p, w in zip(parts, weights)) / total_w, 1)


def compute_renewable_arbitrage_score(metrics: dict) -> float:
    """High = bigger arbitrage opportunity (curtailed clean MWh + low PPA).
    0..100. Surfaces markets where excess renewable supply is being wasted
    *and* a buyer can capture it cheaply.

    Inputs (any may be missing — degrades to neutral 50):
        curtailment_pct           % of renewable gen curtailed last 12mo
        ppa_rate_cents_kwh        latest signed PPA price ¢/kWh
        rps_target_pct            state RPS goal (0..100)
        renewable_share_pct       current renewable share of state gen
    """
    curt    = metrics.get("curtailment_pct")
    ppa     = metrics.get("ppa_rate_cents_kwh")
    rps     = metrics.get("rps_target_pct")
    share   = metrics.get("renewable_share_pct")

    parts, weights = [], []
    if curt is not None:
        # 10%+ curtailment = max arbitrage opportunity
        parts.append(_clip((float(curt) / 10.0) * 100, 0, 100))
        weights.append(0.40)
    if ppa is not None:
        # 3¢/kWh = max opportunity, 8¢/kWh = none
        ppa_f = float(ppa)
        parts.append(_clip(((8.0 - ppa_f) / 5.0) * 100, 0, 100))
        weights.append(0.30)
    if rps is not None and share is not None:
        # Compliance gap — RPS target minus current share — drives demand
        gap = max(0.0, float(rps) - float(share))
        parts.append(_clip((gap / 50.0) * 100, 0, 100))
        weights.append(0.30)
    elif rps is not None:
        parts.append(_clip((float(rps) / 100.0) * 100, 0, 100))
        weights.append(0.30)

    if not parts:
        return 50.0
    total_w = sum(weights)
    return round(sum(p * w for p, w in zip(parts, weights)) / total_w, 1)


def derive_verdict_v2(constraint: float, excess: float,
                       water_risk: float, renewable_arb: float) -> str:
    """v2 verdict adds water + arbitrage as tiebreakers, but stays inside
    the v1 BUILD/CAUTION/AVOID alphabet so downstream consumers don't need
    to learn a new vocabulary."""
    v1 = derive_verdict(constraint, excess)
    # A BUILD market with extreme water risk drops to CAUTION
    if v1 == "BUILD" and water_risk >= 80:
        return "CAUTION"
    # An AVOID market with strong arbitrage + acceptable water becomes CAUTION
    if v1 == "AVOID" and renewable_arb >= 75 and water_risk <= 50:
        return "CAUTION"
    return v1


def estimate_time_to_power(metrics: dict) -> float:
    """Months. Uses queue median wait + capacity headroom adjustment."""
    queue_wait = float(metrics.get("queue_wait_months") or 24)
    headroom = float(metrics.get("reserve_margin_pct") or 12)
    # If reserve is plentiful, projects fast-track via fast-track pathways
    adj = 1.0
    if headroom >= 20: adj = 0.6
    elif headroom >= 16: adj = 0.8
    elif headroom < 10: adj = 1.4
    return round((queue_wait * adj) or 0, 1)


# ---------------------------------------------------------------------------
# Data ingest — pulls from existing tables; fills gaps with conservative
# defaults so the index always renders something. Real values land as our
# extractors enrich them.
# ---------------------------------------------------------------------------
# ── r-basis-source (2026-08-11): the MODELED provenance string, per ISO ──
# It used to be ONE hard-coded literal published for all 323 markets:
#     "2024 ENTSO-E/AEMO/EirGrid grid reports + published market conditions"
# So Dallas (ERCOT) and Atlanta (SOCO/SERC) both told a reader their modeled
# inputs came from ENTSO-E, AEMO and EirGrid — the European, Australian and
# Irish operators. That is the single field an analyst reads to answer "where
# does this come from", and for every US market it named the wrong continent.
#
# Values are the operator whose PUBLISHED disclosures the per-ISO defaults were
# calibrated against. `None` means DELIBERATELY UNNAMED — the key is ambiguous
# (e.g. KEPCO is used by more than one utility) and naming a specific operator
# would repeat the original defect in a new place. An unnamed entry still
# produces a true string; it just claims less.
#
# ★Every key in `iso_defaults` MUST appear here, even if mapped to None.
# tests/test_dcpi_modeled_source.py fails when one is missing, so a newly added
# ISO cannot silently inherit a wrong or generic attribution.
_ISO_MODELED_REFERENCE = {
    # United States
    "ERCOT": "ERCOT", "PJM": "PJM Interconnection", "CAISO": "CAISO",
    "MISO": "MISO", "NYISO": "NYISO", "ISONE": "ISO New England",
    "SPP": "Southwest Power Pool", "WECC": "WECC",
    "SERC": "SERC Reliability Corporation",
    "TVA": "Tennessee Valley Authority", "SOCO": "Southern Company",
    "FRCC": "Florida Reliability Coordinating Council",
    # Europe
    "NGESO": "NESO (Great Britain)", "EirGrid": "EirGrid",
    "ENTSOE-DE": "ENTSO-E Transparency Platform (Germany)",
    "ENTSOE-NL": "ENTSO-E Transparency Platform (Netherlands)",
    "ENTSOE-FR": "ENTSO-E Transparency Platform (France)",
    "ENTSOE-ES": "ENTSO-E Transparency Platform (Spain)",
    "ENTSOE-IT": "ENTSO-E Transparency Platform (Italy)",
    "ENTSOE-PL": "ENTSO-E Transparency Platform (Poland)",
    "ENTSOE-AT": "ENTSO-E Transparency Platform (Austria)",
    "ENTSOE-BE": "ENTSO-E Transparency Platform (Belgium)",
    "ENTSOE-PT": "ENTSO-E Transparency Platform (Portugal)",
    "ENTSOE-CH": "ENTSO-E Transparency Platform (Switzerland)",
    "ENTSOE-GR": "ENTSO-E Transparency Platform (Greece)",
    "ENTSOE-CZ": "ENTSO-E Transparency Platform (Czechia)",
    "NORDPOOL": "Nord Pool",
    # Asia-Pacific
    "TEPCO": "TEPCO / OCCTO (Japan)", "KEPCO-KR": "KEPCO / KPX (South Korea)",
    "AEMO": "AEMO", "EMA": "EMA (Singapore)", "TAIPOWER": "Taipower",
    "POSOCO": "Grid-India (formerly POSOCO)", "PLN": "PLN (Indonesia)",
    "PLN-BATAM": "PLN Batam", "CLP": "CLP Power (Hong Kong)",
    "EGAT": "EGAT (Thailand)", "TNB": "TNB (Malaysia)",
    "NGCP": "NGCP (Philippines)", "EVN": "EVN (Vietnam)",
    "KEPCO": None,   # ambiguous: used by more than one utility — not named
    "TPM": None,     # ambiguous key — not named
    # Canada
    "IESO": "IESO (Ontario)", "HQ": "Hydro-Quebec", "BCH": "BC Hydro",
    "AESO": "AESO (Alberta)", "MH": "Manitoba Hydro",
    # Latin America
    "CENACE": "CENACE (Mexico)",
    # US territories
    "PREPA": "PREPA (Puerto Rico)", "GPA": "Guam Power Authority",
    "WAPA": None,    # ambiguous: Western Area Power Administration vs V.I. WAPA
}

_MODELED_SOURCE_UNNAMED = (
    "DC Hub analyst estimate - calibrated from published grid disclosures; "
    "not a per-market measurement"
)


def modeled_source_for(iso, iso_default_matched=True):
    """The provenance string for this market's MODELED inputs.

    Names the operator whose published disclosures the per-ISO defaults were
    calibrated against, and always says plainly that the value is an estimate
    rather than a measurement.

    Fails VAGUE-BUT-TRUE, never specific-but-wrong: an unknown ISO, an
    unnamed-on-purpose key, or a market that fell through
    `iso_defaults.get(iso, iso_defaults["WECC"])` gets a string that claims no
    operator. The fail-open WECC default is the reason the second argument
    exists - a market inheriting Western-US parameters must not be told its
    numbers were calibrated from WECC as though that were chosen for it.
    """
    if not iso_default_matched:
        return (_MODELED_SOURCE_UNNAMED +
                " (no ISO-specific calibration matched this market)")
    ref = _ISO_MODELED_REFERENCE.get((iso or "").strip())
    if not ref:
        return _MODELED_SOURCE_UNNAMED
    return ("DC Hub analyst estimate - calibrated from published "
            + ref + " disclosures; not a per-market measurement")


def gather_metrics_for_market(market: tuple) -> dict:
    """Return the input dict for the scoring formulas. Pulls from existing
    grid/queue/pipeline tables when available."""
    slug, name, state, iso, lat, lon = market
    metrics = {
        "queue_wait_months": None,
        "queue_capacity_mw": None,
        "reserve_margin_pct": None,
        "gen_additions_12mo_mw": None,
        "curtailment_pct": None,
        "stranded_capacity_mw": None,
        "emergency_count_30d": None,
        "demand_growth_yoy_pct": None,
        "queue_approval_rate_pct": None,
        "btm_headroom_mw": None,
    }

    # r65 (2026-06-02): provenance tracking. Record which metrics were filled
    # from a *live* source (interconnection_queue / capacity_pipeline) so the
    # public output can honestly label each market "live" vs "modeled_estimate"
    # vs "mixed". This is label-only metadata — it never feeds the scoring
    # formulas (compute_* read specific numeric keys; they ignore "data_basis").
    _live_fields: set[str] = set()

    # r-ws3-signal-tier (2026-07-28): record WHICH live adapter actually
    # returned data, not merely which fields ended up populated. The tier below
    # is a claim about the adapters that ran, so it is recorded at the call
    # sites themselves — re-inferring it from _live_fields would mis-attribute
    # the moment a second writer touches one of those fields.
    # HONEST LIMIT: _state_queue_depth and _state_gen_additions both return None
    # for a DB ERROR *and* for a legitimately empty result (swallow-all excepts
    # in each). False here therefore means "returned no data", NOT "the query
    # failed" — this is never reported as an error count.
    _adapters = {
        "interconnect_queue": False,   # _state_queue_depth
        "planned_generators": False,   # _state_gen_additions
        "grid_telemetry":     False,   # _latest_grid_telemetry
    }

    # ── Live enrichment from the REAL interconnection-queue table ──────
    # (2026-07-24 rewrite — see _state_queue_depth for the root-cause note.)
    # Drive the constraint input off REAL per-state active-queue DEPTH. We
    # deliberately do NOT use queue_date as a wait signal: it stores a
    # projected COD in several ISOs and goes negative (TX ≈ -16mo), so it is
    # not a submission-to-energization wait. Queue DEPTH is the canonical
    # interconnection-congestion proxy — a deeper active queue means longer
    # real waits and less deliverable headroom (LBNL queue studies). This
    # changes only the INPUT the scorer reads; the formula is unchanged.
    # r-country-code-collision (2026-08-08): gate BOTH state-keyed adapters on
    # the market actually being American. Without this, a non-US market's
    # country code is read as a US state code and the score is fed another
    # continent's data. See _live_state_reads_allowed for the measured cases.
    _live_state_ok = _live_state_reads_allowed(state, iso)
    metrics["_live_state_reads_allowed"] = _live_state_ok

    q = _state_queue_depth(state) if _live_state_ok else None
    if q:
        active_gw = q["active_mw"] / 1000.0
        # Depth → effective interconnection-wait proxy (months), clipped to a
        # sane 12–66mo band. 0.6 mo/GW is calibrated so the live per-state
        # spread (~3–465 GW) lands across the band without one mega-queue
        # (ERCOT/TX) swamping the scale (it saturates at the 66mo cap, which
        # is correct — TX is the most-contended queue in the country).
        metrics["queue_wait_months"] = round(
            _clip(12.0 + active_gw * 0.6, 12.0, 66.0), 1)
        metrics["queue_capacity_mw"] = round(q["active_mw"], 1)
        _live_fields.add("queue_wait_months")
        _live_fields.add("queue_capacity_mw")
        _adapters["interconnect_queue"] = True

    # Near-term (<=12mo) generation additions from the REAL generation-SUPPLY
    # source: planned_generators (EIA-860M), aggregated per state and cached.
    # (2026-07-24 rewrite — see _state_gen_additions for the root-cause note.)
    # The old query hit `capacity_pipeline WHERE iso=%s AND expected_cod<...`,
    # but that table has NO `iso` and NO completion_date column (it tracks DATA-
    # CENTER capacity, not generation), so it threw on every call inside its
    # swallow-all try and gen_additions was 0 for every market — the excess-side
    # twin of the interconnect_queue constraint bug. This changes only the INPUT
    # the scorer reads; the excess formula (s_additions, 20% weight) is unchanged.
    ga = _state_gen_additions(state) if _live_state_ok else None
    if ga is not None:
        metrics["gen_additions_12mo_mw"] = round(ga, 1)
        _live_fields.add("gen_additions_12mo_mw")
        _adapters["planned_generators"] = True

    # Heuristic defaults by ISO (calibrated from public 2025 data)
    iso_defaults = {
        "ERCOT":  {"queue_wait_months": 30, "reserve_margin_pct": 13.5, "curtailment_pct": 4.0,
                   "queue_approval_rate_pct": 55, "btm_headroom_mw": 800},
        "PJM":    {"queue_wait_months": 48, "reserve_margin_pct": 14.5, "curtailment_pct": 1.0,
                   "queue_approval_rate_pct": 30, "btm_headroom_mw": 400},
        "CAISO":  {"queue_wait_months": 36, "reserve_margin_pct": 17.0, "curtailment_pct": 9.0,
                   "queue_approval_rate_pct": 40, "btm_headroom_mw": 300},
        "MISO":   {"queue_wait_months": 33, "reserve_margin_pct": 18.5, "curtailment_pct": 6.0,
                   "queue_approval_rate_pct": 55, "btm_headroom_mw": 600},
        "NYISO":  {"queue_wait_months": 30, "reserve_margin_pct": 22.0, "curtailment_pct": 2.0,
                   "queue_approval_rate_pct": 50, "btm_headroom_mw": 200},
        "ISONE":  {"queue_wait_months": 27, "reserve_margin_pct": 21.0, "curtailment_pct": 3.0,
                   "queue_approval_rate_pct": 50, "btm_headroom_mw": 150},
        "SPP":    {"queue_wait_months": 24, "reserve_margin_pct": 24.0, "curtailment_pct": 11.0,
                   "queue_approval_rate_pct": 65, "btm_headroom_mw": 700},
        "WECC":   {"queue_wait_months": 28, "reserve_margin_pct": 20.0, "curtailment_pct": 7.5,
                   "queue_approval_rate_pct": 50, "btm_headroom_mw": 500},
        "SERC":   {"queue_wait_months": 24, "reserve_margin_pct": 18.0, "curtailment_pct": 1.5,
                   "queue_approval_rate_pct": 60, "btm_headroom_mw": 350},
        "TVA":    {"queue_wait_months": 22, "reserve_margin_pct": 19.5, "curtailment_pct": 1.0,
                   "queue_approval_rate_pct": 65, "btm_headroom_mw": 250},
        # r-iso-defaults-southeast (2026-07-28): SOCO and FRCC were MISSING, and
        # `iso_defaults.get(iso, iso_defaults["WECC"])` fails OPEN — so ~22 live
        # Southeast markets were being scored with WESTERN-grid parameters. Not
        # merely imprecise: Atlanta (Georgia Power) published `curtailment_pct:
        # 7.5` — a Western curtailment figure, worth 15 of its 49.2 excess-power
        # points (curtailment is 20% of that score) — and every SOCO/FRCC market
        # published the opportunity "500 MW behind-the-meter industrial
        # headroom", which is simply WECC's btm_headroom_mw landing exactly on
        # the `bh >= 500` threshold in derive_top_signals.
        #
        # Both are non-RTO, vertically integrated, so they belong to the SERC/TVA
        # family above (low curtailment, high approval, modest BTM) — NOT WECC's.
        # Same standing as every other row here: heuristic planning-level
        # estimates, and they stay labelled `modeled` in data_basis.
        #
        # SOCO = Georgia Power + Alabama Power + Mississippi Power.
        #   wait 30   longest in the Southeast family: Georgia is the most
        #             contended large-load market in it. (Atlanta's slug_override
        #             of 36 still wins for the metro itself.)
        #   reserve   15.5 — Southern plans to a tighter margin than SERC's 18
        #             under the current load surge.
        #   curtail   0.5 — gas/nuclear/coal fleet with modest solar; the lowest
        #             in the family, below TVA's 1.0.
        #   approval  65 — vertically integrated bilateral interconnection has
        #             far less speculative attrition than an RTO queue (cf. PJM 30).
        #   btm       300 — between TVA 250 and SERC 350, and deliberately under
        #             the 500 threshold that was fabricating the BTM opportunity.
        "SOCO":   {"queue_wait_months": 30, "reserve_margin_pct": 15.5, "curtailment_pct": 0.5,
                   "queue_approval_rate_pct": 65, "btm_headroom_mw": 300},
        # FRCC = FPL + Duke Energy Florida + TECO.
        #   reserve   20.0 — the firmest number here: Florida utilities plan to a
        #             20% reserve margin (long-standing FPSC/FRCC planning basis).
        #   wait 26   peninsula with limited import capability, so transmission
        #             binds sooner, but still non-RTO and faster than an RTO queue.
        #   curtail   1.0 — large FPL solar build, but high summer load
        #             coincidence on a peninsula leaves little to curtail.
        #   approval  60 · btm 200 — tighter transmission envelope and less heavy
        #             industry than the Southeast interior.
        "FRCC":   {"queue_wait_months": 26, "reserve_margin_pct": 20.0, "curtailment_pct": 1.0,
                   "queue_approval_rate_pct": 60, "btm_headroom_mw": 200},
        # r57 (2026-05-25): International ISO defaults. Sourced from
        # ENTSO-E 2024 winter outlook, AEMO ESOO 2024, IESO Annual
        # Planning Outlook, EirGrid Generation Capacity Statement 2024,
        # NGESO ETYS 2024, METI/OCCTO Japan, EMA Singapore stats.
        "NGESO":    {"queue_wait_months": 84, "reserve_margin_pct": 9.0, "curtailment_pct": 7.5,
                     "queue_approval_rate_pct": 25, "btm_headroom_mw": 150},
        "EirGrid":  {"queue_wait_months": 60, "reserve_margin_pct": 8.5, "curtailment_pct": 11.0,
                     "queue_approval_rate_pct": 20, "btm_headroom_mw": 100},
        "ENTSOE-DE":{"queue_wait_months": 72, "reserve_margin_pct": 10.5, "curtailment_pct": 5.5,
                     "queue_approval_rate_pct": 35, "btm_headroom_mw": 220},
        "ENTSOE-NL":{"queue_wait_months": 96, "reserve_margin_pct": 11.0, "curtailment_pct": 6.0,
                     "queue_approval_rate_pct": 15, "btm_headroom_mw": 80},
        "ENTSOE-FR":{"queue_wait_months": 48, "reserve_margin_pct": 16.5, "curtailment_pct": 2.0,
                     "queue_approval_rate_pct": 45, "btm_headroom_mw": 300},
        "NORDPOOL": {"queue_wait_months": 36, "reserve_margin_pct": 22.0, "curtailment_pct": 8.5,
                     "queue_approval_rate_pct": 55, "btm_headroom_mw": 450},
        "TEPCO":    {"queue_wait_months": 42, "reserve_margin_pct": 14.0, "curtailment_pct": 1.5,
                     "queue_approval_rate_pct": 50, "btm_headroom_mw": 180},
        "KEPCO":    {"queue_wait_months": 36, "reserve_margin_pct": 16.5, "curtailment_pct": 1.0,
                     "queue_approval_rate_pct": 50, "btm_headroom_mw": 160},
        "AEMO":     {"queue_wait_months": 30, "reserve_margin_pct": 13.5, "curtailment_pct": 9.5,
                     "queue_approval_rate_pct": 50, "btm_headroom_mw": 350},
        "EMA":      {"queue_wait_months": 24, "reserve_margin_pct": 18.0, "curtailment_pct": 0.5,
                     "queue_approval_rate_pct": 60, "btm_headroom_mw": 50},
        "IESO":     {"queue_wait_months": 30, "reserve_margin_pct": 17.5, "curtailment_pct": 3.5,
                     "queue_approval_rate_pct": 55, "btm_headroom_mw": 250},
        "HQ":       {"queue_wait_months": 18, "reserve_margin_pct": 24.0, "curtailment_pct": 4.5,
                     "queue_approval_rate_pct": 70, "btm_headroom_mw": 500},
        "BCH":      {"queue_wait_months": 24, "reserve_margin_pct": 21.0, "curtailment_pct": 5.0,
                     "queue_approval_rate_pct": 60, "btm_headroom_mw": 280},
        # r71 (2026-06-06): EU expansion. Sourced from ENTSO-E adequacy
        # reports 2024 + national TSO outlooks (Red Eléctrica, Terna,
        # PSE, APG, Elia, REN, Swissgrid, ADMIE, ČEPS). Curtailment
        # numbers from EurObserv'ER 2024 RES barometer.
        "ENTSOE-ES":{"queue_wait_months": 60, "reserve_margin_pct": 14.5, "curtailment_pct": 9.5,
                     "queue_approval_rate_pct": 30, "btm_headroom_mw": 250},
        "ENTSOE-IT":{"queue_wait_months": 78, "reserve_margin_pct": 11.5, "curtailment_pct": 3.0,
                     "queue_approval_rate_pct": 20, "btm_headroom_mw": 150},
        "ENTSOE-PL":{"queue_wait_months": 60, "reserve_margin_pct": 12.5, "curtailment_pct": 6.0,
                     "queue_approval_rate_pct": 40, "btm_headroom_mw": 180},
        "ENTSOE-AT":{"queue_wait_months": 48, "reserve_margin_pct": 17.0, "curtailment_pct": 3.5,
                     "queue_approval_rate_pct": 45, "btm_headroom_mw": 120},
        "ENTSOE-BE":{"queue_wait_months": 72, "reserve_margin_pct": 10.0, "curtailment_pct": 2.0,
                     "queue_approval_rate_pct": 25, "btm_headroom_mw": 90},
        "ENTSOE-PT":{"queue_wait_months": 54, "reserve_margin_pct": 13.5, "curtailment_pct": 7.0,
                     "queue_approval_rate_pct": 35, "btm_headroom_mw": 110},
        "ENTSOE-CH":{"queue_wait_months": 36, "reserve_margin_pct": 18.0, "curtailment_pct": 1.5,
                     "queue_approval_rate_pct": 50, "btm_headroom_mw": 80},
        "ENTSOE-GR":{"queue_wait_months": 60, "reserve_margin_pct": 13.0, "curtailment_pct": 8.5,
                     "queue_approval_rate_pct": 35, "btm_headroom_mw": 100},
        "ENTSOE-CZ":{"queue_wait_months": 54, "reserve_margin_pct": 14.5, "curtailment_pct": 4.0,
                     "queue_approval_rate_pct": 40, "btm_headroom_mw": 130},
        # r71: APAC expansion. KEPCO-KR (South Korea), POSOCO (India),
        # PLN (Indonesia), CLP (Hong Kong), TAIPOWER (Taiwan), EGAT
        # (Thailand), TNB (Malaysia), NGCP (Philippines), EVN (Vietnam),
        # TPM (NZ Transpower).
        "KEPCO-KR": {"queue_wait_months": 30, "reserve_margin_pct": 16.0, "curtailment_pct": 1.5,
                     "queue_approval_rate_pct": 55, "btm_headroom_mw": 200},
        "POSOCO":   {"queue_wait_months": 42, "reserve_margin_pct": 9.5, "curtailment_pct": 5.5,
                     "queue_approval_rate_pct": 45, "btm_headroom_mw": 350},
        "PLN":      {"queue_wait_months": 48, "reserve_margin_pct": 30.0, "curtailment_pct": 4.0,
                     "queue_approval_rate_pct": 50, "btm_headroom_mw": 280},
        "CLP":      {"queue_wait_months": 36, "reserve_margin_pct": 22.0, "curtailment_pct": 0.5,
                     "queue_approval_rate_pct": 55, "btm_headroom_mw": 60},
        "TAIPOWER": {"queue_wait_months": 30, "reserve_margin_pct": 12.0, "curtailment_pct": 2.0,
                     "queue_approval_rate_pct": 50, "btm_headroom_mw": 120},
        "EGAT":     {"queue_wait_months": 36, "reserve_margin_pct": 28.0, "curtailment_pct": 2.5,
                     "queue_approval_rate_pct": 55, "btm_headroom_mw": 220},
        "TNB":      {"queue_wait_months": 30, "reserve_margin_pct": 30.0, "curtailment_pct": 2.0,
                     "queue_approval_rate_pct": 60, "btm_headroom_mw": 200},
        "NGCP":     {"queue_wait_months": 42, "reserve_margin_pct": 18.0, "curtailment_pct": 3.0,
                     "queue_approval_rate_pct": 50, "btm_headroom_mw": 180},
        "EVN":      {"queue_wait_months": 36, "reserve_margin_pct": 14.0, "curtailment_pct": 6.5,
                     "queue_approval_rate_pct": 45, "btm_headroom_mw": 250},
        "TPM":      {"queue_wait_months": 24, "reserve_margin_pct": 18.0, "curtailment_pct": 5.5,
                     "queue_approval_rate_pct": 60, "btm_headroom_mw": 90},
        # r-str-coverage (2026-08-07): two operators the set was missing.
        # Both are MODELED planning anchors, published as modeled_estimate
        # like every other row in this dict — the live saturation terms and
        # any live telemetry override them where a feed exists.
        #
        # PLN-BATAM — bright PLN Batam runs Batam's ISOLATED grid, separate
        # from the Java–Bali system "PLN" describes. Gas-fired and healthy on
        # reserve but SMALL in absolute terms, which is why btm_headroom_mw is
        # a third of PLN's: the percentage is comfortable, the megawatts are
        # not. Free-trade-zone status shortens approval vs Java–Bali; near-zero
        # VRE share means near-zero curtailment. Deliberately NOT inheriting
        # PLN: publishing Java–Bali anchors over an island grid is the same
        # class of error as stamping WECC on a non-WECC market.
        "PLN-BATAM":{"queue_wait_months": 36, "reserve_margin_pct": 22.0, "curtailment_pct": 1.5,
                     "queue_approval_rate_pct": 55, "btm_headroom_mw": 90},
        # CENACE — Mexico's system operator. The constraint in central Mexico
        # is TRANSMISSION, not generation: interconnection studies are slow,
        # new transmission investment has lagged demand, and operating
        # reserves on the SIN have run thin on peak days. Legacy self-supply
        # and private-generation contracts leave some behind-the-meter room,
        # which is where most Querétaro capacity has actually come from.
        "CENACE":   {"queue_wait_months": 54, "reserve_margin_pct": 8.0, "curtailment_pct": 3.0,
                     "queue_approval_rate_pct": 30, "btm_headroom_mw": 150},
        # r71: Canada provincial. AESO (Alberta — fastest queue in Canada,
        # market-based, lots of gas-fired headroom), Manitoba Hydro.
        "AESO":     {"queue_wait_months": 18, "reserve_margin_pct": 23.0, "curtailment_pct": 4.0,
                     "queue_approval_rate_pct": 70, "btm_headroom_mw": 400},
        "MH":       {"queue_wait_months": 12, "reserve_margin_pct": 28.0, "curtailment_pct": 6.5,
                     "queue_approval_rate_pct": 75, "btm_headroom_mw": 350},
        # r71: US territories. Island grids, dense urban load, ZERO
        # excess reserve (PR has been load-shedding since María; USVI
        # WAPA imports diesel; Guam GPA is military-anchored small grid).
        # Conservative defaults emit a CAUTION / AVOID verdict honestly.
        "PREPA":    {"queue_wait_months": 48, "reserve_margin_pct": 6.0, "curtailment_pct": 1.0,
                     "queue_approval_rate_pct": 15, "btm_headroom_mw": 30},
        "GPA":      {"queue_wait_months": 30, "reserve_margin_pct": 14.0, "curtailment_pct": 1.0,
                     "queue_approval_rate_pct": 35, "btm_headroom_mw": 20},
        "WAPA":     {"queue_wait_months": 36, "reserve_margin_pct": 10.0, "curtailment_pct": 1.0,
                     "queue_approval_rate_pct": 25, "btm_headroom_mw": 15},
    }
    # r-ws3-signal-tier (2026-07-28): record WHETHER this .get() fell through to
    # the WECC default. Behaviour is byte-identical (same dict, same fallback
    # value) — this only remembers that the fail-open documented in the
    # SOCO/FRCC note above fired, so the signal tier can refuse to call a market
    # 'partial'/'full' when its modeled anchors are Western-grid parameters for
    # a grid that is not WECC. Underscore key → never a score input.
    _iso_default_matched = bool(iso) and iso in iso_defaults
    metrics["_iso_default_matched"] = _iso_default_matched
    base = iso_defaults.get(iso, iso_defaults["WECC"])
    for k, v in base.items():
        if metrics[k] is None: metrics[k] = v

    # Per-slug overrides — known stranded/excess pockets
    slug_overrides = {
        "northern-virginia":   {"queue_wait_months": 60, "reserve_margin_pct": 12.0, "demand_growth_yoy_pct": 9},
        "phoenix":             {"queue_wait_months": 42, "reserve_margin_pct": 13.5, "demand_growth_yoy_pct": 8},
        "atlanta":             {"queue_wait_months": 36, "reserve_margin_pct": 14.0, "demand_growth_yoy_pct": 7},
        "dallas-fort-worth":   {"queue_wait_months": 30, "reserve_margin_pct": 13.0, "demand_growth_yoy_pct": 8},
        "silicon-valley":      {"queue_wait_months": 48, "reserve_margin_pct": 16.0, "demand_growth_yoy_pct": 5},
        "santa-clara":         {"queue_wait_months": 48, "reserve_margin_pct": 15.0, "demand_growth_yoy_pct": 6},
        "chicago":             {"queue_wait_months": 36, "reserve_margin_pct": 14.0, "demand_growth_yoy_pct": 4},
        # The contrarian set — high excess scores
        "williston-nd":        {"queue_wait_months": 14, "reserve_margin_pct": 28.0, "curtailment_pct": 14.0,
                                "stranded_capacity_mw": 250, "queue_approval_rate_pct": 75, "demand_growth_yoy_pct": 1.5},
        "cheyenne-wy":         {"queue_wait_months": 18, "reserve_margin_pct": 26.0, "curtailment_pct": 12.0,
                                "stranded_capacity_mw": 600, "queue_approval_rate_pct": 70, "demand_growth_yoy_pct": 2},
        "midland-tx":          {"queue_wait_months": 16, "reserve_margin_pct": 22.0, "curtailment_pct": 10.0,
                                "queue_approval_rate_pct": 70, "btm_headroom_mw": 1500, "demand_growth_yoy_pct": 4},
        "appalachia-coal":     {"queue_wait_months": 12, "reserve_margin_pct": 20.0, "stranded_capacity_mw": 1200,
                                "queue_approval_rate_pct": 80, "demand_growth_yoy_pct": 1},
        "the-dalles-or":       {"queue_wait_months": 18, "reserve_margin_pct": 24.0, "curtailment_pct": 5.0,
                                "queue_approval_rate_pct": 65, "demand_growth_yoy_pct": 3},
        "pacific-nw-rural":    {"queue_wait_months": 20, "reserve_margin_pct": 25.0, "curtailment_pct": 6.0,
                                "queue_approval_rate_pct": 60, "demand_growth_yoy_pct": 2.5},
        "rural-spp":           {"queue_wait_months": 18, "reserve_margin_pct": 27.0, "curtailment_pct": 13.0,
                                "stranded_capacity_mw": 400, "queue_approval_rate_pct": 75, "demand_growth_yoy_pct": 2},
        "upper-michigan":      {"queue_wait_months": 16, "reserve_margin_pct": 26.0, "curtailment_pct": 5.0,
                                "stranded_capacity_mw": 800, "queue_approval_rate_pct": 70, "demand_growth_yoy_pct": 1},
        "central-washington":  {"queue_wait_months": 22, "reserve_margin_pct": 23.0, "curtailment_pct": 4.0,
                                "queue_approval_rate_pct": 60, "demand_growth_yoy_pct": 4},
        # r57 (2026-05-25): International overrides. Calibrated from
        # NGESO connection-queue reform (May 2024), Singapore IMDA's
        # 2024 data-center moratorium guidance, Hydro-Québec's stated
        # 5 GW available capacity, EirGrid's data-center moratorium.
        "london":              {"queue_wait_months": 144, "reserve_margin_pct": 7.0, "curtailment_pct": 5.5,
                                "queue_approval_rate_pct": 10, "demand_growth_yoy_pct": 11,
                                "btm_headroom_mw": 50},
        "manchester":          {"queue_wait_months": 96, "reserve_margin_pct": 9.0, "curtailment_pct": 6.5,
                                "queue_approval_rate_pct": 20, "demand_growth_yoy_pct": 8,
                                "btm_headroom_mw": 100},
        "dublin":              {"queue_wait_months": 72, "reserve_margin_pct": 7.5, "curtailment_pct": 13.0,
                                "queue_approval_rate_pct": 5,  "demand_growth_yoy_pct": 14,
                                "btm_headroom_mw": 80},  # de-facto moratorium
        "frankfurt":           {"queue_wait_months": 84, "reserve_margin_pct": 9.5, "curtailment_pct": 6.0,
                                "queue_approval_rate_pct": 25, "demand_growth_yoy_pct": 9,
                                "btm_headroom_mw": 180},
        "amsterdam":           {"queue_wait_months": 120, "reserve_margin_pct": 10.0, "curtailment_pct": 5.5,
                                "queue_approval_rate_pct": 10, "demand_growth_yoy_pct": 12,
                                "btm_headroom_mw": 60},   # grid congestion
        "paris":               {"queue_wait_months": 42, "reserve_margin_pct": 18.0, "curtailment_pct": 1.5,
                                "queue_approval_rate_pct": 50, "demand_growth_yoy_pct": 6,
                                "btm_headroom_mw": 320},
        "marseille":           {"queue_wait_months": 36, "reserve_margin_pct": 17.0, "curtailment_pct": 2.5,
                                "queue_approval_rate_pct": 55, "demand_growth_yoy_pct": 5,
                                "btm_headroom_mw": 280},
        "stockholm":           {"queue_wait_months": 30, "reserve_margin_pct": 26.0, "curtailment_pct": 9.0,
                                "queue_approval_rate_pct": 60, "demand_growth_yoy_pct": 7,
                                "stranded_capacity_mw": 350,  # hydro surplus
                                "btm_headroom_mw": 500},
        "tokyo":               {"queue_wait_months": 48, "reserve_margin_pct": 12.0, "curtailment_pct": 1.0,
                                "queue_approval_rate_pct": 45, "demand_growth_yoy_pct": 6,
                                "btm_headroom_mw": 150},
        "osaka":               {"queue_wait_months": 36, "reserve_margin_pct": 15.5, "curtailment_pct": 0.5,
                                "queue_approval_rate_pct": 50, "demand_growth_yoy_pct": 4,
                                "btm_headroom_mw": 140},
        "sydney":              {"queue_wait_months": 36, "reserve_margin_pct": 12.5, "curtailment_pct": 10.5,
                                "queue_approval_rate_pct": 45, "demand_growth_yoy_pct": 7,
                                "btm_headroom_mw": 280},
        "melbourne":           {"queue_wait_months": 30, "reserve_margin_pct": 14.0, "curtailment_pct": 9.0,
                                "queue_approval_rate_pct": 50, "demand_growth_yoy_pct": 6,
                                "btm_headroom_mw": 310},
        "singapore":           {"queue_wait_months": 36, "reserve_margin_pct": 12.0, "curtailment_pct": 0.2,
                                "queue_approval_rate_pct": 30, "demand_growth_yoy_pct": 5,
                                "btm_headroom_mw": 30},   # IMDA moratorium-era, eased 2022
        "toronto":             {"queue_wait_months": 30, "reserve_margin_pct": 16.5, "curtailment_pct": 3.5,
                                "queue_approval_rate_pct": 55, "demand_growth_yoy_pct": 5,
                                "btm_headroom_mw": 220},
        "montreal":            {"queue_wait_months": 14, "reserve_margin_pct": 28.0, "curtailment_pct": 4.5,
                                "queue_approval_rate_pct": 75, "demand_growth_yoy_pct": 8,
                                "stranded_capacity_mw": 1500,  # HQ's flagship surplus pitch
                                "btm_headroom_mw": 600},
        "vancouver":           {"queue_wait_months": 24, "reserve_margin_pct": 22.0, "curtailment_pct": 5.0,
                                "queue_approval_rate_pct": 60, "demand_growth_yoy_pct": 4,
                                "btm_headroom_mw": 300},
        # r-str-coverage (2026-08-07): johor is the ONE of the four new markets
        # whose national anchor is contradicted by a specific published policy.
        # Malaysia's data-centre Green Lane Pathway targets a far shorter
        # energisation window than TNB's 30-month national anchor, so the
        # inherited value would overstate time-to-power. Held at 18 months
        # rather than the headline target — the pathway is an intent, and the
        # Sedenak/Kulai cluster is already heavily subscribed, which is also
        # why btm_headroom is set BELOW TNB's national figure while demand
        # growth is set well above it. The other three (batam, pune,
        # queretaro) get no override: their ISO anchors plus the live
        # saturation terms are the honest read, and inventing a per-slug
        # number we cannot source would be worse than inheriting one we can.
        "johor":               {"queue_wait_months": 18, "reserve_margin_pct": 26.0, "curtailment_pct": 1.5,
                                "queue_approval_rate_pct": 65, "demand_growth_yoy_pct": 18,
                                "btm_headroom_mw": 150},
    }
    # r47.42 (2026-05-27): slug-tolerant override lookup.
    # _load_markets_dynamic emits bare-city slugs ("cheyenne" from LOWER(city))
    # while slug_overrides historically used state-suffixed keys
    # ("cheyenne-wy", "williston-nd", "midland-tx", "the-dalles-or"). Mismatch
    # → override never applies → WECC ISO default wins → dashboard shows 44.8
    # for Cheyenne when calibration actually puts it at 69.5 (BUILD). Same
    # silent regression for every state-suffixed override key.
    # Fix: try the bare slug first, then synthesize state-suffixed variants
    # using the state from the market tuple. First match wins.
    _state_lc = (state or "").lower()
    _slug_candidates = [
        slug,                                 # "cheyenne"      (dynamic)
        f"{slug}-{_state_lc}" if _state_lc else None,  # "cheyenne-wy"  (hardcoded shape)
    ]
    _override_applied = False
    _override_replaced_live: list[str] = []
    for _candidate in _slug_candidates:
        if _candidate and _candidate in slug_overrides:
            _ov = {k: v for k, v in slug_overrides[_candidate].items()
                   if v is not None}
            # ── r-ws3-methodology (2026-07-29): a hand-calibrated constant that
            # REPLACES a live-read value must also revoke that field's "live"
            # provenance label. This block runs AFTER the interconnect_queue
            # read, and _live_fields was populated at that call site and never
            # revoked — so the published data_basis_note listed
            # queue_wait_months under "live" for every override market whose
            # queue adapter had answered.
            #
            # Measured live 2026-07-29, before this fix:
            #   phoenix — real queue depth 9,920 MW -> 12 + 0.6*9.92 = 18.0 mo,
            #             overridden to 42.0, still published as live.
            #   chicago — same shape, overridden to 36.0, still published live.
            # That is a provenance lie on a published field, not a rounding
            # issue: the number a reader was told came from a table came from
            # a dict. Discarding the label is the correction; the VALUE is
            # unchanged, so no score moves. Some markets will flip data_basis
            # from "mixed" to "modeled_estimate" — that is the point.
            _override_replaced_live = sorted(k for k in _ov if k in _live_fields)
            for _k in _override_replaced_live:
                _live_fields.discard(_k)
            metrics.update(_ov)
            _override_applied = True
            break

    # ── r67 (2026-06-02): LIVE grid-headroom override of reserve_margin_pct ──
    # At this point metrics["reserve_margin_pct"] holds the MODELED planning
    # anchor (iso_default or slug_override). Now read the freshest live
    # grid_telemetry row for this market's ISO and let the real measured
    # operating headroom move that anchor BEFORE it reaches the scorer. This is
    # the whole point of the wiring: the hardcoded value stops being the primary
    # input the moment a live feed exists for the ISO. No live row (or stale /
    # non-telemetry ISO) → the modeled anchor is used unchanged, labelled
    # modeled. We NEVER fabricate: _reserve_margin_with_live only ever returns
    # the modeled anchor or a value derived from a real measured headroom.
    _live_tel = _latest_grid_telemetry(iso)
    _modeled_reserve = metrics.get("reserve_margin_pct")
    _eff_reserve, _live_op_reserve = _reserve_margin_with_live(
        _modeled_reserve, _live_tel)
    if _live_op_reserve is not None:
        # A real live signal exists for this ISO — it now drives the value.
        metrics["reserve_margin_pct"] = round(_eff_reserve, 1)
        _live_fields.add("reserve_margin_pct")
        _adapters["grid_telemetry"] = True
        # Stash the raw live read so data_basis can expose the basis honestly
        # (these underscore-prefixed keys are NOT score inputs — compute_* only
        # read the documented numeric keys, so scores stay a pure function of
        # reserve_margin_pct et al.).
        metrics["_live_grid"] = {
            "iso": _live_tel.get("iso"),
            "zone": _live_tel.get("zone"),
            "online_gen_mw": _live_tel.get("online_gen_mw"),
            "load_mw": _live_tel.get("load_mw"),
            "headroom_mw": _live_tel.get("headroom_mw"),
            "operating_reserve_pct": round(_live_op_reserve, 2),
            "observed_at": (_live_tel.get("observed_at").isoformat()
                            if _live_tel.get("observed_at") is not None else None),
            "age_min": _live_tel.get("age_min"),
            "modeled_reserve_anchor_pct": _modeled_reserve,
            "effective_reserve_margin_pct": round(_eff_reserve, 1),
            "source": _live_tel.get("source"),
        }

    # Demand growth default
    if metrics.get("demand_growth_yoy_pct") is None:
        metrics["demand_growth_yoy_pct"] = 4.0

    # ── r-declone (2026-07-02): per-market differentiation from LOCAL DC
    # saturation. The bug: most of the 317 markets have no slug_override, so
    # they inherit the pure iso_defaults → every market in an ISO gets IDENTICAL
    # queue_wait/scores (every WECC market showed 42mo/AVOID; master-shell
    # iso_clone_ratio=0.23). Real per-market signal is available and currently
    # discarded: the market list is BUILT from discovered_facilities, so each
    # market has a real local DC footprint. A metro dense with data centers
    # competes harder for the SAME ISO grid → longer effective interconnection
    # wait + higher local demand growth than a sparse market in the same ISO.
    # Apply as a BOUNDED delta ANCHORED on the ISO baseline (never replaces it),
    # and ONLY for markets without a hand-calibrated override — so the ~40
    # curated flagship markets stay byte-identical and only the cloned majority
    # de-clone, using real data, without abandoning the grid grounding.
    #
    # r-declone-2 (2026-07-17): two measured flaws fixed (master-shell
    # iso_clone_ratio stuck at 0.653):
    #   1. The footprint query filtered country='US', so EVERY international
    #      market (POSOCO/AEMO/NORDPOOL/ENTSOE-*…) matched 0 facilities and
    #      stayed a byte-identical ISO clone (POSOCO: 1 distinct composite
    #      across 4 markets) even though discovered_facilities holds a real
    #      footprint for those cities (Mumbai 104 rows, Sydney 91, Oslo 41…).
    #      Intl markets now match on city within non-US rows (intl state
    #      spellings are too inconsistent to filter on: 'QLD'/'WAS'/
    #      'Maharashtra'/'' all appear; pooling by city+non-US is the honest
    #      grain we actually have).
    #   2. The linear index (fac/80 + mw/2000 + pipe/1500) compressed real
    #      variation below output rounding: tempe (13 fac) vs henderson
    #      (5 fac, 266 MW) differed by 0.004 sat → identical published values
    #      after round(...,1). Log-scaled terms spread the low/mid range where
    #      nearly all markets live, and DISTINCT-provider diversity (a real
    #      per-city column) joins the index, so metros with genuinely
    #      different footprints land on distinct published values. Markets
    #      with IDENTICAL footprint rows (or none at all) remain identical —
    #      that is real data honestly reflected, never noise.
    #
    # r-namesake (2026-08-07): both branches now take their country predicate
    # from _market_country_scope — one definition, shared with the page
    # facility list, so "the facilities in this market" cannot mean two
    # different row sets on the same page. The US branch's old
    # `country IS NULL OR country=''` clause survives inside that helper, but
    # only for rows whose coordinates do not disprove the US claim.
    #
    # r-list-dedup (2026-08-08): both branches counted a twinned building once
    # per ROW, so a metro with heavy duplication read denser than it is. Left
    # unfixed in that change because it is a product-wide RESCORE, not a
    # cleanup, and shipping it as a rider on an SEO link-list fix is how a score
    # change lands unverified.
    #
    # r-sat-dedup (2026-08-08): fixed here, with the recompute that owed.
    # _SQL_FOOTPRINT_DEDUP is interpolated into BOTH branches from one
    # definition — see its comment for why a hand-copied literal is the thing
    # this function keeps getting wrong.
    #
    # MEASURED, full universe, by replaying this scorer in one process against
    # live Neon and toggling ONLY the predicate (316 markets, 286 with a
    # footprint; each market also scored twice under the AFTER config as a
    # telemetry-noise control — 0 of 286 disagreed, so every delta below is the
    # filter and nothing else):
    #   _saturation_index moved for 273/286  (mean 0.041, max 0.195)
    #   it FELL or held for 286/286 — it cannot rise, by construction
    #   constraint_score   changed 270/286  (mean -0.55, max 3.70)
    #   excess_power_score changed 177/286  (mean +0.10, max 0.80)
    #   composite          changed 250/286  (mean +0.20, max 1.60)
    #   ★ VERDICT FLIPS: 0.  No market changed BUILD/CAUTION/AVOID.
    # Worst movers: boardman 51→5 facilities (sat 0.532→0.337, queue-wait
    # ×1.139→×1.052), west-des-moines 43→11 (0.515→0.363), indianapolis
    # 20→10 (0.530→0.374). Markets read LESS contested, which is the correct
    # direction: the pre-fix number was counting the same building twice.
    #
    # Published distinctness fell slightly — 280→276 distinct
    # (constraint, excess, verdict) triples over the 286. Every new tie is
    # between markets whose deduped footprint tuple is byte-identical
    # (knoxville/lexington-ky/bismarck all really are 1 facility, 3 MW, 1
    # operator), which is r-declone-2's own rule: identical real footprints
    # publish identical values, and that is data honestly reflected, not a
    # clone. No market lost differentiation it had earned from real rows.
    if not _override_applied:
        try:
            _is_us_market = not _is_intl_market((None, None, state, iso))
            _ctry_sql, _ctry_params = _market_country_scope(iso, state, lat, lon)
            with _conn() as c, c.cursor() as cur:
                if _is_us_market:
                    # r-status-taxonomy (2026-07-29): op_mw is an ALLOW-LIST
                    # of in-service statuses, and pipe_mw an allow-list of
                    # pre-service ones. The two are DISJOINT, so the 0.25 and
                    # 0.15 index terms below now measure different megawatts.
                    # Anything matching neither is summed separately into
                    # unclassified_mw — never folded into operational, which
                    # is what the old deny-list did with 'Announced'.
                    cur.execute(f"""
                        SELECT COUNT(*) AS fac,
                               COALESCE(SUM(power_mw) FILTER (WHERE
                                 {_SQL_OP_STATUS}), 0) AS op_mw,
                               COALESCE(SUM(power_mw) FILTER (WHERE
                                 {_SQL_PIPE_STATUS}), 0) AS pipe_mw,
                               COALESCE(SUM(power_mw) FILTER (WHERE
                                 {_SQL_UNK_STATUS}), 0) AS unclassified_mw,
                               COUNT(DISTINCT provider) AS providers
                        FROM discovered_facilities
                        WHERE LOWER(city) = LOWER(%s) AND UPPER(COALESCE(state,'')) = %s
                          {_SQL_FOOTPRINT_DEDUP}
                          {_ctry_sql}
                    """, (name, (state or "").upper(), *_ctry_params))
                else:
                    # Intl: city-level pooling, non-US rows only (keeps
                    # Melbourne FL out of Melbourne AU). Status handling is
                    # byte-identical to the US branch above — the two queries
                    # sit 14 lines apart and the unfiltered-op_mw bug was in
                    # BOTH, so they must never define "operational"
                    # differently again.
                    cur.execute(f"""
                        SELECT COUNT(*) AS fac,
                               COALESCE(SUM(power_mw) FILTER (WHERE
                                 {_SQL_OP_STATUS}), 0) AS op_mw,
                               COALESCE(SUM(power_mw) FILTER (WHERE
                                 {_SQL_PIPE_STATUS}), 0) AS pipe_mw,
                               COALESCE(SUM(power_mw) FILTER (WHERE
                                 {_SQL_UNK_STATUS}), 0) AS unclassified_mw,
                               COUNT(DISTINCT provider) AS providers
                        FROM discovered_facilities
                        WHERE LOWER(city) = LOWER(%s)
                          {_SQL_FOOTPRINT_DEDUP}
                          {_ctry_sql}
                    """, (name, *_ctry_params))
                _fr = cur.fetchone()
            _fac = int((_fr[0] if _fr else 0) or 0)
            _op_mw = float((_fr[1] if _fr else 0) or 0)
            _pipe_mw = float((_fr[2] if _fr else 0) or 0)
            _unk_mw = float((_fr[3] if _fr else 0) or 0)
            _prov = int((_fr[4] if _fr else 0) or 0)
            # Saturation index 0..1: facility density + installed load +
            # pipeline pressure (future demand) + operator diversity. Each
            # term is log-scaled — ln(1+v)/ln(1+ceiling) — so the difference
            # between 5 and 13 facilities is visible in the published output,
            # not just the difference between 0 and 80. Ceilings = "largest
            # real metro" (Ashburn-class): 400 facilities / 8 GW OPERATIONAL
            # installed / 5 GW pipeline / 40 distinct operators.
            #
            # r-status-taxonomy (2026-07-29): NO DOUBLE-COUNT. _op_mw and
            # _pipe_mw are disjoint status buckets, so the 0.25 term and the
            # 0.15 term measure different megawatts. Until today _op_mw was an
            # unfiltered SUM(power_mw) that CONTAINED _pipe_mw, and pipeline
            # was therefore charged twice at a combined 0.40 weight while the
            # word "operational" was published over a total. Measured live:
            # 15% of the figure across the 60 exactly-measured markets was
            # pipeline; 71% across the announced-heavy greenfield markets
            # (richland-parish LA scored 5,000 MW of 'Planned' as built).
            #
            # The 8 GW ceiling is deliberately NOT rebased for the smaller
            # _op_mw. Log scaling makes the term nearly inert to the change:
            # halving _op_mw costs 0.077 of the term, 0.019 of the index, and
            # the worst fully-measured market moved -0.077 with zero verdict
            # flips anywhere in the 216 measured markets. Dropping the ceiling
            # to 4 GW instead RAISES the term for every market and saturates
            # everything above ~2 GW, re-cloning exactly the markets
            # r-declone-2 separated — a far bigger score change than the
            # defect it would be correcting.
            #
            # r-sat-dedup (2026-08-08): the inputs got smaller, so the ceilings
            # were re-examined rather than assumed. NOT rebased, and this time
            # the argument is measured over all 286 footprinted markets:
            #
            #   term      ceiling   max obs   p95   p99   #at/over
            #   fac           400       168    53    83          0
            #   op_mw        8000      4990   621  1343          0
            #   pipe_mw      5000      5000   360  2800          1
            #   prov           40        52    26    46          5
            #
            # Rebasing each ceiling to its post-dedup observed max (168 / 4990 /
            # 5000 / 52 — the stated "largest real metro" basis) buys almost no
            # differentiation and costs a systematic upward bias: distinct
            # saturation values go 189 → 191 of 286 (+2), while saturation RISES
            # for 277 of 286 markets (mean +0.024, max +0.068). That would give
            # back roughly half the correction this change just made, to gain
            # two decimal places. The r-status-taxonomy claim that log scaling
            # makes the op_mw term nearly inert also holds after dedup: of the
            # observed index move, the facility term contributed mean -0.0331
            # and op_mw only -0.0024.
            #
            # The one ceiling genuinely exceeded is prov (40), which 5 markets
            # sit at or over — but they did BEFORE this change too (max was 57),
            # so that is a pre-existing question about operator diversity, not
            # something dedup created. Deliberately not touched here: a ceiling
            # change is a rescore of its own and would need its own diff.
            _sat = _clip(0.40 * _log_sat(_fac, 400.0)
                         + 0.25 * _log_sat(_op_mw, 8000.0)
                         + 0.15 * _log_sat(_pipe_mw, 5000.0)
                         + 0.20 * _log_sat(_prov, 40.0), 0.0, 1.0)
            metrics["local_facility_count"] = _fac
            metrics["local_operational_mw"] = round(_op_mw, 1)
            metrics["local_pipeline_mw"] = round(_pipe_mw, 1)
            # Rows matching NEITHER bucket. Kept visible rather than folded
            # into operational: a status nobody mapped is unknown, not built.
            # Reads 0.0 for every status observed in either table today, so a
            # non-zero value means a producer invented a value — widen
            # util/status_taxonomy, never a call site.
            metrics["local_unclassified_mw"] = round(_unk_mw, 1)
            metrics["local_provider_count"] = _prov
            metrics["_saturation_index"] = round(_sat, 3)
            metrics["_saturation_scope"] = ("us_city_state" if _is_us_market
                                            else "intl_city")
            if _fac > 0 and _sat > 0:
                _qw = metrics.get("queue_wait_months")
                if _qw is not None:
                    # The ISO anchor models a TYPICAL mid-density metro
                    # (sat≈0.22 → ×1.0). Denser metros wait longer for the
                    # same ISO queue (up to ×1.35, the original r-declone
                    # ceiling); sparser ones wait slightly less (floor ×0.90 —
                    # less local competition for the same interconnection
                    # capacity). Bounded, anchored, reproducible from the
                    # market's own footprint row.
                    metrics["queue_wait_months"] = round(
                        float(_qw) * (0.90 + 0.45 * _sat), 1)
                _dg = float(metrics.get("demand_growth_yoy_pct") or 4.0)
                # up to +4pp local demand growth where DC density is high
                metrics["demand_growth_yoy_pct"] = round(_dg + 4.0 * _sat, 1)
                # a saturated metro also has less LOCAL behind-the-meter
                # headroom left → down-weight btm (feeds excess) up to -40%, so
                # excess_power_score de-clones in the same (correct) direction
                # rather than staying ISO-identical.
                _btm = metrics.get("btm_headroom_mw")
                if _btm is not None:
                    metrics["btm_headroom_mw"] = round(float(_btm) * (1.0 - 0.40 * _sat), 1)
                metrics["_saturation_adjusted"] = True
        except Exception:
            pass

    # r65 (2026-06-02): attach honest provenance label, derived from the
    # ACTUAL code path above (not the region). Only queue_wait_months,
    # queue_capacity_mw and gen_additions_12mo_mw can ever come from a live
    # table (interconnection_queue / capacity_pipeline). Every other score
    # input is filled from the static iso_defaults / slug_overrides dicts.
    #   - "live"             → every populated score input came from a live src
    #   - "modeled_estimate" → none did (pure dict-based estimate)
    #   - "mixed"            → some live, some modeled (the common real case:
    #                          live queue data + modeled reserve margin etc.)
    # NOTE: this is metadata only. It is intentionally stored under a
    # non-numeric key so the scoring formulas (which .get() specific numeric
    # keys) never see it and verdicts/scores are byte-identical.
    _score_input_keys = (
        "queue_wait_months", "queue_capacity_mw", "reserve_margin_pct",
        "gen_additions_12mo_mw", "curtailment_pct", "stranded_capacity_mw",
        "queue_approval_rate_pct", "btm_headroom_mw", "demand_growth_yoy_pct",
    )
    _populated = [k for k in _score_input_keys if metrics.get(k) is not None]
    _live_used = sorted(k for k in _populated if k in _live_fields)
    _modeled_used = sorted(k for k in _populated if k not in _live_fields)
    # ★Per-ISO, not one literal for all 323 markets. See modeled_source_for().
    _MODELED_SOURCE = modeled_source_for(iso, _iso_default_matched)
    if _live_used and not _modeled_used:
        data_basis = {"data_basis": "live"}
    elif _live_used and _modeled_used:
        data_basis = {
            "data_basis": "mixed",
            "data_basis_source": _MODELED_SOURCE,
            "data_basis_note": (
                "live: " + ", ".join(_live_used) + "; "
                "modeled: " + ", ".join(_modeled_used)
            ),
        }
    else:
        data_basis = {
            "data_basis": "modeled_estimate",
            "data_basis_source": _MODELED_SOURCE,
            # ★2026-08-11: modeled-only rows carried NO note, so a reader knew
            # the row was modeled but not WHICH inputs. London and Johor are
            # exactly the markets an analyst quotes; name the fields.
            "data_basis_note": "modeled: " + ", ".join(_modeled_used),
        }
    # r67 (2026-06-02): when reserve_margin_pct came from a live grid_telemetry
    # read, expose the measured basis so the honesty is auditable on every
    # surface that prints data_basis_json — observed_at, real headroom, the
    # modeled anchor it adjusted, and the upstream source. Per-field provenance
    # for the reserve margin specifically (the field most consumers care about).
    _lg = metrics.get("_live_grid")
    if _lg:
        # Honest label: the value is a BLEND — the modeled planning-reserve
        # anchor adjusted by a bounded (±6pp) live operating-reserve delta from
        # grid_telemetry — NOT a raw measured reserve margin. The full blend
        # (modeled anchor + operating reserve + effective) is exposed below.
        data_basis["reserve_margin_basis"] = "modeled_anchor_adjusted_by_live_telemetry"
        data_basis["reserve_margin_live"] = _lg
    # r-declone (2026-07-02): expose the per-market saturation adjustment so
    # the de-cloned queue_wait/demand_growth are auditable, not silent.
    # r-declone-2 (2026-07-17): also expose provider diversity, the match
    # scope (US city+state vs intl city pooling) and a plain-language
    # `differentiation` note so every per-market delta from the ISO anchor is
    # reproducible from this row alone. When a market has NO local footprint
    # rows we say so explicitly — identical-to-ISO-baseline is then a
    # documented fact, not a silent clone.
    if metrics.get("_saturation_adjusted"):
        data_basis["local_saturation"] = {
            "index": metrics.get("_saturation_index"),
            "facility_count": metrics.get("local_facility_count"),
            "operational_mw": metrics.get("local_operational_mw"),
            "pipeline_mw": metrics.get("local_pipeline_mw"),
            "unclassified_mw": metrics.get("local_unclassified_mw"),
            "provider_count": metrics.get("local_provider_count"),
            "match_scope": metrics.get("_saturation_scope"),
            # r-status-taxonomy (2026-07-29): a figure called operational is
            # only honest if the reader can see which rows produced it. Name
            # the table, name the filter, name the values — the same house
            # rule as capacity_basis / reserve_margin_basis. Plain lists and
            # strings only, so the dict stays json.dumps-able into the
            # data_basis_json JSONB column below.
            "source_table": "discovered_facilities",
            "status_basis": _status_basis(),
            "adjusts": ["queue_wait_months", "demand_growth_yoy_pct",
                        "btm_headroom_mw"],
            "basis": "iso_anchor_adjusted_by_local_dc_saturation",
            "differentiation": (
                "queue_wait ×(0.90+0.45×index), demand_growth +4pp×index, "
                "btm_headroom ×(1−0.40×index); index is log-scaled from this "
                "market's own discovered_facilities footprint "
                "(count/MW/pipeline/operators) — deterministic, no noise. "
                "operational_mw and pipeline_mw are DISJOINT status buckets "
                "(util/status_taxonomy), so no megawatt is counted twice"
            ),
        }
    elif not _override_applied and metrics.get("local_facility_count") == 0:
        data_basis["local_saturation"] = {
            "index": 0.0,
            "facility_count": 0,
            "match_scope": metrics.get("_saturation_scope"),
            "basis": "no_local_facility_footprint",
            "differentiation": (
                "no discovered_facilities rows for this market — ISO baseline "
                "used unchanged (identical values to other footprint-less "
                "markets in this ISO are real, not fabricated variation)"
            ),
        }
    # r-local-granularity (2026-07-25): the market's OWN infrastructure within
    # a radius of its coordinates — the input the intra-state and intl-default
    # clones were missing. Stamped into data_basis so every delta is auditable
    # from this row alone (house rule from local_saturation above).
    _loc = _local_infra_metrics(lat, lon)
    metrics.update(_loc)
    if any(v for v in _loc.values()):
        data_basis["local_grid_access"] = dict(
            _loc,
            radius_km={"substations": 40, "generation": 60, "dc_competition": 25},
            adjusts=["excess_power_score (<= +8)", "constraint_score (<= +6)"],
            basis="own_footprint_bbox_counts (HIFLD substations US; GEM "
                  "gem_power operating GLOBAL; discovered_facilities canonical)",
            differentiation=(
                "excess += 0.08 x clip(subs/300x55 + kv_class + gen/3000x20); "
                "constraint += 0.06 x clip(dc_count/40x100) — deterministic, "
                "additive-only, zero when no rows (absence never penalizes)"),
        )
    else:
        data_basis["local_grid_access"] = {
            "basis": "no_local_infrastructure_rows",
            "differentiation": ("no substations/gem_power/facilities rows in "
                                "radius — scores carry zero local adjustment "
                                "(identical-to-baseline is a documented fact, "
                                "not fabricated variation)"),
        }
    # ── r-ws3-signal-tier (2026-07-28): per-market SIGNAL QUALITY ───────────
    # data_basis already says live / mixed / modeled_estimate, but "mixed"
    # spans everything from one live adapter to all three, and NOTHING today
    # marks a market whose every score input is a hardcoded constant — it is
    # published with exactly the confidence of a live-fed one. Derive an
    # explicit tier from what actually happened this run, and emit the numeric
    # basis beside it (house rule: never an adjective without its number).
    #
    #   full    = all 3 live-capable adapters returned data
    #   partial = 1 or 2 did
    #   low     = none did, OR the ISO fell through to the WECC default (the
    #             modeled anchors are then Western-grid parameters for a grid
    #             that is not WECC — see the SOCO/FRCC post-mortem above)
    #
    # SCOPE, stated so no consumer can over-read it: "full" means every adapter
    # that CAN be live was live. It does NOT mean every score input is
    # measured. curtailment_pct, queue_approval_rate_pct, btm_headroom_mw,
    # stranded_capacity_mw and demand_growth_yoy_pct have no live source at all
    # and are always modeled; emergency_count_30d is never assigned anywhere in
    # this module and the scorer reads it as 0 (20% of constraint_score). Both
    # facts are stamped into signal_detail below.
    _live_adapters = sorted(k for k, v in _adapters.items() if v)
    _live_adapter_n = len(_live_adapters)
    if not _iso_default_matched:
        _tier = "low"
        _tier_reason = (
            "iso_default_fail_open: iso "
            + (repr(iso) if iso else "NULL")
            + " is not a key in iso_defaults, so every modeled anchor on this "
              "market is WECC's — Western-grid parameters for a grid that is "
              "not WECC")
    elif _live_adapter_n == 0:
        _tier = "low"
        _tier_reason = ("no_live_adapter_returned_data: every score input is a "
                        "modeled constant from iso_defaults / slug_overrides")
    elif _live_adapter_n >= len(_adapters):
        _tier = "full"
        _tier_reason = "all_live_capable_adapters_returned_data"
    else:
        _tier = "partial"
        _tier_reason = ("live_adapters_returned_data: "
                        + ", ".join(_live_adapters))
    data_basis["signal_tier"] = _tier
    data_basis["signal_detail"] = {
        "live_adapter_count": _live_adapter_n,
        "live_adapter_max": len(_adapters),
        "live_adapters": _live_adapters,
        "silent_adapters": sorted(k for k, v in _adapters.items() if not v),
        "live_fields": _live_used,
        "modeled_fields": _modeled_used,
        "iso_default_matched": _iso_default_matched,
        # r-ws3-methodology (2026-07-29): fields where a live adapter DID
        # answer but a hand-calibrated slug_override then replaced the value.
        # The adapter still counts toward the tier (it genuinely returned
        # data), but the field is no longer live, so the two facts are
        # reported separately instead of one of them being silently wrong.
        "override_replaced_live_fields": _override_replaced_live,
        "local_infra_rows": bool(any(_loc.values())),
        "reason": _tier_reason,
        "tier_rule": ("full = 3/3 live-capable adapters returned data; "
                      "partial = 1-2; low = 0, or the ISO fell through to the "
                      "WECC default"),
        # r-ws3-methodology: derived from the published input registry rather
        # than retyped here. These two lists were previously a hand-copy, i.e.
        # the same drift mechanism that let the static methodology page
        # describe a formula this file does not implement.
        "always_modeled_inputs": list(_METHOD_SIGNAL_TIER["always_modeled_inputs"]),
        "never_populated_inputs": list(_METHOD_SIGNAL_TIER["never_populated_inputs"]),
        "scope_note": ("'full' means every adapter that CAN be live was live — "
                       "NOT that every score input is measured. The fields in "
                       "always_modeled_inputs have no live source; "
                       "emergency_count_30d is never assigned and the scorer "
                       "reads it as 0, i.e. 20% of constraint_score is a "
                       "permanent zero for every market at every tier."),
        "adapter_null_semantics": ("an adapter reads absent when it returned no "
                                   "data; the queue and generator adapters "
                                   "cannot distinguish an empty result from a "
                                   "failed query, so silent_adapters is NOT an "
                                   "error count"),
    }
    # First-class key so the writer can persist the tier without re-parsing the
    # JSONB. Non-numeric → compute_constraint_score / compute_excess_power_score
    # (which .get() only the documented numeric keys) cannot see it, so every
    # score and verdict stays byte-identical.
    metrics["signal_tier"] = _tier
    # r-ws3-methodology (2026-07-29): stamp the method that produced these
    # inputs, so a later step change in the published series is attributable.
    # Non-numeric, same as signal_tier → invisible to both scorers.
    data_basis["method_version"] = DCPI_METHOD_VERSION
    data_basis["method_doc"] = "/api/v1/dcpi/methodology"
    metrics["method_version"] = DCPI_METHOD_VERSION
    metrics["data_basis"] = data_basis

    return metrics


def derive_top_signals(market: tuple, metrics: dict, c_score: float, e_score: float):
    """Top 3 risks + top 3 opportunities — one-line strings."""
    slug, name, state, iso, _, _ = market
    risks, opps = [], []

    qw = metrics.get("queue_wait_months") or 0
    rm = metrics.get("reserve_margin_pct") or 0
    cu = metrics.get("curtailment_pct") or 0
    ga = metrics.get("gen_additions_12mo_mw") or 0
    sc = metrics.get("stranded_capacity_mw") or 0
    bh = metrics.get("btm_headroom_mw") or 0
    qa = metrics.get("queue_approval_rate_pct") or 0
    dg = metrics.get("demand_growth_yoy_pct") or 0

    if qw >= 36: risks.append(f"{int(qw)}-month interconnection queue")
    if rm <= 14: risks.append(f"reserve margin only {rm:.1f}% (NERC floor 13%)")
    if dg >= 7: risks.append(f"{dg:.1f}% YoY demand growth — outpacing additions")
    if not risks: risks.append("Markets generally well-supplied; standard diligence")

    if cu >= 8: opps.append(f"{cu:.1f}% renewable curtailment — gigawatt-hours wasted, available for behind-the-meter")
    if sc >= 200: opps.append(f"{int(sc)} MW stranded interconnection at retiring plants")
    if bh >= 500: opps.append(f"{int(bh)} MW behind-the-meter industrial headroom")
    if rm >= 22: opps.append(f"reserve margin {rm:.1f}% — capacity available right now")
    if ga >= 2000: opps.append(f"{int(ga)} MW additions queued <12mo")
    if qa >= 65: opps.append(f"{int(qa)}% queue approval rate — fast-track candidates")
    if not opps:
        opps.append("Standard market — score reflects typical conditions for this ISO")

    return risks[:3], opps[:3]


# ---------------------------------------------------------------------------
# Recompute (called by cron + manual)
# ---------------------------------------------------------------------------
def recompute_all_scores(source: str = "manual",
                          offset: int = 0,
                          limit: int | None = None) -> dict:
    """Phase ZZ (2026-05-16): chunked execution. Single-shot recompute of
    200+ markets exceeded the cron's 120s timeout, so only ~30 markets
    completed each run. New `offset`+`limit` params let the cron drive
    the recompute in 3 chunks of ~100 markets, each finishing well under
    timeout. With no params (offset=0, limit=None), behavior is identical
    to the old single-shot for back-compat.
    """
    _ensure_tables()
    started = datetime.datetime.now(datetime.timezone.utc)
    scored = 0
    errors = 0
    error_notes = []
    chunk_label = (f" chunk[{offset}:{offset + (limit or 0)}]"
                   if limit else "")

    with _conn() as c, c.cursor() as cur:
        cur.execute("INSERT INTO dcpi_runs (started_at, source) VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING id",
                    (started, source + chunk_label))
        run_id = cur.fetchone()[0]
        c.commit()

    # Phase SS (2026-05-14): one-time dedup before scoring. The recompute
    # had been dying every run with "UniqueViolation on
    # market_power_scores_slug_key" despite an ON CONFLICT clause — which
    # only happens when the live table has accumulated DUPLICATE
    # market_slug rows (so the slug uniqueness the ON CONFLICT relies on
    # isn't actually enforced). Collapse to the newest row per slug so
    # reads and the upsert below are sane. Best-effort — never blocks the
    # recompute; scores stayed frozen at a 3-day-stale snapshot until this.
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                DELETE FROM market_power_scores
                WHERE id NOT IN (
                    SELECT MAX(id) FROM market_power_scores GROUP BY market_slug
                )
            """)
            c.commit()
    except Exception as _dedup_err:
        print(f"[dcpi] recompute dedup skipped: {_dedup_err}")

    # r-period-slug (2026-07-06): collapse malformed period-slug duplicates.
    # 'st.-louis' is a soft-404 duplicate of the canonical 'st-louis' (same
    # market_name 'St. Louis', two rows) — it was born because the CTE slug
    # (LOWER(city)) + the .replace(" ","-") clean-up never stripped the period.
    # Delete any period-containing market_slug whose '-'-normalized twin ALSO
    # exists as a row, so a market that ONLY has a period form is never
    # orphaned. The _load_markets_dynamic write guard (period-strip) keeps the
    # deleted row from being re-created on this same run. Best-effort — never
    # blocks the recompute.
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                DELETE FROM market_power_scores
                 WHERE market_slug LIKE '%.%'
                   AND EXISTS (
                       SELECT 1 FROM market_power_scores t
                        WHERE t.market_slug = REPLACE(market_power_scores.market_slug, '.', '')
                   )
            """)
            c.commit()
    except Exception as _pslug_err:
        print(f"[dcpi] period-slug collapse skipped: {_pslug_err}")

    # r-portland-canon (2026-08-02): self-healing rename for
    # _CITY_MARKET_DISAMBIGUATION rows (st.-louis pattern). A pre-existing
    # bare-city row (e.g. 'portland' = Portland, ME) is RENAMED to its
    # disambiguated slug/name when the target slug has no row yet, or
    # DELETED when the disambiguated row already exists — so the collision
    # can never orphan the market, and a bare twin can't survive a deploy
    # gap or a hand insert. The loader override keeps the bare form from
    # being re-minted on this same run. Best-effort — never blocks the
    # recompute.
    try:
        with _conn() as c, c.cursor() as cur:
            for (_oslug, _ostate), (_nslug, _nname) in \
                    sorted(_CITY_MARKET_DISAMBIGUATION.items()):
                cur.execute(
                    "SELECT 1 FROM market_power_scores WHERE market_slug = %s"
                    " LIMIT 1", (_nslug,))
                if cur.fetchone():
                    cur.execute(
                        "DELETE FROM market_power_scores"
                        " WHERE market_slug = %s AND state = %s",
                        (_oslug, _ostate))
                else:
                    cur.execute(
                        "UPDATE market_power_scores"
                        "   SET market_slug = %s, market_name = %s"
                        " WHERE market_slug = %s AND state = %s",
                        (_nslug, _nname, _oslug, _ostate))
            c.commit()
    except Exception as _dis_err:
        print(f"[dcpi] city-market disambiguation skipped: {_dis_err}")

    # Phase QQ+3 (2026-05-13): use MARKETS only (canonical 6-tuple shape).
    # Previously: `_dcpi_dynamic_markets() or MARKETS`. The dynamic helper
    # returns 9-key dicts (slug, name, cities, state, country, facility_count,
    # pipeline_mw, operational_mw, avg_kwh_usd), but the unpack on the next
    # line expects 6 tuple positions (slug, name, state, iso, lat, lon).
    # When the dynamic call succeeded, every iteration threw ValueError out
    # of the for-loop (it's OUTSIDE the try/except below), bubbled up to
    # api_recompute, and either 500'd or produced spurious values that the
    # downstream INSERT silently rejected. End result: market_power_scores
    # hadn't been updated in 45h despite the daily cron firing successfully
    # — the truth endpoint /api/v1/system/loops caught it as dcpi_recompute
    # stale=45.1h.
    #
    # MARKETS itself is `_load_markets_dynamic() or _MARKETS_HARDCODED` —
    # both of those return 6-tuples, so unpacking is safe.
    # Phase ZZ: chunked slice. When the cron passes offset+limit, only
    # that slice runs in this invocation. Total coverage achieved by
    # running multiple chunks per cron tick (see dcpi-daily.yml).
    # r70 (2026-06-03): rebuild the market list FRESH per recompute run instead
    # of slicing the module-import-time MARKETS global. If the dynamic-market DB
    # load failed at import (DATABASE_URL not yet set / transient pool error),
    # MARKETS was poisoned to ~50 hardcoded markets, so chunks 2-4 (offset>=100)
    # sliced empty and 180+ markets froze stale. Rebuilding here recovers the
    # full 200+ dynamic set on every cron tick.
    try:
        _markets = _build_markets_list() or MARKETS
    except Exception:
        _markets = MARKETS
    _slice = _markets[offset:(offset + limit)] if limit else _markets[offset:]
    for m in _slice:
        slug, name, state, iso, lat, lon = m
        try:
            metrics = gather_metrics_for_market(m)
            c_score = compute_constraint_score(metrics)
            e_score = compute_excess_power_score(metrics)
            ttp = estimate_time_to_power(metrics)
            verdict = derive_verdict(c_score, e_score)
            risks, opps = derive_top_signals(m, metrics, c_score, e_score)

            with _conn() as c, c.cursor() as cur:
                # r-provenance-writer (2026-08-08): the statement now lives in
                # util/dcpi_score_row.py and is shared with the two writers in
                # routes/dcpi_freshness_watchdog.py. Those two were hand-copies
                # of this one that stopped tracking it: neither stamped the
                # provenance triple, and on 2026-08-08 the gap-filler published
                # 8 new markets with method_version, signal_tier and
                # data_basis_json all NULL. Everything this call does — the
                # UPDATE-or-INSERT shape (Phase SS), the COALESCE on lat/lon
                # (r-market-resolve-guard), the derived iso_type
                # (r-iso-taxonomy), reading the triple from `metrics` rather
                # than the module constant (r-ws3-methodology) and
                # published=TRUE on a fresh row (r58) — is documented there.
                upsert_scored_market(cur, m, metrics, c_score, e_score, ttp,
                                     verdict, risks, opps, publish=True)
                c.commit()
            scored += 1
        except Exception as e:
            errors += 1
            error_notes.append(f"{slug}: {type(e).__name__}: {str(e)[:120]}")

    # r-twin-unpublish (2026-07-28): retire the redundant alias-twin rows.
    #
    # r-twin-dedup removed these slugs from the scoring universe, and its
    # comment says the redundant row "is unpublished separately" — that step
    # did not exist. Result: seven rows stayed published and simply stopped
    # being updated (frozen 2026-07-19, iso_type NULL, still ranked) while
    # their canonical twins were recomputed daily. Because they are excluded
    # from MARKETS, no offset/limit chunk can ever reach them — a full sweep
    # reports success and leaves them stale forever.
    #
    # Runs every recompute so it self-heals rather than needing a one-off
    # UPDATE that dchub_self_heal's blanket re-publish would undo anyway.
    # ONLY unpublishes a twin whose canonical row actually exists, so this
    # can never orphan a market that has nowhere to redirect to.
    try:
        from util.market_aliases import REDUNDANT_TWIN_SLUGS
        _retired = []
        with _conn() as c, c.cursor() as cur:
            # Seven rows — a plain parameterized statement per pair. Deliberately
            # NOT a VALUES join built with %-formatting: mixing Python % into a
            # string that also carries %s placeholders is how a literal % ends
            # up in SQL (see the psycopg2 percent trap), and there is no
            # performance case for cleverness at this size.
            for _twin in sorted(REDUNDANT_TWIN_SLUGS):
                _canon = DCPI_METRO_ALIASES.get(_twin)
                if not _canon:
                    continue
                cur.execute("""
                    UPDATE market_power_scores t
                       SET published = false
                     WHERE t.market_slug = %s
                       AND COALESCE(t.published, true) = true
                       AND EXISTS (
                             SELECT 1 FROM market_power_scores c2
                              WHERE c2.market_slug = %s
                                AND COALESCE(c2.published, true) = true
                           )
                    RETURNING t.market_slug
                """, (_twin, _canon))
                _retired += [r[0] for r in cur.fetchall()]
            c.commit()
        if _retired:
            print(f"[dcpi] r-twin-unpublish: retired {len(_retired)} redundant "
                  f"alias-twin rows: {_retired}", flush=True)
    except Exception as e:
        # Never fail a recompute over this — the scores are the product.
        print(f"[dcpi] r-twin-unpublish: skipped (non-fatal): {e}", flush=True)

    with _conn() as c, c.cursor() as cur:
        cur.execute("""
            UPDATE dcpi_runs
               SET finished_at = NOW(), markets_scored = %s, error_count = %s, notes = %s
             WHERE id = %s
        """, (scored, errors, "\n".join(error_notes)[:2000], run_id))
        c.commit()

    return {"run_id": run_id, "markets_scored": scored, "errors": errors,
            "error_notes": error_notes[:5]}


# ---------------------------------------------------------------------------
# r-gate-everywhere (2026-06-27): ONE server-side DCPI gate, used by EVERY
# emitter (JSON endpoints + SSR pages + JSON-LD/og/meta). Extracted VERBATIM
# from the proven api_scores() gate so paid access stays identical and the gate
# can never drift endpoint-by-endpoint again (the root cause of the leak audit:
# one gate, copied nowhere). The numeric DCPI scores are the PAID product;
# non-paid callers get name/slug/geo/ISO + the BUILD/CAUTION/AVOID verdict only.
# ---------------------------------------------------------------------------
_DCPI_PLAN_RANK = {"anonymous": 0, "anon": 0, "free": 0, "identified": 1,
                   "starter": 2, "developer": 3, "pro": 4, "founding": 4,
                   "enterprise": 5, "admin": 6, "internal": 6}
_DCPI_PAID_PLANS = {"starter", "developer", "pro", "founding", "enterprise",
                    "admin", "internal"}
# Core paid fields (the scores) masked on every surface.
_DCPI_MASK_FIELDS = ("composite_score", "excess_power_score", "constraint_score",
                     "time_to_power_months", "top_risks_json", "top_opportunities_json")
# Raw grid metrics some endpoints also expose (history / iso-comparison /
# single-market / movers) — masked when extra=True.
_DCPI_MASK_EXTRA = ("queue_wait_months", "reserve_margin_pct", "stranded_capacity_mw",
                    "curtailment_pct", "avg_kwh_cents", "quality_score",
                    "now_excess", "now_constraint", "prev_excess", "prev_constraint",
                    "excess_delta_7d", "constraint_delta_7d",
                    "avg_excess", "avg_constraint", "total_stranded_capacity_mw",
                    "total_queue_capacity_mw", "total_gen_additions_12mo_mw",
                    "avg_queue_wait_months", "avg_reserve_margin_pct", "avg_kwh_cents",
                    # r-one-ttp (2026-08-08): the ISO-level average of the PAID
                    # time_to_power_months column is itself paid, exactly like
                    # the per-market field two lines up in _DCPI_MASK_FIELDS.
                    "avg_time_to_power_months")


def _dcpi_caller_plan():
    """Effective plan name (lowercased), cookie-aware + admin-key aware.
    Mirrors the proven api_scores() resolution EXACTLY (never downgrades a paid
    caller; takes the more-privileged of the JWT/key tier and the website
    session-cookie tier)."""
    _plan = "anonymous"
    try:
        from util.tier_gate import resolve_tier
        _t, _ctx = resolve_tier()
        _plan = (_ctx.get("plan") or _t.name).lower()
    except Exception:
        pass
    try:
        from map_tier_gating import _detect_caller_tier

        def _dec(_tok):
            try:
                import jwt as _j
                from main import JWT_SECRET
                return _j.decode(_tok, JWT_SECRET, algorithms=["HS256"])
            except Exception:
                return None
        _ct, _ = _detect_caller_tier(decode_jwt_func=_dec)
        _ct = (_ct or "anon").lower()
        if _DCPI_PLAN_RANK.get(_ct, -1) > _DCPI_PLAN_RANK.get(_plan, -1):
            _plan = _ct
    except Exception:
        pass
    # Admin-key bypass (QA harness / internal probes) — never leaks to anon.
    try:
        _admin_key_env = (os.environ.get("DCHUB_ADMIN_KEY")
                          or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
        if _admin_key_env:
            _sent = (request.headers.get("X-Admin-Key")
                     or request.args.get("admin_key") or "").strip()
            if _sent and _sent == _admin_key_env:
                _plan = "admin"
    except Exception:
        pass
    return _plan


def _dcpi_is_paid(plan=None):
    """True if the caller may see the numeric scores."""
    return (plan if plan is not None else _dcpi_caller_plan()) in _DCPI_PAID_PLANS


_DCPI_SINGLE_MARKET_FREE = True  # r-free-per-market (2026-07-03)
def _dcpi_single_market_paid():
    """Policy 2026-07-03 (free-per-market, paid-bulk): a SINGLE-market surface
    (/dcpi/<slug>, /api/v1/dcpi/scores/<slug>, its og/embed) shows the numeric
    DCPI scores to everyone — one market is free to cite; the BULK/all-market
    endpoints + CSV export stay Pro-gated (they keep calling _dcpi_is_paid()).
    Flip _DCPI_SINGLE_MARKET_FREE to revert."""
    return True if _DCPI_SINGLE_MARKET_FREE else _dcpi_is_paid()


def _dcpi_mask_rows(rows, *, extra=False, paid=None):
    """If the caller is NOT paid, null the paid fields on each row dict and set
    locked=True. Returns (rows, gated_bool). Operates on copies (never mutates
    the caller's row objects). Lists ('*_json') become [] so templates iterating
    them render empty rather than crash."""
    if paid is None:
        paid = _dcpi_is_paid()
    if paid:
        return rows, False
    fields = _DCPI_MASK_FIELDS + (_DCPI_MASK_EXTRA if extra else ())
    out = []
    for _r in rows:
        _r = dict(_r)
        for _k in fields:
            if _k in _r:
                _r[_k] = [] if str(_k).endswith("_json") else None
        _r["locked"] = True
        out.append(_r)
    return out, True


def _dcpi_gated_meta(total_available=None):
    """Standard _gated payload metadata (matches api_scores())."""
    m = {"_gated": True, "_preview_only": True, "_required_tier": "pro",
         "_locked_fields": list(_DCPI_MASK_FIELDS),
         "_signup_url": "https://dchub.cloud/pricing",
         "_playground_url": "https://dchub.cloud/playground",
         "_upgrade_cta": ("BUILD/CAUTION/AVOID verdicts + the market list are free. "
                          "The numeric DCPI scores (composite, excess-power, "
                          "grid-constraint, time-to-power) and risk/opportunity "
                          "detail are Pro — unlock all markets at "
                          "https://dchub.cloud/pricing.")}
    if total_available is not None:
        m["_total_available"] = total_available
    try:
        from routes.email_capture import build_agent_coaching
        m.update(build_agent_coaching(
            "get_market_dcpi_rank",
            "Retry GET /api/v1/dcpi/scores with header X-API-Key: <api_key>"))
    except Exception:
        pass
    return m


# ---------------------------------------------------------------------------
# JSON endpoints
# ---------------------------------------------------------------------------
# r-poolfix2 (2026-07-11): in-process memo of the PROCESSED score rows.
# /api/v1/dcpi/scores ran the SELECT DISTINCT ON full scan + Python
# post-processing on EVERY origin hit (observed 0.4-2.3s, 6 hits in one 18s
# self-probe window on 2026-07-11) — a steady feeder of web pool checkouts.
# The base rows are caller-INDEPENDENT: filters, sorting, row-capping and
# tier masking all happen downstream in api_scores on copies, so one shared
# 300s memo is leak-safe. Rows are dict-copied on every read so downstream
# in-place edits can never poison the cache. DCPI recomputes 4x/day; the
# edge already caches this path at 1800s, so 300s in-process is strictly
# fresher than what anon callers could already see.
_SCORES_ROWS_CACHE: dict = {}
_SCORES_ROWS_TTL_S = 300


def _fetch_scores_rows() -> list:
    """The DB fetch + post-processing api_scores always ran inline.
    Caller-independent by construction — no request state touched."""
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT DISTINCT ON (market_slug)
                market_slug, market_name, state, iso, latitude, longitude,
                constraint_score, excess_power_score, time_to_power_months,
                verdict,
                top_risks_json, top_opportunities_json,
                data_basis_json,
                signal_tier,
                computed_at
            FROM market_power_scores WHERE published = true ORDER BY market_slug, computed_at DESC
        """)
        rows = cur.fetchall()
    for r in rows:
        if r.get("computed_at"):
            r["computed_at"] = r["computed_at"].isoformat()
        # r65 (2026-06-02): surface the provenance label read-only. The JSONB
        # column comes back as a dict (or None for legacy/LITE-scored rows);
        # flatten it onto the output object. Default to modeled_estimate when
        # absent — those rows are dict-derived by construction (no live source).
        _db = r.pop("data_basis_json", None)
        if isinstance(_db, dict) and _db.get("data_basis"):
            r["data_basis"] = _db.get("data_basis")
            if _db.get("data_basis_source"):
                r["data_basis_source"] = _db.get("data_basis_source")
            if _db.get("data_basis_note"):
                r["data_basis_note"] = _db.get("data_basis_note")
        else:
            r["data_basis"] = "modeled_estimate"
            # ★2026-08-11: was a hard-coded ENTSO-E/AEMO/EirGrid literal, so a
            # row with NO recorded basis was handed a specific FOREIGN
            # attribution it never had. Derive from the row's own ISO;
            # modeled_source_for() falls back to a string that names no
            # operator when the ISO is absent or unrecognised.
            r["data_basis_source"] = modeled_source_for(r.get("iso"))
        # r-ws3-signal-tier (2026-07-28): per-market signal quality, read-only.
        # NULL = the row's writer recorded none (rows predating the column, and
        # every row from the lite_recompute upsert). Emitted as null + an
        # explicit basis string, NEVER coerced to "low" — that would publish a
        # measurement we did not take.
        _st = r.get("signal_tier") or None
        r["signal_tier"] = _st
        # r-ws3-methodology (2026-07-29): the previous basis string blamed "the
        # lite recompute path" for unrecorded tiers. That claim is FALSE and
        # was published on every masked row: POST /api/v1/dcpi/lite-recompute
        # iterates MARKETS, which holds tuples, and raises AttributeError on
        # every market inside its own swallow-all — it has written zero rows.
        # A wrong reason is worse than no reason, so name only what is true.
        r["signal_tier_basis"] = _signal_tier_basis(_st)
        # r-ws3-methodology: which version of the scoring method produced this
        # row. NULL on rows written before version stamping — surfaced as
        # unknown, never backfilled to the current version, which would
        # backdate a claim we cannot make. See /api/v1/dcpi/methodology.
        r["method_version"] = r.get("method_version") or None
        r["method_doc"] = "/api/v1/dcpi/methodology"
        # r41-dcpi-composite (2026-05-25): include a sortable single-number
        # composite_score so agents can rank markets without recombining
        # the three components themselves. See derive_composite_score().
        # r41.1: pass verdict so LOW_SIGNAL markets don't outrank trusted ones.
        r["composite_score"] = derive_composite_score(
            r.get("excess_power_score"),
            r.get("constraint_score"),
            r.get("time_to_power_months"),
            r.get("verdict"),
        )
    return rows


def _scores_rows_cached() -> list:
    """Fetch + post-process the published DCPI score rows, memoized 300s.
    Returns fresh dict copies — callers may mutate freely."""
    import time as _t
    _now = _t.time()
    _hit = _SCORES_ROWS_CACHE.get("rows")
    if _hit is not None and (_now - _hit[0]) < _SCORES_ROWS_TTL_S:
        return [dict(r) for r in _hit[1]]
    rows = _fetch_scores_rows()
    _SCORES_ROWS_CACHE["rows"] = (_now, rows)
    return [dict(r) for r in rows]


@dcpi_bp.route("/api/v1/dcpi/scores", methods=["GET"])
def api_scores():
    """List DCPI scores. Query params:
        sort=excess|constraint|time_to_power  (default excess)
        sort_by=<same as sort, alt name>
        verdict=BUILD|CAUTION|AVOID|LOW_SIGNAL  (filter, Phase MM 2026-05-15)
        iso=<iso_code>  (filter, Phase MM)
        state=<state_code>  (filter, Phase MM)
        limit=N  (slice, Phase MM)
    Phase MM Bundle 9 caught in QA sweep: ?verdict= was being IGNORED —
    all 300+ markets were returned regardless of filter. Fix shipped here.
    """
    _ensure_tables()
    sort_by = (request.args.get("sort") or request.args.get("sort_by")
               or "excess").lower().strip()
    verdict_filter = (request.args.get("verdict") or "").strip().upper() or None
    iso_filter = (request.args.get("iso") or "").strip().upper() or None
    state_filter = (request.args.get("state") or "").strip().upper() or None
    try:
        limit = int(request.args.get("limit") or 0)
    except Exception:
        limit = 0

    rows = _scores_rows_cached()

    # Phase MM Bundle 9: apply filters (server-side instead of client-side).
    if verdict_filter:
        rows = [r for r in rows if (r.get("verdict") or "").upper() == verdict_filter]
    if iso_filter:
        rows = [r for r in rows if (r.get("iso") or "").upper() == iso_filter]
    if state_filter:
        rows = [r for r in rows if (r.get("state") or "").upper() == state_filter]

    if sort_by in ("constraint", "constraint_score"):
        rows.sort(key=lambda r: -(r.get("constraint_score") or 0))
    elif sort_by in ("time_to_power", "time_to_power_months", "ttp"):
        rows.sort(key=lambda r: (r.get("time_to_power_months") or 1e9))
    elif sort_by in ("composite", "composite_score", "rank"):
        # r41-dcpi-composite: default rank for agents asking "top markets"
        rows.sort(key=lambda r: -(r.get("composite_score") or 0))
    else:
        rows.sort(key=lambda r: -(r.get("excess_power_score") or 0))

    if limit > 0:
        rows = rows[:limit]

    # Phase YY (2026-05-16): proper caching. DCPI scores recompute on a
    # daily cron — there's no reason to hit Neon on every request. The
    # endpoint was clocking 1.7s for 2 rows pre-fix because the SELECT
    # DISTINCT ON does a full scan + sort. ETag based on max(computed_at)
    # so any actual recompute busts the cache; otherwise 304 short-circuits.
    max_ts = ""
    if rows:
        # rows is sorted by either constraint/excess/ttp, not by computed_at,
        # but each row has its own computed_at; the table-level max is fine.
        try:
            max_ts = max((r.get("computed_at") or "") for r in rows)
        except Exception:
            max_ts = ""
    import hashlib as _hl
    etag_src = f"{len(rows)}|{max_ts}|{verdict_filter}|{iso_filter}|{state_filter}|{sort_by}|{limit}"
    etag = '"' + _hl.md5(etag_src.encode()).hexdigest()[:16] + '"'

    if_none_match = request.headers.get("If-None-Match", "")
    if if_none_match and if_none_match == etag:
        from flask import Response as _Resp
        resp = _Resp(status=304)
        resp.headers["ETag"] = etag
        resp.headers["Cache-Control"] = "public, max-age=120, stale-while-revalidate=300"
        return resp

    # Phase WW (2026-05-17) — soft-paywall the bulk dump.
    # r43-E (2026-05-27): operator audit caught the `not limit` bypass —
    # anon callers were adding ?limit=500 and walking away with all 232
    # markets (102KB) for free. That's why Pro Map sign-ups were zero
    # despite high demand. Now: anon cap is enforced REGARDLESS of
    # ?limit parameter. Single-market lookup (/api/v1/dcpi/scores/<slug>)
    # stays FREE — that's the discovery hook for AI agents + journalists.
    # Phase WW + r43-E: anon row cap.
    # 2026-05-30 (revenue): the precise DCPI NUMBERS (composite / excess /
    # constraint / time-to-power + risk/opportunity detail) are the PAID
    # product. Non-paid callers (anon / free / identified) get the catalog —
    # market name, state, ISO, geo, and the BUILD/CAUTION/AVOID verdict as a
    # teaser — with the numeric gold masked SERVER-SIDE so it can't be
    # scraped, unblurred, or seen on mobile. resolve_tier only reads
    # X-API-Key / Bearer JWT, NOT the website's session cookie, so a logged-in
    # web user looked anonymous to it (that's why an enterprise user saw
    # "Upgrade to Pro"). We add the cookie-aware resolver /pockets uses and
    # take the MORE privileged plan — never downgrades a paid caller.
    _PREVIEW_CAP = 10
    _PAID_PLANS = {"starter", "developer", "pro", "founding", "enterprise",
                   "admin", "internal"}
    _PLAN_RANK = {"anonymous": 0, "anon": 0, "free": 0, "identified": 1,
                  "starter": 2, "developer": 3, "pro": 4, "founding": 4,
                  "enterprise": 5, "admin": 6, "internal": 6}
    _MASK_FIELDS = ("composite_score", "excess_power_score", "constraint_score",
                    "time_to_power_months", "top_risks_json", "top_opportunities_json")
    _gated = False
    _total_rows = len(rows)
    _plan = "anonymous"
    try:
        from util.tier_gate import resolve_tier
        _t, _ctx = resolve_tier()
        _plan = (_ctx.get("plan") or _t.name).lower()
    except Exception:
        pass
    try:
        from map_tier_gating import _detect_caller_tier
        def _dec(_tok):
            try:
                import jwt as _j
                from main import JWT_SECRET
                return _j.decode(_tok, JWT_SECRET, algorithms=["HS256"])
            except Exception:
                return None
        _ct, _ = _detect_caller_tier(decode_jwt_func=_dec)
        _ct = (_ct or "anon").lower()
        if _PLAN_RANK.get(_ct, -1) > _PLAN_RANK.get(_plan, -1):
            _plan = _ct
    except Exception:
        pass
    _paid = _plan in _PAID_PLANS

    # Admin-key bypass (2026-06-07): the State-of-2026 QA harness and other
    # internal probes (brain consistency radar, freshness watchdog) need the
    # REAL row total to fact-check the "300+ markets" claim. X-Admin-Key
    # matching DCHUB_ADMIN_KEY is the same gate as funnel_health.admin_*
    # and state_of_2026_precheck — and the keys never leak to anonymous
    # callers (only the harness has them). Treats admin as full-tier:
    # NO row cap, NO masking, AND surfaces _total_available unconditionally
    # so the harness has a single field to read regardless of plan.
    _admin_bypass = False
    try:
        _admin_key_env = (os.environ.get("DCHUB_ADMIN_KEY")
                          or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
        if _admin_key_env:
            _sent = (request.headers.get("X-Admin-Key")
                     or request.args.get("admin_key") or "").strip()
            if _sent and _sent == _admin_key_env:
                _admin_bypass = True
                _paid = True
                _plan = "admin"
    except Exception:
        pass

    if not _paid:
        # r-free-breadth (2026-06-27): TRULY-anonymous (no key) is row-capped to
        # the preview; ANY keyed caller (free/identified) keeps the FULL catalog
        # (all market names + verdicts) with the numbers masked — that's the
        # reason to sign up free. Was gated on plan-rank<1, which ALSO capped a
        # free key (resolves to rank-0 'free'), making anon and free identical.
        # Gate on key PRESENCE instead.
        _has_key = bool(request.headers.get("X-API-Key")
                        or (request.headers.get("Authorization") or "").lower().startswith("bearer ")
                        or request.args.get("api_key") or request.args.get("key"))
        if (not _has_key) and _total_rows > _PREVIEW_CAP:
            rows = rows[:_PREVIEW_CAP]
        _masked = []
        for _r in rows:
            _r = dict(_r)
            for _k in _MASK_FIELDS:
                if _k in _r:
                    _r[_k] = None
            _r["locked"] = True
            _masked.append(_r)
        rows = _masked
        _gated = True

    # r-ws3-signal-tier (2026-07-28): top-level as-of + the signal-tier mix.
    # BOTH are computed over the rows ACTUALLY RETURNED — anon callers are
    # capped at _PREVIEW_CAP and every filter above has already been applied —
    # so the key says `_returned`. Reading it as a catalog-wide figure would be
    # wrong; the catalog total is _total_available. computed_at is already
    # isoformat by here, so max() is a lexicographic max over ISO-8601 strings.
    _as_of = max((r.get("computed_at") or "" for r in rows), default="") or None
    _tier_counts = {"full": 0, "partial": 0, "low": 0, "unrecorded": 0}
    for _r in rows:
        _k = _r.get("signal_tier") or "unrecorded"
        _tier_counts[_k] = _tier_counts.get(_k, 0) + 1
    payload = {"scores": rows, "count": len(rows), "sort": sort_by,
               "as_of": _as_of,
               "signal_tier_counts_returned": _tier_counts,
               "signal_tier_note": (
                   "Per-market signal quality. full = all 3 live-capable "
                   "adapters (interconnect_queue, planned_generators, "
                   "grid_telemetry) returned data for that market; partial = "
                   "1-2; low = 0, or the market's ISO fell through to the WECC "
                   "default; unrecorded = the row's writer recorded no tier "
                   "(unknown, NOT low). 'full' does not mean every score input "
                   "is measured — see each row's data_basis."),
               "filters": {"verdict": verdict_filter, "iso": iso_filter,
                           "state": state_filter}}
    # ALWAYS surface _total_available for paid/admin callers so the QA
    # harness's narrative-claim verifier doesn't have to fall back to
    # len(scores) (which equals the page limit, not the universe). For
    # gated callers the field is set in the _gated branch below.
    if _paid:
        payload["_total_available"] = _total_rows
    if _admin_bypass:
        payload["_admin_bypass"] = True
    if _gated:
        payload["_gated"] = True
        payload["_preview_only"] = True
        payload["_total_available"] = _total_rows
        payload["_locked_fields"] = list(_MASK_FIELDS)
        payload["_required_tier"] = "pro"
        payload["_upgrade_cta"] = (
            f"Market list + BUILD/CAUTION/AVOID verdicts are free. The numeric "
            f"DCPI scores (composite, excess-power, grid-constraint, "
            f"time-to-power) and risk/opportunity detail are Pro — unlock all "
            f"{_total_rows} markets with scores at https://dchub.cloud/pricing."
        )
        payload["_signup_url"] = "https://dchub.cloud/pricing"
        payload["_playground_url"] = "https://dchub.cloud/playground"  # r80 #3: human can see what is gated, no signup
        # Conversion coaching: api_scores builds its gated payload INLINE (it does
        # NOT call _dcpi_gated_meta), so the shared claim_free_key/email_capture
        # bridge must be merged here too. Additive + fail-safe.
        try:
            from routes.email_capture import build_agent_coaching
            payload.update(build_agent_coaching(
                "get_market_dcpi_rank",
                "Retry GET /api/v1/dcpi/scores with header X-API-Key: <api_key>"))
        except Exception:
            pass

    resp = jsonify(**payload)
    resp.headers["ETag"] = etag
    resp.headers["Cache-Control"] = "public, max-age=120, stale-while-revalidate=300"
    return resp, 200


@dcpi_bp.route("/api/v1/dcpi/total", methods=["GET"])
def api_dcpi_total():
    """Admin-gated DCPI total market count.

    Returns the REAL row count from market_power_scores (no preview cap,
    no masking, no payload). Designed for the State-of-2026 QA harness's
    narrative-claim verifier and any other internal probe that just needs
    the "how many markets are scored?" number to fact-check public claims
    like "300+ DCPI markets".

    Auth: X-Admin-Key header OR ?admin_key= matching DCHUB_ADMIN_KEY
    (falls back to DCHUB_INTERNAL_KEY). Same gate as funnel_health and
    state_of_2026_precheck.

    Returns {ok, total, generated_at} on success, 401 otherwise.
    """
    _admin_key_env = (os.environ.get("DCHUB_ADMIN_KEY")
                      or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
    _sent = (request.headers.get("X-Admin-Key")
             or request.args.get("admin_key") or "").strip()
    if not _admin_key_env or _sent != _admin_key_env:
        return jsonify({"ok": False, "error": "admin_key required"}), 401

    _ensure_tables()
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT COUNT(DISTINCT market_slug)
                  FROM market_power_scores
                 WHERE published = true
            """)
            row = cur.fetchone()
            total = int(row[0]) if row and row[0] is not None else 0
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500

    from datetime import datetime as _dt, timezone as _tz
    return jsonify({
        "ok":           True,
        "total":        total,
        "source":       "market_power_scores (published=true, DISTINCT market_slug)",
        "generated_at": _dt.now(_tz.utc).isoformat(),
    }), 200


@dcpi_bp.route("/api/v1/dcpi/scores/<slug>", methods=["GET"])
def api_score_market(slug):
    _ensure_tables()
    # r47.43 (2026-05-27): metro→city alias resolution. The HTML route at
    # /dcpi/<slug> already 301-redirects metro slugs to their canonical city
    # (northern-virginia → ashburn, silicon-valley → santa-clara, etc.). The
    # API endpoint had no equivalent, so AI agents hitting the canonical
    # metro slug got "market not found" instead of the city's row. Match
    # the HTML behavior by trying the alias when the original slug misses;
    # surface BOTH the requested slug and the resolved canonical so the
    # caller knows what was returned.
    requested_slug = slug
    candidates = [slug]
    _low = slug.lower()
    # r-slugfix (2026-07-15): also accept the city+state slug that rank_markets
    # emits (LOWER(city)-LOWER(state), e.g. 'ashburn-va') by stripping a trailing
    # 2-letter state suffix to the bare city row ('ashburn'), mirroring the HTML
    # /dcpi route's existing strip. Fixes the rank_markets → get_market_dcpi_rank
    # 404 chain on the #1 market.
    if "-" in _low and len(_low.rsplit("-", 1)[1]) == 2:
        _stripped = _low.rsplit("-", 1)[0]
        if _stripped not in candidates:
            candidates.append(_stripped)
    for _cand_slug in list(candidates):
        _alias = DCPI_METRO_ALIASES.get(_cand_slug)
        if _alias and _alias not in candidates:
            candidates.append(_alias)
    # r-twin-unpublish (2026-07-28): same promotion as the HTML route — an
    # alias key with a leftover row of its own must still resolve to the
    # canonical market, so agents get the fresh row and `_canonical_slug`.
    candidates = _canonical_first(slug, candidates)
    row = None
    matched_slug = None
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        for cand in candidates:
            cur.execute("""
                SELECT * FROM market_power_scores
                 WHERE market_slug = %s
                 ORDER BY computed_at DESC LIMIT 1
            """, (cand,))
            row = cur.fetchone()
            if row:
                matched_slug = cand
                break
    if not row: return jsonify(error="market not found", slug=slug), 404
    # If we resolved via alias, tell the caller — preserves transparency for
    # programmatic clients building URLs from the canonical slug.
    if matched_slug and matched_slug != requested_slug:
        row["_requested_slug"] = requested_slug
        row["_canonical_slug"] = matched_slug
    if row.get("computed_at"): row["computed_at"] = row["computed_at"].isoformat()
    # r65 (2026-06-02): flatten the provenance label onto the output object.
    # SELECT * returns the raw data_basis_json (a dict via JSONB, or None for
    # legacy/LITE rows). Expose a consistent data_basis/data_basis_source pair;
    # default to modeled_estimate when absent (dict-derived by construction).
    _db = row.pop("data_basis_json", None)
    if isinstance(_db, dict) and _db.get("data_basis"):
        row["data_basis"] = _db.get("data_basis")
        if _db.get("data_basis_source"):
            row["data_basis_source"] = _db.get("data_basis_source")
        if _db.get("data_basis_note"):
            row["data_basis_note"] = _db.get("data_basis_note")
    else:
        row["data_basis"] = "modeled_estimate"
        # ★2026-08-11: same fix as the list surface above — no invented foreign
        # attribution for a row whose basis was never recorded.
        row["data_basis_source"] = modeled_source_for(row.get("iso"))
    # r-ws3-signal-tier (2026-07-28): SELECT * already returned the column.
    # NULL = the writer of this row recorded no tier (rows predating the column
    # + every lite_recompute row) → emit null plus the reason, never a
    # fabricated "low". These keys are ADDITIVE to the contract-locked v1
    # envelope, and MCP get_market_dcpi_rank passes this body through verbatim,
    # so that tool inherits the tier with no change of its own.
    _st = row.get("signal_tier") or None
    row["signal_tier"] = _st
    row["signal_tier_basis"] = _signal_tier_basis(_st)
    row["signal_tier_note"] = (
        "full = all 3 live-capable adapters (interconnect_queue, "
        "planned_generators, grid_telemetry) returned data; partial = 1-2; "
        "low = 0, or this market's ISO fell through to the WECC default. "
        "'full' does NOT mean every score input is measured — data_basis "
        "carries the per-field live/modeled split and the adapter counts.")
    # r41-dcpi-composite (2026-05-25): include sortable composite_score
    # alongside the existing component scores for consistency with /scores.
    # r41.1: verdict-aware so LOW_SIGNAL markets don't outrank trusted ones.
    row["composite_score"] = derive_composite_score(
        row.get("excess_power_score"),
        row.get("constraint_score"),
        row.get("time_to_power_months"),
        row.get("verdict"),
    )

    # r42h (2026-05-25): include the per-market analyst narrative when
    # the LLM is configured. Lets MCP-aware agents (Claude.ai, Claude
    # Code, anything calling /api/v1/dcpi/scores/<slug>) pull the same
    # interpretation that human readers get on the HTML page. ?narrative=0
    # opts out for cost-sensitive callers.
    from flask import request as _req
    if (_req.args.get("narrative") or "1") != "0":
        try:
            from routes.report_narrative import attach_market_narrative
            risks = row.get("top_risks_json") or []
            opps = row.get("top_opportunities_json") or []
            narr = attach_market_narrative(row, risks, opps)
            if narr:
                row["narrative"] = {
                    "text": narr,
                    "model": "claude-haiku-4-5-20251001",
                    "license": "CC-BY-4.0",
                }
        except Exception:
            pass

    # r43-C (2026-05-27): forecast block — linear trend extrapolation from the
    # last 30 days of DCPI history. Skipped if there aren't enough samples
    # (<3 data points in 30d).
    #
    # r-ws3-methodology (2026-07-29): repointed from market_power_scores to
    # dcpi_daily_snapshots. market_power_scores is UPDATE-in-place with
    # computed_at=NOW() — it holds EXACTLY ONE row per slug, forever — so this
    # query could only ever return 1 sample and _compute_forecast returned
    # insufficient_history for every market, permanently. Verified live on
    # midland-tx: samples_in_30d = 1, while /api/v1/dcpi/history returned 53
    # real points for the same market from dcpi_daily_snapshots. api_history
    # was repointed at r80; this endpoint was the straggler.
    #
    #  - snapshot_date::timestamptz AS computed_at: _compute_forecast keys on
    #    "computed_at" and reads .tzinfo, so a bare DATE would raise inside the
    #    swallow below and silently re-break the block.
    #  - time_to_power_months is not snapshotted. Emitting NULL is correct:
    #    _trend() drops None values and _project_ttp returns None, so the TTP
    #    projection reads UNMEASURED rather than a fabricated 0. The
    #    excess/constraint guard is unaffected.
    try:
        with _conn() as c2, c2.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur2:
            cur2.execute("""
                SELECT snapshot_date::timestamptz AS computed_at,
                       excess_power_score, constraint_score,
                       NULL::real AS time_to_power_months
                  FROM dcpi_daily_snapshots
                 WHERE market_slug = %s
                   AND snapshot_date > CURRENT_DATE - INTERVAL '30 days'
                 ORDER BY snapshot_date ASC
            """, (slug,))
            hist = cur2.fetchall() or []
        row["forecast"] = _compute_forecast(hist, row)
        if isinstance(row.get("forecast"), dict):
            row["forecast"]["history_source"] = "dcpi_daily_snapshots"
            row["forecast"]["time_to_power_projection"] = (
                "unavailable — time_to_power_months is not snapshotted daily")
    except Exception:
        row["forecast"] = {"available": False, "reason": "history_query_failed"}

    # r-gate-everywhere (2026-06-27): per-market twin of api_scores() — was a raw
    # jsonify(row) that leaked composite/excess/constraint/time-to-power +
    # risk/opp to anon. SINGLE-market is free-to-cite (2026-07-03); bulk stays paid.
    _rows, _g = _dcpi_mask_rows([row], extra=True, paid=_dcpi_single_market_paid())
    row = _rows[0]
    if _g:
        row["forecast"] = {"available": False, "reason": "pro_only"}
        row.update(_dcpi_gated_meta())
    # provenance-v1 (2026-07-11): collection block, stamped AFTER masking so
    # gating never strips it. as_of = the score's computed_at (already
    # isoformat by here); data_basis/data_basis_source above stay as the
    # legacy per-field labels. Fail-soft.
    try:
        from routes.provenance import attach_provenance, DCPI_CITE_TEMPLATE
        attach_provenance(
            row,
            source="DC Hub Power Index (DCPI) — market_power_scores",
            # r-ws3-methodology (2026-07-29): carry the method VERSION, not just
            # a prose string. A citation that cannot name the method version is
            # not reproducible, because DCPI scores are UPDATE-in-place and a
            # methodology change restates the whole back series.
            method=("DCPI scoring method_version="
                    + str(row.get("method_version") or "unrecorded")
                    + " (full method: /api/v1/dcpi/methodology); see "
                    "data_basis/data_basis_source for the score's input "
                    "basis; signal_tier="
                    + str(row.get("signal_tier") or "unrecorded")
                    + " (see signal_tier_note)"),
            as_of=row.get("computed_at"),
            cite_template=DCPI_CITE_TEMPLATE,
            # v1: DCPI scores are DC Hub's own model output (derived, not a
            # source's published figure) — inferred is the honest baseline.
            default_v="inferred",
        )
    except Exception:
        pass
    return jsonify(row), 200


def _compute_forecast(history: list[dict], current: dict) -> dict:
    """Linear extrapolation of excess/constraint scores + TTP. Returns
    forecast_6mo, forecast_12mo, forecast_24mo and a probability of
    verdict change. Conservative — surfaces uncertainty if data is
    insufficient or trend is noisy."""
    if not history or len(history) < 3:
        return {
            "available": False,
            "reason": "insufficient_history",
            "samples_in_30d": len(history) if history else 0,
            "note": "Need ≥3 daily DCPI samples in last 30 days to project.",
        }

    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)

    def _trend(field):
        """Returns (slope_per_day, current_value). slope is units per day."""
        xs, ys = [], []
        for h in history:
            ts = h.get("computed_at")
            if not ts: continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=_dt.timezone.utc)
            days_ago = (now - ts).total_seconds() / 86400.0
            x = -days_ago  # so most recent is highest x
            y = h.get(field)
            if y is None: continue
            xs.append(x)
            ys.append(float(y))
        if len(xs) < 3:
            return None, None
        n = len(xs)
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        num = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
        den = sum((xs[i] - mean_x) ** 2 for i in range(n))
        if den == 0:
            return 0.0, mean_y
        slope = num / den
        return slope, ys[-1]  # current = most recent observed value

    excess_slope, excess_now = _trend("excess_power_score")
    constraint_slope, constraint_now = _trend("constraint_score")
    ttp_slope, ttp_now = _trend("time_to_power_months")

    if excess_slope is None or constraint_slope is None:
        return {"available": False, "reason": "insufficient_field_data"}

    def _project(now_val, slope, days):
        if now_val is None or slope is None:
            return None
        v = now_val + slope * days
        return round(max(0, min(100, v)), 1)

    def _saturated(now_val, slope, days):
        # SH52-138 (2026-08-08): did the raw linear projection run past the
        # [0,100] score bound before _project clamped it? A steep slope pins
        # BOTH excess and constraint at 100 for the 12/24mo horizon, and
        # _verdict_for(100, 100) then stamps a self-contradictory verdict
        # (excess_power=100 AND implied_verdict=AVOID) that was being served
        # to anon crawlers on the free single-market endpoint. When the
        # extrapolation saturates it is no longer a plausible trajectory, so
        # the caller declines to imply a verdict from it.
        if now_val is None or slope is None:
            return False
        v = now_val + slope * days
        return v > 100 or v < 0

    def _project_ttp(now_val, slope, days):
        if now_val is None or slope is None:
            return None
        v = now_val + slope * days
        return round(max(0, v), 1)

    def _verdict_for(excess, constraint):
        # r-ws3-methodology (2026-07-29): use the REAL verdict function.
        # This helper previously hand-copied a THIRD set of bands
        # (excess>=60 & constraint<40 -> BUILD) that disagreed with
        # derive_verdict's published 65/50 — so "implied_verdict" and
        # "verdict_change_from_now" could report a change that the actual
        # scorer would never make. Same hand-copy bug class as the fabricated
        # static methodology page. Safe to correct now: this block returned
        # insufficient_history for every market until the history source was
        # repointed in the same change, so no published forecast moves.
        if excess is None or constraint is None:
            return None
        return derive_verdict(constraint, excess)

    current_verdict = current.get("verdict")
    forecasts = {}
    for label, days in (("3mo", 90), ("6mo", 180), ("12mo", 365), ("24mo", 730)):
        e = _project(excess_now, excess_slope, days)
        c = _project(constraint_now, constraint_slope, days)
        t = _project_ttp(ttp_now, ttp_slope, days)
        # SH52-138: a projection that hit the score ceiling/floor is a clamp
        # artifact, not a forecast — do not derive an implied verdict from it
        # (that is how "excess 100 + AVOID" reached the public endpoint).
        saturated = _saturated(excess_now, excess_slope, days) or \
            _saturated(constraint_now, constraint_slope, days)
        v = None if saturated else _verdict_for(e, c)
        forecasts[label] = {
            "excess_power_score": e,
            "constraint_score":   c,
            "time_to_power_months": t,
            "implied_verdict":    v,
            "verdict_change_from_now": (
                None if saturated
                else ((v != current_verdict) if (v and current_verdict) else None)),
            "projection_saturated": saturated,
        }

    return {
        "available":           True,
        "method":              "linear_regression",
        "samples_in_30d":      len(history),
        "trend_per_day": {
            "excess_power_score":     round(excess_slope, 3),
            "constraint_score":       round(constraint_slope, 3),
            "time_to_power_months":   round(ttp_slope, 3) if ttp_slope is not None else None,
        },
        "projection":          forecasts,
        "license":             "CC-BY-4.0",
        "disclaimer":          ("Linear extrapolation of last 30 days. Not a "
                                "guarantee. Real markets shift discontinuously "
                                "around grid policy + capex announcements. "
                                "Use as a directional signal, not a target."),
    }


# ─── Phase SS DCPI v2 enrichment endpoint ──────────────────────────
# Surfaces water_risk + renewable_arbitrage scores for one market.
# Pulls signal inputs from usgs_water_stress + eia_retail_rates + the
# existing market_power_scores row, then computes the v2 components on
# the fly. No schema change; consumers opt in by adding `?v=2` or by
# hitting this dedicated path.
@dcpi_bp.route("/api/v1/dcpi/scores/<slug>/v2", methods=["GET"])
def api_score_market_v2(slug):
    _ensure_tables()
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT * FROM market_power_scores
             WHERE market_slug = %s
             ORDER BY computed_at DESC LIMIT 1
        """, (slug,))
        row = cur.fetchone()
        if not row:
            return jsonify(error="market not found", slug=slug), 404

        state = (row.get("state") or "").upper()

        # Pull water + renewable signals from sibling tables (best effort).
        water_metrics = {}
        renew_metrics = {"curtailment_pct": row.get("curtailment_pct")}
        if state:
            try:
                cur.execute("""
                    SELECT AVG(stress_index) AS stress
                      FROM usgs_water_stress
                     WHERE UPPER(state) = %s
                """, (state,))
                r = cur.fetchone()
                if r and r.get("stress") is not None:
                    water_metrics["water_stress_index"] = float(r["stress"])
            except Exception:
                pass
            try:
                cur.execute("""
                    SELECT DISTINCT ON (UPPER(state)) rate_cents_kwh
                      FROM eia_retail_rates
                     WHERE LOWER(sector) = 'industrial'
                       AND UPPER(state) = %s
                     ORDER BY UPPER(state), period DESC
                """, (state,))
                r = cur.fetchone()
                if r and r.get("rate_cents_kwh") is not None:
                    renew_metrics["ppa_rate_cents_kwh"] = float(r["rate_cents_kwh"])
            except Exception:
                pass

    water_risk   = compute_water_risk_score(water_metrics)
    renewable_a  = compute_renewable_arbitrage_score(renew_metrics)
    verdict_v2   = derive_verdict_v2(
        float(row.get("constraint_score") or 0),
        float(row.get("excess_power_score") or 0),
        water_risk, renewable_a,
    )

    if row.get("computed_at"):
        row["computed_at"] = row["computed_at"].isoformat()
    # r-gate-everywhere (2026-06-27): mask the numeric v1 + v2 sub-scores for
    # non-paid (verdicts stay free). SINGLE-market is free-to-cite (2026-07-03).
    _paid_v2 = _dcpi_single_market_paid()
    return jsonify(
        market_slug=row["market_slug"],
        market_name=row["market_name"],
        state=row.get("state"),
        iso=row.get("iso"),
        v1={
            "constraint_score":     row.get("constraint_score") if _paid_v2 else None,
            "excess_power_score":   row.get("excess_power_score") if _paid_v2 else None,
            "verdict":              row.get("verdict"),
            "time_to_power_months": row.get("time_to_power_months") if _paid_v2 else None,
        },
        v2={
            "water_risk_score":         water_risk if _paid_v2 else None,
            "renewable_arbitrage_score": renewable_a if _paid_v2 else None,
            "verdict_v2":               verdict_v2,
            "inputs": {
                "water_stress_index":  water_metrics.get("water_stress_index") if _paid_v2 else None,
                "ppa_rate_cents_kwh":  renew_metrics.get("ppa_rate_cents_kwh") if _paid_v2 else None,
                "curtailment_pct":     renew_metrics.get("curtailment_pct") if _paid_v2 else None,
            },
            "notes": "v2 verdict downgrades BUILD→CAUTION when water_risk≥80, "
                     "upgrades AVOID→CAUTION when renewable_arbitrage≥75 and water_risk≤50",
        },
        computed_at=row.get("computed_at"),
        **({} if _paid_v2 else _dcpi_gated_meta()),
    ), 200


# ─────────────────────────────────────────────────────────────────────────
# Phase SS (2026-05-15): /api/v1/dcpi/recommend — the "where should I build"
# oracle. Single endpoint that ranks markets against a user's capacity +
# deadline + constraint envelope and returns ranked picks with narrative
# justifications. Powers the new MCP tool `recommend_market` — every other
# tool answers a *what* question; this answers a *which* question, which is
# the actual decision an operator makes.
#
# Inputs (query string OR JSON body):
#   capacity_mw            float, MW the user needs                  (default 50)
#   deadline_months        int, months until they need power live    (default 24)
#   water_stress_max       int 1-5 (USGS), 5 = no constraint         (default 5)
#   max_retail_rate_cents  float ¢/kWh industrial cap                (default 99)
#   iso                    optional ISO filter (PJM/ERCOT/...)
#   states                 optional CSV of state codes
#   include_avoid          bool — include AVOID-verdict markets       (default false)
#   top_n                  int, results to return (1-20)             (default 5)
#
# Output:
#   {"ranked_markets": [
#       {"rank": 1, "market_slug": ..., "market_name": ..., "state": ..., "iso": ...,
#        "verdict": "BUILD",
#        "scores": {"composite": 73.2, "excess_power": 84, "constraint": 22,
#                   "time_to_power_months": 14, "queue_capacity_mw": 1200},
#        "constraint_check": {"capacity_ok": true, "deadline_ok": true,
#                             "water_ok": true, "rate_ok": true},
#        "retail_rate_cents_kwh": 5.2,
#        "water_stress_state": 2,
#        "reason": "200 MW queue-free grid headroom in PJM, ...",
#        "risk_flags": ["high_water_stress", ...]
#       }, ...],
#    "criteria_echo": {...inputs as parsed...},
#    "total_evaluated": 276, "passed_filters": 18, "generated_at": "..."}
# ─────────────────────────────────────────────────────────────────────────
@dcpi_bp.route("/api/v1/dcpi/recommend", methods=["GET", "POST", "OPTIONS"])
def api_dcpi_recommend():
    if request.method == "OPTIONS":
        resp = jsonify(ok=True)
        resp.headers["Access-Control-Allow-Origin"]  = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type,X-API-Key,Authorization"
        return resp, 200

    _ensure_tables()

    # Parse inputs from JSON body OR query string (MCP tools tend to POST JSON).
    body = {}
    if request.method == "POST":
        try:
            body = request.get_json(silent=True) or {}
        except Exception:
            body = {}

    def _g(name, default=None):
        if name in body and body[name] not in (None, ""):
            return body[name]
        v = request.args.get(name)
        if v in (None, ""):
            return default
        return v

    def _f(v, default):
        try:    return float(v)
        except (TypeError, ValueError): return default

    def _i(v, default):
        try:    return int(float(v))
        except (TypeError, ValueError): return default

    capacity_mw           = _f(_g("capacity_mw"), 50.0)
    deadline_months       = _i(_g("deadline_months"), 24)
    water_stress_max      = _i(_g("water_stress_max"), 5)
    max_retail_rate_cents = _f(_g("max_retail_rate_cents"), 99.0)
    iso_filter            = (_g("iso") or "").strip().upper() or None
    states_csv            = (_g("states") or "").strip()
    state_set             = {s.strip().upper() for s in states_csv.split(",") if s.strip()} if states_csv else None
    include_avoid         = str(_g("include_avoid", "false")).lower() in ("1","true","yes","y")
    top_n                 = max(1, min(20, _i(_g("top_n"), 5)))

    # ── Step 1: pull current DCPI snapshot (one row per market, most recent) ──
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT DISTINCT ON (market_slug)
                market_slug, market_name, state, iso, latitude, longitude,
                constraint_score, excess_power_score, time_to_power_months,
                queue_capacity_mw, queue_wait_months, reserve_margin_pct,
                stranded_capacity_mw, curtailment_pct,
                verdict, top_risks_json, top_opportunities_json,
                signal_tier,
                computed_at
              FROM market_power_scores
             WHERE published = true
             ORDER BY market_slug, computed_at DESC
        """)
        rows = cur.fetchall()

        # ── Step 2: enrich with retail rates per state (one query) ──
        state_rates = {}
        try:
            cur.execute("""
                SELECT DISTINCT ON (UPPER(state))
                       UPPER(state) AS state_code, rate_cents_kwh, period
                  FROM eia_retail_rates
                 WHERE LOWER(sector) = 'industrial'
                 ORDER BY UPPER(state), period DESC
            """)
            for r in cur.fetchall():
                state_rates[r["state_code"]] = _safe_round(r["rate_cents_kwh"], 2)
        except Exception:
            pass  # table may not exist in dev; degrade gracefully

        # ── Step 3: enrich with water stress per state (one query) ──
        state_water = {}
        try:
            cur.execute("""
                SELECT UPPER(state) AS state_code,
                       AVG(stress_index) AS avg_stress
                  FROM usgs_water_stress
                 WHERE stress_index IS NOT NULL
                 GROUP BY UPPER(state)
            """)
            for r in cur.fetchall():
                # Normalize to a 1-5 scale (1 = low, 5 = extreme).
                # USGS stress_index is already 1-5 in our schema; clamp defensively.
                v = r["avg_stress"]
                if v is None: continue
                state_water[r["state_code"]] = max(1, min(5, int(round(float(v)))))
        except Exception:
            pass

    total_evaluated = len(rows)

    # ── Step 4: filter to candidates that meet hard constraints ──
    candidates = []
    for r in rows:
        verdict = (r.get("verdict") or "").upper()
        if not include_avoid and verdict == "AVOID":
            continue
        if iso_filter and (r.get("iso") or "").upper() != iso_filter:
            continue
        if state_set and (r.get("state") or "").upper() not in state_set:
            continue

        ttp = r.get("time_to_power_months")
        qcap = r.get("queue_capacity_mw")
        st = (r.get("state") or "").upper()
        rate = state_rates.get(st)
        water = state_water.get(st)

        capacity_ok = (qcap is None) or (float(qcap) >= capacity_mw)
        deadline_ok = (ttp is None) or (float(ttp) <= deadline_months)
        water_ok    = (water is None) or (int(water) <= water_stress_max)
        rate_ok     = (rate is None) or (float(rate) <= max_retail_rate_cents)

        if not (capacity_ok and deadline_ok and water_ok and rate_ok):
            continue

        # ── Step 5: composite score ──
        # Same weighting as persona_briefs.py (line 173) — keeps DCPI consistent.
        excess     = _safe_round(r.get("excess_power_score"), 1)
        constraint = _safe_round(r.get("constraint_score"), 1)
        composite  = excess - 0.5 * constraint

        # Penalize markets with no queue capacity signal at all
        if qcap is None:
            composite -= 5

        # Bonus for sub-12-month time-to-power (urgency premium)
        if ttp is not None and float(ttp) <= 12:
            composite += 8
        elif ttp is not None and float(ttp) <= 18:
            composite += 4

        risk_flags = []
        if water is not None and water >= 4: risk_flags.append("high_water_stress")
        if rate is not None and rate > 9:    risk_flags.append("high_retail_rate")
        if ttp is not None and ttp > 36:     risk_flags.append("slow_time_to_power")
        if qcap is not None and float(qcap) < capacity_mw * 1.5:
            risk_flags.append("tight_capacity_margin")

        # Narrative reason — extract top opportunity if available
        ops = r.get("top_opportunities_json") or []
        if isinstance(ops, str):
            try: ops = json.loads(ops)
            except Exception: ops = []
        top_opp = ""
        if isinstance(ops, list) and ops:
            first = ops[0]
            if isinstance(first, dict):
                top_opp = first.get("label") or first.get("title") or ""
            elif isinstance(first, str):
                top_opp = first

        bits = []
        if qcap is not None:
            bits.append(f"{int(qcap)} MW queue capacity in {r.get('iso') or 'grid'}")
        if ttp is not None:
            bits.append(f"~{int(ttp)}mo to power")
        if rate is not None:
            bits.append(f"{rate}¢/kWh industrial")
        if water is not None:
            bits.append(f"water stress {water}/5")
        if top_opp:
            bits.append(top_opp)
        reason = "; ".join(bits) if bits else (verdict or "scored market")

        candidates.append({
            "market_slug": r["market_slug"],
            "market_name": r["market_name"],
            "state":       r.get("state"),
            "iso":         r.get("iso"),
            "verdict":     verdict,
            "scores": {
                "composite":             round(composite, 1),
                "excess_power":          excess,
                "constraint":            constraint,
                "time_to_power_months":  _safe_round(ttp, 1) if ttp is not None else None,
                "queue_capacity_mw":     _safe_round(qcap, 0) if qcap is not None else None,
            },
            "constraint_check": {
                "capacity_ok": capacity_ok, "deadline_ok": deadline_ok,
                "water_ok":    water_ok,    "rate_ok":     rate_ok,
            },
            "retail_rate_cents_kwh": rate,
            "water_stress_state":    water,
            "reason":                reason,
            "risk_flags":            risk_flags,
            # r-ws3-signal-tier (2026-07-28): this endpoint hand-builds its
            # result dicts, so it inherits nothing from the row automatically.
            # NULL tier stays NULL (unknown) with the reason attached.
            "signal_tier":           r.get("signal_tier") or None,
            "signal_tier_basis":     _signal_tier_basis(r.get("signal_tier")),
            "as_of":                 (r["computed_at"].isoformat()
                                      if hasattr(r.get("computed_at"), "isoformat")
                                      else r.get("computed_at")),
        })
        # r-ws3-methodology: only present when this endpoint's SELECT actually
        # carried the column — absent != null. See _attach_method_version.
        _attach_method_version(candidates[-1], r)

    # ── Step 6: rank by composite, then take top_n ──
    candidates.sort(key=lambda c: -c["scores"]["composite"])
    ranked = candidates[:top_n]
    for i, c in enumerate(ranked, 1):
        c["rank"] = i

    return jsonify(
        ranked_markets=ranked,
        criteria_echo={
            "capacity_mw":           capacity_mw,
            "deadline_months":       deadline_months,
            "water_stress_max":      water_stress_max,
            "max_retail_rate_cents": max_retail_rate_cents,
            "iso":                   iso_filter,
            "states":                sorted(state_set) if state_set else None,
            "include_avoid":         include_avoid,
            "top_n":                 top_n,
        },
        total_evaluated=total_evaluated,
        passed_filters=len(candidates),
        generated_at=datetime.datetime.utcnow().isoformat() + "Z",
        methodology="composite = excess_power_score − 0.5 × constraint_score + urgency_bonus; filters: verdict≠AVOID (default), queue ≥ capacity, time_to_power ≤ deadline, water ≤ max, retail ≤ max",
        signal_tier_note=("Each ranked market carries signal_tier: full = all 3 "
                          "live-capable adapters (interconnect_queue, "
                          "planned_generators, grid_telemetry) returned data for "
                          "it; partial = 1-2; low = 0, or its ISO fell through to "
                          "the WECC default; null = the row's writer recorded no "
                          "tier (unknown, NOT low). The composite above is "
                          "computed identically at every tier — the tier tells "
                          "you how much of it rests on measured inputs."),
    ), 200


# Phase RR (2026-05-15): /api/v1/dcpi/ask — DCPI-flavored Q&A.
# The /dcpi page's inline "Ask the Index" widget POSTs/GETs here.
# Before this endpoint existed, the page hit /api/v1/dcpi/ask via GET
# which 404'd → CF Worker fell through to its 503 fallback. The bug
# surfaced as "Backend unreachable" 503s on every DCPI agent query.
#
# Implementation: proxy to the existing /api/v1/demo/ask endpoint.
# Same Anthropic tool-loop, same rate limiting, same cache — but
# accept GET ?q= (the dcpi widget's actual call shape) in addition
# to the POST body shape demo/ask requires.
# AUTO-REPAIR: duplicate route '/api/v1/dcpi/ask' also in routes/dcpi_ask.py:107 — review and remove one
@dcpi_bp.route("/api/v1/dcpi/ask", methods=["GET", "POST", "OPTIONS"])
def dcpi_ask():
    if request.method == "OPTIONS":
        resp = jsonify(ok=True)
        resp.headers["Access-Control-Allow-Origin"]  = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return resp, 204
    # Normalize the question from either GET ?q= or POST {question}.
    if request.method == "GET":
        question = (request.args.get("q") or "").strip()
    else:
        body = request.get_json(silent=True) or {}
        question = (body.get("question") or body.get("q") or "").strip()
    if not question:
        return jsonify(ok=False, error="question required (q= query param or body.question)"), 400
    if len(question) > 400:
        return jsonify(ok=False, error="question too long (max 400 chars)"), 400

    # Delegate to the demo_ask handler — reuses its rate-limit + cache +
    # Anthropic tool-loop. The demo handler reads POST JSON, so we forge
    # a request context with the question normalized into the body.
    try:
        from routes.demo import (_ensure_schema, _is_dc_question, _hash_q,
                                   _cached, _cache_set, _check_and_bump_rate,
                                   _call_claude_with_tools, _client_ip,
                                   PER_IP_DAILY)
        _ensure_schema()
        if not _is_dc_question(question):
            return jsonify(
                ok=True,
                answer=("I'm the DCPI agent — I answer data center power "
                        "questions only. Try: 'What's the DCPI for Ashburn?' "
                        "or 'Compare ERCOT, PJM, and CAISO by excess power.'"),
                tool_calls=[],
                note="off-topic; no Claude call burned"), 200
        qh = _hash_q(question)
        cached = _cached(qh)
        if cached:
            _check_and_bump_rate(_client_ip())
            return jsonify(ok=True, answer=cached["answer"],
                           tool_calls=cached["tool_calls"], cached=True), 200
        used, allowed = _check_and_bump_rate(_client_ip())
        if not allowed:
            return jsonify(
                ok=False, error="rate_limited",
                used_today=used, limit_per_day=PER_IP_DAILY,
                hint="Free demo limit hit. Sign up free for unlimited MCP: https://dchub.cloud/signup",
                signup_url="https://dchub.cloud/signup"), 429
        answer, tool_calls = _call_claude_with_tools(question)
        _cache_set(qh, question, answer, tool_calls)
        return jsonify(
            ok=True, answer=answer, tool_calls=tool_calls,
            rate_limit={"used_today": used, "limit_per_day": PER_IP_DAILY},
            cached=False), 200
    except ImportError as e:
        # Demo module not available — fail soft, don't leak the stack.
        return jsonify(ok=False,
                       error="dcpi_ask_unavailable",
                       detail=f"demo backend not configured: {e}"), 503
    except Exception as e:
        return jsonify(ok=False,
                       error="dcpi_ask_internal_error",
                       detail=str(e)[:200]), 500


@dcpi_bp.route("/api/v1/dcpi/movers", methods=["GET"])
def api_movers():
    _ensure_tables()
    # Phase 268 (2026-05-29): rewrite week_ago to read from the new
    # dcpi_daily_snapshots table. Previously read from market_power_scores
    # WHERE computed_at < NOW() - 7d, but every writer to that table is
    # UPDATE-in-place (UNIQUE on market_slug), so each slug had exactly
    # ONE row with a recent timestamp — the CTE returned 0 rows for every
    # market and excess_delta_7d was always 0.
    #
    # New behavior: pick the closest snapshot ≥7 days old per slug. If
    # no snapshot is old enough yet (e.g. day 0-7 of the bootstrap),
    # prev_excess is NULL and the COALESCE-fallback keeps the delta at 0
    # — same NULL-tolerant behavior the old query had, just for a real
    # reason now (bootstrap window) instead of broken logic.
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            WITH latest AS (
                SELECT DISTINCT ON (market_slug)
                    market_slug, market_name, excess_power_score AS now_excess,
                    constraint_score AS now_constraint, signal_tier, computed_at
                FROM market_power_scores WHERE published = true ORDER BY market_slug, computed_at DESC
            ),
            week_ago AS (
                SELECT DISTINCT ON (market_slug)
                    market_slug, excess_power_score AS prev_excess,
                    constraint_score AS prev_constraint
                FROM dcpi_daily_snapshots
                WHERE snapshot_date <= CURRENT_DATE - INTERVAL '7 days'
                ORDER BY market_slug, snapshot_date DESC
            )
            SELECT l.market_slug, l.market_name, l.now_excess, l.now_constraint,
                   l.signal_tier, l.computed_at,
                   w.prev_excess, w.prev_constraint,
                   (l.now_excess - COALESCE(w.prev_excess, l.now_excess)) AS excess_delta_7d,
                   (l.now_constraint - COALESCE(w.prev_constraint, l.now_constraint)) AS constraint_delta_7d
            FROM latest l LEFT JOIN week_ago w ON l.market_slug = w.market_slug
            ORDER BY ABS(l.now_excess - COALESCE(w.prev_excess, l.now_excess)) DESC
            LIMIT 10
        """)
        rows = cur.fetchall()
    # r-ws3-signal-tier (2026-07-28): stamp the as-of explicitly (jsonify would
    # otherwise emit a raw datetime as an RFC-822 string, inconsistent with the
    # ISO-8601 computed_at every other DCPI surface returns) and carry the tier
    # with its basis. NULL tier stays NULL — never rendered as "low".
    for _r in rows:
        if hasattr(_r.get("computed_at"), "isoformat"):
            _r["computed_at"] = _r["computed_at"].isoformat()
        _st = _r.get("signal_tier") or None
        _r["signal_tier"] = _st
        _r["signal_tier_basis"] = _signal_tier_basis(_st)
        _attach_method_version(_r, _r)
    # r-gate-everywhere (2026-06-27): null the numeric now/prev/delta scores for
    # non-paid (kept the market list + move-ranking order as the free teaser).
    _rows, _g = _dcpi_mask_rows(rows, extra=True)
    if _g:
        return jsonify(movers=_rows, **_dcpi_gated_meta()), 200
    return jsonify(movers=rows), 200


def _dedup_leaderboard(rows):
    """2026-06-08: collapse duplicate market rows (e.g. 'Cheyenne' + 'Cheyenne, WY'
    are the same place with separate slugs) so the leaderboard never shows the same
    market twice — a credibility tell now that search engines index it. Conservative:
    keys on (normalized city name, state), so genuinely-distinct same-name markets in
    different states (Springfield IL vs MO) are kept. Rows arrive pre-sorted by
    composite desc, so the first (best) instance wins."""
    seen, out = set(), []
    for r in rows:
        city = (r.get("market_name") or "").split(",")[0].strip().lower()
        city = "".join(ch for ch in city if ch.isalnum())
        key = (city, (r.get("state") or "").strip().upper())
        if not city or key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


# phase 267: public, machine-readable leaderboard so the DCPI is citable
#            without scraping the HTML page.
@dcpi_bp.route("/api/v1/dcpi/leaderboard", methods=["GET"])
def api_leaderboard():
    """Public ranked DCPI leaderboard.

    Query params:
        verdict  optional filter: BUILD | CAUTION | AVOID | LOW_SIGNAL
                 (default: exclude LOW_SIGNAL to surface actionable markets)
        limit    int, default 25, max 100
        format   json (default) | csv

    Returns ranked markets by excess_power_score (descending) with each
    market's verdict, quality, constraint, and freshness timestamp. JSON-LD
    Dataset markup on /dcpi points here as the canonical machine surface.
    """
    _ensure_tables()
    verdict = (request.args.get("verdict") or "").upper().strip() or None
    try:
        limit = min(int(request.args.get("limit", 25)), 100)
    except (TypeError, ValueError):
        limit = 25
    fmt = (request.args.get("format") or "json").lower()

    # Default: exclude LOW_SIGNAL (high-noise) markets so the leaderboard
    # surfaces only verdicts a buyer/journalist/AI can act on. Pass
    # ?verdict=LOW_SIGNAL explicitly to include them.
    where_verdict = ""
    params = []
    if verdict:
        where_verdict = "AND verdict = %s"
        params.append(verdict)
    else:
        where_verdict = "AND verdict <> 'LOW_SIGNAL'"

    sql = f"""
        SELECT DISTINCT ON (market_slug)
            market_slug, market_name, iso, state,
            excess_power_score, constraint_score, quality_score,
            time_to_power_months,
            verdict, signal_tier, computed_at,
            ('https://dchub.cloud/dcpi/' || market_slug) AS url
        FROM market_power_scores
        WHERE published = true {where_verdict}
        ORDER BY market_slug, computed_at DESC
    """
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    # r41-leaderboard-composite (2026-05-25): add composite_score so the
    # leaderboard's rank is consistent with /api/v1/dcpi/scores?sort=composite.
    # Pre-fix the leaderboard sorted by excess_power_score only; agents
    # asking for "top markets" got a single-component rank, not the
    # verdict-aware multi-component composite. Now: composite is in
    # every row AND drives the rank (top of list = highest composite).
    for r in rows:
        r["composite_score"] = derive_composite_score(
            r.get("excess_power_score"),
            r.get("constraint_score"),
            r.get("time_to_power_months"),
            r.get("verdict"),
        )
    rows.sort(key=lambda r: -(r.get("composite_score") or 0))
    rows = _dedup_leaderboard(rows)[:limit]
    for r in rows:
        if r.get("computed_at"):
            r["computed_at"] = r["computed_at"].isoformat()
        # r-ws3-signal-tier (2026-07-28): NULL tier stays NULL (unknown), with
        # the reason spelled out — never silently rendered as "low".
        _st = r.get("signal_tier") or None
        r["signal_tier"] = _st
        # r-ws3-methodology (2026-07-29): the previous basis string blamed "the
        # lite recompute path" for unrecorded tiers. That claim is FALSE and
        # was published on every masked row: POST /api/v1/dcpi/lite-recompute
        # iterates MARKETS, which holds tuples, and raises AttributeError on
        # every market inside its own swallow-all — it has written zero rows.
        # A wrong reason is worse than no reason, so name only what is true.
        r["signal_tier_basis"] = _signal_tier_basis(_st)
        # r-ws3-methodology: which version of the scoring method produced this
        # row. NULL on rows written before version stamping — surfaced as
        # unknown, never backfilled to the current version, which would
        # backdate a claim we cannot make. See /api/v1/dcpi/methodology.
        r["method_version"] = r.get("method_version") or None
        r["method_doc"] = "/api/v1/dcpi/methodology"
        # Phase 297 (Phase P): add a deterministic reasoning chain so AI
        # agents and journalists can quote the WHY, not just the score.
        # Uses score thresholds from derive_verdict() — keeps reasoning
        # consistent with the verdict logic.
        r["reasoning"] = _build_reasoning(
            r.get("verdict"), r.get("excess_power_score") or 0,
            r.get("constraint_score") or 0, r.get("quality_score") or 0,
        )

    # r-gate-everywhere (2026-06-27): mask scores + the score-bearing reasoning
    # prose for non-paid, on BOTH the JSON body and the CSV export (the CSV was a
    # downloadable spreadsheet of every paid score). Ranking ORDER is preserved
    # (masked AFTER the composite sort) so the free "who is #1" SEO ranking
    # survives — only the /100 numbers are gated.
    _lb_paid = _dcpi_is_paid()
    if not _lb_paid:
        for r in rows:
            for _k in _DCPI_MASK_FIELDS + ("quality_score",):
                if _k in r:
                    r[_k] = [] if str(_k).endswith("_json") else None
            r["locked"] = True
            r["reasoning"] = (f"{(r.get('verdict') or '').upper()} verdict. "
                              "Numeric DCPI scores are Pro — https://dchub.cloud/pricing.")

    if fmt == "csv" and not _lb_paid:
        # r-csv-gate (2026-07-10): CSV export is advertised Developer+ only. The
        # score cells were already masked for non-paid, but the CSV *file* itself
        # was served to anyone — the format gate was never enforced. Refuse the
        # file for non-paid callers (they can still use ?format=json for the gated
        # preview). _lb_paid is computed above (same flag that masks the cells).
        return jsonify({
            "error": "csv_export_requires_developer",
            "tier_required": "developer",
            "message": ("CSV export of the DCPI leaderboard is a Developer "
                        "($49/mo) feature. Use ?format=json for the gated preview, "
                        "or upgrade at https://dchub.cloud/pricing."),
            "upgrade_url": "https://dchub.cloud/pricing?utm_source=dcpi_leaderboard_csv",
        }), 402

    if fmt == "csv":
        import csv, io
        buf = io.StringIO()
        cols = ["rank", "market_slug", "market_name", "iso", "state",
                "verdict", "excess_power_score", "constraint_score",
                "quality_score", "signal_tier", "computed_at", "url",
                "reasoning"]
        w = csv.DictWriter(buf, fieldnames=cols)
        w.writeheader()
        for i, r in enumerate(rows, 1):
            row = {k: r.get(k) for k in cols}
            row["rank"] = i
            w.writerow(row)
        resp = Response(buf.getvalue(), mimetype="text/csv")
        resp.headers["Content-Disposition"] = 'attachment; filename="dcpi-leaderboard.csv"'
        resp.headers["Cache-Control"] = "public, max-age=300, must-revalidate"
        return resp

    body = {
        "as_of": rows[0]["computed_at"] if rows else None,
        "count": len(rows),
        "filter": {"verdict": verdict, "excludes_low_signal": verdict is None},
        "leaderboard": [
            {"rank": i, **r} for i, r in enumerate(rows, 1)
        ],
        "methodology_url": "https://dchub.cloud/dcpi#methodology",
        # r-ws3-signal-tier (2026-07-28): the mix over the RETURNED rows (this
        # list is capped by ?limit and deduped), not the catalog.
        "signal_tier_counts_returned": {
            "full": sum(1 for r in rows if r.get("signal_tier") == "full"),
            "partial": sum(1 for r in rows if r.get("signal_tier") == "partial"),
            "low": sum(1 for r in rows if r.get("signal_tier") == "low"),
            "unrecorded": sum(1 for r in rows if not r.get("signal_tier")),
        },
        "signal_tier_note": (
            "full = all 3 live-capable adapters (interconnect_queue, "
            "planned_generators, grid_telemetry) returned data for that market; "
            "partial = 1-2; low = 0, or the market's ISO fell through to the "
            "WECC default; unrecorded = the row's writer recorded no tier "
            "(unknown, NOT low)."),
        "citation": "DC Hub Data Center Power Index. https://dchub.cloud/dcpi",
        **(_dcpi_gated_meta() if not _lb_paid else {}),
    }
    # Phase 299 (fix PR #21 regression): restore the response wrapper that
    # was accidentally dropped. Without these lines the Flask handler returns
    # None → Flask falls back to a generic HTML error page → CDN caches it
    # → leaderboard endpoint broken for every consumer.
    resp = jsonify(body)
    resp.headers["Cache-Control"] = "public, max-age=300, must-revalidate"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


@dcpi_bp.route("/dcpi/leaderboard", methods=["GET"])
def dcpi_leaderboard_page():
    """Server-rendered, search-INDEXABLE DCPI leaderboard with schema.org
    ItemList + Dataset markup.

    2026-06-08: Perplexity cited DC Hub's LINKEDIN launch posts (frozen numbers
    — "Midlothian 65.6") instead of the live data, because search engines index
    HTML pages, not the JSON API, and there was no canonical leaderboard PAGE to
    outrank the posts. This is that page: the ranked table lives in the raw HTML
    (no JS needed) + ItemList structured data, so Perplexity/Google/Bing extract
    the CURRENT ranking and attribute it to dchub.cloud. Mirrors /api/v1/dcpi/leaderboard.
    """
    import html as _h
    from routes._brand_shell import brand_page
    _ensure_tables()
    try:
        limit = min(int(request.args.get("limit", 25)), 100)
    except Exception:
        limit = 25
    sql = """
        SELECT DISTINCT ON (market_slug)
            market_slug, market_name, iso, state,
            excess_power_score, constraint_score, quality_score,
            time_to_power_months, verdict, computed_at
        FROM market_power_scores
        WHERE published = true AND verdict <> 'LOW_SIGNAL'
        ORDER BY market_slug, computed_at DESC
    """
    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    except Exception:
        rows = []
    for r in rows:
        r["composite_score"] = derive_composite_score(
            r.get("excess_power_score"), r.get("constraint_score"),
            r.get("time_to_power_months"), r.get("verdict"))
    rows.sort(key=lambda r: -(r.get("composite_score") or 0))
    rows = _dedup_leaderboard(rows)[:limit]
    as_of = (rows[0]["computed_at"].isoformat()
             if rows and rows[0].get("computed_at") else "")
    as_of_date = as_of[:10] if as_of else ""

    # r-gate-everywhere (2026-06-27): mask the numeric scores on the SSR
    # leaderboard (table + ItemList/Dataset JSON-LD) for non-paid; keep rank +
    # market + verdict + ISO (the free "who's #1" ranking, order preserved
    # post-sort). The CSV/JSON API endpoints are gated separately.
    _lbp_paid = _dcpi_is_paid()
    if not _lbp_paid:
        for r in rows:
            for _k in ("composite_score", "excess_power_score", "constraint_score",
                       "time_to_power_months", "quality_score"):
                if _k in r:
                    r[_k] = None

    e = lambda x: (_h.escape(str(x)) if x is not None else ("🔒" if not _lbp_paid else ""))
    item_list = {
        "@context": "https://schema.org", "@type": "ItemList",
        "name": "DC Hub Power Index — Data Center Market Leaderboard",
        "description": ("Data-center markets ranked by available power (excess-power "
                        "score) with BUILD / CAUTION / AVOID verdicts and time-to-power. "
                        "Source: DC Hub Data Center Power Index (DCPI)."),
        "url": "https://dchub.cloud/dcpi/leaderboard",
        "numberOfItems": len(rows),
        "itemListOrder": "https://schema.org/ItemListOrderDescending",
        "itemListElement": [{
            "@type": "ListItem", "position": i,
            "name": f"{r.get('market_name')} ({r.get('state')})",
            "url": f"https://dchub.cloud/dcpi/{r.get('market_slug')}",
            "item": {"@type": "Place", "name": r.get("market_name"),
                     "description": (
                         f"DCPI verdict {r.get('verdict')}; ISO {r.get('iso')}. "
                         "Numeric DCPI scores are DC Hub Pro (dchub.cloud/pricing)."
                         if not _lbp_paid else
                         f"DCPI verdict {r.get('verdict')}; excess-power "
                         f"{r.get('excess_power_score')}; composite "
                         f"{r.get('composite_score')}; time-to-power "
                         f"{r.get('time_to_power_months')} months; ISO {r.get('iso')}.")},
        } for i, r in enumerate(rows, 1)],
    }
    dataset = {
        "@context": "https://schema.org", "@type": "Dataset",
        "name": "DC Hub Data Center Power Index (DCPI) Leaderboard",
        "description": ("Live ranking of data-center markets by available power, grid "
                        "constraint and time-to-power, with BUILD/CAUTION/AVOID verdicts."),
        "url": "https://dchub.cloud/dcpi/leaderboard",
        "creator": {"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"},
        "dateModified": as_of_date, "isAccessibleForFree": bool(_lbp_paid),
        "distribution": [
            {"@type": "DataDownload", "encodingFormat": "application/json",
             "contentUrl": "https://dchub.cloud/api/v1/dcpi/leaderboard"},
            {"@type": "DataDownload", "encodingFormat": "text/csv",
             "contentUrl": "https://dchub.cloud/api/v1/dcpi/leaderboard?format=csv"}],
    }
    tr = []
    for i, r in enumerate(rows, 1):
        ttp = r.get("time_to_power_months")
        v = e(r.get("verdict"))
        tr.append(
            f"<tr><td>{i}</td>"
            f"<td><a href='/dcpi/{e(r.get('market_slug'))}'>{e(r.get('market_name'))}</a></td>"
            f"<td>{e(r.get('state'))}</td><td>{e(r.get('iso'))}</td>"
            f"<td><b>{e(r.get('composite_score'))}</b></td>"
            f"<td>{e(r.get('excess_power_score'))}</td>"
            f"<td><span class='v {v.lower()}'>{v}</span></td>"
            f"<td>{ttp if ttp is not None else '—'} mo</td></tr>")
    rows_html = "".join(tr) or "<tr><td colspan=8>Leaderboard refreshing — see <a href='/api/v1/dcpi/leaderboard'>the API</a>.</td></tr>"
    ld1 = json.dumps(item_list, separators=(",", ":"))
    ld2 = json.dumps(dataset, separators=(",", ":"))
    body = (
        "<h1>Data Center Power Index — Market Leaderboard</h1>"
        f"<p class=\"lede\">The top data-center markets ranked by <b>available power</b> for new AI/HPC builds, with DC Hub's BUILD / CAUTION / AVOID verdicts. <b>Live data</b>, updated {as_of_date}. <a href=\"/api/v1/dcpi/leaderboard\">JSON API</a> · <a href=\"/dcpi#methodology\">methodology</a>.</p>"
        "<table><thead><tr><th>Rank</th><th>Market</th><th>State</th><th>ISO</th><th>Composite</th><th>Excess Power</th><th>Verdict</th><th>Time-to-Power</th></tr></thead><tbody>"
        + rows_html +
        "</tbody></table>"
        "<p class=\"cite\">Source: <a href=\"https://dchub.cloud\">DC Hub</a> Data Center Power Index (DCPI). Cite as “DC Hub DCPI Leaderboard, dchub.cloud.” Free JSON (no key): <a href=\"/api/v1/dcpi/leaderboard\">/api/v1/dcpi/leaderboard</a>. Per-market detail: <a href=\"/dcpi\">full DCPI index</a>. Building an agent? <a href=\"/api/v1/onboard\">Start here</a>.</p>")
    page = brand_page(
        title=f"Data Center Market Leaderboard — DC Hub Power Index (DCPI) {as_of_date}",
        description=(f"Live ranking of the top data-center markets by available power for AI/HPC "
                     f"builds — BUILD/CAUTION/AVOID verdicts, excess-power scores and time-to-power. "
                     f"Updated {as_of_date}. Source: DC Hub DCPI."),
        canonical="https://dchub.cloud/dcpi/leaderboard",
        body_html=body,
        ld_jsons=[ld1, ld2],
        og_desc="Top data-center markets ranked by available power. BUILD/CAUTION/AVOID + time-to-power. Live data.")
    resp = Response(page, mimetype="text/html")
    # r-gate-everywhere (2026-06-27): tier-varying body (scores masked for non-paid)
    # → never shared-cache, or a CDN could serve a paid table to anon.
    resp.headers["Cache-Control"] = "private, no-store" if not _lbp_paid else "public, max-age=600, must-revalidate"
    return resp, 200


# ============================================================================
# Phase AA (2026-05-12): ISO Intelligence Layer
#
# User asked: "what can we do strengthen our DCPI index, more ISO
# intelligence?" The market_power_scores table already carries deep
# per-market data we never surface — queue_wait_months, queue_capacity_mw,
# reserve_margin_pct, gen_additions_12mo_mw, curtailment_pct,
# stranded_capacity_mw, emergency_count_30d, avg_kwh_cents. Aggregating
# these per-ISO turns DCPI from "market scorecard" into "ISO power-supply
# diagnostic" — exactly the depth buyers + journalists + AI agents need
# to make ISO-level decisions (which ISO is easiest to enter? cheapest?
# fastest interconnect?).
#
# Two new endpoints:
#   GET /api/v1/dcpi/iso/<code>       — one ISO deep-dive
#   GET /api/v1/dcpi/iso-comparison   — all ISOs ranked side-by-side
# ============================================================================

_ISO_NAMES = {
    "PJM":   "PJM Interconnection (mid-Atlantic + Ohio Valley)",
    "ERCOT": "Electric Reliability Council of Texas",
    "CAISO": "California ISO",
    "NYISO": "New York ISO",
    "ISONE": "ISO New England",
    "ISO-NE": "ISO New England",
    "MISO":  "Midcontinent ISO",
    "SPP":   "Southwest Power Pool",
    "WECC":  "Western Electricity Coordinating Council (non-CAISO)",
    "IESO":  "Independent Electricity System Operator (Ontario)",
    # r-iso-taxonomy (2026-07-28): these labels were already live in
    # market_power_scores but missing here, so /api/v1/dcpi/iso/<code>
    # echoed the bare code back as the name. SERC newly appears at all —
    # it had zero markets until Carolinas/Kentucky stopped claiming PJM.
    # Names say what the label IS, so a reader can tell an RTO with a
    # queue from a balancing authority without one.
    "SERC":  "SERC Reliability Corporation (region — non-RTO utilities)",
    "SOCO":  "Southern Company (balancing authority — no organised market)",
    "TVA":   "Tennessee Valley Authority (balancing authority — no organised market)",
    "FRCC":  "Florida Reliability Coordinating Council (region — non-RTO)",
}


# ── r-per-state-sums (2026-08-08) ────────────────────────────────────────────
# queue_capacity_mw and gen_additions_12mo_mw are NOT per-market figures. Both
# are written by a PER-STATE adapter (_state_queue_depth / _state_gen_additions,
# see the enrichment block above), so every market in a state carries that
# state's whole figure. SUM()ing them over markets therefore multiplies the
# state total by the market count. Measured live 2026-08-08 before this fix:
# ERCOT (19 markets, 3 states) published total_queue_capacity_mw = 8,946,976 MW
# — 8,947 GW, more than world installed capacity — on an anonymous, uncached,
# keyless endpoint.
#
# The honest aggregate counts each STATE once. It still is NOT an ISO-metered
# total (a state that spans two ISOs contributes its whole figure to each), so
# the endpoint publishes the basis + a states_counted alongside it and says
# explicitly that these are not additive across ISOs.
_PER_STATE_COLS = ("queue_capacity_mw", "gen_additions_12mo_mw")


def _iso_state_unique_totals(rows):
    """PURE. Sum the per-state columns ONCE PER STATE within each ISO.

    `rows` are per-market dicts carrying iso / state / market_slug and the
    _PER_STATE_COLS. Returns {iso: {total_queue_capacity_mw,
    total_gen_additions_12mo_mw, queue_states_counted}}.

    A market with no state is keyed by its own slug, so international markets
    (state IS NULL) stay counted individually instead of collapsing into one
    bucket. All markets in a state carry the identical value, so which row wins
    is irrelevant — max() is used only so the choice is deterministic and NULLs
    lose to real numbers.
    """
    per_state: dict = {}
    for r in rows or []:
        iso = (r.get("iso") or "UNKNOWN") or "UNKNOWN"
        state = r.get("state")
        key = (iso, state if state else "slug:%s" % (r.get("market_slug") or "?"))
        cell = per_state.setdefault(key, {c: None for c in _PER_STATE_COLS})
        for c in _PER_STATE_COLS:
            v = r.get(c)
            if v is None:
                continue
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            cell[c] = v if cell[c] is None else max(cell[c], v)
    out: dict = {}
    for (iso, _state), cell in per_state.items():
        acc = out.setdefault(iso, {"total_queue_capacity_mw": 0.0,
                                   "total_gen_additions_12mo_mw": 0.0,
                                   "queue_states_counted": 0})
        acc["queue_states_counted"] += 1
        acc["total_queue_capacity_mw"] += cell["queue_capacity_mw"] or 0.0
        acc["total_gen_additions_12mo_mw"] += cell["gen_additions_12mo_mw"] or 0.0
    return out


# Published beside the two totals on every surface that carries them, so a
# reader (or an AI agent quoting us) cannot mistake them for ISO-metered,
# cross-ISO-additive numbers.
STATE_SUM_BASIS_NOTE = (
    "total_queue_capacity_mw and total_gen_additions_12mo_mw are STATE-level "
    "figures counted ONCE PER STATE (queue_states_counted says how many), not "
    "once per market: the interconnection-queue and EIA-860M generation feeds "
    "are metered per state, and every market in a state carries its state's "
    "figure. They are therefore NOT ISO-metered and NOT additive across ISOs — "
    "a state served by two ISOs contributes its whole figure to each, so "
    "summing these across ISOs double-counts.")


def _aggregate_iso_stats(iso_code: str | None = None, conn=None):
    """Compute per-ISO aggregate stats from market_power_scores. When
       iso_code is given, return one ISO; otherwise return all ISOs
       ranked. Uses DISTINCT ON to take the latest snapshot per market
       so a market that's been recomputed several times doesn't skew
       the avg.

       r-one-ttp (2026-08-08): `conn` lets a caller that already holds a
       connection reuse it (routes.iso_snapshot does). This is THE ISO-level
       DCPI rollup — a second implementation is how the two
       avg_time_to_power_months values diverged — and it must not cost a
       second pooled connection per request to share it."""
    where_iso = ""
    params = []
    if iso_code:
        where_iso = "AND UPPER(iso) = %s"
        params.append(iso_code.upper())

    # DISTINCT ON (market_slug) — most recent row per market. Split out so the
    # aggregate query and the per-state query below read the SAME population.
    latest_cte = f"""
        WITH latest AS (
            SELECT DISTINCT ON (market_slug)
                market_slug, market_name, iso, state,
                excess_power_score, constraint_score, quality_score,
                queue_wait_months, queue_capacity_mw, time_to_power_months,
                reserve_margin_pct, gen_additions_12mo_mw,
                curtailment_pct, stranded_capacity_mw,
                emergency_count_30d, avg_kwh_cents,
                verdict, signal_tier, computed_at
            FROM market_power_scores
            WHERE published = true {where_iso}
            ORDER BY market_slug, computed_at DESC
        )
    """
    sql = latest_cte + """
        SELECT
            COALESCE(iso, 'UNKNOWN') AS iso,
            COUNT(*) AS market_count,
            AVG(excess_power_score) AS avg_excess,
            AVG(constraint_score) AS avg_constraint,
            AVG(quality_score) AS avg_quality,
            AVG(NULLIF(queue_wait_months, 0)) AS avg_queue_wait_months,
            -- r-one-ttp (2026-08-08): time_to_power_months is a DIFFERENT
            -- column from queue_wait_months and must be aggregated as itself.
            -- Both /api/v1/iso/<region>/snapshot and the MCP shaper publish a
            -- field NAMED avg_time_to_power_months; the shaper was filling it
            -- from avg_queue_wait_months, so ERCOT read 71.5 there and 55.3 on
            -- the REST snapshot, same name, same instant. Plain AVG (not
            -- NULLIF-0) so it matches _dcpi_for_iso, which now reads this row.
            AVG(time_to_power_months) AS avg_time_to_power_months,
            -- r-per-state-sums (2026-08-08): total_queue_capacity_mw and
            -- total_gen_additions_12mo_mw are NOT summed here. They were, and
            -- because both columns are per-STATE the sum multiplied the state
            -- figure by the market count (ERCOT: 8,947 GW). They are now
            -- computed once per state by _iso_state_unique_totals below.
            AVG(NULLIF(reserve_margin_pct, 0)) AS avg_reserve_margin_pct,
            AVG(NULLIF(curtailment_pct, 0)) AS avg_curtailment_pct,
            SUM(COALESCE(stranded_capacity_mw, 0)) AS total_stranded_capacity_mw,
            SUM(COALESCE(emergency_count_30d, 0)) AS sum_emergency_30d,
            AVG(NULLIF(avg_kwh_cents, 0)) AS avg_kwh_cents,
            SUM(CASE WHEN verdict = 'BUILD'      THEN 1 ELSE 0 END) AS build_count,
            SUM(CASE WHEN verdict = 'CAUTION'    THEN 1 ELSE 0 END) AS caution_count,
            SUM(CASE WHEN verdict = 'AVOID'      THEN 1 ELSE 0 END) AS avoid_count,
            SUM(CASE WHEN verdict = 'LOW_SIGNAL' THEN 1 ELSE 0 END) AS low_signal_count,
            -- r-ws3-signal-tier (2026-07-28): SIGNAL-QUALITY counts. These are
            -- NOT interchangeable with low_signal_count above, which counts the
            -- LOW_SIGNAL *verdict* and is a permanent 0 in production (that
            -- verdict is written by dchub_self_heal's strict matrix only when a
            -- score is exactly 0, which the iso_defaults guarantee never
            -- happens — measured 0/310 on 2026-07-28). Both readings are
            -- exposed so no consumer can mistake one for the other.
            SUM(CASE WHEN signal_tier = 'full'    THEN 1 ELSE 0 END) AS signal_tier_full_count,
            SUM(CASE WHEN signal_tier = 'partial' THEN 1 ELSE 0 END) AS signal_tier_partial_count,
            SUM(CASE WHEN signal_tier = 'low'     THEN 1 ELSE 0 END) AS signal_tier_low_count,
            SUM(CASE WHEN signal_tier IS NULL     THEN 1 ELSE 0 END) AS signal_tier_unrecorded_count,
            MAX(computed_at) AS latest_computed_at
        FROM latest
        GROUP BY iso
        ORDER BY market_count DESC
    """
    # Per-market rows for the two PER-STATE columns. Deliberately a second
    # read of the same CTE rather than a SQL-side DISTINCT: the dedup rule
    # then lives in a pure function a unit test can exercise without a
    # database (see tests/test_dcpi_iso_per_state_sums.py), which is what
    # was missing when the multiplied total shipped. <=315 rows.
    state_sql = latest_cte + """
        SELECT COALESCE(iso, 'UNKNOWN') AS iso, state, market_slug,
               queue_capacity_mw, gen_additions_12mo_mw
        FROM latest
    """
    def _read(c):
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            _rows = [dict(r) for r in cur.fetchall()]
            cur.execute(state_sql, params)
            _state = [dict(r) for r in cur.fetchall()]
        return _rows, _state

    if conn is not None:
        rows, state_rows = _read(conn)
    else:
        with _conn() as c:
            rows, state_rows = _read(c)
    totals = _iso_state_unique_totals(state_rows)
    for r in rows:
        t = totals.get(r.get("iso")) or {}
        r["total_queue_capacity_mw"] = t.get("total_queue_capacity_mw")
        r["total_gen_additions_12mo_mw"] = t.get("total_gen_additions_12mo_mw")
        r["queue_states_counted"] = t.get("queue_states_counted")
        r["state_totals_basis"] = "sum over distinct states, not over markets"
    return rows


# Keys that survive the non-paid ISO-aggregate mask: identity, the market and
# verdict COUNTS (the free breadth hook), and the basis strings that describe
# what a masked number WOULD have meant.
# NB queue_states_counted is listed explicitly: it does not end in "_count" and
# is not a paid metric — it says how many states built the (possibly masked)
# total, which is the whole point of publishing the basis.
_ISO_ROW_FREE_KEYS = ("iso", "iso_name", "market_count", "latest_computed_at",
                      "locked", "state_totals_basis", "queue_states_counted")


def _mask_iso_rows_inplace(rows):
    """r-gate-everywhere (2026-06-27): null the numeric ISO aggregates (MW
    headroom, avg excess/constraint, queue/reserve, $/kWh) for a non-paid
    caller. Mutates in place so any list that shares these row objects (e.g.
    body['rankings']) is masked too.

    r-per-state-sums (2026-08-08): extracted from api_iso_comparison so
    /api/v1/dcpi/iso/<code> gets the SAME gate. That route had none — it
    served every _DCPI_MASK_EXTRA aggregate (total_queue_capacity_mw,
    avg_kwh_cents, avg_reserve_margin_pct, …) to anonymous callers under
    `Cache-Control: public, max-age=300`, i.e. also cacheable at the edge.
    """
    for r in rows:
        for _k in list(r.keys()):
            if _k in _ISO_ROW_FREE_KEYS or _k.endswith("_count"):
                continue
            r[_k] = None
        r["locked"] = True
    return rows


def _iso_top_markets(iso_code: str, verdict_filter: str, limit: int = 5):
    """Top markets in an ISO by verdict. BUILD ranked by excess_power_score
       DESC; AVOID ranked by constraint_score DESC."""
    order_col = "excess_power_score" if verdict_filter == "BUILD" else "constraint_score"
    sql = f"""
        SELECT DISTINCT ON (market_slug)
            market_slug, market_name, state,
            excess_power_score, constraint_score, quality_score, verdict,
            signal_tier, queue_wait_months, avg_kwh_cents,
            ('https://dchub.cloud/dcpi/' || market_slug) AS url
        FROM market_power_scores
        WHERE published = true
          AND UPPER(iso) = %s
          AND verdict = %s
        ORDER BY market_slug, computed_at DESC
    """
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (iso_code.upper(), verdict_filter))
        rows = [dict(r) for r in cur.fetchall()]
    # Sort by the ranking column, descending
    rows.sort(key=lambda r: -(r.get(order_col) or 0))
    return rows[:limit]


def _normalize_iso_row(r: dict) -> dict:
    """Round floats + serialize datetimes + add narrative labels."""
    out = dict(r)
    for k, v in r.items():
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif isinstance(v, (int,)) or v is None:
            out[k] = v
        else:
            try:
                fv = float(v)
                # 1 decimal place for percentages, 2 for prices, 0 for counts
                if k.endswith("_pct") or k.startswith("avg_") and k != "avg_kwh_cents":
                    out[k] = round(fv, 1)
                elif k == "avg_kwh_cents":
                    out[k] = round(fv, 2)
                else:
                    out[k] = round(fv, 1)
            except (TypeError, ValueError):
                out[k] = v
    # Friendly ISO name
    out["iso_name"] = _ISO_NAMES.get(str(r.get("iso") or "").upper(), r.get("iso"))
    return out


@dcpi_bp.route("/api/v1/dcpi/iso/<iso_code>", methods=["GET"])
def api_iso_deep_dive(iso_code):
    """Deep-dive per ISO. Aggregates queue depth, avg cost, curtailment,
       reserve margin, etc. and surfaces the top BUILD + AVOID markets.

       Citable: machine surface for AI agents asking "what's the state of
       MISO grid right now?" — single fetch returns the whole picture.
    """
    _ensure_tables()
    iso_code = (iso_code or "").upper().strip()
    if not iso_code:
        return jsonify(error="iso_code_required"), 400

    rows = _aggregate_iso_stats(iso_code)
    if not rows:
        return jsonify(
            error="iso_not_found",
            iso=iso_code,
            hint="Try one of: " + ", ".join(sorted(_ISO_NAMES.keys())),
        ), 404

    iso_stats = _normalize_iso_row(rows[0])
    top_build = [_normalize_iso_row(r) for r in _iso_top_markets(iso_code, "BUILD", 5)]
    top_avoid = [_normalize_iso_row(r) for r in _iso_top_markets(iso_code, "AVOID", 5)]

    body = {
        "iso": iso_code,
        "iso_name": _ISO_NAMES.get(iso_code, iso_code),
        "as_of": iso_stats.get("latest_computed_at"),
        "stats": iso_stats,
        "top_build_markets": top_build,
        "top_avoid_markets": top_avoid,
        # r-per-state-sums (2026-08-08): say what the two state-sourced totals
        # are before anyone quotes them. Published unconditionally — a masked
        # caller sees the basis even though the number is null.
        "state_totals_note": STATE_SUM_BASIS_NOTE,
        # r-ws3-signal-tier (2026-07-28): name what the two count families mean,
        # because stats carries BOTH low_signal_count (the LOW_SIGNAL verdict,
        # a permanent 0 in production) and signal_tier_*_count (the real
        # per-market signal quality). They are not the same measurement.
        "signal_tier_note": (
            "stats.signal_tier_full_count / _partial_count / _low_count / "
            "_unrecorded_count are the per-market SIGNAL QUALITY mix: full = "
            "all 3 live-capable adapters (interconnect_queue, "
            "planned_generators, grid_telemetry) returned data; partial = 1-2; "
            "low = 0, or the market's ISO fell through to the WECC default; "
            "unrecorded = the row's writer recorded no tier (unknown, NOT low). "
            "stats.low_signal_count is a DIFFERENT measurement — the count of "
            "the LOW_SIGNAL verdict, which is 0 for every ISO in production."),
        "methodology_url": "https://dchub.cloud/dcpi#methodology",
        "citation": (f"DC Hub DCPI · {iso_code} ISO intelligence. "
                      f"https://dchub.cloud/dcpi/iso/{iso_code.lower()}"),
    }
    # r-per-state-sums (2026-08-08): this route shipped the whole
    # _DCPI_MASK_EXTRA family ungated — same aggregates api_iso_comparison has
    # masked since r-gate-everywhere. Same gate, same masker, same no-store
    # (the body now varies by tier, so a 300s public cache would serve one
    # caller's tier to the next).
    if not _dcpi_is_paid():
        _mask_iso_rows_inplace([body["stats"]])
        body["top_build_markets"], _ = _dcpi_mask_rows(top_build, extra=True, paid=False)
        body["top_avoid_markets"], _ = _dcpi_mask_rows(top_avoid, extra=True, paid=False)
        body.update(_dcpi_gated_meta())
    resp = jsonify(body)
    resp.headers["Cache-Control"] = "private, no-store"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


@dcpi_bp.route("/api/v1/dcpi/iso-comparison", methods=["GET"])
def api_iso_comparison():
    """Side-by-side ISO comparison. Ranks every ISO across the same set
       of dimensions so a buyer can answer "which ISO has the most queue
       capacity coming online? cheapest power? best build verdicts?"
       in a single chart.

       This is the headline new view of DCPI++ — moves the index from
       "276 market scorecards" to "8 ISO diagnostics + 276 underlying
       markets" so the same data answers a strategic question alongside
       the tactical one.
    """
    _ensure_tables()
    rows = [_normalize_iso_row(r) for r in _aggregate_iso_stats()]
    # r47.47 (2026-05-27): drop the null-ISO bucket. 6 rows in
    # market_power_scores have iso=NULL (international markets that didn't
    # tag during recompute); the JS rendered them as a "?" ISO row with no
    # label. Cleaner UX: hide them from the comparison entirely. The
    # underlying market scores are still visible on /dcpi and per-slug
    # pages — just not aggregated into a meaningless "?" row.
    rows = [r for r in rows if (r.get("iso") or "").strip()]

    body = {
        "as_of": max((r.get("latest_computed_at") or "" for r in rows), default=None),
        "count": len(rows),
        "isos": rows,
        "rankings": {
            # Build a sortable "best for X" view — handy for journalists.
            "fastest_interconnect": sorted(
                [r for r in rows if r.get("avg_queue_wait_months") is not None],
                key=lambda r: r["avg_queue_wait_months"])[:5],
            "cheapest_power": sorted(
                [r for r in rows if r.get("avg_kwh_cents") is not None],
                key=lambda r: r["avg_kwh_cents"])[:5],
            "most_build_verdicts": sorted(
                rows, key=lambda r: -(r.get("build_count") or 0))[:5],
            "highest_excess_capacity": sorted(
                rows, key=lambda r: -(r.get("avg_excess") or 0))[:5],
            "most_curtailment_risk": sorted(
                [r for r in rows if r.get("avg_curtailment_pct") is not None],
                key=lambda r: -(r["avg_curtailment_pct"]))[:5],
        },
        # r-ws3-signal-tier (2026-07-28): same two-readings warning as
        # /api/v1/dcpi/iso/<iso>. The *_count keys survive the non-paid mask
        # below (it skips every key ending in _count), so the signal-quality mix
        # stays visible to free callers exactly like the verdict counts do.
        "signal_tier_note": (
            "Each ISO row carries signal_tier_full_count / _partial_count / "
            "_low_count / _unrecorded_count — the per-market SIGNAL QUALITY "
            "mix (full = all 3 live-capable adapters returned data; partial = "
            "1-2; low = 0 or the ISO fell through to the WECC default; "
            "unrecorded = no tier recorded, unknown NOT low). low_signal_count "
            "on the same row is a DIFFERENT measurement — the LOW_SIGNAL "
            "verdict count, which is 0 for every ISO in production."),
        "methodology_url": "https://dchub.cloud/dcpi#methodology",
        "citation": "DC Hub DCPI · ISO comparison. https://dchub.cloud/dcpi/iso-comparison",
    }
    # r-gate-everywhere (2026-06-27): mask the numeric ISO aggregates (MW
    # headroom, avg excess/constraint, queue/reserve, $/kWh) for non-paid; keep
    # iso/iso_name + market & verdict COUNTS + the ranking ORDER (the free
    # breadth hook). Masking the row dicts in place propagates to body['rankings']
    # (same objects). Tier-varying body → private/no-store so a CDN can't serve a
    # paid body to anon (also add /api/v1/dcpi/ to the CF bypass Cache Rule).
    _iso_paid = _dcpi_is_paid()
    if not _iso_paid:
        _mask_iso_rows_inplace(rows)
        body.update(_dcpi_gated_meta())
    resp = jsonify(body)
    resp.headers["Cache-Control"] = "private, no-store"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


# Phase 297 (Phase P): deterministic reasoning chain. Templates the WHY
# behind each verdict using the underlying scores. No LLM call per market —
# cheap, consistent, citable. The thresholds mirror the derive_verdict()
# matrix in this file so reasoning never contradicts the verdict.
def _build_reasoning(verdict, excess, constraint, quality):
    e_band = ("strong" if excess >= 65 else "moderate" if excess >= 40
              else "thin" if excess > 0 else "no_signal")
    c_band = ("clear" if constraint < 45 else "tight" if constraint < 70
              else "saturated" if constraint > 0 else "no_signal")

    e_label = {
        "strong":    f"Excess Power {int(excess)} (strong — stranded capacity + queued additions <12mo)",
        "moderate":  f"Excess Power {int(excess)} (moderate — some headroom)",
        "thin":      f"Excess Power {int(excess)} (thin — limited spare capacity)",
        "no_signal": f"Excess Power {int(excess)} (no signal — insufficient data)",
    }[e_band]
    c_label = {
        "clear":     f"Constraint {int(constraint)} (clear — healthy queue, reserve margin)",
        "tight":     f"Constraint {int(constraint)} (tight — queue backed up)",
        "saturated": f"Constraint {int(constraint)} (saturated — near NERC floor or queue dead)",
        "no_signal": f"Constraint {int(constraint)} (no signal)",
    }[c_band]

    quality_note = (
        f"Quality {int(quality)} — high-confidence" if quality >= 80
        else f"Quality {int(quality)} — moderate-confidence" if quality >= 60
        else f"Quality {int(quality)} — low-confidence" if quality > 0
        else "Quality unknown"
    )

    # Verdict-specific framing
    if verdict == "BUILD":
        framing = "Why BUILD: stranded power + cleared queue make this a near-term siting target."
    elif verdict == "AVOID":
        framing = "Why AVOID: saturated grid + thin excess. Site selection here forces years of queue wait."
    elif verdict == "CAUTION":
        framing = "Why CAUTION: mixed signal. One of (excess, constraint) is unfavorable; diligence required."
    elif verdict == "LOW_SIGNAL":
        framing = "Why LOW_SIGNAL: scores too noisy to call. Market is tracked, not yet rated."
    elif verdict == "NODATA":
        framing = "Why NODATA: source feed has not yet populated for this market."
    else:
        framing = f"Verdict: {verdict}"

    return f"{framing} {e_label}. {c_label}. {quality_note}."
    resp = jsonify(body)
    resp.headers["Cache-Control"] = "public, max-age=300, must-revalidate"
    resp.headers["Access-Control-Allow-Origin"] = "*"  # citable from anywhere
    return resp, 200


# phase 267: OEmbed discovery — journalists / Substack / Medium can paste
# https://dchub.cloud/dcpi and get a live ticker widget back.
# phase 270 hardening: validate URL host so this can't be used as an open
# OEmbed redirector against other domains, and whitelist slug charset so
# user-controllable input can't break out of the iframe attributes.
import re as _oembed_re
import urllib.parse as _oembed_url
_OEMBED_ALLOWED_HOSTS = {"dchub.cloud", "www.dchub.cloud"}
_OEMBED_SLUG_RE = _oembed_re.compile(r"^[a-z0-9][a-z0-9_-]{0,80}$")


@dcpi_bp.route("/api/v1/dcpi/oembed", methods=["GET"])
def api_oembed():
    """OEmbed 1.0 provider for the DCPI page + per-market pages.

    Resolves the URL → an embeddable ticker (or per-market card) so external
    publishers (Substack, Medium, news CMSes) can cite DCPI inline.
    """
    target = request.args.get("url", "").strip()
    fmt = (request.args.get("format") or "json").lower()
    if fmt not in ("json",):
        return jsonify(error="only format=json supported"), 501

    # phase 270: validate the URL points at us before resolving anything.
    # Without this check the endpoint would happily build OEmbed payloads for
    # arbitrary domains, which would make us an open redirector for embed
    # crawlers.
    try:
        parsed = _oembed_url.urlparse(target)
    except Exception:
        parsed = None
    if not parsed or parsed.scheme not in ("http", "https") or parsed.netloc.lower() not in _OEMBED_ALLOWED_HOSTS:
        return jsonify(error="url must point to dchub.cloud"), 400

    # Parse target — accept /dcpi or /dcpi/<slug>
    slug = None
    path = parsed.path or ""
    if "/dcpi/" in path:
        slug_raw = path.rsplit("/dcpi/", 1)[-1].strip("/")
        # whitelist: only lowercase alnum/_/- slugs of reasonable length
        if _OEMBED_SLUG_RE.match(slug_raw):
            slug = slug_raw
    is_market = bool(slug and slug not in ("ticker.html", "press"))

    if is_market:
        embed_html = (
            f'<iframe src="https://dchub.cloud/api/v1/dcpi/embed/{slug}" '
            f'width="600" height="240" frameborder="0" '
            f'style="border:1px solid #1f2030;border-radius:8px;max-width:100%;" '
            f'title="DCPI · {slug}"></iframe>'
        )
        body = {
            "version": "1.0", "type": "rich",
            "provider_name": "DC Hub",
            "provider_url": "https://dchub.cloud",
            "title": f"DCPI · {slug}",
            "html": embed_html, "width": 600, "height": 240,
            "cache_age": 300,
        }
    else:
        embed_html = (
            '<iframe src="https://dchub.cloud/dcpi/ticker.html" '
            'width="100%" height="48" frameborder="0" '
            'style="border:0;max-width:100%;" '
            'title="DCPI · Live Ticker"></iframe>'
        )
        body = {
            "version": "1.0", "type": "rich",
            "provider_name": "DC Hub",
            "provider_url": "https://dchub.cloud",
            "title": "Data Center Power Index — Live",
            "html": embed_html, "width": 1280, "height": 48,
            "cache_age": 300,
        }
    resp = jsonify(body)
    resp.headers["Cache-Control"] = "public, max-age=300, must-revalidate"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


@dcpi_bp.route("/api/v1/dcpi/recompute", methods=["POST"])
def api_recompute():
    """Trigger a DCPI recompute. Phase ZZ (2026-05-16) adds optional
    chunking params for the GitHub Actions cron, which has a 120s
    workflow timeout that the full 276-market recompute overruns.

    Query params:
        offset  start index into MARKETS (default 0)
        limit   max markets to process in this chunk (default: all)
        admin_key  shared secret (also accepted via X-Admin-Key header)

    Cron usage (dcpi-daily.yml drives 3 chunks back-to-back):
        POST /api/v1/dcpi/recompute?offset=0&limit=100
        POST /api/v1/dcpi/recompute?offset=100&limit=100
        POST /api/v1/dcpi/recompute?offset=200&limit=100
    """
    # Accept only with admin token; simple shared-secret check
    expected = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")
    provided = request.headers.get("X-Admin-Key") or request.args.get("admin_key")
    if expected and provided != expected:
        return jsonify(error="unauthorized"), 401
    try:    offset = max(0, int(request.args.get("offset") or 0))
    except ValueError: offset = 0
    try:    limit  = int(request.args.get("limit")) if request.args.get("limit") else None
    except ValueError: limit = None
    res = recompute_all_scores(source="api", offset=offset, limit=limit)
    res["total_markets_known"] = len(MARKETS)
    res["chunk_offset"]        = offset
    res["chunk_limit"]         = limit
    return jsonify(res), 200


@dcpi_bp.route("/api/v1/dcpi/snapshot", methods=["POST"])
def api_snapshot():
    """Phase 268 (2026-05-29) — admin/cron endpoint that writes today's
    market_power_scores into dcpi_daily_snapshots. The /api/v1/dcpi/movers
    week_ago lookup reads from this table; without daily snapshots, the
    movers endpoint can never compute a real delta (every score row in
    market_power_scores is UPDATE-in-place, so its computed_at is always
    "now"). Idempotent per day: rerunning UPDATES the existing rows.

    Driven by .github/workflows/facility-snapshot-daily.yml at 05:17 UTC
    (piggybacking on the existing daily cron — no new schedule needed).
    """
    expected = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")
    provided = request.headers.get("X-Admin-Key") or request.args.get("admin_key")
    if expected and provided != expected:
        return jsonify(error="unauthorized"), 401
    # Bootstrap first (no-op after first call), then write today's row.
    backfill = backfill_dcpi_snapshots_if_empty()
    snap     = write_dcpi_snapshot()
    return jsonify(snapshot=snap, backfill=backfill), 200


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------
DCPI_INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>DCPI · Data Center Power Index | datacenterpowerindex.com | DC Hub</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="DCPI (Data Center Power Index) tracks power availability across {{ total_rows }}+ U.S. data center markets in real time. The Excess Power Score surfaces stranded capacity nobody else publishes. Also at datacenterpowerindex.com.">
<meta property="og:title" content="DCPI — The Data Center Power Index | datacenterpowerindex.com">
<meta property="og:description" content="Real-time power availability across {{ total_rows }}+ U.S. markets. Find the excess capacity hidden in plain sight. The industry-standard power index.">
<meta property="og:image" content="https://dchub.cloud/dcpi/og.svg">
<meta property="og:url" content="https://dchub.cloud/dcpi">
<meta name="twitter:card" content="summary_large_image">
<meta name="robots" content="index,follow,max-snippet:-1,max-image-preview:large">
<link rel="canonical" href="https://dchub.cloud/dcpi">
<link rel="stylesheet" href="/static/dchub-brand.css">
<script src="/js/dchub-nav.js" defer></script>
<!-- Phase NNN (2026-05-17) — own the category. datacenterpowerindex.com
     is a vanity domain (GoDaddy 301 → /dcpi). Self-reference via
     <link rel="alternate"> so search engines + AI crawlers know they're
     the same resource, and we get the SEO credit for both. -->
<link rel="alternate" href="https://datacenterpowerindex.com" hreflang="x-default" title="datacenterpowerindex.com (canonical)">
<link rel="alternate" type="application/json+oembed" href="https://dchub.cloud/api/v1/dcpi/oembed?url=https%3A%2F%2Fdchub.cloud%2Fdcpi" title="DCPI OEmbed">
<!-- phase 267: schema.org Dataset markup so DCPI is citable by LLMs and search engines -->
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Dataset",
  "name": "Data Center Power Index (DCPI)",
  "alternateName": "DCPI",
  "description": "Real-time power-availability scoring across {{ total_rows }} U.S. data center markets. Combines ISO grid constraint signals, retail electricity prices, and interconnection-queue pressure into a 0–100 Excess Power Score with an actionable BUILD / CAUTION / AVOID / LOW_SIGNAL verdict per market. Recomputed continuously.",
  "url": "https://dchub.cloud/dcpi",
  "sameAs": "https://dchub.cloud/dcpi",
  "creator": {"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"},
  "publisher": {"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"},
  "keywords": "data center, power index, grid intelligence, market capacity, hyperscale, AI infrastructure, ISO, ERCOT, PJM, MISO, CAISO",
  "license": "https://dchub.cloud/dcpi#methodology",
  "isAccessibleForFree": true,
  {% if spatial_coverage %}"spatialCoverage": {{ spatial_coverage|tojson }},
  {% endif %}
  "temporalCoverage": "2024-01-01/..",
  "distribution": [
    {"@type": "DataDownload", "encodingFormat": "application/json", "contentUrl": "https://dchub.cloud/api/v1/dcpi/scores", "name": "All market scores (current)"},
    {"@type": "DataDownload", "encodingFormat": "application/json", "contentUrl": "https://dchub.cloud/api/v1/dcpi/leaderboard", "name": "Ranked leaderboard (top markets)"},
    {"@type": "DataDownload", "encodingFormat": "application/json", "contentUrl": "https://dchub.cloud/api/v1/dcpi/history", "name": "30-day score history per market"}
  ],
  "citation": "DC Hub Data Center Power Index. https://dchub.cloud/dcpi"
}
</script>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root {
  --bg:        #0a0a12;
  --bg2:       #0f1119;
  --bg3:       #181a25;
  --card:      #11121a;
  --card-hi:   #1a1c28;
  --bd:        #1f2030;
  --bd-hi:     #2a2c3e;
  --tx:        #fff;
  --tx2:       #9ca3af;
  --tx3:       #6b7280;
  --acc:       #6366f1;
  --acc-light: #818cf8;
  --acc-vivid: #a855f7;
  --green:     #10b981;
  --orange:    #f59e0b;
  --red:       #ef4444;
  --gradient:  linear-gradient(135deg,#6366f1 0%,#a855f7 100%);
}
* { box-sizing: border-box; }
body {
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
  background: var(--bg);
  color: var(--tx);
  margin: 0;
  padding: 0;
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
}
code, pre, .mono { font-family: 'JetBrains Mono', monospace; }

/* ===== TOP NAV ===== */
.top-nav {
  border-bottom: 1px solid var(--bd);
  background: rgba(10,10,18,0.85);
  backdrop-filter: blur(8px);
  position: sticky;
  top: 0;
  z-index: 100;
}
.top-nav-inner {
  max-width: 1280px;
  margin: 0 auto;
  padding: 1rem 1.5rem;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1.5rem;
}
.logo {
  font-weight: 800;
  font-size: 1.05rem;
  color: var(--tx);
  text-decoration: none;
  letter-spacing: -0.01em;
}
.logo span { color: var(--acc); }
.nav-links { display: flex; gap: 1.5rem; flex-wrap: wrap; }
.nav-links a {
  color: var(--tx2);
  text-decoration: none;
  font-size: 0.92rem;
  font-weight: 500;
  position: relative;
}
.nav-links a:hover { color: var(--tx); }
.nav-links a.active { color: var(--tx); }
.nav-links a sup {
  color: var(--green);
  font-size: 0.55rem;
  font-weight: 800;
  letter-spacing: 0.04em;
  margin-left: 0.2rem;
  vertical-align: super;
}

/* ===== STATUS PULSE ===== */
.status-strip {
  background: var(--bg2);
  border-bottom: 1px solid var(--bd);
  padding: 0.55rem 1.5rem;
  text-align: center;
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.78rem;
  color: var(--tx2);
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.pulse {
  display: inline-block;
  width: 8px; height: 8px;
  background: var(--green);
  border-radius: 50%;
  margin-right: 0.5rem;
  animation: pulse 1.6s ease-in-out infinite;
  vertical-align: middle;
}
@keyframes pulse { 50% { opacity: 0.3; transform: scale(0.85); } }

.wrap { max-width: 1280px; margin: 0 auto; padding: 3rem 1.5rem; }

/* ===== HERO ===== */
.hero { margin-bottom: 3rem; }
.hero h1 {
  font-size: clamp(2.4rem, 5vw, 3.6rem);
  margin: 0 0 1rem;
  font-weight: 800;
  letter-spacing: -0.025em;
  line-height: 1.05;
}
.hero h1 .accent {
  background: var(--gradient);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}
.hero .lede {
  color: var(--tx2);
  font-size: 1.1rem;
  max-width: 720px;
  margin: 0 0 1.5rem;
}

/* ===== STATS ROW ===== */
.stats-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 1rem;
  margin: 2rem 0 3rem;
  padding: 1.5rem;
  background: var(--card);
  border: 1px solid var(--bd);
  border-radius: 12px;
}
.stat .num {
  font-family: 'JetBrains Mono', monospace;
  font-size: 2rem;
  font-weight: 700;
  color: var(--tx);
  letter-spacing: -0.02em;
}
.stat .label {
  color: var(--tx2);
  font-size: 0.72rem;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  margin-top: 0.3rem;
}

/* ===== SECTION HEADER ===== */
.section-h {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  margin: 3rem 0 1rem;
  font-size: 0.78rem;
  font-weight: 700;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--tx2);
}
.section-h .pip { width: 4px; height: 12px; background: var(--acc); border-radius: 2px; }
h2 {
  font-size: 1.6rem;
  font-weight: 700;
  margin: 0 0 1rem;
  letter-spacing: -0.015em;
}

/* ===== TOGGLE ===== */
.toggle {
  display: inline-flex;
  background: var(--card);
  border: 1px solid var(--bd);
  border-radius: 10px;
  overflow: hidden;
  margin: 0 0 1.5rem;
}
.toggle button {
  background: transparent;
  color: var(--tx2);
  border: 0;
  padding: 0.7rem 1.25rem;
  cursor: pointer;
  font-weight: 600;
  font-size: 0.85rem;
  font-family: inherit;
  transition: all 0.15s;
}
.toggle button.active {
  background: var(--gradient);
  color: white;
}

/* phase 271: verdict filter tabs — Actionable (BUILD/CAUTION/AVOID) is the
   default view; Monitoring (LOW_SIGNAL) is the noisy long tail; All shows
   everything. Designed to mirror .toggle visual language. */
.verdict-tabs {
  display: inline-flex;
  background: var(--card);
  border: 1px solid var(--bd);
  border-radius: 10px;
  overflow: hidden;
  margin: 0 0 1rem;
}
.verdict-tabs button {
  background: transparent;
  color: var(--tx2);
  border: 0;
  padding: 0.6rem 1.15rem;
  cursor: pointer;
  font-weight: 600;
  font-size: 0.82rem;
  font-family: inherit;
  transition: all 0.15s;
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
}
.verdict-tabs button.active {
  background: var(--gradient);
  color: white;
}
.verdict-tabs button .count {
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.72rem;
  padding: 0.12rem 0.45rem;
  border-radius: 99px;
  background: rgba(255,255,255,0.08);
  color: inherit;
}
.verdict-tabs button.active .count {
  background: rgba(255,255,255,0.22);
}
.hidden-by-verdict { display: none !important; }

/* ===== GRID ===== */
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 1rem;
}
/* r47.44 (2026-05-27): .card-link is the clickable <a> wrapping each
   .card. Without explicit display:block it was default-inline, which in
   a CSS grid context collapses the anchor's hit region to ~0px wide.
   The card looked clickable (because .card has cursor:pointer) but the
   click never actually hit the anchor — it landed on the inner <div>,
   which has no href. Symptom: users hover the BUILD/CAUTION/AVOID cards
   and nothing happens on click. Caught in the pre-CBRE DCPI sweep.
   Fix: make the anchor a block participating in the grid, so the whole
   card becomes one big clickable region.
*/
.card-link {
  display: block;
  text-decoration: none;
  color: inherit;
}
.card-link:focus-visible {
  outline: 2px solid var(--bd-hi);
  outline-offset: 2px;
  border-radius: 14px;
}
.card {
  background: var(--card);
  border: 1px solid var(--bd);
  border-radius: 12px;
  padding: 1.4rem 1.5rem;
  transition: all 0.18s ease;
  cursor: pointer;
  position: relative;
  overflow: hidden;
}
.card:hover {
  transform: translateY(-3px);
  border-color: var(--bd-hi);
  background: var(--card-hi);
  box-shadow: 0 12px 32px rgba(99,102,241,0.10);
}
.card:hover::before {
  opacity: 1;
}
.card::before {
  content: '';
  position: absolute;
  inset: 0;
  background: linear-gradient(135deg, rgba(99,102,241,0.07), transparent 60%);
  opacity: 0;
  transition: opacity 0.18s;
  pointer-events: none;
}
.card .market-name {
  font-size: 1.1rem;
  font-weight: 600;
  margin: 0 0 0.25rem;
  letter-spacing: -0.01em;
}
.card .iso {
  color: var(--tx2);
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.75rem;
  margin-bottom: 1rem;
}
.score {
  font-family: 'JetBrains Mono', monospace;
  font-size: 2.6rem;
  font-weight: 800;
  line-height: 1;
  letter-spacing: -0.04em;
}
.score.green { color: var(--green); }
.score.orange { color: var(--orange); }
.score.red { color: var(--red); }
.label {
  color: var(--tx2);
  font-size: 0.7rem;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  margin-top: 0.4rem;
  font-weight: 600;
}
.verdict {
  display: inline-block;
  padding: 0.22rem 0.7rem;
  border-radius: 5px;
  font-size: 0.7rem;
  font-weight: 800;
  letter-spacing: 0.08em;
  margin-top: 0.9rem;
}
.verdict.BUILD   { background: rgba(16,185,129,0.18); color: var(--green); }
.verdict.CAUTION { background: rgba(245,158,11,0.18); color: var(--orange); }
.verdict.AVOID   { background: rgba(239,68,68,0.18); color: var(--red); }
.ttp {
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.78rem;
  color: var(--tx2);
  margin-top: 0.55rem;
}

/* ===== CTA ===== */
.cta-banner {
  background: var(--gradient);
  padding: 2rem 2.25rem;
  border-radius: 14px;
  margin: 3rem 0 2rem;
  position: relative;
  overflow: hidden;
}
.cta-banner::after {
  content: '';
  position: absolute;
  right: -40px; bottom: -40px;
  width: 200px; height: 200px;
  background: radial-gradient(circle, rgba(255,255,255,0.15), transparent 70%);
  pointer-events: none;
}
.cta-banner h2 { margin: 0 0 0.4rem; font-size: 1.4rem; color: white; }
.cta-banner p {
  margin: 0 0 1.1rem;
  color: rgba(255,255,255,0.88);
  font-size: 0.95rem;
  max-width: 540px;
}
.cta-banner a.btn {
  display: inline-block;
  background: white;
  color: var(--acc);
  padding: 0.7rem 1.3rem;
  border-radius: 7px;
  text-decoration: none;
  font-weight: 700;
  font-size: 0.92rem;
  transition: transform 0.1s;
}
.cta-banner a.btn:hover { transform: translateY(-1px); }

footer {
  border-top: 1px solid var(--bd);
  margin-top: 3rem;
  padding: 2rem 0 1rem;
  color: var(--tx3);
  font-size: 0.84rem;
}
footer a { color: var(--tx2); }
footer a:hover { color: var(--acc-light); }

@media (max-width: 600px) {
  .nav-links { display: none; }
}
</style>
</head>
<body>
<nav class="top-nav">
  <div class="top-nav-inner">
    <a class="logo" href="/">DC <span>Hub</span></a>
    <div class="nav-links">
      <a href="/">Home</a>
      <a href="/markets">Markets</a>
      <a href="/dcpi" class="active">DCPI<sup>NEW</sup></a>
      <a href="/land-power">Land &amp; Power</a>
      <a href="/ai">AI Platform</a>
      <a href="/news">News</a>
      <a href="/pricing">Pricing</a>
    </div>
  </div>
</nav>

<div class="status-strip">
  {# r-hero-total (2026-07-26): coverage claims use the TOTAL catalog, not the
     tier-capped card count — anon mobile read "LIVE · 25 MARKETS SCORED",
     underselling a 317-market index (same r47.47 rule as the stats row). #}
  <span class="pulse"></span>LIVE · {{ total_rows }} MARKETS SCORED · UPDATED DAILY 06:00 UTC · FREE FOR PRESS CITATION
</div>

<div class="wrap">
  <section class="hero">
    <h1>The <span class="accent">Data Center Power Index</span></h1>
    <p class="lede">Real-time power availability across {{ total_rows }} U.S. data center markets. Two scores per market: <strong>Excess Power</strong> (where buyers don't know to look) and <strong>Constraint</strong> (where the queue is dead). The contrarian metric the incumbents won't publish.</p>
  </section>

  <a href="/dcgi" style="display:block;text-decoration:none;background:linear-gradient(135deg,#10b981 0%,#0ea5e9 100%);border-radius:14px;padding:1.6rem 2rem;margin:0 0 2rem;position:relative;overflow:hidden;">
    <div style="font-family:'JetBrains Mono',monospace;font-size:11px;text-transform:uppercase;letter-spacing:.14em;color:rgba(255,255,255,.82);margin-bottom:6px;">🔥 The other half of the story</div>
    <div style="font-size:1.25rem;font-weight:800;color:#fff;letter-spacing:-0.01em;">See the gas story &rarr; DC Hub Gas Index (DCGI)</div>
    <p style="color:rgba(255,255,255,.9);font-size:0.95rem;margin:0.35rem 0 0;max-width:640px;">When the grid queue runs 5&ndash;7 years, behind-the-meter natural gas is how AI capacity actually gets energized. DCGI scores every US state on gas-to-power siting.</p>
  </a>

  <div class="stats-row">
    {# r47.47 (2026-05-27): hero "Markets Scored" is the TOTAL catalog,
       not the truncated `count` shown in the grid. Anon viewers see 5
       BUILD + 20 others = 25 rows, but the catalog is the full 233.
       Using {{ count }} here said "25 Markets Scored" — directly
       contradicting the upgrade banner below ("Showing 25 of 300+ markets").
       Fixed to total_rows. #}
    <div class="stat"><div class="num">{{ total_rows }}</div><div class="label">Markets Scored</div></div>
    <div class="stat"><div class="num">8</div><div class="label">Inputs per Score</div></div>
    <div class="stat"><div class="num">06:00 UTC</div><div class="label">Daily Refresh</div></div>
    <div class="stat"><div class="num">FREE</div><div class="label">Press &amp; Citation</div></div>
  </div>

  <div class="section-h"><span class="pip"></span>📊 Index View</div>

  {% if gated_to_anon %}
  <div style="background:linear-gradient(135deg,rgba(99,102,241,.10),rgba(168,85,247,.06));border:1px solid rgba(99,102,241,.35);border-radius:10px;padding:20px 24px;margin:0 0 24px;display:flex;justify-content:space-between;align-items:center;gap:20px;flex-wrap:wrap">
    <div style="flex:1;min-width:280px">
      <div style="font-family:'JetBrains Mono',monospace;font-size:11px;text-transform:uppercase;letter-spacing:.12em;color:#a855f7;margin-bottom:6px">Showing {{ count }} of {{ total_rows }} markets</div>
      {% if tier_state == 'free' %}
      <div style="font-size:15px;line-height:1.5;color:#e5e7eb">You're on the <strong>free tier</strong> — viewing the top {{ count }} ranked markets. Upgrade to Pro to unlock all <strong>{{ total_rows }}</strong> scored markets + ISO drill + daily refresh + market alerts.</div>
      {% else %}
      <div style="font-size:15px;line-height:1.5;color:#e5e7eb">You're viewing the <strong>top {{ count }}</strong> ranked markets. Claim a free DC Hub dev key (60 sec, just your email) to see the top 50 — or go Pro to unlock all <strong>{{ total_rows }}</strong> scored markets + ISO drill + daily refresh.</div>
      {% endif %}
    </div>
    {% if tier_state == 'free' %}
    <a href="https://dchub.cloud/pricing?source=dcpi_free_cap" style="background:#6366f1;color:#fff;padding:11px 22px;border-radius:6px;font-weight:600;text-decoration:none;font-size:14px;white-space:nowrap">Upgrade to Pro →</a>
    {% else %}
    <a href="https://dchub.cloud/signup?source=dcpi_anon_cap" style="background:#6366f1;color:#fff;padding:11px 22px;border-radius:6px;font-weight:600;text-decoration:none;font-size:14px;white-space:nowrap">Claim free key →</a>
    {% endif %}
  </div>
  {% endif %}

  <!-- phase 271: verdict tabs — Actionable is default so credibility-grade
       verdicts get visual primacy; Monitoring keeps LOW_SIGNAL covered but
       demoted; All preserves the full-coverage claim. Counts are accurate
       to the rendered DOM. -->
  <div class="verdict-tabs" role="tablist" aria-label="Filter markets by verdict">
    <button class="vt active" data-verdict-filter="actionable" role="tab" aria-selected="true">
      Actionable <span class="count">{{ count_actionable }}</span>
    </button>
    <button class="vt" data-verdict-filter="monitoring" role="tab" aria-selected="false">
      Monitoring <span class="count">{{ count_low_signal }}</span>
    </button>
    <button class="vt" data-verdict-filter="all" role="tab" aria-selected="false">
      All <span class="count">{{ count }}</span>
    </button>
  </div>

  <div class="toggle" role="tablist" aria-label="Switch score axis">
    <button class="active" data-mode="excess">Excess Power · Opportunity</button>
    <button data-mode="constraint">Constraint · Avoid</button>
  </div>

  <div class="grid" id="grid">
    {% for s in scores %}
    <a href="/dcpi/{{ s.market_slug }}" style="text-decoration:none;color:inherit;"
       class="card-link {% if s.verdict == 'LOW_SIGNAL' %}hidden-by-verdict{% endif %}"
       data-verdict="{{ s.verdict }}">
    <div class="card"{% if not gated_to_anon %} data-excess="{{ s.excess_power_score }}" data-constraint="{{ s.constraint_score }}"{% endif %}>
      <div class="market-name">{{ s.market_name }}</div>
      <div class="iso">{{ s.iso }} · {{ s.state }}</div>
      <div class="score-block excess-view">
        <div class="score{% if not gated_to_anon %} {{ 'green' if s.excess_power_score>=65 else 'orange' if s.excess_power_score>=40 else 'red' }}{% endif %}">{% if gated_to_anon %}🔒{% else %}{{ s.excess_power_score }}{% endif %}</div>
        <div class="label">Excess Power</div>
      </div>
      <div class="score-block constraint-view" style="display:none">
        <div class="score{% if not gated_to_anon %} {{ 'red' if s.constraint_score>=70 else 'orange' if s.constraint_score>=45 else 'green' }}{% endif %}">{% if gated_to_anon %}🔒{% else %}{{ s.constraint_score }}{% endif %}</div>
        <div class="label">Constraint</div>
      </div>
      <div class="verdict {{ s.verdict }}">{{ s.verdict }}</div>
      <div class="ttp">{% if gated_to_anon %}🔒 Pro{% else %}~{{ (s.time_to_power_months or 0)|round(0)|int }}mo to power{% endif %}</div>
    </div>
    </a>
    {% endfor %}
  </div>

  <!-- Phase AA (2026-05-12): ISO Intelligence panel — surfaces the
       per-ISO aggregate data we always had but never exposed. Each
       chip is a click-to-deep-dive into /dcpi/iso/<code>. Free preview;
       deep ISO comparison + alerts are Pro. -->
  <div class="section-h"><span class="pip"></span>🌐 ISO Intelligence (NEW)</div>
  <p style="color:var(--tx2);font-size:0.95rem;max-width:780px;margin-bottom:14px;">
    Eight North-American ISOs ranked across queue depth, average power cost, build verdicts, and curtailment risk. Click any ISO for the full diagnostic.
  </p>
  <div id="iso-grid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;margin-bottom:24px;">
    <div style="grid-column:1/-1;color:var(--tx2);font-size:0.85rem;padding:14px;text-align:center;border:1px dashed rgba(255,255,255,0.06);border-radius:10px;">Loading ISO intelligence…</div>
  </div>
  <script>
    // Phase AA: render ISO comparison chips from /api/v1/dcpi/iso-comparison.
    // Fail-soft — banner stays as loading if the endpoint is down.
    fetch('/api/v1/dcpi/iso-comparison').then(r => r.json()).then(data => {
      const grid = document.getElementById('iso-grid');
      const isos = (data && data.isos) || [];
      if (!isos.length) { grid.innerHTML = '<div style="grid-column:1/-1;color:var(--tx2);font-size:0.85rem;padding:14px;text-align:center;">ISO data is being recomputed — check back shortly.</div>'; return; }
      grid.innerHTML = isos.map(iso => {
        const queue = iso.avg_queue_wait_months != null ? iso.avg_queue_wait_months.toFixed(0) + 'mo' : '—';
        const cost  = iso.avg_kwh_cents != null ? '$' + (iso.avg_kwh_cents/100).toFixed(3) + '/kWh' : '—';
        const build = iso.build_count || 0;
        const total = iso.market_count || 0;
        const buildPct = total ? Math.round(100*build/total) : 0;
        const escapeIso = (iso.iso || '').toLowerCase().replace(/[^a-z0-9-]/g,'');
        return `<a href="/api/v1/dcpi/iso/${escapeIso}" style="text-decoration:none;color:inherit;display:block;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.08);border-radius:10px;padding:14px 16px;transition:.15s;"
                  onmouseover="this.style.borderColor='rgba(99,102,241,0.4)'" onmouseout="this.style.borderColor='rgba(255,255,255,0.08)'">
          <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px;">
            <div style="font-weight:700;font-size:1.1rem;color:#fff">${iso.iso || '?'}</div>
            <div style="font-size:0.75rem;color:var(--tx2);">${total} markets</div>
          </div>
          <div style="font-size:0.78rem;color:var(--tx2);margin-bottom:10px;line-height:1.35;">${iso.iso_name || ''}</div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:0.82rem;">
            <div><span style="color:var(--tx2)">Queue wait:</span> <b>${queue}</b></div>
            <div><span style="color:var(--tx2)">Avg cost:</span> <b>${cost}</b></div>
            <div><span style="color:var(--tx2)">BUILD verdicts:</span> <b style="color:#10b981">${build} (${buildPct}%)</b></div>
            <div><span style="color:var(--tx2)">Emergencies/30d:</span> <b>${iso.sum_emergency_30d || 0}</b></div>
          </div>
        </a>`;
      }).join('');
    }).catch(e => {
      const grid = document.getElementById('iso-grid');
      if (grid) grid.innerHTML = '<div style="grid-column:1/-1;color:var(--tx2);font-size:0.85rem;padding:14px;text-align:center;">ISO intelligence temporarily offline.</div>';
    });
  </script>

  <div id="pro-cta-block">
  <div class="section-h"><span class="pip"></span>🔓 Pro Access</div>
  <div class="cta-banner">
    <h2>Drill to county level. Get alerts. Export branded PDFs.</h2>
    <p>Pro shows scores at the county level so you can pinpoint where the headroom actually lives. Plus alert when any market moves &gt;5 points and one-click PDF export for your buyers. $199/mo.</p>
    <a class="btn" href="/pricing">Upgrade to Pro →</a>
  </div>
  </div>
  <script>
  /* r44 (2026-05-30): hide the "Upgrade to Pro" CTA for users who are ALREADY
     paid — it was shown to everyone, including enterprise, which read as broken.
     Defensive: only hides on a confirmed paid tier; leaves it for anon/free or
     on any fetch failure. */
  (function(){ try { fetch('/api/v1/me/tier', {credentials:'include'})
    .then(function(r){ return r.json(); })
    .then(function(d){
      var t = ((d && (d.tier || d.plan)) || '').toLowerCase();
      var paid = ['pro','enterprise','founding','developer','starter','admin'].indexOf(t) >= 0;
      if (paid) {
        var b = document.getElementById('pro-cta-block');
        if (b) b.style.display = 'none';
      }
      /* r-ssr-reconcile (2026-07-26, tier-gating QA): srv is the tier this
         RENDER was built for. If the session is actually PAID but we're
         showing the anon/free teaser (stale edge copy or race), reload ONCE —
         authed requests bypass the edge cache (zone rule 2026-07-26), so the
         reload returns the true per-tier render. sessionStorage guard = no
         reload loops, ever. */
      var srv = '{{ tier_state }}';
      if (paid && srv !== 'paid' && !sessionStorage.getItem('dcpi_tier_reload')) {
        sessionStorage.setItem('dcpi_tier_reload', '1');
        location.reload();
        return;
      }
      if (paid && srv === 'paid') {
        try { sessionStorage.removeItem('dcpi_tier_reload'); } catch(e2) {}
      }
    }).catch(function(){}); } catch(e){} })();
  </script>

  <div class="section-h"><span class="pip"></span>📋 Methodology</div>
  <p style="color:var(--tx2);font-size:0.92rem;max-width:720px;">
    <strong>Constraint Score</strong> combines queue wait time, reserve margin proximity to NERC floor, demand-growth YoY, and 30-day grid-emergency frequency.
    <strong style="color:var(--acc-light);">Excess Power Score</strong> is the contrarian metric: reserve-margin headroom, generation additions queued &lt;12mo, renewable curtailment volume, queue approval rate, stranded interconnection at retiring plants, and behind-the-meter industrial generation. Updated daily from ISO public filings + DC Hub's grid extractors.
  </p>

  <!-- r-coverage (2026-07-29): COVERAGE & EXPANSION. The expansion story used to
       live on /dcpi-v2, which was a frozen launch teaser ("275+ markets across
       14+ countries", a hardcoded Frankfurt MW figure) and has been deleted and
       301'd here. So: no literal counts, no MW claim, and every figure comes
       from the route as an int or None — None drops the figure and keeps the
       claim (see _dcpi_index_coverage / _dcpi_footprint_figures). Styling is
       the page's existing .section-h / .stats-row / .stat, unchanged. -->
  <div class="section-h"><span class="pip"></span>🌍 Coverage &amp; Expansion</div>
  <p style="color:var(--tx2);font-size:0.95rem;max-width:780px;margin-bottom:14px;">
    DCPI launched as a US-only, ISO-by-ISO read. It isn't one any more — metros outside North America are scored on the same two axes, by the same daily recompute, and appear in the same ranking as the US ones. Every figure in this section is measured when the page renders; if one can't be measured, the claim ships without the number instead of with a stale one.
  </p>
  {% if cov_markets or cov_grid_regions or cov_countries or cov_facilities %}
  <div class="stats-row">
    {% if cov_markets %}<div class="stat"><div class="num">{{ '{:,}'.format(cov_markets) }}</div><div class="label">Distinct Markets Scored</div></div>{% endif %}
    {% if cov_grid_regions %}<div class="stat"><div class="num">{{ '{:,}'.format(cov_grid_regions) }}</div><div class="label">Grid Regions &amp; Operators</div></div>{% endif %}
    {% if cov_countries %}<div class="stat"><div class="num">{{ '{:,}'.format(cov_countries) }}</div><div class="label">Countries · Facility Footprint</div></div>{% endif %}
    {% if cov_facilities %}<div class="stat"><div class="num">{{ '{:,}'.format(cov_facilities) }}</div><div class="label">Distinct Facilities Mapped</div></div>{% endif %}
  </div>
  {% endif %}
  {% if cov_countries or cov_facilities %}
  <p style="color:var(--tx2);font-size:0.9rem;max-width:780px;margin:0 0 1rem;">
    Read the halves separately: <strong>markets scored</strong> and <strong>grid regions</strong> measure the index itself. <strong>Countries</strong> and <strong>facilities</strong> measure the physical footprint the market list is built from &mdash; DC Hub tracks facilities in many more countries than the index currently scores, and the market universe is drawn from that footprint, not the reverse.
  </p>
  {% endif %}
  {% if cov_markets or cov_grid_regions or cov_countries or cov_facilities %}
  <p style="color:var(--tx3);font-size:0.84rem;max-width:780px;margin-bottom:2rem;">
    Where each figure comes from, so you can check it.
    {% if cov_markets %}Markets scored is the canonical distinct-market count &mdash; the same figure <a href="/api/v1/stats/canonical" style="color:var(--tx2);">/api/v1/stats/canonical</a> publishes as <span class="mono">dcpi_markets_scored</span>. It sits below the {{ total_rows }} entries listed further down this page because retired alias slugs and the rural aggregate regions collapse out of it.{% endif %}
    {% if cov_grid_regions %}Grid regions is the row count of <a href="/api/v1/dcpi/iso-comparison" style="color:var(--tx2);">/api/v1/dcpi/iso-comparison</a>, computed from this same published score set.{% endif %}
    {% if cov_countries or cov_facilities %}Footprint counts are <span class="mono">countries_covered</span> and <span class="mono">facilities_distinct</span> on <a href="/api/v1/stats/canonical" style="color:var(--tx2);">/api/v1/stats/canonical</a>.{% endif %}
    Free for press citation, like the rest of the index.
  </p>
  {% endif %}

  {% if all_market_links %}
  <div class="section-h"><span class="pip"></span>🗺️ All {{ all_market_links|length }} Markets</div>
  <p style="color:var(--tx2);font-size:0.88rem;max-width:720px;margin-bottom:8px;">Every market in the DC Hub Power Index — open any market's free detail page:</p>
  <div style="display:flex;flex-wrap:wrap;gap:4px 12px;font-size:0.9rem;line-height:1.85;max-width:960px;">
    {% for m in all_market_links %}<a href="/dcpi/{{ m.slug }}" style="color:var(--tx2);text-decoration:none;white-space:nowrap;">{{ m.name }}</a>{% if not loop.last %}<span style="color:#555;">·</span>{% endif %}{% endfor %}
  </div>
  {% endif %}

  <footer>
    <p>This is the free preview. Full methodology + raw data via <a href="/api-docs">API</a>. Press inquiries: <a href="/dcpi/press">press kit</a>.</p>
    <p>© 2026 DC Hub · Data Center Intelligence Platform · <a href="/about">About</a> · <a href="/pricing">Pricing</a> · <a href="/openapi.json">OpenAPI</a></p>
  </footer>
</div>

<script>
const buttons = document.querySelectorAll('.toggle button');
buttons.forEach(b => b.addEventListener('click', () => {
  buttons.forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  const mode = b.dataset.mode;
  document.querySelectorAll('.excess-view').forEach(v => v.style.display = mode==='excess'?'block':'none');
  document.querySelectorAll('.constraint-view').forEach(v => v.style.display = mode==='constraint'?'block':'none');
  const grid = document.getElementById('grid');
  const cards = Array.from(grid.children);
  cards.sort((a,b) => {
    const ea = parseFloat(a.querySelector('.card').dataset[mode]);
    const eb = parseFloat(b.querySelector('.card').dataset[mode]);
    return eb - ea;
  });
  cards.forEach(c => grid.appendChild(c));
}));

// phase 271: verdict-tab filter — Actionable / Monitoring / All
// Server has already pre-hidden LOW_SIGNAL on the initial DOM via the
// `hidden-by-verdict` class so the default view loads correctly even
// before JS executes. This script handles user clicks.
(function(){
  const tabs = document.querySelectorAll('.verdict-tabs button');
  if (!tabs.length) return;
  function apply(filter){
    document.querySelectorAll('.card-link').forEach(el => {
      const v = el.getAttribute('data-verdict') || '';
      const isLow = v === 'LOW_SIGNAL';
      let hide = false;
      if (filter === 'actionable') hide = isLow;
      else if (filter === 'monitoring') hide = !isLow;
      // 'all' — hide nothing
      el.classList.toggle('hidden-by-verdict', hide);
    });
  }
  tabs.forEach(b => b.addEventListener('click', () => {
    tabs.forEach(x => { x.classList.remove('active'); x.setAttribute('aria-selected','false'); });
    b.classList.add('active'); b.setAttribute('aria-selected','true');
    apply(b.getAttribute('data-verdict-filter'));
  }));
})();
</script>



<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<div id="dcpi-chart-section" style="margin:3rem 0;background:#11121a;border:1px solid #1f2030;border-radius:14px;padding:1.5rem;">
  <div style="display:flex;align-items:center;gap:0.6rem;margin-bottom:1rem;font-family:'Instrument Sans',sans-serif;">
    <span style="width:4px;height:12px;background:#6366f1;border-radius:2px;"></span>
    <span style="font-size:0.78rem;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:#9ca3af;">📈 30-day Excess Power · Top 3 BUILD markets</span>
  </div>
  <div style="position:relative;height:280px;"><canvas id="dcpi-history-chart"></canvas></div>
</div>
<script>
(function(){
  if (!document.getElementById('dcpi-history-chart')) return;
  fetch('/api/v1/dcpi/history').then(r=>r.json()).then(d=>{
    const series = d.series || {};
    const top3 = ['cheyenne-wy','rural-spp','williston-nd'];
    const colors = ['#10b981','#a855f7','#6366f1'];
    const datasets = top3.map((slug,i)=>{
      const s = series[slug]; if (!s) return null;
      return { label: s.name, data: s.data.map(p=>({x:p.day,y:p.excess})),
               borderColor: colors[i], backgroundColor: colors[i]+'22',
               borderWidth: 2.5, tension: 0.35, pointRadius: 0 };
    }).filter(Boolean);
    if (!datasets.length) {
      document.getElementById('dcpi-chart-section').style.display = 'none';
      return;
    }
    new Chart(document.getElementById('dcpi-history-chart'), {
      type: 'line', data: { datasets },
      options: { responsive: true, maintainAspectRatio: false,
        plugins: { legend: { labels: { color: '#9ca3af' } } },
        scales: {
          x: { type: 'time', time: { unit: 'day' }, ticks: { color: '#9ca3af' }, grid: { color: '#1f2030' } },
          y: { ticks: { color: '#9ca3af' }, grid: { color: '#1f2030' }, suggestedMin: 0, suggestedMax: 100 }
        }
      }
    });
  }).catch(e=>{
    console.error('[DCPI chart] error', e);
    document.getElementById('dcpi-chart-section').style.display = 'none';
  });
})();
</script>


<div id="dcpi-subscribe" style="margin:3rem 0;background:linear-gradient(135deg,rgba(99,102,241,0.10),rgba(168,85,247,0.06));border:1px solid #2a2c3e;border-radius:14px;padding:1.5rem;">
  <div style="display:flex;align-items:center;gap:0.6rem;margin-bottom:0.6rem;font-family:'Instrument Sans',sans-serif;">
    <span style="width:4px;height:12px;background:#6366f1;border-radius:2px;"></span>
    <span style="font-size:0.78rem;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:#9ca3af;">📬 Daily DCPI Brief</span>
  </div>
  <h3 style="margin:0 0 0.4rem;font-size:1.2rem;font-weight:700;font-family:'Instrument Sans',sans-serif;">Wake up to the DC market.</h3>
  <p style="margin:0 0 1rem;color:#9ca3af;font-size:0.92rem;font-family:'Instrument Sans',sans-serif;">Top 5 BUILD markets, biggest movers, news count — emailed Mon–Fri at 14:00 UTC. Free.</p>
  <form id="dcpi-sub-form" style="display:flex;gap:0.5rem;flex-wrap:wrap;">
    <input type="email" id="dcpi-sub-email" placeholder="you@company.com" required
      style="flex:1;min-width:220px;background:#0a0a12;border:1px solid #1f2030;color:white;padding:0.7rem 1rem;border-radius:6px;font-family:'Instrument Sans',sans-serif;font-size:0.92rem;outline:none;">
    <button type="submit" id="dcpi-sub-go"
      style="background:linear-gradient(135deg,#6366f1,#a855f7);color:white;border:0;padding:0.7rem 1.3rem;border-radius:6px;font-weight:700;font-size:0.9rem;cursor:pointer;font-family:'Instrument Sans',sans-serif;">Subscribe →</button>
  </form>
  <div id="dcpi-sub-msg" style="margin-top:0.6rem;font-size:0.85rem;color:#9ca3af;font-family:'Instrument Sans',sans-serif;"></div>
</div>
<script>
(function(){
  const f = document.getElementById('dcpi-sub-form'); if (!f) return;
  f.addEventListener('submit', async function(e){
    e.preventDefault();
    const em = document.getElementById('dcpi-sub-email').value.trim();
    const msg = document.getElementById('dcpi-sub-msg');
    const btn = document.getElementById('dcpi-sub-go');
    btn.disabled = true;
    msg.textContent = 'Subscribing...';
    try {
      const r = await fetch('/api/v1/digest/subscribe', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({email: em})
      });
      const d = await r.json();
      if (d.ok) {
        msg.innerHTML = '<span style="color:#10b981">✓ You\\'re in. First brief lands tomorrow at 14:00 UTC.</span>';
        document.getElementById('dcpi-sub-email').value = '';
      } else {
        msg.innerHTML = '<span style="color:#ef4444">' + (d.error || 'error') + '</span>';
      }
    } catch (e) {
      msg.innerHTML = '<span style="color:#ef4444">Error: ' + e + '</span>';
    } finally { btn.disabled = false; }
  });
})();
</script>

<div id="ask-the-index" style="position:fixed;bottom:1.5rem;right:1.5rem;width:400px;max-width:calc(100vw - 3rem);background:#11121a;border:1px solid #2a2c3e;border-radius:14px;padding:1.1rem;font-family:'Instrument Sans',system-ui;color:white;box-shadow:0 16px 48px rgba(0,0,0,0.5);z-index:1000;">
  <div style="display:flex;align-items:center;gap:0.5rem;margin-bottom:0.6rem;">
    <span style="display:inline-block;width:8px;height:8px;background:#10b981;border-radius:50%;animation:pulse 1.4s ease-in-out infinite;"></span>
    <strong style="font-size:0.78rem;letter-spacing:0.06em;text-transform:uppercase;color:#9ca3af;">Ask the Index</strong>
  </div>
  <div id="ask-out" style="font-size:0.88rem;line-height:1.55;min-height:80px;color:#ddd;margin-bottom:0.6rem;max-height:340px;overflow-y:auto;padding:0.4rem 0;">
    Ask anything about U.S. data center power markets — try: <em style="color:#a5b4fc">show me markets above 70 excess within 300 miles of Atlanta</em>
  </div>
  <textarea id="ask-q" placeholder="e.g. where can I get 100MW within 12 months?" style="width:100%;background:#0a0a12;border:1px solid #1f2030;color:white;padding:0.6rem 0.8rem;border-radius:6px;font-family:inherit;font-size:0.88rem;min-height:54px;resize:none;outline:none;"></textarea>
  <button id="ask-go" style="width:100%;margin-top:0.5rem;background:linear-gradient(135deg,#6366f1,#a855f7);color:white;border:0;padding:0.6rem;border-radius:6px;font-weight:700;font-size:0.88rem;cursor:pointer;">Ask DCPI →</button>
</div>
<script>
(function(){
  function bind(){
    var go = document.getElementById('ask-go');
    var q = document.getElementById('ask-q');
    var out = document.getElementById('ask-out');
    if (!go || !q || !out) {
      console.error('[Ask DCPI] DOM elements not found', {go: !!go, q: !!q, out: !!out});
      return;
    }
    console.log('[Ask DCPI] handlers bound');

    function showError(msg){
      out.innerHTML = '<span style="color:#ef4444;">' + msg + '</span>';
    }

    async function send(){
      var question = (q.value || '').trim();
      if (!question) { q.focus(); return; }
      out.innerHTML = '<em style="color:#9ca3af;">Thinking…</em>';
      go.disabled = true;
      go.style.opacity = '0.6';
      try {
        var resp = await fetch('/api/v1/dcpi/ask?q=' + encodeURIComponent(question), {
          method: 'GET',
          headers: { 'Accept': 'application/json' },
          credentials: 'same-origin'
        });
        if (!resp.ok) {
          showError('HTTP ' + resp.status + ': ' + (await resp.text()).slice(0, 200));
          return;
        }
        var data = await resp.json();
        if (data.error) {
          showError(data.error);
          return;
        }
        var answer = (data.answer || 'No answer.')
          .replace(/\\n/g, '<br>')
          .replace(/\\[([^\\]]+)\\]/g, '<strong style="color:#a5b4fc">[$1]</strong>');
        out.innerHTML = answer;
      } catch(e) {
        console.error('[Ask DCPI] fetch error', e);
        showError('Error: ' + (e && e.message ? e.message : e));
      } finally {
        go.disabled = false;
        go.style.opacity = '1';
      }
    }

    go.addEventListener('click', send);
    q.addEventListener('keydown', function(e){
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        send();
      }
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }
})();
</script>


<!-- r66-a (2026-05-26): Cited-by strip — proof that ChatGPT, Claude,
     Gemini, Perplexity, and Groq quote DCPI. Linked to /cited-by for
     the full citation gallery + per-platform examples. Lives right
     above the "Cite this index" block so readers see the social
     proof, THEN the citation format. -->
<div style="background:linear-gradient(135deg,rgba(34,211,238,0.05) 0%,rgba(139,92,246,0.05) 100%);border:1px solid rgba(34,211,238,0.18);border-radius:12px;padding:24px 28px;margin:32px auto;max-width:760px;font-family:system-ui">
  <div style="font-size:11px;color:#a78bfa;text-transform:uppercase;letter-spacing:.14em;margin-bottom:14px;font-weight:700">Cited by AI</div>
  <div style="display:flex;flex-wrap:wrap;gap:10px 14px;align-items:center;margin-bottom:12px">
    <span style="background:rgba(255,255,255,.06);color:#e8eef8;padding:6px 12px;border-radius:999px;font-size:13px;font-weight:600">ChatGPT</span>
    <span style="background:rgba(255,255,255,.06);color:#e8eef8;padding:6px 12px;border-radius:999px;font-size:13px;font-weight:600">Claude</span>
    <span style="background:rgba(255,255,255,.06);color:#e8eef8;padding:6px 12px;border-radius:999px;font-size:13px;font-weight:600">Gemini</span>
    <span style="background:rgba(255,255,255,.06);color:#e8eef8;padding:6px 12px;border-radius:999px;font-size:13px;font-weight:600">Perplexity</span>
    <span style="background:rgba(255,255,255,.06);color:#e8eef8;padding:6px 12px;border-radius:999px;font-size:13px;font-weight:600">Groq</span>
    <span style="color:#9eb5d8;font-size:13px">· and 10+ AI platforms</span>
  </div>
  <p style="color:#cbd5ff;font-size:14px;margin:0 0 12px;line-height:1.55">ChatGPT, Gemini, and Groq all named DC Hub in independent answers within a single week — Groq quoted DCPI's ERCOT 410 GW interconnection-queue numbers verbatim. <a href="/cited-by" style="color:#22d3ee;text-decoration:none;font-weight:600">See the full citations →</a></p>
</div>

<div style="background:#11121a;border:1px solid #1f2030;border-radius:12px;padding:20px;margin:32px auto;max-width:760px;font-family:system-ui">
  <div style="font-size:12px;color:#9eb5d8;text-transform:uppercase;letter-spacing:.1em;margin-bottom:8px">Cite this index</div>
  <code style="display:block;background:rgba(255,255,255,.03);padding:12px;border-radius:6px;color:#e8eef8;font-size:13px;margin-bottom:8px">DC Hub. (2026). Data Center Power Index v2. https://dchub.cloud/dcpi</code>
  <a href="/dcpi/methodology" style="color:#5aa3ff;font-size:14px;text-decoration:none">View methodology + BibTeX →</a>
</div>
<script>
// Phase 241: live DCPI market count
(function(){
  fetch('/api/v1/dcpi/live-count')
    .then(r => r.json())
    .then(d => {
      const n = d.published || d.total || 280;
      // Find any element containing "280+ MARKETS" or hardcoded number, replace with live count
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      let node;
      while (node = walker.nextNode()) {
        if (/\\b(280\\+?|276)\\s*MARKETS/.test(node.nodeValue)) {
          node.nodeValue = node.nodeValue.replace(/\\b(280\\+?|276)\\s*MARKETS/, n + ' MARKETS');
        }
        if (/\\b(280\\+?|276)\\s+U\\.S\\./.test(node.nodeValue)) {
          node.nodeValue = node.nodeValue.replace(/\\b(280\\+?|276)\\s+U\\.S\\./, n + ' U.S.');
        }
      }
    })
    .catch(() => {});
})();
</script>
</body>
</html>"""


DCPI_MARKET_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{{ s.market_name }} · DCPI {% if gated %}{{ s.verdict or 'LOW_SIGNAL' }}{% else %}{{ s.composite_score }}{% endif %}{% if s.iso %} · {{ s.iso }} grid{% endif %} | DC Hub</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<!-- r78: all ~308 DCPI city pages shipped WITHOUT a meta description (GSC
     indexability drag) — og:description existed but Google reads name=description.
     r-gate-everywhere (2026-06-27): numeric scores are Pro — anon/crawler meta is
     verdict-only (non-cloaking: same gated meta to humans AND Googlebot). -->
<meta name="description" content="{{ s.market_name }} Data Center Power Index: {{ s.verdict or 'LOW_SIGNAL' }} verdict{{ (', ' ~ s.iso) if s.iso else '' }}.{% if gated %} Numeric power-readiness scores (excess-power, grid-constraint, time-to-power) available to DC Hub Pro at dchub.cloud/pricing — recomputed daily.{% else %} Excess Power {{ (s.excess_power_score or 0)|round(1) }}/100, Grid Constraint {{ (s.constraint_score or 0)|round(1) }}/100. Power availability, time-to-power, and queue context — recomputed daily by DC Hub.{% endif %}">
<meta property="og:title" content="{{ s.market_name }}{% if gated %} · DCPI {{ s.verdict or 'LOW_SIGNAL' }}{% else %} · DCPI {{ s.composite_score }} · Excess {{ s.excess_power_score }} · Constraint {{ s.constraint_score }}{% endif %}">
<meta property="og:description" content="{{ s.verdict or 'LOW_SIGNAL' }}{% if gated %} · Numeric DCPI scores available to DC Hub Pro. {% else %} · ~{{ (s.time_to_power_months or 0)|round(0)|int }} months to power. {% endif %}Updated daily.">
<meta property="og:image" content="https://dchub.cloud/dcpi/og/{{ s.market_slug }}.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:image:type" content="image/png">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="https://dchub.cloud/dcpi/og/{{ s.market_slug }}.png">
<link rel="canonical" href="https://dchub.cloud/dcpi/{{ s.market_slug }}">
<meta name="robots" content="index,follow,max-snippet:-1,max-image-preview:large">
<!-- seo: per-market DCPI structured data so AI Overviews + agents ingest the
     load-bearing numbers (Excess Power score, Constraint score, verdict, ISO,
     coordinates) directly. schema.org Dataset is the type Google Dataset
     Search + LLM crawlers index. Values emitted via Jinja |tojson so they're
     correctly JSON-escaped/typed; geo is omitted when lat/long are null. -->
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Dataset",
  "name": {{ (s.market_name ~ " — Data Center Power Index (DCPI)")|tojson }},
  "description": {% if gated %}{{ ((s.market_name ~ ": DCPI verdict " ~ (s.verdict or "LOW_SIGNAL") ~ (", ISO " ~ s.iso if s.iso else "") ~ ". Numeric DCPI scores (Excess Power, Grid Constraint, time-to-power) are available to DC Hub Pro at dchub.cloud/pricing, or to AI agents via the DC Hub MCP (dchub.cloud/mcp). Recomputed daily."))|tojson }}{% else %}{{ ((s.market_name ~ ": DCPI verdict " ~ (s.verdict or "LOW_SIGNAL") ~ ". Excess Power score " ~ ((s.excess_power_score or 0)|round(1)) ~ "/100, Grid Constraint score " ~ ((s.constraint_score or 0)|round(1)) ~ "/100" ~ (", ISO " ~ s.iso if s.iso else "") ~ ". Recomputed daily by DC Hub from interconnection-queue, capacity-pipeline, and grid-emergency signals."))|tojson }}{% endif %},
  "url": "https://dchub.cloud/dcpi/{{ s.market_slug }}",
  "creator": {"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"},
  "publisher": {"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"},
  "isAccessibleForFree": {% if gated %}false{% else %}true{% endif %},
  "isPartOf": {"@type": "Dataset", "name": "Data Center Power Index (DCPI)", "url": "https://dchub.cloud/dcpi", "description": "Daily-recomputed power-readiness index for 300 data center markets: Excess Power score, Grid Constraint score, and a BUILD/CAUTION/AVOID verdict per market."},
  "keywords": {{ (("data center power, DCPI, " ~ s.market_name ~ ", grid constraint, excess power, " ~ (s.iso or "ISO") ~ ", site selection, " ~ (s.verdict or "LOW_SIGNAL")))|tojson }},
  "temporalCoverage": "2024-01-01/..",
  "citation": "DC Hub Data Center Power Index, dchub.cloud/dcpi",
  "distribution": [{"@type": "DataDownload", "encodingFormat": "application/json", "contentUrl": "https://dchub.cloud/mcp"}],
  "potentialAction": {"@type": "SearchAction", "target": {"@type": "EntryPoint", "urlTemplate": "https://dchub.cloud/api/v1/rag/search?q={search_term_string}"}, "query-input": "required name=search_term_string"},
  {% if s.computed_at %}"dateModified": {{ (s.computed_at[:10])|tojson }},
  {% endif %}"measurementTechnique": "DCPI is recomputed daily from interconnection-queue depth, capacity-pipeline additions, grid-emergency events, and reserve-margin signals for the serving ISO/RTO. Methodology: dchub.cloud/dcpi",
  "spatialCoverage": {
    "@type": "Place",
    "name": {{ place_label|tojson }}{% if s.latitude is not none and s.longitude is not none %},
    "geo": {"@type": "GeoCoordinates", "latitude": {{ s.latitude|tojson }}, "longitude": {{ s.longitude|tojson }}}{% endif %}
  },
  "variableMeasured": [
    {% if not gated %}{"@type": "PropertyValue", "name": "Excess Power Score", "value": {{ ((s.excess_power_score or 0)|round(1))|tojson }}, "minValue": 0, "maxValue": 100, "description": "Higher = more available/stranded power capacity"},
    {"@type": "PropertyValue", "name": "Grid Constraint Score", "value": {{ ((s.constraint_score or 0)|round(1))|tojson }}, "minValue": 0, "maxValue": 100, "description": "Higher = more impediment to new data-center builds"},
    {% endif %}{"@type": "PropertyValue", "name": "DCPI Verdict", "value": {{ (s.verdict or "LOW_SIGNAL")|tojson }}, "description": "BUILD / CAUTION / AVOID / LOW_SIGNAL"}{% if s.iso %},
    {"@type": "PropertyValue", "name": "ISO / Grid Operator", "value": {{ s.iso|tojson }}}{% endif %}{% if s.time_to_power_months is not none %},
    {"@type": "PropertyValue", "name": "Time to Power (months)", "value": {{ (s.time_to_power_months|round(0)|int)|tojson }}, "unitText": "MON", "description": "Estimated months to energize new large load in this market (free/agent-accessible decision output)"}{% endif %}{% if s.queue_wait_months is not none %},
    {"@type": "PropertyValue", "name": "Interconnection Queue Wait (months)", "value": {{ (s.queue_wait_months|round(0)|int)|tojson }}, "unitText": "MON", "description": "Typical interconnection-queue wait for the serving grid region"}{% endif %}
  ]
}
</script>
<!-- r-geo-dcpi-faq (2026-06-25): BreadcrumbList (crawl context) + FAQPage (the schema
     AI engines lift verbatim into cited answers, e.g. "Is Ashburn BUILD or AVOID?").
     Q&A generated from the live score via |tojson so it can never contradict the page. -->
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "BreadcrumbList",
  "itemListElement": [
    {"@type": "ListItem", "position": 1, "name": "DC Hub", "item": "https://dchub.cloud/"},
    {"@type": "ListItem", "position": 2, "name": "Power Index (DCPI)", "item": "https://dchub.cloud/dcpi"},
    {"@type": "ListItem", "position": 3, "name": {{ s.market_name|tojson }}, "item": "https://dchub.cloud/dcpi/{{ s.market_slug }}"}
  ]
}
</script>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "FAQPage",
  "mainEntity": [
    {"@type": "Question", "name": {{ ("What is the DCPI score for " ~ s.market_name ~ "?")|tojson }},
     "acceptedAnswer": {"@type": "Answer", "text": {% if gated %}{{ (s.market_name ~ " has a DC Hub Power Index (DCPI) verdict of " ~ (s.verdict or "LOW_SIGNAL") ~ ". The numeric Excess Power and Grid Constraint scores are available to DC Hub Pro at dchub.cloud/pricing (or to AI agents via dchub.cloud/mcp). Recomputed daily.")|tojson }}{% else %}{{ (s.market_name ~ " has a DC Hub Power Index (DCPI) verdict of " ~ (s.verdict or "LOW_SIGNAL") ~ ", with an Excess Power score of " ~ ((s.excess_power_score or 0)|round(1)) ~ "/100 and a Grid Constraint score of " ~ ((s.constraint_score or 0)|round(1)) ~ "/100. Recomputed daily by DC Hub.")|tojson }}{% endif %}}},
    {"@type": "Question", "name": {{ ("Is " ~ s.market_name ~ " a good market to build a data center?")|tojson }},
     "acceptedAnswer": {"@type": "Answer", "text": {% if gated %}{{ ("DC Hub rates " ~ s.market_name ~ " as " ~ (s.verdict or "LOW_SIGNAL") ~ " for new data-center builds" ~ (", in the " ~ s.iso ~ " grid region" if s.iso else "") ~ ". The underlying Excess Power and Grid Constraint scores are DC Hub Pro (dchub.cloud/pricing).")|tojson }}{% else %}{{ ("DC Hub rates " ~ s.market_name ~ " as " ~ (s.verdict or "LOW_SIGNAL") ~ " for new data-center builds, based on an Excess Power score of " ~ ((s.excess_power_score or 0)|round(1)) ~ "/100 and a Grid Constraint score of " ~ ((s.constraint_score or 0)|round(1)) ~ "/100" ~ (", in the " ~ s.iso ~ " grid region" if s.iso else "") ~ ".")|tojson }}{% endif %}}}{% if s.iso %},
    {"@type": "Question", "name": {{ ("Which grid operator (ISO) serves " ~ s.market_name ~ "?")|tojson }},
     "acceptedAnswer": {"@type": "Answer", "text": {{ (s.market_name ~ " is in the " ~ s.iso ~ " ISO/RTO grid region.")|tojson }}}}{% endif %}
  ]
}
</script>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root {
  --bg:#0a0a12; --bg2:#0f1119; --card:#11121a; --bd:#1f2030; --bd-hi:#2a2c3e;
  --tx:#fff; --tx2:#9ca3af; --tx3:#6b7280;
  --acc:#6366f1; --acc-light:#818cf8; --acc-vivid:#a855f7;
  --green:#10b981; --orange:#f59e0b; --red:#ef4444;
  --gradient:linear-gradient(135deg,#6366f1 0%,#a855f7 100%);
}
* { box-sizing: border-box; }
body {
  font-family: 'Inter', -apple-system, system-ui, sans-serif;
  background: var(--bg); color: var(--tx); margin: 0; padding: 0;
  line-height: 1.55; -webkit-font-smoothing: antialiased;
}
.mono, code { font-family: 'JetBrains Mono', monospace; }

.top-nav {
  border-bottom: 1px solid var(--bd);
  background: rgba(10,10,18,0.85);
  backdrop-filter: blur(8px);
  position: sticky; top: 0; z-index: 100;
}
.top-nav-inner {
  max-width: 1080px; margin: 0 auto; padding: 1rem 1.5rem;
  display: flex; align-items: center; justify-content: space-between; gap: 1.5rem;
}
.logo { font-weight: 800; font-size: 1.05rem; color: var(--tx); text-decoration: none; }
.logo span { color: var(--acc); }
.nav-links { display: flex; gap: 1.5rem; flex-wrap: wrap; }
.nav-links a { color: var(--tx2); text-decoration: none; font-size: 0.92rem; font-weight: 500; }
.nav-links a:hover { color: var(--tx); }
.nav-links a.active { color: var(--tx); }

.wrap { max-width: 1080px; margin: 0 auto; padding: 2.5rem 1.5rem; }
.crumbs {
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.78rem; color: var(--tx3); margin-bottom: 1rem;
}
.crumbs a { color: var(--acc-light); text-decoration: none; }
.crumbs a:hover { color: var(--tx); }

h1 {
  font-size: clamp(2.2rem, 5vw, 3.2rem);
  margin: 0 0 0.4rem;
  font-weight: 800;
  letter-spacing: -0.025em;
  line-height: 1.05;
}
.subtitle {
  color: var(--tx2);
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.9rem;
  margin: 0 0 2rem;
}

.verdict-banner {
  padding: 1.1rem 1.5rem;
  border-radius: 10px;
  margin: 2rem 0;
  font-weight: 700;
  font-size: 1rem;
  border: 1px solid;
}
.verdict-banner.BUILD   { background: rgba(16,185,129,0.10); border-color: var(--green); color: var(--green); }
.verdict-banner.CAUTION { background: rgba(245,158,11,0.10); border-color: var(--orange); color: var(--orange); }
.verdict-banner.AVOID   { background: rgba(239,68,68,0.10); border-color: var(--red); color: var(--red); }

.scoreboard {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 1rem;
  margin: 2rem 0;
}
.sb {
  background: var(--card);
  border: 1px solid var(--bd);
  border-radius: 12px;
  padding: 1.75rem;
  position: relative;
  overflow: hidden;
}
.sb::after {
  content: '';
  position: absolute; inset: 0;
  background: linear-gradient(135deg, rgba(99,102,241,0.05), transparent 60%);
  pointer-events: none;
}
.sb .v {
  font-family: 'JetBrains Mono', monospace;
  font-size: clamp(3.5rem, 8vw, 5.5rem);
  font-weight: 800;
  line-height: 1;
  letter-spacing: -0.03em;
}
.sb .l {
  color: var(--tx2);
  font-size: 0.78rem;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  margin-top: 0.6rem;
  font-weight: 600;
}
.green { color: var(--green); }
.orange { color: var(--orange); }
.red { color: var(--red); }

.section-h {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  margin: 2.5rem 0 1rem;
  font-size: 0.78rem;
  font-weight: 700;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--tx2);
}
.section-h .pip { width: 4px; height: 12px; background: var(--acc); border-radius: 2px; }

.section {
  background: var(--card);
  border: 1px solid var(--bd);
  border-radius: 12px;
  padding: 1.75rem;
  margin: 1rem 0;
}
.section h2 {
  margin: 0 0 1rem;
  font-size: 1.2rem;
  font-weight: 700;
  letter-spacing: -0.01em;
}
.section ul { padding-left: 1.2rem; margin: 0; }
.section li {
  margin: 0.5rem 0;
  color: #ddd;
  font-size: 0.95rem;
}

.metrics {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px,1fr));
  gap: 0.85rem;
}
.metric {
  background: var(--bg2);
  border: 1px solid var(--bd);
  border-radius: 8px;
  padding: 0.9rem 1.1rem;
  transition: border-color 0.15s;
}
.metric:hover { border-color: var(--bd-hi); }
.metric .v {
  font-family: 'JetBrains Mono', monospace;
  font-size: 1.35rem;
  font-weight: 700;
  letter-spacing: -0.01em;
}
/* r-null-not-zero: an absent measurement must not look like a value. Smaller,
   dimmer and non-numeric, so it can never be mistaken for a reading of zero. */
.metric .v.nm {
  font-size: 0.82rem;
  font-weight: 500;
  color: var(--tx2);
  letter-spacing: 0.01em;
  text-transform: none;
}
.metric .l {
  color: var(--tx2);
  font-size: 0.7rem;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  margin-top: 0.3rem;
  font-weight: 600;
}

.cta-pro {
  background: var(--gradient);
  padding: 2rem 2.25rem;
  border-radius: 14px;
  margin: 2rem 0;
  position: relative;
  overflow: hidden;
}
.cta-pro::after {
  content: '';
  position: absolute;
  right: -40px; bottom: -40px;
  width: 200px; height: 200px;
  background: radial-gradient(circle, rgba(255,255,255,0.15), transparent 70%);
  pointer-events: none;
}
.cta-pro h2 { margin: 0 0 0.5rem; font-size: 1.3rem; color: white; }
.cta-pro p {
  margin: 0 0 1.1rem;
  color: rgba(255,255,255,0.88);
  font-size: 0.94rem;
}
.cta-pro a {
  display: inline-block;
  background: white; color: var(--acc);
  padding: 0.65rem 1.2rem; border-radius: 6px;
  text-decoration: none; font-weight: 700;
  font-size: 0.9rem;
}

@media (max-width: 600px) {
  .nav-links { display: none; }
}
</style>
</head>
<body>

<nav class="top-nav">
  <div class="top-nav-inner">
    <a class="logo" href="/">DC <span>Hub</span></a>
    <div class="nav-links">
      <a href="/">Home</a>
      <a href="/markets">Markets</a>
      <a href="/dcpi" class="active">DCPI</a>
      <a href="/land-power">Land &amp; Power</a>
      <a href="/ai">AI Platform</a>
      <a href="/news">News</a>
      <a href="/pricing">Pricing</a>
    </div>
  </div>
</nav>

<div class="wrap">
  <div class="crumbs"><a href="/dcpi">DCPI</a> / <a href="/markets">Markets</a> / {{ s.market_name }}</div>
  <h1>{{ s.market_name }}</h1>
  <p class="subtitle">{{ s.iso }} · {{ s.state }} · UPDATED {{ s.computed_at[:10] }}</p>
  <p class="signal-tier" style="margin:.2rem 0 .3rem;font-size:.78rem;color:#9ca3af">SIGNAL QUALITY:
    <strong style="color:{% if s.signal_tier == 'full' %}#22c55e{% elif s.signal_tier == 'partial' %}#f59e0b{% elif s.signal_tier == 'low' %}#ef4444{% else %}#6b7280{% endif %}">{{ (s.signal_tier or 'unrecorded')|upper }}</strong>
    — {{ s.signal_tier_basis }}</p>
  <p class="dc-maplink" style="margin:.4rem 0 .2rem"><a href="/map" style="color:#3b82f6;font-weight:600;text-decoration:none">📍 See {{ s.market_name }} data centers on the live facility map →</a></p>

  <div class="verdict-banner {{ s.verdict }}">
    {% if s.verdict == 'BUILD' %}🟢 BUILD HERE — Excess capacity available, manageable constraints
    {% elif s.verdict == 'CAUTION' %}🟡 CAUTION — Mixed signals, due-diligence required
    {% else %}🔴 AVOID FOR NEW BUILDS — Severe constraints, multi-year wait{% endif %}
  </div>

  <!-- r-geo-rag-summary (2026-07-31, Gemini round 4): zero-JS semantic summary
       table as the FIRST extractable body block — RAG fetchers on latency
       budgets extract body tables before head JSON-LD; the Dataset/FAQ schemas
       above mirror these same values. Gating unchanged (r-gate-everywhere):
       numeric scores render only in the non-gated branch — same HTML for
       humans and crawlers, never a per-UA variant. -->
  <section id="dchub-market-summary" aria-label="Data center market summary" data-source="dchub.cloud"
           style="background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:1rem 1.2rem;margin:1.2rem 0">
    <h2 style="font-size:1rem;margin:0 0 .6rem">{{ s.market_name }} data-center market — grid summary</h2>
    <table style="width:100%;border-collapse:collapse;font-size:.92rem">
      <thead><tr>
        <th style="text-align:left;padding:.3rem .4rem;border-bottom:1px solid var(--bd);color:var(--tx2)">Metric</th>
        <th style="text-align:left;padding:.3rem .4rem;border-bottom:1px solid var(--bd);color:var(--tx2)">Value</th>
        <th style="text-align:left;padding:.3rem .4rem;border-bottom:1px solid var(--bd);color:var(--tx2)">Source</th>
      </tr></thead>
      <tbody>
        <tr><td style="padding:.3rem .4rem">DCPI verdict</td><td style="padding:.3rem .4rem"><strong>{{ s.verdict or 'LOW_SIGNAL' }}</strong></td><td style="padding:.3rem .4rem">DC Hub Power Index — recomputed daily</td></tr>
        {% if s.iso %}<tr><td style="padding:.3rem .4rem">Grid operator (ISO)</td><td style="padding:.3rem .4rem">{{ s.iso }}</td><td style="padding:.3rem .4rem">DC Hub grid telemetry</td></tr>{% endif %}
        {% if not gated %}
        <tr><td style="padding:.3rem .4rem">Excess-power score</td><td style="padding:.3rem .4rem">{{ (s.excess_power_score or 0)|round(1) }} / 100</td><td style="padding:.3rem .4rem">DCPI</td></tr>
        <tr><td style="padding:.3rem .4rem">Grid-constraint score</td><td style="padding:.3rem .4rem">{{ (s.constraint_score or 0)|round(1) }} / 100</td><td style="padding:.3rem .4rem">DCPI</td></tr>
        {% if s.time_to_power_months is not none %}<tr><td style="padding:.3rem .4rem">Est. time-to-power</td><td style="padding:.3rem .4rem">{{ s.time_to_power_months|round(0)|int }} months</td><td style="padding:.3rem .4rem">DCPI</td></tr>{% endif %}
        {% if s.queue_wait_months is not none %}<tr><td style="padding:.3rem .4rem">Interconnection queue wait</td><td style="padding:.3rem .4rem">{{ s.queue_wait_months|round(0)|int }} months</td><td style="padding:.3rem .4rem">Serving-ISO queue</td></tr>{% endif %}
        {% else %}
        <tr><td style="padding:.3rem .4rem">Numeric scores</td><td style="padding:.3rem .4rem">DC Hub Pro — same gated view for humans and crawlers (<a href="/pricing" style="color:var(--acc-light)">pricing</a>)</td><td style="padding:.3rem .4rem">DCPI</td></tr>
        {% endif %}
        <tr><td style="padding:.3rem .4rem">Signal quality</td><td style="padding:.3rem .4rem">{{ (s.signal_tier or 'unrecorded')|upper }}</td><td style="padding:.3rem .4rem">{{ s.signal_tier_basis }}</td></tr>
      </tbody>
    </table>
    <p style="margin:.6rem 0 0;font-size:.82rem;color:var(--tx2)"><em>Source: DC Hub (dchub.cloud), updated {{ s.computed_at[:10] }}. AI agents: <code>get_market_dcpi_rank market_slug="{{ s.market_slug }}"</code> via https://dchub.cloud/mcp for the citable record.</em></p>
  </section>

  {% if narrative %}
  <div style="background:rgba(99,102,241,0.06);border-left:3px solid var(--acc);
              padding:1.4rem 1.8rem;border-radius:8px;margin:1.5rem 0 2rem;
              font-size:1.02rem;line-height:1.7;color:#e5e7eb">
    <div style="font-family:'JetBrains Mono',monospace;font-size:0.72rem;
                text-transform:uppercase;letter-spacing:0.12em;color:var(--acc);
                margin-bottom:0.6rem">Analyst read · auto-generated · claude-haiku</div>
    {{ narrative }}
  </div>
  {% endif %}

  <div class="scoreboard">
    {% if gated %}
    <div class="sb">
      <div class="v" style="color:var(--tx2)">🔒</div>
      <div class="l">Excess Power Score · <a href="/pricing" style="color:#5aa3ff;text-decoration:none">Unlock with Pro</a></div>
    </div>
    <div class="sb">
      <div class="v" style="color:var(--tx2)">🔒</div>
      <div class="l">Constraint Score · <a href="/pricing" style="color:#5aa3ff;text-decoration:none">Unlock with Pro</a></div>
    </div>
    {% else %}
    <div class="sb">
      <div class="v {{ 'green' if s.excess_power_score>=65 else 'orange' if s.excess_power_score>=40 else 'red' }}">{{ s.excess_power_score }}</div>
      <div class="l">Excess Power Score · Opportunity</div>
    </div>
    <div class="sb">
      <div class="v {{ 'red' if s.constraint_score>=70 else 'orange' if s.constraint_score>=45 else 'green' }}">{{ s.constraint_score }}</div>
      <div class="l">Constraint Score · Avoid</div>
    </div>
    {% endif %}
  </div>

  <div class="section-h"><span class="pip"></span>🌟 Top Opportunities</div>
  <div class="section">
    <ul>{% if gated %}<li style="color:var(--tx2)">🔒 Top opportunities are a DC Hub Pro feature — <a href="/pricing" style="color:#5aa3ff;text-decoration:none">unlock</a></li>{% else %}{% for o in opps %}<li>{{ o }}</li>{% endfor %}{% endif %}</ul>
  </div>

  <div class="section-h"><span class="pip"></span>⚠️ Top Risks</div>
  <div class="section">
    <ul>{% if gated %}<li style="color:var(--tx2)">🔒 Top risks are a DC Hub Pro feature — <a href="/pricing" style="color:#5aa3ff;text-decoration:none">unlock</a></li>{% else %}{% for r in risks %}<li>{{ r }}</li>{% endfor %}{% endif %}</ul>
  </div>

  <div class="section-h"><span class="pip"></span>📊 Underlying Metrics</div>
  <div class="section">
    {% if s._metrics_source == 'iso_baseline' %}<div style="font-size:12px;color:var(--tx2);margin:-4px 0 10px">ISO-baseline estimate — refined as DC Hub's extractors enrich this market.</div>{% endif %}
    <div class="metrics">
    {% if gated %}
      <div class="metric"><div class="v" style="color:var(--tx2)">🔒</div><div class="l">Queue Wait</div></div>
      <div class="metric"><div class="v" style="color:var(--tx2)">🔒</div><div class="l">Reserve Margin</div></div>
      <div class="metric"><div class="v" style="color:var(--tx2)">🔒</div><div class="l">Generation Additions &lt;12mo</div></div>
      <div class="metric"><div class="v" style="color:var(--tx2)">🔒</div><div class="l">Renewable Curtailment</div></div>
      <div class="metric"><div class="v" style="color:var(--tx2)">🔒</div><div class="l">Stranded Capacity</div></div>
      <div class="metric"><div class="v" style="color:var(--tx2)">🔒</div><div class="l">Est. Time to Power</div></div>
    {% else %}
      {#- r-null-not-zero (2026-08-08): `or 0` turned an ABSENT measurement into
          a confident measured zero. Live before this fix, /dcpi/tokyo and
          /dcpi/johor both printed "0 MW Stranded Capacity" and "0 MW
          Generation Additions" while the API returned null for both — a reader
          takes that as "we looked and there is none". The gated branch above
          already had the right idea (it renders a lock rather than a number);
          the ungated branch never got it. `not_measured` is the ONLY way a
          nullable metric may render when it has no value. -#}
      {%- macro metric(value, unit, label, digits=0) -%}
        <div class="metric">
          {%- if value is none -%}
            <div class="v nm" title="No measurement for this market — not a zero">not measured</div>
          {%- else -%}
            <div class="v">{{ value|round(digits)|int if digits == 0 else value|round(digits) }}{{ unit if unit == '%' else (' ' ~ unit if unit else '') }}</div>
          {%- endif -%}
          <div class="l">{{ label }}</div>
        </div>
      {%- endmacro -%}
      {{ metric(s.queue_wait_months, 'mo', 'Queue Wait') }}
      {{ metric(s.reserve_margin_pct, '%', 'Reserve Margin', 1) }}
      {{ metric(s.gen_additions_12mo_mw, 'MW', 'Generation Additions &lt;12mo'|safe) }}
      {{ metric(s.curtailment_pct, '%', 'Renewable Curtailment', 1) }}
      {{ metric(s.stranded_capacity_mw, 'MW', 'Stranded Capacity') }}
      {{ metric(s.time_to_power_months, 'mo', 'Est. Time to Power') }}
    {% endif %}
    </div>
  </div>

  <div class="section-h"><span class="pip"></span>📬 Free Market-Movement Alerts</div>
  <div class="section" id="alert-box">
    <h2 style="margin-bottom:0.4rem">Get an email when {{ s.market_name }} moves</h2>
    <p style="color:var(--tx2);font-size:0.92rem;margin:0 0 1rem">
      DC Hub snapshots {{ s.market_name }} every day. The moment its verdict flips
      — or its constraint score or time-to-power shifts meaningfully — you get a
      one-line email. No account, no password, free.</p>
    <form id="alert-form" style="display:flex;gap:0.5rem;flex-wrap:wrap">
      <input type="email" id="alert-email" placeholder="you@company.com" required
        style="flex:1;min-width:220px;background:var(--bg);border:1px solid var(--bd);color:#fff;padding:0.7rem 1rem;border-radius:6px;font-family:'Instrument Sans',sans-serif;font-size:0.92rem;outline:none">
      <button type="submit" id="alert-go"
        style="background:var(--gradient);color:#fff;border:0;padding:0.7rem 1.3rem;border-radius:6px;font-weight:700;font-size:0.9rem;cursor:pointer;font-family:'Instrument Sans',sans-serif">Alert me →</button>
    </form>
    <div id="alert-msg" style="margin-top:0.6rem;font-size:0.85rem;color:var(--tx2)"></div>
  </div>
  <script>
  (function(){
    var f = document.getElementById('alert-form'); if (!f) return;
    f.addEventListener('submit', async function(e){
      e.preventDefault();
      var em = document.getElementById('alert-email').value.trim();
      var msg = document.getElementById('alert-msg');
      var btn = document.getElementById('alert-go');
      btn.disabled = true; msg.textContent = 'Subscribing…';
      try {
        var r = await fetch('/api/v1/alerts/subscribe', {
          method: 'POST', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({market:'{{ s.market_slug }}', channel:'email',
                                destination: em, source:'dcpi_market_page'})
        });
        var d = await r.json();
        if (d.ok) {
          msg.innerHTML = '<span style="color:var(--green)">✓ Done — you\\'ll get an email the next time {{ s.market_name }} moves.</span>';
          document.getElementById('alert-email').value = '';
        } else {
          msg.innerHTML = '<span style="color:var(--red)">' + (d.error || 'Could not subscribe') + '</span>';
        }
      } catch (err) {
        msg.innerHTML = '<span style="color:var(--red)">Error: ' + err + '</span>';
      } finally { btn.disabled = false; }
    });
  })();
  </script>

  <div class="cta-pro">
    <h2>Drill into {{ s.market_name }} at the county level.</h2>
    <p>Free alerts tell you {{ s.market_name }} moved. Pro tells you <em>where</em> — the score at the county level so you can pinpoint which sub-markets have the headroom, plus PDF export for your buyers.</p>
    <a href="/pricing?ref=dcpi&tool={{ s.market_slug }}">Get Pro · $199/mo →</a>
  </div>
</div>

{{ facilities_html|safe }}

<div style="background:#11121a;border:1px solid #1f2030;border-radius:12px;padding:20px;margin:32px auto;max-width:760px;font-family:system-ui">
  <div style="font-size:12px;color:#9eb5d8;text-transform:uppercase;letter-spacing:.1em;margin-bottom:8px">Cite this index</div>
  <code style="display:block;background:rgba(255,255,255,.03);padding:12px;border-radius:6px;color:#e8eef8;font-size:13px;margin-bottom:8px">DC Hub. (2026). Data Center Power Index v2. https://dchub.cloud/dcpi</code>
  <a href="/dcpi/methodology" style="color:#5aa3ff;font-size:14px;text-decoration:none">View methodology + BibTeX →</a>
</div>
<div style="margin:12px auto 32px;max-width:760px;font-family:system-ui;font-size:12.5px;color:#9eb5d8">
  Query this market live via MCP: <a href="https://dchub.cloud/connect?src=page-onramp&amp;entity={{ s.market_slug }}" style="color:#5aa3ff;text-decoration:none">https://dchub.cloud/connect?src=page-onramp&amp;entity={{ s.market_slug }}</a>
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# COVERAGE & EXPANSION figures  (r-coverage, 2026-07-29)
# ---------------------------------------------------------------------------
# Why this exists: /dcpi-v2 was a FROZEN launch teaser — "275+ markets across
# 14+ countries" and a hardcoded "Frankfurt (1,782 MW)" — and has been deleted
# and 301'd to /dcpi (frontend PR #1091). The expansion story moves onto THIS
# page, so it must not repeat that failure: every figure in the Coverage &
# Expansion section is MEASURED at render time, or the claim renders without a
# figure. No literal counts. No MW claim. Nothing that can silently go stale.
#
# The three rural AGGREGATE regions. These are not metros — each is a rollup of
# a rural area — so the canonical market count excludes them, exactly as
# canonical_stats.py:172-174 does (the query behind `markets` on /api/v1/stats
# and `dcpi_markets_scored` on /api/v1/stats/canonical).
_DCPI_AGGREGATE_REGION_SLUGS = ("pacific-nw-rural", "rural-spp", "upper-michigan")


def dcpi_index_spatial_coverage(rows):
    """PURE. schema.org spatialCoverage for the WHOLE DCPI dataset.

    r-index-coverage (2026-08-08): the /dcpi index Dataset declared
    `"spatialCoverage": {"@type": "Place", "name": "United States"}` — a flat
    literal — for an index that ranks 300+ markets across ~40 countries. Verified
    live on 2026-08-08. This is the same defect PR #2389 fixed on the per-market
    embed, one level up and on the flagship page: the whole dataset was asserting
    it was American in the channel AI engines lift verbatim.

    Derived from the SAME published rows the page already ranks — no second
    query and no second definition — using the same _market_country resolver as
    the per-market blocks, so the index and the markets under it cannot
    disagree. Returns a list of Places, or None when nothing resolves (in which
    case the caller omits the property rather than asserting a country).
    """
    countries = set()
    for r in rows or []:
        c = _market_country(r.get("state"), r.get("iso"), r.get("market_slug"))
        if c:
            countries.add(c)
    if not countries:
        return None
    return [{"@type": "Place", "addressCountry": c} for c in sorted(countries)]


def _dcpi_index_coverage(rows) -> dict:
    """Coverage figures for the INDEX ITSELF, derived from the same published
    score set /dcpi already ranks — no second query and no second definition,
    so this cannot disagree with the grid on the page.

    Returns {'markets': int|None, 'grid_regions': int|None}.

    markets       COUNT(DISTINCT market_name) over the published DISTINCT ON
                  (market_slug) rows, minus the aggregate regions — the
                  CANONICAL market count (canonical_stats.py:164-177), measured
                  306 on 2026-07-29, the same as `dcpi_markets_scored`.
                  ONE deliberate difference from that query: it filters
                  COALESCE(published, true) = true, while the rows handed in
                  here come from `published = true`, so a row with a NULL
                  `published` counts there and not here. That makes this figure
                  a FLOOR on the canonical one — never higher, which is the
                  only safe direction for a published claim.
                  It is deliberately NOT len(rows)/total_rows (310): that counts
                  published SLUGS, which include retired alias twins (cheyenne +
                  cheyenne-wy, portland + portland-or) plus the 3 aggregates.
                  And it is NOT COUNT(*) of market_power_scores (317) — see
                  routes/facilities_by_dims.py:229-255 for why publishing rows
                  under a "markets" label is a published-number defect.
    grid_regions  COUNT(DISTINCT iso) over the same rows, dropping the
                  blank/NULL-iso bucket exactly as /api/v1/dcpi/iso-comparison
                  does (routes/dcpi.py:4742), so this equals that endpoint's
                  row count — live 49. Grouped on the RAW iso value (no upper/
                  strip) for the same reason: to match that endpoint exactly.

    A figure that measures 0 is returned as None. An unmeasured count must read
    as unknown on the page, never as "0" — the reason /dcpi-v2 was retired.
    """
    rows = rows or []
    names = {
        (r.get("market_name") or "").strip()
        for r in rows
        if (r.get("market_name") or "").strip()
        and r.get("market_slug") not in _DCPI_AGGREGATE_REGION_SLUGS
    }
    isos = {r.get("iso") for r in rows if (r.get("iso") or "").strip()}
    return {
        "markets": len(names) or None,
        "grid_regions": len(isos) or None,
    }


# 10-minute in-process cache: these move on the daily 06:00 UTC recompute, and
# /dcpi is a hot public page — one COUNT(DISTINCT) pair per 10 min per process.
_DCPI_FOOTPRINT_TTL_S = 600
_dcpi_footprint_cache: dict = {"ts": 0.0, "val": None}


def _dcpi_footprint_figures() -> dict:
    """The physical footprint the DCPI market list is DERIVED from (the market
    universe is built from cities with >=3 tracked facilities — see the query at
    routes/dcpi.py:954-977).

    Returns {'countries': int|None, 'facilities_distinct': int|None}.

    ONE query, and each figure uses the SAME SQL /api/v1/stats/canonical already
    publishes, so the two surfaces cannot drift apart:
      countries            -> routes/facilities_by_dims.py:209-211
                              (`countries_covered`, live 178). ★2026-07-30:
                              counted on discovered_facilities — the same fleet
                              facilities_distinct counts. It used to read the
                              LEGACY `facilities` table (186), which mixes full
                              names with ISO codes ("USA"+"US"), pairing a
                              wrong-table country count with a discovered-fleet
                              facility count.
      facilities_distinct  -> routes/facilities_by_dims.py:290-293
                              (`facilities_distinct`, live 15,363). That file's
                              comment at :272-288 is explicit that
                              facilities_verified / facilities_tracked are
                              AMBIGUOUS and that new consumers must read
                              facilities_distinct — so that is what this reads.

    Fail-soft and LOUD: on any DB error both figures come back None (the section
    then prints its claims with no numbers) and the failure is logged at
    warning with the exception type. A failure is NOT cached, so recovery is
    immediate. Never returns 0 and never a frozen literal.
    """
    import time as _t
    now = _t.time()
    cached = _dcpi_footprint_cache.get("val")
    if cached is not None and (now - (_dcpi_footprint_cache.get("ts") or 0.0)) < _DCPI_FOOTPRINT_TTL_S:
        return dict(cached)
    out = {"countries": None, "facilities_distinct": None}
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT
                  (SELECT COUNT(DISTINCT country) FROM discovered_facilities
                    WHERE country IS NOT NULL AND country <> '')          AS countries,
                  (SELECT COUNT(DISTINCT canonical_slug) FROM discovered_facilities
                    WHERE canonical_slug IS NOT NULL)                     AS facilities_distinct
            """)
            row = cur.fetchone() or (None, None)
        out["countries"] = int(row[0]) if row[0] else None
        out["facilities_distinct"] = int(row[1]) if row[1] else None
    except Exception as e:
        import logging
        logging.warning(
            "[dcpi] coverage footprint query failed (%s: %s) — the Coverage & "
            "Expansion section will render its claims WITHOUT figures rather "
            "than with stale ones", type(e).__name__, e)
        return dict(out)
    _dcpi_footprint_cache["val"] = dict(out)
    _dcpi_footprint_cache["ts"] = now
    return dict(out)


@_safe_dcpi_page
# strict_slashes=False (2026-05-14): Flask's default 404s the trailing-slash
# variant of a no-slash route. /dcpi/ was returning a hard 404 (and
# /dcpi/<slug>/ likewise) while /dcpi and /dcpi/<slug> served fine — a
# silent dead-end for any link or crawler that appended a slash. False
# alarms from this also fed the healer's dcpi_flaky_404 pattern. Accept
# both forms.
@dcpi_bp.route("/dcpi", methods=["GET"], strict_slashes=False)
def public_dashboard():
    _ensure_tables()
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT DISTINCT ON (market_slug) *
            FROM market_power_scores WHERE published = true ORDER BY market_slug, computed_at DESC
        """)
        rows = cur.fetchall()
    rows.sort(key=lambda r: -(r.get("excess_power_score") or 0))
    if not rows:
        # Trigger an initial recompute so the page is never empty
        try: recompute_all_scores(source="cold-start")
        except Exception: pass
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""SELECT DISTINCT ON (market_slug) *
                           FROM market_power_scores WHERE published = true ORDER BY market_slug, computed_at DESC""")
            rows = cur.fetchall()
        rows.sort(key=lambda r: -(r.get("excess_power_score") or 0))
    # phase 271: surface verdict counts so the LOW_SIGNAL "Monitoring" tab can
    # show a count badge, and the page's "Actionable" default isn't an opaque
    # filter. Actionable = BUILD + CAUTION + AVOID (decision-grade); monitoring
    # = LOW_SIGNAL (covered but no actionable signal yet).
    _ACTIONABLE = {"BUILD", "CAUTION", "AVOID"}
    count_actionable = sum(1 for r in rows if (r.get("verdict") or "") in _ACTIONABLE)
    count_low_signal = sum(1 for r in rows if (r.get("verdict") or "") == "LOW_SIGNAL")

    # r42ab (2026-05-27): anon-tier cap on /dcpi. Operator observed 25
    # free dev keys claimed with 1 paid conversion — too much value
    # was reaching anonymous users to motivate signup. Originally anon saw
    # only top 5 BUILD + top 20 AVOID/CAUTION = 25 cards; free-key callers
    # (X-API-Key in querystring or header) saw the full set.
    #
    # r42ab-followup (2026-05-30): surface ALL published markets in the
    # index grid for every visitor. The page only had ~25 of the 232
    # published market_power_scores rows reaching anon viewers, which
    # made the public coverage claim ("{{ count }} MARKETS SCORED") and
    # the verdict tabs (Actionable / Monitoring / All) badly undercount —
    # the "All" tab showed 25, the "Monitoring"/LOW_SIGNAL tab rendered
    # empty for anon. The catalog (market name, ISO, state, top-level
    # Excess/Constraint, BUILD/CAUTION/AVOID verdict) is the free
    # discovery hook; the PAID moat (county-level drill, >5pt move
    # alerts, branded PDF export) is unchanged and still gated via the
    # Pro CTA + the per-market detail pages stay free for AI citation.
    # ADDITIVE: anon now sees more (all) markets rather than fewer.
    # To re-introduce an anon row cap, restore the `_has_key` slice below.
    from flask import request as _req
    _has_key = bool((_req.headers.get('X-API-Key') or _req.args.get('api_key') or '').strip())
    _total_rows = len(rows)
    # r-deorphan (2026-06-23): capture EVERY published market (slug+name only, NO
    # scores) BEFORE the anon teaser slice below, so the page renders a crawlable link
    # to every /dcpi/<slug> for anonymous crawlers too. The per-market pages are
    # already free (AI-citation) and names+links aren't the paid moat (county drill /
    # >5pt alerts / branded PDF are) — this de-orphans the ~200 market pages the anon
    # 25-card cap hid from crawlers, with ZERO new data exposure. The brain's sentinel
    # flagged these as the bulk of its orphan-pages finding; internal links feed the
    # one acquisition channel that's actually growing (SEO).
    all_market_links = sorted(
        ({'slug': r.get('market_slug'), 'name': (r.get('market_name') or r.get('market_slug'))}
         for r in rows if r.get('market_slug')),
        key=lambda m: (m['name'] or '').lower())
    # r-coverage (2026-07-29): Coverage & Expansion figures, computed HERE — from
    # the FULL published set, BEFORE the tier slice below — for the same reason
    # all_market_links is. A tier-capped teaser must never shrink a COVERAGE
    # claim: that is the r47.47 / r-hero-total bug class, where an anon viewer
    # read "25 MARKETS SCORED" on a 310-market index.
    _index_cov = _dcpi_index_coverage(rows)
    # r-index-coverage-precap (2026-08-08): the Dataset's spatialCoverage is a
    # claim about the DATASET, so it belongs here with the other coverage
    # figures — computed from the FULL published set, before the tier slice.
    # #2393 computed it at the render call instead, i.e. AFTER `rows` is rebound
    # to the anon 25-card teaser. That teaser is all-US, so the literal
    # "United States" was replaced by a DERIVED "United States" and the
    # crawlable surface — the one AI engines read — was unchanged. Exactly the
    # bug class the comment above names.
    _spatial_cov = dcpi_index_spatial_coverage(rows)
    _footprint_cov = _dcpi_footprint_figures()
    # 2026-05-30: keep the full 300+-market catalog for AUTHENTICATED viewers
    # (API key OR a logged-in session cookie — incl. the operator), but a
    # truly-anonymous visitor gets the capped r42ab teaser so we don't give the
    # whole catalog away (the prior commit removed the cap for EVERYONE, which
    # re-opened the anon leak). resolve_tier/_has_key miss the website session
    # cookie, so consult the same cookie-aware resolver /pockets uses.
    # r-freecap (2026-06-23): gate the SCORED-CARD count on the REAL tier, not just
    # "has a key". Before, _authed=_has_key handed the FULL 317 to ANY key — incl. a
    # no-cost free/identified key minted by claim_free_key — which gutted the upgrade
    # pull (why pay if a free key shows every ranked market?). New 3-tier funnel:
    #   anon  → 25-card teaser  (the crawlable SEO surface; unchanged)
    #   free  → 50-card teaser  (rewards claiming a key, still leaves a reason to pay)
    #   paid  → all 317         (mcp_dev_keys enterprise/paid/developer/pro/founding/
    #                            starter, OR a paid logged-in session)
    # The names-only "All N Markets" list (all_market_links) is still emitted for
    # EVERYONE, so this caps SCORES, not discovery. _paid is resolved from the canonical
    # MCP table (mcp_dev_keys via _resolve_key_tier — free/identified map to None) with a
    # cookie-tier fallback for logged-in browser sessions.
    _paid = False
    if _has_key:
        try:
            from api_data_protection import _resolve_key_tier
            _paid = bool(_resolve_key_tier(
                (_req.headers.get('X-API-Key') or _req.args.get('api_key') or '').strip()))
        except Exception:
            _paid = False
    if not _paid:
        try:
            from map_tier_gating import _detect_caller_tier
            def _dec(_t):
                try:
                    import jwt as _j
                    from main import JWT_SECRET
                    return _j.decode(_t, JWT_SECRET, algorithms=['HS256'])
                except Exception:
                    return None
            _ct, _ = _detect_caller_tier(decode_jwt_func=_dec)
            _ctl = str(_ct or '').lower()
            _paid = bool(_ctl and _ctl not in (
                '', 'anonymous', 'anon', 'free', 'identified',
                'trial', 'trial_taste', 'trial_preview', 'preview'))
        except Exception:
            pass
    _gated_to_anon = not _paid                       # name kept; now means "not paid"
    _tier_state = 'paid' if _paid else ('free' if _has_key else 'anon')
    if _gated_to_anon:
        # r-free-breadth (2026-06-27): a free signup (any key) now unlocks the
        # FULL map — ALL markets, names + verdicts — with the numbers still
        # masked below; the scores are the paid line. Truly-anon stays a capped
        # teaser (5 BUILD + 20 others = 25) with the full count in the header +
        # verdict tabs as the "sign up free to see them all" hook.
        if not _has_key:
            _builds = [r for r in rows if (r.get('verdict') or '') == 'BUILD'][:5]
            _others = [r for r in rows if (r.get('verdict') or '') != 'BUILD'][:20]
            rows = _builds + _others
        rows.sort(key=lambda r: -(r.get("excess_power_score") or 0))
        # r-gate-everywhere (2026-06-27): null the numeric scores on the teaser
        # cards (was leaking excess/constraint/time-to-power in the card divs AND
        # the data-excess/data-constraint attrs). Masked AFTER the sort so the
        # ranking ORDER survives; verdict + market + ISO stay (the free breadth
        # hook). The template guards the comparisons on gated_to_anon.
        rows = [{**dict(r), "excess_power_score": None, "constraint_score": None,
                 "composite_score": None, "time_to_power_months": None} for r in rows]

    html = render_template_string(
        DCPI_INDEX_TEMPLATE,
        scores=rows,
        count=len(rows),
        count_actionable=count_actionable,
        count_low_signal=count_low_signal,
        gated_to_anon=_gated_to_anon,
        total_rows=_total_rows,
        all_market_links=all_market_links,
        tier_state=_tier_state,
        # r-coverage (2026-07-29): each of these is an int or None. None means
        # "not measured on this render" and the template drops the figure while
        # keeping the claim — never 0, never a frozen literal.
        cov_markets=_index_cov["markets"],
        cov_grid_regions=_index_cov["grid_regions"],
        cov_countries=_footprint_cov["countries"],
        cov_facilities=_footprint_cov["facilities_distinct"],
        # r-index-coverage (2026-08-08): the real country list, derived from the
        # rows this page ranks — replaces a literal "United States" on a
        # dataset that spans ~40 countries.
        # Computed from the FULL set above, NOT from the tier-sliced `rows`
        # this line is inside — see the r-index-coverage-precap note there.
        spatial_coverage=_spatial_cov,
    )
    # phase 284: ship a Content-Security-Policy header on /dcpi so the
    # dchub-frontend qa-csp-parse preflight CI doesn't fail on this page.
    # Mirrors the policy that the Pages-served pages (/, /pricing, /news,
    # etc.) get from Cloudflare Pages _headers — same allowed sources, same
    # directive coverage. Without this header, the CSP-watch automation
    # treated /dcpi as a regression even though the page is intentional.
    resp = Response(html, mimetype="text/html")
    resp.headers["Content-Security-Policy"] = _DCPI_CSP
    # r-tierleak (2026-06-23): /dcpi body VARIES by tier — anon gets the 25-card
    # teaser, authed gets all 317 scored cards. It set NO Cache-Control, so it fell
    # through to the after_request's PUBLIC s-maxage=300 HTML cache → an AUTHED render
    # got edge-cached at CF and served to ANON (the leak the owner caught on mobile).
    # Fix per the established pattern (main.py ~9848 honors an explicit private/no-store):
    # authed render → private/no-store (never edge-shared); the anon teaser stays
    # edge-cacheable (SEO/perf). The CF worker already bypasses the edge cache for
    # authed (hasAuth), so an authed visitor still gets their full render — not a
    # cached anon teaser. NOTE: the 317 market name+link list is names-only (no scores)
    # and the per-market /dcpi/<slug> pages are intentionally free (SEO/citation) — the
    # scored CARDS are what stays gated for anon/free.
    # r-freecap (2026-06-23): only the PURE-ANON render (25-card teaser = the crawlable
    # SEO surface) is publicly edge-cacheable. The free render (50 cards) and the paid
    # render (317) both VARY from anon, so they're private/no-store — otherwise the free
    # 50-card render could be edge-shared to anon (over-exposure) or the anon 25 served
    # to a free/paid caller (under-served). Keyed/cookie callers bypass the edge cache.
    if _tier_state == 'anon':
        resp.headers["Cache-Control"] = "public, max-age=120, s-maxage=300, stale-while-revalidate=86400"
    else:
        resp.headers["Cache-Control"] = "private, no-store, max-age=0"
        resp.headers["CDN-Cache-Control"] = "no-store"
    return resp


# Phase TT-2 (2026-05-15) — single source of truth for the CSP.
#
# Why this exists: /dcpi is served by Flask (this file), not CF Pages.
# The dchub-frontend/_headers file ONLY applies to Pages-served static
# assets (/, /pricing, /news, etc.) — it doesn't reach proxied responses.
# So Flask MUST set the CSP itself, but it must be EXACTLY the same as
# the Pages CSP to avoid the drift bug (PR #188 fixed three live cases).
#
# Sync rule: if you change /_headers in dchub-frontend, also bump this
# constant. The util/csp_canonical.get_csp() helper (Phase TT-2) tries
# to fetch /_headers from disk first (when both repos sit side-by-side
# in dev) and falls back to this hardcoded copy. In production they're
# separate deploys so the fallback wins.
try:
    from util.csp_canonical import get_canonical_csp as _get_canonical_csp
    _DCPI_CSP = _get_canonical_csp()
except Exception:
    # Hardcoded fallback — must match dchub-frontend/_headers exactly.
    _DCPI_CSP = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' "
            "https://cdnjs.cloudflare.com https://unpkg.com https://cdn.jsdelivr.net "
            "https://www.googletagmanager.com https://accounts.google.com "
            "https://static.cloudflareinsights.com https://plausible.io; "
        "script-src-elem 'self' 'unsafe-inline' "
            "https://cdnjs.cloudflare.com https://unpkg.com https://cdn.jsdelivr.net "
            "https://www.googletagmanager.com https://accounts.google.com "
            "https://static.cloudflareinsights.com https://plausible.io; "
        "style-src 'self' 'unsafe-inline' "
            "https://fonts.googleapis.com https://cdnjs.cloudflare.com "
            "https://accounts.google.com; "
        "style-src-elem 'self' 'unsafe-inline' "
            "https://fonts.googleapis.com https://cdnjs.cloudflare.com "
            "https://accounts.google.com; "
        "img-src 'self' data: https:; "
        "font-src 'self' data: https: https://fonts.gstatic.com; "
        "connect-src 'self' https://plausible.io "
            "https://dchub-backend-production.up.railway.app "
            "https://dchub-daily-production.up.railway.app "
            "https://cdnjs.cloudflare.com https://gateway.ai.cloudflare.com "
            "https://www.google-analytics.com https://stats.g.doubleclick.net "
            "https://accounts.google.com https://cloudflareinsights.com "
            "https://www.google.com https://nominatim.openstreetmap.org "
            "https://overpass-api.de https://overpass.kumi.systems "
            "https://overpass.private.coffee https://*.arcgis.com "
            "https://geo.dot.gov https://*.usgs.gov "
            "https://carto.nationalmap.gov https://hazards.fema.gov "
            "https://geodata.epa.gov https://geocoding.geo.census.gov; "
        "frame-src 'self' https://accounts.google.com; "
        "frame-ancestors 'self'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "report-uri /api/csp-report"
    )


# r43-H (2026-05-28): per-slug rendered-page cache. #3's latency tracker
# flagged /dcpi/<slug> at p95 4.37s — each render runs gather_metrics_for_market
# (DB + ISO baseline) plus a Claude narrative call on the first read per slug
# per hour. DCPI scores recompute every ~4h and the narrative is already cached
# 1h, so a 10-min rendered-HTML cache is plenty fresh and makes repeat loads
# instant. Keyed by the canonical slug (aliases 301-redirect before render).
_DCPI_PAGE_CACHE: dict = {}
_DCPI_PAGE_TTL = 600


# RENDER-PERF (2026-06-01): _DCPI_PAGE_CACHE above is a per-gunicorn-worker dict
# wiped on every worker recycle and not shared across workers/replicas, so a cold
# /dcpi/<slug> re-runs gather_metrics_for_market + the inline Claude narrative
# call (the documented dcpi cold-render timeout). Front it with the already-
# connected, cross-worker Redis helper (redis_cache.py — same best-effort pattern
# report_narrative.py uses) so a warm rendered page survives a recycle. Redis is
# BEST-EFFORT in front of the dict: any import/connection/serialization error
# falls through to the dict path unchanged. cache_get/set already swallow their
# own errors and no-op when REDIS_URL is unset. We store ONLY the rendered HTML
# string (NOT the process-local expiry epoch) — Redis setex governs expiry via
# _DCPI_PAGE_TTL.
def _redis_get_page(slug: str):
    """Return cached rendered DCPI page HTML from Redis, or None on miss/error."""
    try:
        from redis_cache import cache_get
        payload = cache_get(f"dcpi_page:{slug}")
        if isinstance(payload, str) and payload:
            return payload
    except Exception:
        pass
    return None


def _redis_set_page(slug: str, html: str) -> None:
    """Best-effort write rendered DCPI page HTML to Redis with the module TTL.
    No-op on any error (incl. REDIS_URL unset)."""
    try:
        from redis_cache import cache_set
        cache_set(f"dcpi_page:{slug}", html, ttl=_DCPI_PAGE_TTL)
    except Exception:
        pass


def _cite_as_header(slug: str) -> str:
    """r-page-onramp (2026-07-04): ASCII-only X-Cite-As value with an as-of
    stamp. Headers must be latin-1 — an em-dash in this exact header 502'd
    every /api/v1/industry/pulse response (routes/industry_pulse.py); follow
    that fixed pattern exactly. Never raises."""
    try:
        return (f"DC Hub DCPI {slug} - as of {datetime.date.today().isoformat()}"
                ).encode("ascii", "ignore").decode("ascii")
    except Exception:
        return "DC Hub DCPI"


@dcpi_bp.route("/dcpi/<slug>", methods=["GET"], strict_slashes=False)
def public_market_page(slug):
    _ensure_tables()
    # r-period-slug (2026-07-06): strip periods and 301 to the '-'-normalized
    # slug BEFORE the candidate lookup below. A malformed period slug like
    # 'st.-louis' has its OWN published market_power_scores row, so the
    # candidate loop would match it first (cand == slug → no redirect) and
    # serve a soft-404 duplicate of the canonical /dcpi/st-louis. A period is
    # never valid in a canonical slug — consolidate to the normalized page.
    _pnorm = (slug or "").replace(".", "")
    if _pnorm and _pnorm != slug:
        from flask import redirect
        return redirect(f"/dcpi/{_pnorm}", code=301)
    # Phase JJ (2026-05-14): slug aliasing. The market_power_scores table
    # uses bare slugs (e.g. 'allen' not 'allen-tx'), but external links
    # often append state suffix because that's the natural-language
    # form (yesterday's auto-press wrote "Allen, TX ranked #3" which
    # AI agents parsing the article reasonably resolved to /dcpi/allen-tx).
    # Try the exact slug first, then strip common suffix patterns.
    candidates = [slug]
    # Strip -<state> suffix: 'allen-tx' → 'allen'
    if "-" in slug and len(slug.rsplit("-", 1)[1]) == 2:
        candidates.append(slug.rsplit("-", 1)[0])
    # Strip -texas / -california / etc. (full state names)
    _STATE_FULL_SUFFIXES = (
        '-texas', '-california', '-virginia', '-georgia', '-illinois',
        '-arizona', '-wyoming', '-nevada', '-oregon', '-washington',
        '-florida', '-ohio', '-michigan', '-newyork', '-new-york',
    )
    for suf in _STATE_FULL_SUFFIXES:
        if slug.endswith(suf):
            candidates.append(slug[:-len(suf)])
            break

    # r43-H (2026-05-28): metro→city slug aliases. The /markets/ pages
    # canonicalize to METRO slugs (northern-virginia, dallas-fort-worth) but
    # market_power_scores keys on the dominant CITY (ashburn, dallas). Inbound
    # /dcpi/<metro> links from /market-intelligence and /markets/<metro> 404'd
    # (e.g. /dcpi/northern-virginia). Map known metros to their DCPI city slug;
    # the cand!=slug branch below then 301-redirects to the canonical
    # /dcpi/<city>. This is the inverse of market_deep_dive._CANONICAL_REDIRECT.
    # r47.43 (2026-05-27): map now defined at module level (DCPI_METRO_ALIASES)
    # so the API endpoint can apply the same alias logic. Added silicon-valley
    # → santa-clara — the third of the big-three US metros where /markets/*
    # gives 200 but /dcpi/* gave 404. Caught by pre-CBRE DCPI sweep.
    _alias = DCPI_METRO_ALIASES.get(slug.lower())
    if _alias:
        candidates.append(_alias)
    # r-twin-unpublish (2026-07-28): try the canonical target BEFORE the alias
    # key's own row, so a leftover twin row cannot shadow the redirect.
    candidates = _canonical_first(slug, candidates)

    s = None
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        for cand in candidates:
            cur.execute("""SELECT * FROM market_power_scores
                           WHERE market_slug = %s
                           ORDER BY computed_at DESC LIMIT 1""", (cand,))
            s = cur.fetchone()
            if s:
                # If we matched on an alias (not the original), 301-redirect
                # to the canonical slug. Preserves SEO equity for inbound
                # links and ensures Google deduplicates the page.
                if cand != slug:
                    from flask import redirect
                    return redirect(f"/dcpi/{cand}", code=301)
                break
    if not s:
        # phase 284: even 404 should ship the CSP so it doesn't trip the watch
        r = Response(f"<h1>Market not found: {slug}</h1>", status=404, mimetype="text/html")
        r.headers["Content-Security-Policy"] = _DCPI_CSP
        return r

    # r-gate-everywhere (2026-06-27): the numeric DCPI scores (rendered into page
    # text + JSON-LD + meta/og) are the PAID product. Resolve tier ONCE and split
    # the page cache by tier — a slug-only cache key would let the first anon
    # render poison the paid cache (and vice-versa). Masking happens on the render
    # path below; cache hits are already the correct tier variant.
    # SINGLE-market page is free-to-cite (2026-07-03); bulk endpoints stay paid.
    _paid = _dcpi_single_market_paid()
    _gated = not _paid
    _ckey = slug + (":paid" if _paid else ":anon")

    # r43-H: serve the cached rendered page if fresh (skips the metric backfill
    # + the Claude narrative call). slug here is canonical — aliases already
    # 301-redirected above, so a hit can't leak the wrong market.
    import time as _t
    _now = _t.time()
    _ch = _DCPI_PAGE_CACHE.get(_ckey)
    if _ch and _ch[0] > _now:
        _cr = Response(_ch[1], mimetype="text/html")
        _cr.headers["Content-Security-Policy"] = _DCPI_CSP
        _cr.headers["X-DC-Cache"] = "hit"
        _cr.headers["X-Cite-As"] = _cite_as_header(slug)
        return _cr

    # RENDER-PERF: cross-worker Redis layer (survives gunicorn recycle). On a
    # hit, warm the local dict (with a fresh local expiry) so subsequent same-
    # worker reads stay dict-fast, and serve the page without the metric
    # backfill + the inline Claude narrative call.
    _r_html = _redis_get_page(_ckey)
    if _r_html:
        if len(_DCPI_PAGE_CACHE) < 500:
            _DCPI_PAGE_CACHE[_ckey] = (_now + _DCPI_PAGE_TTL, _r_html)
        _cr = Response(_r_html, mimetype="text/html")
        _cr.headers["Content-Security-Policy"] = _DCPI_CSP
        _cr.headers["X-DC-Cache"] = "hit"
        _cr.headers["X-Cite-As"] = _cite_as_header(slug)
        return _cr

    # Phase RR (2026-05-14): backfill lite-scored markets. ~250+ markets
    # are scored by the LITE path (bulk_dcpi_score / api lite recompute),
    # which only writes constraint_score + excess_power_score and leaves
    # every underlying metric at 0/null. Their detail pages rendered a
    # wall of "0 mo / 0% / 0 MW" — looked broken, surfaced nothing
    # useful, and gave the brain a "stats empty" signal. When the row's
    # detail metrics are all empty, re-derive them from
    # gather_metrics_for_market (one indexed query + ISO-baseline
    # defaults) so every market shows real directional intelligence.
    _detail_keys = ("queue_wait_months", "reserve_margin_pct",
                    "gen_additions_12mo_mw", "curtailment_pct",
                    "stranded_capacity_mw", "time_to_power_months")
    if all(not s.get(k) for k in _detail_keys):
        try:
            _mkt = (s.get("market_slug"), s.get("market_name"),
                    s.get("state"), s.get("iso"),
                    s.get("latitude"), s.get("longitude"))
            _m = gather_metrics_for_market(_mkt)
            s["queue_wait_months"]     = _m.get("queue_wait_months")
            s["reserve_margin_pct"]    = _m.get("reserve_margin_pct")
            s["gen_additions_12mo_mw"] = _m.get("gen_additions_12mo_mw")
            s["curtailment_pct"]       = _m.get("curtailment_pct")
            s["stranded_capacity_mw"]  = _m.get("stranded_capacity_mw")
            s["time_to_power_months"]  = estimate_time_to_power(_m)
            if not s.get("top_risks_json") and not s.get("top_opportunities_json"):
                _r, _o = derive_top_signals(
                    _mkt, _m,
                    float(s.get("constraint_score") or 0),
                    float(s.get("excess_power_score") or 0))
                s["top_risks_json"] = _r
                s["top_opportunities_json"] = _o
            s["_metrics_source"] = "iso_baseline"
            # r65 (2026-06-02): carry the freshly-derived provenance label so
            # the re-derived detail page reflects the same live/modeled basis.
            _db = _m.get("data_basis")
            if isinstance(_db, dict) and _db.get("data_basis"):
                s["data_basis"] = _db.get("data_basis")
                if _db.get("data_basis_source"):
                    s["data_basis_source"] = _db.get("data_basis_source")
                if _db.get("data_basis_note"):
                    s["data_basis_note"] = _db.get("data_basis_note")
        except Exception:
            pass  # best-effort — fall back to whatever the row carried

    if s.get("computed_at"): s["computed_at"] = s["computed_at"].isoformat()
    # r-ws3-signal-tier (2026-07-28): render the score's signal quality next to
    # the as-of date. NULL tier prints "UNRECORDED", never "LOW" — the page must
    # not state a measurement the row does not carry.
    s["signal_tier"] = s.get("signal_tier") or None
    s["signal_tier_basis"] = (
        "3 of 3 live grid feeds fed this score" if s.get("signal_tier") == "full"
        else "1-2 of 3 live grid feeds fed this score"
        if s.get("signal_tier") == "partial"
        else "no live grid feed fed this score — every input is a modeled "
             "regional estimate" if s.get("signal_tier") == "low"
        else "this row was scored before signal tiering was recorded — quality "
             "unknown, not assumed low")
    risks = s.get("top_risks_json") or []
    opps = s.get("top_opportunities_json") or []

    # r42g (2026-05-25): per-market analyst narrative. ~100 words,
    # 1 paragraph, in CBRE/JLL per-market H2 voice. Silent no-op when
    # ANTHROPIC_API_KEY absent. Cached 1h per (slug, date). Cost ~$0.001
    # × 300+ markets × 1 cache cycle/day = ~$0.30/day if every market is
    # read at least once.
    narrative_text = ""
    try:
        from routes.report_narrative import attach_market_narrative
        narrative_text = attach_market_narrative(s, risks, opps) or ""
    except Exception:
        pass

    # r80 SEO INTERNAL-LINK MESH (see market_deep_dive): emit the facilities
    # in this DCPI market as real /facilities/<slug> links so the 21k facility
    # pages stop being a crawl island. Built BEFORE the cache-set below so the
    # cached copy carries the links.
    _facilities_html = ""
    try:
        from routes.facility_profile_page import _fac_slug as _fslug, _esc as _fesc
        _mkt_name = s.get("market_name") or ""
        _mkt_state = s.get("state") or ""
        # r-namesake (2026-08-07): this list had NO country predicate, so it
        # answered "every facility on earth whose city or market string
        # matches", and it is what put Equinix MA1/MA3/MA4 Manchester UK,
        # Meta Clonee (Dublin IE) and Arelion Wien Sud on pages whose own
        # geography said New Hampshire, Ohio and Virginia. It is also why
        # /dcpi/birmingham listed Pulsant Birmingham WM-1 (UK) next to DC BLOX
        # Birmingham (AL), and /dcpi/richmond listed AAPT Richmond (Melbourne)
        # next to QTS Richmond (VA) — those two markets have correct geography
        # and were contaminated by the LIST alone.
        # The name-match grain is unchanged (market OR city, so Waltham MA
        # keeps appearing under Boston); only the country is now bounded, by
        # the same helper the saturation terms use.
        _fac_ctry_sql, _fac_ctry_params = _market_country_scope(
            s.get("iso"), _mkt_state, s.get("latitude"), s.get("longitude"))
        # r-list-dedup (2026-08-08): this list had NO duplicate-visibility
        # predicate, so it rendered the SAME BUILDING two or three times and
        # the repeats ate the LIMIT 50. Measured on /dcpi/manchester: 50 rendered
        # rows carried 25 duplicate pointers and only 28 distinct names — "Equinix
        # MA1 - Manchester, Williams/Kilburn", "Joule House", "Teledata Manchester
        # - Delta House", "Greenheys", "ANS - MAN4/5/6" each twice — while 8 real
        # Manchester facilities were pushed off the end by their own twins. 38%
        # of discovered_facilities (9,459 / 24,676) carry a duplicate_of_id, and
        # 301 of 322 scored markets had at least one repeat in their top 50.
        # ★ Visibility here is duplicate_of_id ALONE, never is_duplicate: the flag
        # is a suppression bit that ALSO drops the row from counts and the
        # sitemap, and a slug whose rows are all flagged still serves 200. The
        # pointer is what says "this row is another row's twin" — the same
        # predicate routes/facilities_by_dims.py and routes/d1_sync.py scope on,
        # and the same one routes/facility_profile_page.py keys the canonical on.
        # r-sat-dedup (2026-08-08): the saturation footprint above now carries
        # the same predicate, from the shared _SQL_FOOTPRINT_DEDUP constant.
        # It shipped separately, with its own recompute and verdict diff,
        # because it rescores every published market — see that comment.
        if _mkt_name:
            with _conn() as _fc, _fc.cursor() as _fcur:
                _fcur.execute(f"""
                    SELECT id, name, provider, power_mw
                      FROM discovered_facilities
                     WHERE (market = %s OR LOWER(city) = LOWER(%s))
                       AND name IS NOT NULL AND name <> ''
                       AND duplicate_of_id IS NULL
                       {_fac_ctry_sql}
                     ORDER BY power_mw DESC NULLS LAST LIMIT 50
                """, (_mkt_name, _mkt_name, *_fac_ctry_params))
                _frows = _fcur.fetchall() or []
            if _frows:
                _items = "".join(
                    f'<li><a href="/facilities/{_fslug(_rid,_rprov,_rname)}" '
                    f'style="color:#5aa3ff;text-decoration:none">{_fesc(_rname)}</a>'
                    f'{(" &middot; " + str(round(_rpow)) + " MW") if _rpow else ""}</li>'
                    for _rid, _rname, _rprov, _rpow in _frows)
                _facilities_html = (
                    '<div style="margin:32px auto;max-width:760px;font-family:system-ui">'
                    f'<h2 style="color:#e8eef8;font-size:18px">Data centers in {_fesc(_mkt_name)}</h2>'
                    f'<ul style="columns:2;color:#9eb5d8;font-size:14px;line-height:1.9">{_items}</ul></div>')
    except Exception:
        pass

    # r-gate-everywhere (2026-06-27): for non-paid, mask the numeric scores +
    # drop risk/opp + the score-bearing narrative BEFORE render. The template
    # guards every emit site on `gated`; masking s is defense-in-depth (a missed
    # guard then renders empty, never the real number).
    if _gated:
        s = dict(s)
        for _k in _DCPI_MASK_FIELDS + _DCPI_MASK_EXTRA:
            if _k in s:
                s[_k] = None
        risks, opps = [], []
        narrative_text = (
            f"DC Hub rates {s.get('market_name', 'this market')} "
            f"{s.get('verdict') or 'LOW_SIGNAL'} for new data-center builds. "
            "The numeric DCPI scores (excess-power, grid-constraint, time-to-power) "
            "are available to DC Hub Pro — unlock at dchub.cloud/pricing.")
    # r-iso-taxonomy-2 (2026-07-28): the SSR page builds its OWN JSON-LD
    # spatialCoverage, separate from the api_scores Dataset block. The first
    # pass fixed only the Python one, so /dcpi/cheyenne-wy kept publishing
    # "Cheyenne, WY, WY" to Google — the template had been concatenating
    # `market_name ~ ", " ~ state` on its own. Same helper both places now;
    # two copies of the rule was the whole reason one of them stayed wrong.
    # r-one-dcpi (2026-08-08): the page must publish the SAME number the API
    # calls DCPI. Before today the <title> rendered excess_power_score under the
    # bare label "DCPI" while /api/v1/dcpi/scores/<slug> returned a different
    # composite_score, and the composite appeared nowhere on the page. Measured
    # live across every market — the US flagships were the worst:
    #     dallas   title "DCPI 65.8"  vs composite 43.6   (22.2 apart)
    #     phoenix  title "DCPI 62.5"  vs composite 42.7   (19.8)
    #     ashburn  title "DCPI 45.5"  vs composite 27.1   (18.4)
    #     tokyo    title "DCPI 19.8"  vs composite 14.4
    # An analyst quoting the page and an agent quoting the API were BOTH citing
    # DC Hub correctly, and disagreeing. composite_score is the value every
    # ranking endpoint sorts on, so it takes the name; excess keeps its own.
    # Derived here rather than stored: market_power_scores has no composite
    # column, which is why the template never had one to render.
    if not _gated:
        s["composite_score"] = derive_composite_score(
            s.get("excess_power_score"), s.get("constraint_score"),
            s.get("time_to_power_months"), s.get("verdict"))
    else:
        s["composite_score"] = None

    market_html = render_template_string(DCPI_MARKET_TEMPLATE, s=s,
                                          risks=risks, opps=opps, gated=_gated,
                                          narrative=narrative_text,
                                          place_label=_place_label(s.get('market_name'),
                                                                   s.get('state')),
                                          facilities_html=_facilities_html)
    # r43-H: cache the rendered page (bounded — 300+ markets max).
    if len(_DCPI_PAGE_CACHE) < 500:
        _DCPI_PAGE_CACHE[_ckey] = (_now + _DCPI_PAGE_TTL, market_html)
    # RENDER-PERF: write-through to the cross-worker Redis layer so the next
    # worker/replica skips the metric backfill + inline narrative call.
    _redis_set_page(_ckey, market_html)
    market_resp = Response(market_html, mimetype="text/html")
    market_resp.headers["Content-Security-Policy"] = _DCPI_CSP  # phase 284
    market_resp.headers["X-DC-Cache"] = "miss"
    market_resp.headers["X-Cite-As"] = _cite_as_header(slug)
    return market_resp


# AUTO-REPAIR: duplicate route '/api/v1/dcpi/history' also in routes/dcpi_temporal.py:52 — review and remove one

@dcpi_bp.route("/api/v1/dcpi/history", methods=["GET"])
def api_history():
    """Return per-day score history for top BUILD markets, last 30 days."""
    _ensure_tables()
    # r80: read the REAL daily history table. market_power_scores is
    # UPDATE-in-place (computed_at=NOW() per recompute), so grouping it by
    # day collapsed every market to a single point → /history looked frozen
    # ("heartbeat_surfaces_stale" 26h). dcpi_daily_snapshots is the genuine
    # one-row-per-market-per-day series (306 markets/day, ~14d deep, written
    # by the facility-snapshot-daily cron) — /trending and /movers already
    # read it; /history was the straggler.
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT market_slug, market_name,
                   snapshot_date AS day,
                   COALESCE(excess_power_score, 0) AS excess,
                   COALESCE(constraint_score, 0) AS constraint
            FROM dcpi_daily_snapshots
            WHERE snapshot_date > CURRENT_DATE - INTERVAL '30 days'
            ORDER BY market_slug, snapshot_date
        """)
        rows = cur.fetchall()
    series = {}
    for r in rows:
        slug = r["market_slug"]
        if slug not in series:
            series[slug] = {"name": r["market_name"], "data": []}
        series[slug]["data"].append({
            "day": r["day"].isoformat()[:10] if r.get("day") else None,
            "excess": float(r["excess"] or 0),
            "constraint": float(r["constraint"] or 0),
        })
    # r-gate-everywhere (2026-06-27): the daily score time-series for ALL ~317
    # markets is the crown-jewel paid dataset (an anon could reconstruct every
    # composite). Keep the day axis + market names (SEO: "daily history exists"),
    # null the numeric values for non-paid. SINGLE-market free-to-cite (2026-07-03).
    if not _dcpi_single_market_paid():
        for _slug in series:
            for _d in series[_slug].get("data", []):
                _d["excess"] = None
                _d["constraint"] = None
        return jsonify(series=series, count=len(series), **_dcpi_gated_meta()), 200
    return jsonify(series=series, count=len(series)), 200



@dcpi_bp.route("/api/v1/dcpi/trending", methods=["GET"])
def api_trending():
    """Top 5 weekly movers, formatted for ticker display."""
    _ensure_tables()
    # Phase 268 (2026-05-29): same fix as /api/v1/dcpi/movers — read
    # week_ago from dcpi_daily_snapshots (real history) instead of
    # market_power_scores (UPDATE-in-place, always recent).
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            WITH latest AS (
              SELECT DISTINCT ON (market_slug) market_slug, market_name, excess_power_score AS now_e
              FROM market_power_scores WHERE published = true ORDER BY market_slug, computed_at DESC
            ),
            week_ago AS (
              SELECT DISTINCT ON (market_slug) market_slug, excess_power_score AS prev_e
              FROM dcpi_daily_snapshots
              WHERE snapshot_date <= CURRENT_DATE - INTERVAL '7 days'
              ORDER BY market_slug, snapshot_date DESC
            )
            SELECT l.market_slug, l.market_name, l.now_e,
                   COALESCE(l.now_e - w.prev_e, 0) AS delta_7d
            FROM latest l LEFT JOIN week_ago w ON l.market_slug=w.market_slug
            ORDER BY ABS(COALESCE(l.now_e - w.prev_e, 0)) DESC LIMIT 5
        """)
        rows = cur.fetchall()
    return jsonify(trending=rows, count=len(rows)), 200


@dcpi_bp.route("/dcpi/ticker.html", methods=["GET"])
@dcpi_bp.route("/api/v1/dcpi/ticker.html", methods=["GET"])
def ticker_widget():
    """Embeddable horizontal ticker widget. Drop in any iframe."""
    html = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>
body{margin:0;padding:0;font-family:-apple-system,sans-serif;background:#0a0a12;color:#fff;overflow:hidden}
.ticker{display:flex;align-items:center;height:48px;border-top:1px solid #1f2030;border-bottom:1px solid #1f2030;animation:scroll 40s linear infinite}
.item{flex:0 0 auto;display:flex;align-items:center;gap:0.5rem;padding:0 1.5rem;border-right:1px solid #1f2030;white-space:nowrap}
.lbl{font-size:0.7rem;color:#9ca3af;text-transform:uppercase;letter-spacing:0.08em}
.market{font-weight:600;font-size:0.92rem}
.score{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:0.92rem}
.up{color:#10b981}.down{color:#ef4444}
.brand{flex:0 0 auto;background:linear-gradient(135deg,#6366f1,#a855f7);padding:0 1.25rem;height:48px;display:flex;align-items:center;font-weight:700;font-size:0.85rem;letter-spacing:0.08em;text-transform:uppercase}
@keyframes scroll{0%{transform:translateX(0)}100%{transform:translateX(-100%)}}
</style></head><body>
<div style="display:flex;align-items:center;height:48px;background:#0a0a12">
<div class="brand">DCPI · Live</div>
<div class="ticker" id="t"></div>
</div>
<script>
fetch('/api/v1/dcpi/trending').then(r=>r.json()).then(d=>{
  const html = (d.trending||[]).map(t=>{
    const dir = t.delta_7d>0?'up':'down', arrow=t.delta_7d>0?'▲':'▼';
    return '<div class=item><span class=lbl>'+arrow+'</span><a href="https://dchub.cloud/dcpi/'+t.market_slug+'" target="_blank" style="color:#fff;text-decoration:none"><span class=market>'+t.market_name+'</span></a><span class="score '+dir+'">'+t.now_e.toFixed(1)+' ('+(t.delta_7d>0?'+':'')+t.delta_7d.toFixed(1)+')</span></div>';
  }).join('');
  document.getElementById('t').innerHTML = html + html;  // double for seamless scroll
});
</script></body></html>"""
    resp = Response(html, mimetype="text/html")
    resp.headers["X-Frame-Options"] = "ALLOWALL"
    resp.headers["Cache-Control"] = "public, max-age=300, must-revalidate"
    return resp


@dcpi_bp.route("/dcpi/og/<slug>.svg", methods=["GET"])
@dcpi_bp.route("/dcpi/og/<slug>", methods=["GET"])
def og_card(slug):
    """1200x630 SVG for LinkedIn/X cards. Phase 121C: fixed layout."""
    _ensure_tables()
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""SELECT * FROM market_power_scores WHERE market_slug = %s
                       ORDER BY computed_at DESC LIMIT 1""", (slug,))
        s = cur.fetchone()
    if not s: return Response("not found", status=404)

    # r-one-dcpi-card (2026-08-08): two faults in these three lines, on a
    # public OCR-able image that is what a shared market link previews.
    #  (1) int() TRUNCATES rather than rounds — 19.8 rendered as 19 and 43.8 as
    #      43, so the card disagreed with the page on essentially every market.
    #  (2) `or 0` turns an ABSENT score into a confident 0 — the same
    #      null-as-zero class fixed on the market page today. A market with no
    #      score must not have a zero drawn for it in an image.
    _excess_raw = s["excess_power_score"]
    _constraint_raw = s["constraint_score"]
    _ttp_raw = s["time_to_power_months"]
    _has_scores = _excess_raw is not None and _constraint_raw is not None
    excess_score = int(round(float(_excess_raw))) if _excess_raw is not None else None
    constraint_score = int(round(float(_constraint_raw))) if _constraint_raw is not None else None
    ttp = int(round(float(_ttp_raw))) if _ttp_raw is not None else None
    excess_color = ("#9ca3af" if excess_score is None else
                    "#10b981" if excess_score >= 65 else
                    "#f59e0b" if excess_score >= 40 else "#ef4444")
    verdict_color = {"BUILD": "#10b981", "CAUTION": "#f59e0b", "AVOID": "#ef4444"}.get(s["verdict"], "#9ca3af")
    # the social card is an OCR-able public surface — under free-per-market
    # (2026-07-03) it shows the single-market scores, not a Pro placeholder.
    if not _dcpi_single_market_paid():
        _excess_disp, _constraint_disp = "Pro", "Pro"
        _ttp_disp = "SCORES: DC HUB PRO"
        excess_color = verdict_color
    else:
        # "n/a" — never a drawn zero — when a component has no value.
        _excess_disp = "n/a" if excess_score is None else str(excess_score)
        _constraint_disp = "n/a" if constraint_score is None else str(constraint_score)
        _ttp_disp = "TIME TO POWER: N/A" if ttp is None else f"~{ttp}mo TO POWER"

    svg = f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#0a0a12"/>
      <stop offset="1" stop-color="#1a1a2e"/>
    </linearGradient>
    <linearGradient id="brand" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#6366f1"/>
      <stop offset="1" stop-color="#a855f7"/>
    </linearGradient>
  </defs>
  <rect width="1200" height="630" fill="url(#bg)"/>

  <!-- Brand strip top -->
  <rect x="0" y="0" width="1200" height="6" fill="url(#brand)"/>

  <!-- Header -->
  <text x="60" y="80" font-family="-apple-system, sans-serif" font-size="22" font-weight="700"
        fill="#9ca3af" letter-spacing="2">DCPI · DC HUB POWER INDEX</text>

  <!-- Market name + region -->
  <text x="60" y="180" font-family="-apple-system, sans-serif" font-size="76" font-weight="800"
        fill="white" letter-spacing="-1">{s['market_name']}</text>
  <text x="60" y="220" font-family="-apple-system, sans-serif" font-size="22"
        fill="#9ca3af">{s['iso']} · {s['state']}</text>

  <!-- Excess Power, left column -->
  <text x="60" y="320" font-family="-apple-system, sans-serif" font-size="18" font-weight="600"
        fill="#9ca3af" letter-spacing="2">EXCESS POWER SCORE</text>
  <text x="60" y="500" font-family="-apple-system, sans-serif" font-size="180" font-weight="800"
        fill="{excess_color}" letter-spacing="-6">{_excess_disp}</text>

  <!-- Constraint, right column -->
  <text x="700" y="320" font-family="-apple-system, sans-serif" font-size="18" font-weight="600"
        fill="#9ca3af" letter-spacing="2">CONSTRAINT</text>
  <text x="700" y="450" font-family="-apple-system, sans-serif" font-size="120" font-weight="700"
        fill="#9ca3af" letter-spacing="-3">{_constraint_disp}</text>
  <text x="700" y="495" font-family="-apple-system, sans-serif" font-size="20" font-weight="600"
        fill="#9ca3af" letter-spacing="2">{_ttp_disp}</text>

  <!-- Verdict bottom -->
  <text x="60" y="565" font-family="-apple-system, sans-serif" font-size="26" font-weight="800"
        fill="{verdict_color}" letter-spacing="3">VERDICT: {s['verdict']}</text>

  <!-- URL bottom right -->
  <text x="1140" y="600" font-family="-apple-system, sans-serif" font-size="16"
        fill="#6b7280" text-anchor="end">dchub.cloud/dcpi/{slug}</text>
</svg>"""
    return Response(svg, mimetype="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=600, must-revalidate"})


# r42w (2026-05-26): PNG OG card for /dcpi/<slug>. LinkedIn cards render
# SVG inconsistently — most posts come back with bare-link, no thumbnail.
# PIL-based 1200x630 PNG renders reliably in every platform's link card.
@dcpi_bp.route("/dcpi/og/<slug>.png", methods=["GET"])
def og_card_png(slug):
    """1200x630 PNG card for LinkedIn/X/Bluesky/Slack link-card previews."""
    _ensure_tables()
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""SELECT * FROM market_power_scores WHERE market_slug = %s
                       ORDER BY computed_at DESC LIMIT 1""", (slug,))
        s = cur.fetchone()
    if not s:
        return Response("not found", status=404)

    from PIL import Image, ImageDraw, ImageFont
    import io as _io

    # r-one-dcpi-card (2026-08-08): same two faults as the SVG card — int()
    # truncates (19.8 -> 19) and `or 0` draws a zero for a market that has no
    # score. This PNG renderer was missed on the first pass and only surfaced
    # because the guard enumerated EVERY function registered under the card
    # routes rather than the two I had in mind.
    _e_raw = s.get("excess_power_score")
    _c_raw = s.get("constraint_score")
    _t_raw = s.get("time_to_power_months")
    excess = int(round(float(_e_raw))) if _e_raw is not None else None
    constraint = int(round(float(_c_raw))) if _c_raw is not None else None
    ttp = int(round(float(_t_raw))) if _t_raw is not None else None
    market = s.get("market_name") or slug
    iso = s.get("iso") or "—"
    verdict = s.get("verdict") or "—"

    verdict_color = {"BUILD": (16, 185, 129),
                     "CAUTION": (245, 158, 11),
                     "AVOID": (239, 68, 68)}.get(verdict, (156, 163, 175))
    excess_color = ((156, 163, 175) if excess is None else
                    (16, 185, 129) if excess >= 65 else
                    (245, 158, 11) if excess >= 40 else (239, 68, 68))

    img = Image.new("RGB", (1200, 630), (10, 10, 18))
    draw = ImageDraw.Draw(img)

    # Subtle gradient background — two-tone vertical
    for y in range(630):
        t = y / 630.0
        r = int(10 + t * 16)
        g = int(10 + t * 16)
        b = int(18 + t * 30)
        draw.line([(0, y), (1200, y)], fill=(r, g, b))

    # Top brand strip
    draw.rectangle([(0, 0), (1200, 6)], fill=(99, 102, 241))

    # Fonts (graceful fallback if not available)
    try:
        f_label = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 22)
        f_market = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 64)
        f_iso = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 28)
        f_score = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 96)
        f_score_lbl = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 20)
        f_verdict = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 40)
        f_foot = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 20)
    except (OSError, IOError):
        f_label = ImageFont.load_default()
        f_market = ImageFont.load_default()
        f_iso = ImageFont.load_default()
        f_score = ImageFont.load_default()
        f_score_lbl = ImageFont.load_default()
        f_verdict = ImageFont.load_default()
        f_foot = ImageFont.load_default()

    draw.text((60, 50), "DCPI · DC HUB POWER INDEX", fill=(156, 163, 175), font=f_label)
    draw.text((60, 105), market[:30], fill=(255, 255, 255), font=f_market)
    draw.text((60, 195), f"{iso}", fill=(156, 163, 175), font=f_iso)

    # Verdict pill
    pill_x, pill_y = 60, 250
    verdict_text = verdict
    bbox = draw.textbbox((pill_x, pill_y), verdict_text, font=f_verdict)
    pw = (bbox[2] - bbox[0]) + 40
    ph = (bbox[3] - bbox[1]) + 16
    draw.rounded_rectangle([(pill_x - 10, pill_y - 8),
                             (pill_x - 10 + pw, pill_y - 8 + ph)],
                            radius=12, fill=verdict_color)
    draw.text((pill_x + 10, pill_y - 4), verdict_text,
              fill=(10, 14, 26), font=f_verdict)

    # Score blocks — free-per-market (2026-07-03): single-market card shows the
    # numeric scores; only bulk/all-market surfaces stay Pro.
    _og_paid = _dcpi_single_market_paid()
    _excess_disp = ("Pro" if not _og_paid
                    else ("n/a" if excess is None else f"{excess}"))
    _constraint_disp = ("Pro" if not _og_paid
                        else ("n/a" if constraint is None else f"{constraint}"))
    _ttp_disp = ("Pro" if not _og_paid else ("n/a" if ttp is None else f"{ttp}mo"))
    if not _og_paid:
        excess_color = verdict_color
    draw.text((60, 360), "Excess Power", fill=(156, 163, 175), font=f_score_lbl)
    draw.text((60, 390), _excess_disp, fill=excess_color, font=f_score)

    constraint_color = ((156, 163, 175) if constraint is None else
                        (239, 68, 68) if constraint >= 70 else
                        (245, 158, 11) if constraint >= 45 else (16, 185, 129))
    if not _og_paid:
        constraint_color = verdict_color
    draw.text((420, 360), "Constraint", fill=(156, 163, 175), font=f_score_lbl)
    draw.text((420, 390), _constraint_disp, fill=constraint_color, font=f_score)

    draw.text((780, 360), "Time to Power", fill=(156, 163, 175), font=f_score_lbl)
    draw.text((780, 390), _ttp_disp, fill=(255, 255, 255), font=f_score)

    # Footer
    draw.text((60, 570), f"dchub.cloud/dcpi/{slug}  ·  CC-BY-4.0",
              fill=(156, 163, 175), font=f_foot)

    buf = _io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return Response(
        buf.read(),
        mimetype="image/png",
        headers={
            "Cache-Control": "public, max-age=3600, s-maxage=3600",
            "X-DC-Card-Slug": slug,
            "X-DC-Card-Format": "png",
        },
    )


# === Phase FF (2026-05-21): three live-bug fixes ===
# 1) embed_widget was referenced by embed_widget_alias (/api/v1/dcpi/embed/<slug>)
#    and advertised by the oembed endpoint, but was NEVER DEFINED → NameError 500.
# 2) /dcpi/og.svg (the INDEX social card, referenced by og:image on the main page)
#    had no route → swallowed by /dcpi/<slug> → broken LinkedIn/X preview.
# 3) /dcpi/methodology (linked 3× in the footer as "methodology + BibTeX") had no
#    route → swallowed by /dcpi/<slug> → bogus market page / 404.
# Static routes outrank /dcpi/<slug> in Flask's URL map, so order is irrelevant.

@dcpi_bp.route("/dcpi/embed/<slug>", methods=["GET"])
def embed_widget(slug):
    """Compact self-contained HTML score card for iframe embedding.
    Mirrors og_card's data fetch. Safe: read-only, public market scores."""
    _ensure_tables()
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""SELECT * FROM market_power_scores WHERE market_slug = %s
                       ORDER BY computed_at DESC LIMIT 1""", (slug,))
        s = cur.fetchone()
    if not s:
        return Response("market not found", status=404, mimetype="text/plain")

    # free-per-market (2026-07-03): a single-market iframe embed shows the numeric
    # scores; bulk/all-market surfaces stay Pro.
    if not _dcpi_single_market_paid():
        s = dict(s)
        for _k in ("excess_power_score", "constraint_score", "time_to_power_months",
                   "composite_score"):
            if _k in s:
                s[_k] = None
        _embed_gated = True
    else:
        _embed_gated = False

    # r-one-dcpi-card (2026-08-08): two faults in these three lines, on a
    # public OCR-able image that is what a shared market link previews.
    #  (1) int() TRUNCATES rather than rounds — 19.8 rendered as 19 and 43.8 as
    #      43, so the card disagreed with the page on essentially every market.
    #  (2) `or 0` turns an ABSENT score into a confident 0 — the same
    #      null-as-zero class fixed on the market page today. A market with no
    #      score must not have a zero drawn for it in an image.
    _excess_raw = s["excess_power_score"]
    _constraint_raw = s["constraint_score"]
    _ttp_raw = s["time_to_power_months"]
    _has_scores = _excess_raw is not None and _constraint_raw is not None
    excess_score = int(round(float(_excess_raw))) if _excess_raw is not None else None
    constraint_score = int(round(float(_constraint_raw))) if _constraint_raw is not None else None
    ttp = int(round(float(_ttp_raw))) if _ttp_raw is not None else None
    excess_color = ("#9ca3af" if excess_score is None else
                    "#10b981" if excess_score >= 65 else
                    "#f59e0b" if excess_score >= 40 else "#ef4444")
    # Display strings so a missing component reads "n/a" in the rendered embed
    # AND in its JSON-LD prose — never the literal "None", never a drawn zero.
    _e_txt = "n/a" if excess_score is None else str(excess_score)
    _c_txt = "n/a" if constraint_score is None else str(constraint_score)
    _t_txt = "n/a" if ttp is None else str(ttp)
    verdict_color = {"BUILD": "#10b981", "CAUTION": "#f59e0b",
                     "AVOID": "#ef4444"}.get(s["verdict"], "#9ca3af")
    # r41-dcpi-jsonld (2026-05-25): Dataset + Place schema.org JSON-LD
    # so search engines / AI crawlers recognize this as structured data.
    # Google rewards Dataset markup with rich snippets in result pages
    # and the existing check_schema_org_coverage_low detector stops
    # flagging /dcpi/<slug> pages. Composite score in JSON-LD lets
    # AI agents quote "DCPI score 73.1, BUILD" with a citation source.
    import json as _json_jl
    _composite = derive_composite_score(
        s.get('excess_power_score'), s.get('constraint_score'),
        s.get('time_to_power_months'), s.get('verdict'),
    )
    _dataset = {
        "@type": "Dataset",
        "name": f"DCPI Score — {s['market_name']}",
        "description": (f"Data Center Power Index (DCPI) score for "
                        f"{s['market_name']}: {_composite}/100 — "
                        f"verdict {s['verdict']}. Excess power "
                        f"{_e_txt}, constraint {_c_txt}, "
                        f"time-to-power ~{_t_txt} months. ISO: {s['iso']}."),
        "url": f"https://dchub.cloud/dcpi/{slug}",
        "creator": {"@type": "Organization", "name": "DC Hub",
                    "url": "https://dchub.cloud"},
        "license": "https://dchub.cloud/dcpi#methodology",
        "isAccessibleForFree": True,
        "keywords": ["data center", "power availability", "ISO",
                     s['iso'], s['state'], s['verdict'], "DCPI"],
        # r-jsonld-country (2026-08-08): addressCountry was the literal "US" for
        # every market, so 61 non-US markets — Tokyo, Singapore, Frankfurt —
        # asserted they were in the United States in the one channel AI engines
        # lift verbatim. Resolved per market now, and OMITTED rather than
        # guessed when it cannot be determined.
        "spatialCoverage": _dcpi_place(s, slug),
        "variableMeasured": [
            {"@type": "PropertyValue", "name": "composite_score",
             "value": _composite, "minValue": 0, "maxValue": 100},
            {"@type": "PropertyValue", "name": "excess_power_score",
             "value": excess_score, "minValue": 0, "maxValue": 100},
            {"@type": "PropertyValue", "name": "constraint_score",
             "value": constraint_score, "minValue": 0, "maxValue": 100},
            {"@type": "PropertyValue", "name": "verdict",
             "value": s['verdict']},
        ],
        "dateModified": (s.get('computed_at').isoformat()
                          if hasattr(s.get('computed_at'), 'isoformat')
                          else str(s.get('computed_at') or '')),
    }
    # r-geo-dcpi-faq (2026-06-25): add BreadcrumbList (crawl context) + FAQPage
    # (the schema AI engines lift verbatim into cited answers, e.g. "Is Ashburn
    # BUILD or AVOID?"). Q&A is generated from the live DCPI score so it never
    # contradicts the page. Emitted as one @graph to keep a single JSON-LD block.
    _breadcrumb = {
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "DC Hub",
             "item": "https://dchub.cloud/"},
            {"@type": "ListItem", "position": 2, "name": "Power Index (DCPI)",
             "item": "https://dchub.cloud/dcpi"},
            {"@type": "ListItem", "position": 3, "name": s['market_name'],
             "item": f"https://dchub.cloud/dcpi/{slug}"},
        ],
    }
    _faq = {
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question",
             "name": f"What is the DCPI score for {s['market_name']}?",
             "acceptedAnswer": {"@type": "Answer",
                "text": (f"{s['market_name']} has a DC Hub Power Index (DCPI) "
                         f"composite score of {_composite}/100 with a "
                         f"{s['verdict']} verdict.")}},
            {"@type": "Question",
             "name": f"Is {s['market_name']} a good market to build a data center?",
             "acceptedAnswer": {"@type": "Answer",
                "text": (f"DC Hub rates {s['market_name']} as {s['verdict']}: "
                         f"excess-power score {_e_txt}/100, grid-constraint "
                         f"score {_c_txt}/100, and modeled time-to-power "
                         f"~{_t_txt} months. It is served by the {s['iso']} grid region.")}},
            {"@type": "Question",
             "name": f"Which grid operator (ISO) serves {s['market_name']}?",
             "acceptedAnswer": {"@type": "Answer",
                "text": f"{s['market_name']} is in the {s['iso']} ISO/RTO grid region."}},
        ],
    }
    _jsonld = _json_jl.dumps({
        "@context": "https://schema.org",
        "@graph": [_dataset, _breadcrumb, _faq],
    }, separators=(',', ':'))
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DCPI · {s['market_name']}</title>
<script type="application/ld+json">{_jsonld}</script>
<style>
*{{margin:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#0a0a12;color:#fff;padding:18px;border-radius:12px}}
.h{{font-size:11px;letter-spacing:2px;color:#9ca3af;font-weight:600}}
.m{{font-size:26px;font-weight:800;margin:2px 0 1px;letter-spacing:-.5px}}
.sub{{font-size:12px;color:#9ca3af;margin-bottom:14px}}
.row{{display:flex;gap:24px}}
.lbl{{font-size:10px;letter-spacing:1px;color:#9ca3af;font-weight:600}}
.big{{font-size:44px;font-weight:800;line-height:1;letter-spacing:-2px}}
.med{{font-size:30px;font-weight:700;line-height:1;color:#9ca3af}}
.v{{margin-top:14px;font-size:14px;font-weight:800;letter-spacing:2px}}
a{{color:#6366f1;text-decoration:none;font-size:11px}}
</style></head><body>
<div class="h">DCPI · DC HUB POWER INDEX</div>
<div class="m">{s['market_name']}</div>
<div class="sub">{s['iso']} · {s['state']}</div>
<div class="row">
  <div><div class="lbl">EXCESS POWER</div><div class="big" style="color:{excess_color}">{_e_txt}</div></div>
  <div><div class="lbl">CONSTRAINT</div><div class="med">{_c_txt}</div></div>
  <div><div class="lbl">TO POWER</div><div class="med">{"n/a" if ttp is None else f"~{ttp}mo"}</div></div>
</div>
<div class="v" style="color:{verdict_color}">VERDICT: {s['verdict']}</div>
<div style="margin-top:14px"><a href="https://dchub.cloud/dcpi/{slug}" target="_blank" rel="noopener">dchub.cloud/dcpi/{slug} →</a></div>
</body></html>"""
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=600",
                             "X-Frame-Options": "ALLOWALL"})


@dcpi_bp.route("/dcpi/og.svg", methods=["GET"])
def og_card_index():
    """National INDEX social card (no slug). Fixes the broken og:image on /dcpi."""
    _ensure_tables()
    markets = 0
    top_name = top_score = None
    builds = 0
    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                WITH latest AS (
                  SELECT DISTINCT ON (market_slug) market_name, excess_power_score, verdict
                    FROM market_power_scores
                   ORDER BY market_slug, computed_at DESC)
                SELECT COUNT(*) n,
                       COUNT(*) FILTER (WHERE verdict='BUILD') builds,
                       (SELECT market_name FROM latest ORDER BY excess_power_score DESC NULLS LAST LIMIT 1) top_name,
                       (SELECT MAX(excess_power_score) FROM latest) top_score
                  FROM latest
            """)
            r = cur.fetchone() or {}
            markets = int(r.get("n") or 0)
            builds = int(r.get("builds") or 0)
            top_name = r.get("top_name") or "—"
            top_score = int(r.get("top_score") or 0)
    except Exception:
        top_name = top_name or "—"; top_score = top_score or 0
    svg = f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#0a0a12"/><stop offset="1" stop-color="#1a1a2e"/>
    </linearGradient>
    <linearGradient id="brand" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#6366f1"/><stop offset="1" stop-color="#a855f7"/>
    </linearGradient>
  </defs>
  <rect width="1200" height="630" fill="url(#bg)"/>
  <rect x="0" y="0" width="1200" height="6" fill="url(#brand)"/>
  <text x="60" y="100" font-family="-apple-system, sans-serif" font-size="24" font-weight="700"
        fill="#9ca3af" letter-spacing="3">DCPI · DATA CENTER POWER INDEX</text>
  <text x="60" y="230" font-family="-apple-system, sans-serif" font-size="84" font-weight="800"
        fill="white" letter-spacing="-2">Where the power is.</text>
  <text x="60" y="300" font-family="-apple-system, sans-serif" font-size="26"
        fill="#9ca3af">{markets} U.S. markets scored daily · {builds} rated BUILD</text>
  <text x="60" y="430" font-family="-apple-system, sans-serif" font-size="18" font-weight="600"
        fill="#9ca3af" letter-spacing="2">TOP MARKET FOR EXCESS POWER</text>
  <text x="60" y="510" font-family="-apple-system, sans-serif" font-size="64" font-weight="800"
        fill="#10b981" letter-spacing="-1">{top_name} · {top_score}</text>
  <text x="1140" y="600" font-family="-apple-system, sans-serif" font-size="18"
        fill="#6b7280" text-anchor="end">dchub.cloud/dcpi</text>
</svg>"""
    return Response(svg, mimetype="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=600, must-revalidate"})


# NOTE: there is intentionally NO backend /dcpi/methodology route — Cloudflare
# Pages serves /dcpi/methodology/ as a static page (it intercepts /dcpi/* before
# the request reaches this Flask backend). A backend route here would be dead,
# unreachable code. If the methodology page needs JSON-LD/BibTeX, edit the CF
# Pages static file, not this file. (Phase FF, 2026-05-21.)


@dcpi_bp.route("/dcpi/press", methods=["GET"], strict_slashes=False)
def press_kit():
    return Response("""<!DOCTYPE html><html><head><title>DCPI Press Kit</title>
<style>body{font-family:system-ui;max-width:780px;margin:2rem auto;padding:2rem;line-height:1.6;color:#222}
h1{margin:0 0 0.5rem}h2{margin:1.5rem 0 0.5rem;border-bottom:1px solid #ddd;padding-bottom:0.3rem}
code{background:#f3f3f3;padding:0.1rem 0.4rem;border-radius:3px;font-size:0.9em}
.embed{background:#1a1a2e;color:#eee;padding:1rem;border-radius:6px;font-size:0.85rem;overflow-x:auto}
</style></head><body>
<h1>DCPI Press Kit</h1>
<p>The Data Center Power Index (DCPI) is a daily-updated indicator of power
availability across U.S. data center markets. Free for the press to cite.</p>
<h2>What is DCPI?</h2>
<p>Two scores per market: <strong>Excess Power Score</strong> (0–100, high = opportunity) and
<strong>Constraint Score</strong> (0–100, high = avoid). Excess Power surfaces stranded capacity,
curtailed renewables, and behind-the-meter industrial headroom — power
that's available but not commonly tracked.</p>
<h2>Citation format</h2>
<p><code>According to the DC Hub Power Index, [Market] scored [N] on [date]. Source: dchub.cloud/dcpi.</code></p>
<h2>API access</h2>
<p>Free JSON: <code>GET dchub.cloud/api/v1/dcpi/scores</code></p>
<h2>Embed widget</h2>
<p>Drop into any article (forthcoming in phase 110):</p>
<div class="embed">&lt;iframe src="https://dchub.cloud/dcpi/embed/atlanta" width="400" height="200" frameborder="0"&gt;&lt;/iframe&gt;</div>
<h2>Methodology</h2>
<p>Excess Power Score = weighted sum of: ISO reserve margin headroom, queued generation additions &lt;12mo,
renewable curtailment volume, queue approval rate, stranded interconnection capacity at retiring plants,
and behind-the-meter industrial generation. Constraint Score = queue wait time, reserve margin proximity to
NERC floor, demand growth YoY, recent grid emergencies. Inputs ingested daily from ISO public filings,
EIA monthly data, and DC Hub's grid-feed extractors.</p>
<h2>Contact</h2>
<p>Press inquiries: jonathan@dchub.cloud</p>
</body></html>""", mimetype="text/html")

# === Phase 117b: CF-allowlisted aliases for public DCPI pages ===
@dcpi_bp.route("/api/v1/dcpi/page", methods=["GET"])
def public_dashboard_alias():
    return public_dashboard()

@dcpi_bp.route("/api/v1/dcpi/page/<slug>", methods=["GET"])
def public_market_page_alias(slug):
    return public_market_page(slug)

@dcpi_bp.route("/api/v1/dcpi/og/<slug>", methods=["GET"])
@dcpi_bp.route("/api/v1/dcpi/og/<slug>.svg", methods=["GET"])
def og_card_alias(slug):
    return og_card(slug)

@dcpi_bp.route("/api/v1/dcpi/embed/<slug>", methods=["GET"])
def embed_widget_alias(slug):
    return embed_widget(slug)

@dcpi_bp.route("/api/v1/dcpi/press", methods=["GET"])
def press_kit_alias():
    return press_kit()



# AUTO-REPAIR: duplicate route '/api/v1/dcpi/lite-recompute' also in main.py:42457 — review and remove one
# (phase 215 lite-recompute moved to main.py in phase 216 — removed duplicate here)

@dcpi_bp.route("/api/v1/dcpi/lite-recompute", methods=["POST"])
def lite_recompute():
    """Computes lite DCPI scores for ALL markets in MARKETS.
    Uses only facility count + pipeline MW + state $/kWh — no grid stress data.
    Marks results with tier_required='lite-pro' so we can distinguish from full scoring."""
    import psycopg2, os, math
    try:
        admin_key = request.headers.get("X-Admin-Key", "")
        if admin_key != os.environ.get("DCHUB_ADMIN_KEY", ""):
            return jsonify({"error": "unauthorized"}), 401

        url = os.environ.get("DATABASE_URL")
        conn = psycopg2.connect(url, connect_timeout=8)
        scored = 0
        errors = 0
        with conn.cursor() as cur:
            for m in MARKETS:
                try:
                    slug = m.get("slug") if isinstance(m, dict) else m
                    name = m.get("name") if isinstance(m, dict) else slug.replace("-", " ").title()
                    if not slug: continue
                    # Pull facility stats. r-status-taxonomy (2026-07-29):
                    # SECOND WRITER — this path never calls
                    # gather_metrics_for_market, so it carried its own copy of
                    # the unfiltered SUM and the dead 5-string literal. It
                    # upserts market_power_scores ON CONFLICT DO UPDATE, i.e.
                    # it overwrites the full path's scores, so the two must
                    # agree on what "operational" means.
                    #
                    # r-namesake (2026-08-07): country-scoped like every other
                    # market-scoped facility query. NOTE this loop is currently
                    # DEAD and the scope is closing a latent hole, not a live
                    # one: MARKETS is a list of 6-TUPLES, so `slug = m` binds
                    # the tuple and `slug.replace(...)` on the next line raises
                    # AttributeError into the per-market `except: errors += 1`.
                    # Every market fails, markets_scored is 0. Deliberately NOT
                    # repaired here — making this loop run again would let a
                    # lite score (facility count + $/kWh, no grid data)
                    # overwrite the full path's row, which is a separate
                    # decision and a regression if made by accident.
                    _lite_ctry_sql, _lite_ctry_params = _market_country_scope(
                        m[3] if isinstance(m, tuple) and len(m) >= 4 else None,
                        m[2] if isinstance(m, tuple) and len(m) >= 3 else None,
                        m[4] if isinstance(m, tuple) and len(m) >= 5 else None,
                        m[5] if isinstance(m, tuple) and len(m) >= 6 else None)
                    # r-list-dedup (2026-08-08): duplicate-scoped like the page
                    # facility list. Latent only — the loop still cannot run
                    # (see above), so this cannot move a published score today.
                    # It is here so that whoever repairs the tuple/dict bug does
                    # not simultaneously reintroduce double-counted buildings.
                    cur.execute(f"""
                        SELECT COUNT(*),
                               COALESCE(SUM(power_mw) FILTER (WHERE {_SQL_OP_STATUS}), 0),
                               COALESCE(SUM(power_mw) FILTER (WHERE {_SQL_PIPE_STATUS}), 0),
                               COALESCE(MAX(state), 0)
                        FROM discovered_facilities
                        WHERE (LOWER(city) = %s OR LOWER(city) LIKE %s)
                          AND duplicate_of_id IS NULL
                          {_lite_ctry_sql};
                    """, (slug.replace("-", " "), '%' + slug.replace("-", " ") + '%',
                          *_lite_ctry_params))
                    row = cur.fetchone()
                    if not row: continue
                    fac, op_mw, pipe_mw, state = row
                    if not fac: continue
                    op_mw = float(op_mw or 0)
                    pipe_mw = float(pipe_mw or 0)

                    # $/kWh from state
                    cur.execute("""
                        SELECT COALESCE(AVG(price_cents_kwh), 0)/100.0 FROM eia_electricity_rates
                        WHERE state=%s AND sector='ALL'
                          AND retrieved_at > NOW() - INTERVAL '365 days';
                    """, (state,))
                    kr = cur.fetchone()
                    kwh = float(kr[0]) if kr and kr[0] else None

                    # Lite scoring (0-100 scale):
                    # constraint_score: high pipeline ratio → constrained
                    # excess_power_score: low pipeline + cheap kWh → opportunity
                    # r-status-taxonomy (2026-07-29): the denominator is
                    # the market's TOTAL footprint, not op_mw. It MUST move
                    # with the op_mw fix or the fix inverts here — op_mw used
                    # to BE the total (pipeline included), so pipe/op already
                    # meant "share of footprint that is pipeline" and stayed
                    # bounded 0..1. Dividing the corrected, smaller op_mw into
                    # pipe_mw instead multiplies the ratio ~2.6x and pins 7 of
                    # the 20 measured movers at constraint=100 → AVOID.
                    # Verified against the measured set: columbus, houston,
                    # fort-worth, new-albany and albuquerque reproduce their
                    # previous ratio to 3dp under this form.
                    _footprint_mw = op_mw + pipe_mw
                    pipe_ratio = (pipe_mw / _footprint_mw) if _footprint_mw > 0 else 0
                    constraint = min(100, pipe_ratio * 150)  # >0.67 ratio → max constraint
                    excess = 0
                    if kwh:
                        # Cheaper → higher excess opportunity
                        excess = max(0, min(100, (0.30 - kwh) * 333))  # $0.08 → 73, $0.20 → 33
                    if pipe_mw < 50 and op_mw > 100:
                        excess = max(excess, 60)  # underbuilt market

                    verdict = "BUILD" if excess > 50 and constraint < 60 else ("AVOID" if constraint > 75 else "CAUTION")

                    # r-provenance-writer (2026-08-08): the lite formula shares
                    # no weight, ceiling or band with the published method, so
                    # it must never overwrite a row the full scorer produced —
                    # doing so leaves that row's method_version in place over
                    # numbers that version did not compute. Guard imported, not
                    # retyped; see util/dcpi_score_row.
                    cur.execute(f"""
                        INSERT INTO market_power_scores
                        (market_slug, market_name, latitude, longitude,
                         constraint_score, excess_power_score, time_to_power_months,
                         verdict, tier_required, computed_at)
                        VALUES (%s, %s, NULL, NULL, %s, %s, NULL, %s, 'lite-pro', NOW() ON CONFLICT DO NOTHING)
                        ON CONFLICT (market_slug) DO UPDATE SET
                          constraint_score = EXCLUDED.constraint_score,
                          excess_power_score = EXCLUDED.excess_power_score,
                          verdict = EXCLUDED.verdict,
                          tier_required = EXCLUDED.tier_required,
                          computed_at = NOW()
                        WHERE {LITE_MAY_NOT_CLOBBER_FULL};
                    """, (slug, name, constraint, excess, verdict))
                    scored += 1
                except Exception as e:
                    errors += 1

        conn.commit()
        conn.close()

        return jsonify({
            "ok": True,
            "markets_scored": scored,
            "errors": errors,
            "total_markets": len(MARKETS),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Phase 215: ensure UNIQUE on market_slug for upsert
def _phase215_ensure_unique():
    import os, psycopg2
    try:
        conn = psycopg2.connect(os.environ.get("DATABASE_URL"), connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = 'market_power_scores_slug_key'
                    ) THEN
                        ALTER TABLE market_power_scores
                            ADD CONSTRAINT market_power_scores_slug_key UNIQUE (market_slug);
                    END IF;
                END $$;
            """)
        conn.commit()
        conn.close()
    except Exception as e:
        import logging
        logging.warning(f"phase215 unique constraint err: {e}")

try: _phase215_ensure_unique()
except: pass


# ============================================================================
# Phase 225: graceful failure — never show JSON error on user-facing pages
# ============================================================================

DCPI_FALLBACK_HTML = """<!doctype html><html><head>
<title>DC Hub Power Index · Recomputing</title>
<meta charset="utf-8"><meta http-equiv="refresh" content="30">
<style>html,body{background:rgb(5,8,16);color:#e8eef8;margin:0;padding:60px 20px;font-family:'Instrument Sans',system-ui;text-align:center;line-height:1.6}
h1{font-weight:800;font-size:36px;margin:0 0 12px}
.sub{color:#9eb5d8;font-size:18px;max-width:560px;margin:0 auto 32px}
.spinner{width:32px;height:32px;margin:0 auto 24px;border:3px solid rgba(90,163,255,.2);border-top-color:#5aa3ff;border-radius:50%;animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
a{color:#5aa3ff;text-decoration:none}
</style></head><body>
<div class="spinner"></div>
<h1>DCPI is recomputing</h1>
<p class="sub">The Data Center Power Index updates daily. Today's scoring is in progress — refresh in a moment, or browse <a href="/markets/">all markets</a> meanwhile.</p>
<p style="color:#5aa3ff"><a href="/dcpi/methodology">View methodology →</a></p>
</body></html>"""

def _phase225_dcpi_error_page(err=""):
    """Returns the recomputing-message HTML so users never see raw JSON errors."""
    import logging
    if err: logging.warning(f"[dcpi-fallback] {err}")
    return DCPI_FALLBACK_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


# Wrap the dcpi_bp blueprint errorhandler
try:
    @dcpi_bp.errorhandler(Exception)
    def _phase225_dcpi_bp_error_handler(e):
        return _phase225_dcpi_error_page(str(e))
except Exception:
    pass
