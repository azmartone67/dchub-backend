"""
brain_coverage_radar.py — the self-aware COVERAGE radar. v1 (2026-05-31).
==========================================================================

The detection half of the brain+media self-learning loop. Where
data_freshness_radar.py answers "is the data we HAVE going stale?", this
module answers the orthogonal question the brain was blind to: "what data
are we MISSING?" — i.e. where are the holes in DCPI / DCGI / ISO / gas
coverage, so the brain can PROPOSE filling them (its proposal→PR loop now
works) and DC Hub Media can CELEBRATE the ones we just closed.

Four coverage dimensions, all computed from REAL live data (never hardcoded
"we're at 86%"):

  dcgi  — per-state Data Center Gas Index. covered = US states with a
          non-null DCGI score (reuses routes.dcgi._gas_state_rollup); the
          50-US-state target minus those = gaps. A state with zero pipeline
          rows can't be scored, so it surfaces as a high-priority gap.
  dcpi  — market power index. covered = DISTINCT market_slug rows in
          market_power_scores; total = the canonical ~286 markets
          (canonical_stats). The shortfall is the gap (reported as a count,
          not a list — we don't have a canonical 286-name registry here, so
          enumerating the *missing* market names is out of scope for v1).
  iso   — the 10 live grid operators + 43 US utility BAs. covered = those
          whose grid_data feed has FRESH recent rows; STALE (>SLA) or
          0-recent-row feeds are real coverage gaps (a registered-but-dead
          feed is worse than an unregistered one — it looks live but isn't).
  gas   — natural-gas pipeline + storage presence by state. Reuses the same
          gas rollup: states with zero pipeline segments lack gas-infra
          coverage entirely (distinct from dcgi, which is the *scored index*
          gap — a state can have pipelines but still lack a price for the
          cost factor).

EVERYTHING is fail-soft. A missing table, empty result, or import error
degrades to a sane summary (coverage unknown / 0 gaps for that dimension),
NEVER a 500 — the radar going quiet must never take down the public
endpoint or poison the /heal/findings stream.

Wiring (this module owns ONLY the blueprint + helpers; the three shared-file
hooks are reported, not edited):
  GET /api/v1/brain/coverage-gaps   public-safe — gap COUNTS + coverage %%
                                     only (NO proprietary scores).
  coverage_findings()  → list shaped EXACTLY like /heal/findings
                         actionable_backend_issues entries, so appending it
                         into that aggregation makes the brain propose
                         expansions. (Hook reported for main.py.)
  coverage_wins(days)  → recently-closed gaps as agent-broadcast items
                         (kind "coverage_win", weight 78). (Hook reported
                         for routes/agent_broadcast.py.)
"""

import os
import time
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

brain_coverage_radar_bp = Blueprint("brain_coverage_radar", __name__)


# ── Canonical targets ─────────────────────────────────────────────────────
# The 50 US states DCGI / gas aim to cover (DC excluded — DCGI scores states).
_US_STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY",
]
_US_STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut",
    "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine",
    "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri",
    "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota",
    "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
}

# The 10 live North-American grid operators (mirrors iso_orchestrator.health
# registered_isos). grid_data tags each with these exact uppercase labels.
_REGISTERED_ISOS = ["ERCOT", "CAISO", "NYISO", "MISO", "PJM", "SPP", "ISONE",
                    "IESO", "TVA", "BPA"]

# US utility balancing authorities (the eia_utility_bas slot). grid_data tags
# each row with the BA `code`. Mirrors routes/eia_utility_bas._BAS codes — the
# canonical headline count is 43; this list is the set we actively register.
_REGISTERED_UTILITY_BAS = [
    "APS", "SRP", "FPL", "FPC", "SOCO", "DUK", "SCEG", "PACE", "PACW", "PSCO",
    "NEVP", "IPCO", "PNM", "TEC", "AECI", "SEC", "PGE", "PSEI", "SCL", "TPWR",
    "AVA", "CHPD", "DOPD", "GCPD", "NWMT", "LDWP", "BANC", "IID", "TIDC",
    "EPE", "TEPC", "WACM", "WALC", "WAUW", "CPLE", "CPLW", "SC", "JEA", "TAL",
    "GVL", "AEC", "LGEE", "SPA",
]

# A registered feed is "covered" only if it produced a row within this window.
# 48h matches data_freshness_radar's DCPI SLA and tolerates one cron miss on
# the (mostly daily-EIA-backed) BA feeds. Env-overridable.
_ISO_FRESH_HOURS = int(os.environ.get("DCHUB_COVERAGE_ISO_FRESH_HOURS", "48") or 48)

# How far back coverage_wins() looks for newly-closed gaps (a feed/state that
# only just started producing fresh rows). Bounded 1..30.
_DEFAULT_WIN_DAYS = 7


def _conn():
    """Shared DB connection contextmanager — the same one every ISO/DCGI
    route uses. Never raises here; callers wrap in try/except."""
    from routes._iso_common import conn
    return conn()


# ── Per-dimension coverage probes (each fully guarded) ─────────────────────

def _dcgi_coverage():
    """DCGI per-state scoring coverage. Returns
    (covered_states:set, total:int, scored_map:dict|None, err:str|None).

    Reuses routes.dcgi._gas_state_rollup() — the SAME computation the live
    /api/v1/dcgi/scores serves — so coverage is judged against real scored
    output, not a re-derivation that could drift. A state is "covered" if it
    has a non-null `dcgi` score."""
    try:
        from routes.dcgi import _gas_state_rollup
    except Exception as e:
        return set(), len(_US_STATES), None, "dcgi_import: " + str(e)[:120]
    try:
        states, err = _gas_state_rollup()
    except Exception as e:
        return set(), len(_US_STATES), None, "rollup_raise: " + str(e)[:120]
    if err:
        return set(), len(_US_STATES), None, str(err)[:160]
    states = states or {}
    covered = {st for st, s in states.items()
               if isinstance(s, dict) and s.get("dcgi") is not None
               and st in _US_STATE_NAMES}
    return covered, len(_US_STATES), states, None


def _gas_pipeline_coverage(scored_map):
    """Natural-gas pipeline presence by state. covered = states with >=1
    pipeline segment. Reuses the dcgi rollup output (passed in) so we don't
    re-query gas_pipelines twice. Returns (covered_states:set, total:int,
    err:str|None)."""
    if scored_map is None:
        # The dcgi rollup already failed/empty — report unknown gas coverage
        # rather than a second failing query.
        return set(), len(_US_STATES), "no_rollup"
    covered = {st for st, s in scored_map.items()
               if isinstance(s, dict) and int(s.get("pipelines") or 0) > 0
               and st in _US_STATE_NAMES}
    return covered, len(_US_STATES), None


def _dcpi_coverage():
    """DCPI market scoring coverage. covered = DISTINCT market_slug rows in
    market_power_scores; total = canonical ~286 markets. Returns
    (covered:int, total:int, err:str|None). Never raises."""
    total = 286
    try:
        from canonical_stats import get_canonical_stats
        total = int(get_canonical_stats().get("markets", 286) or 286)
    except Exception:
        total = 286
    covered = 0
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SET statement_timeout = 6000")
            try:
                cur.execute(
                    "SELECT COUNT(DISTINCT market_slug) FROM market_power_scores "
                    "WHERE market_slug IS NOT NULL AND market_slug <> ''")
                covered = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                try: c.rollback()
                except Exception: pass
                return 0, total, "market_power_scores_unreadable"
    except Exception as e:
        return 0, total, "dcpi_db: " + str(e)[:120]
    # Coverage can't exceed the canonical target in the reported %% (a market
    # we score that isn't in the 286 canon shouldn't inflate us past 100).
    if covered > total and total > 0:
        total = covered
    return covered, total, None


def _iso_feed_freshness():
    """Recency of every registered grid feed (ISOs + utility BAs) from
    grid_data. Returns ({label: {"age_hours": float|None, "rows": int,
    "fresh": bool}}, err:str|None). Mirrors _iso_common.health_for_iso's
    MAX(timestamp)/COUNT query, batched into one grouped scan. Never raises.

    A feed is "fresh" (covered) iff it has >=1 row newer than
    _ISO_FRESH_HOURS. STALE or absent → coverage gap."""
    labels = _REGISTERED_ISOS + _REGISTERED_UTILITY_BAS
    out = {lbl: {"age_hours": None, "rows": 0, "fresh": False} for lbl in labels}
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SET statement_timeout = 8000")
            # One grouped pass: newest ts + total + recent-row count per iso.
            # %s placeholders for the label set keep it injection-safe.
            placeholders = ",".join(["%s"] * len(labels))
            cur.execute(
                "SELECT iso, MAX(timestamp) AS newest, COUNT(*) AS total, "
                "       SUM(CASE WHEN timestamp >= NOW() - (%s || ' hours')::interval "
                "                THEN 1 ELSE 0 END) AS recent "
                "  FROM grid_data "
                " WHERE iso IN (" + placeholders + ") "
                " GROUP BY iso",
                [str(_ISO_FRESH_HOURS)] + labels,
            )
            now = datetime.now(timezone.utc)
            for iso, newest, total, recent in cur.fetchall() or []:
                if iso not in out:
                    continue
                age = None
                if newest is not None:
                    nt = newest
                    if getattr(nt, "tzinfo", None) is None:
                        nt = nt.replace(tzinfo=timezone.utc)
                    age = round((now - nt).total_seconds() / 3600.0, 1)
                out[iso] = {
                    "age_hours": age,
                    "rows": int(total or 0),
                    "fresh": bool(int(recent or 0) > 0),
                }
    except Exception as e:
        return out, "iso_db: " + str(e)[:140]
    return out, None


# ── Gap assembly ──────────────────────────────────────────────────────────

def _pct(covered, total):
    if not total:
        return 0
    return int(round(100.0 * covered / total))


def compute_coverage_gaps():
    """RANKED coverage gaps across the 4 dimensions + per-dimension coverage
    %%. Fully guarded — any dimension that errors degrades to a sane summary
    (its gaps empty / coverage unknown) and is noted under `errors`; this
    function NEVER raises.

    Returns:
      {
        "coverage": {"dcgi": {"covered","total","pct"}, "dcpi": {...},
                     "iso": {...}, "gas": {...}},
        "gaps": [ {dimension, key, label, why, priority, suggested_action}, ... ],
        "gap_count": int,
        "errors": {dimension: "msg", ...},
        "as_of": iso8601,
      }
    Each gap dict:
      dimension: dcgi|dcpi|iso|gas
      key:       stable identifier (state code / ISO label / "markets")
      label:     human label
      why:       why it's a gap (the real evidence)
      priority:  high|med|low
      suggested_action: human-readable expansion to propose
    """
    errors = {}
    gaps = []

    # ---- DCGI (per-state gas index scoring) ----
    dcgi_covered, dcgi_total, scored_map, dcgi_err = _dcgi_coverage()
    if dcgi_err:
        errors["dcgi"] = dcgi_err
    dcgi_missing = [st for st in _US_STATES if st not in dcgi_covered]
    # If the rollup failed entirely (no scored_map) we don't fabricate 50
    # gaps — that would spam the brain with a transient DB blip. Only emit
    # per-state gaps when we actually have a working rollup.
    if scored_map is not None:
        for st in dcgi_missing:
            name = _US_STATE_NAMES.get(st, st)
            gaps.append({
                "dimension": "dcgi",
                "key": st,
                "label": "DCGI unscored: " + name + " (" + st + ")",
                "why": ("no Data Center Gas Index score for " + name
                        + " — state absent from the live gas-state rollup"),
                "priority": "high" if st in ("TX", "PA", "OH", "LA", "VA",
                                              "OK", "WV", "NM", "WY", "CO")
                            else "med",
                "suggested_action": ("add DCGI scoring for " + name
                                     + " (ingest gas_pipelines coverage for "
                                     + st + " so it gets an access + cost score)"),
            })

    # ---- Gas (raw pipeline/storage presence by state) ----
    gas_covered, gas_total, gas_err = _gas_pipeline_coverage(scored_map)
    if gas_err and gas_err != "no_rollup":
        errors["gas"] = gas_err
    if scored_map is not None:
        gas_missing = [st for st in _US_STATES if st not in gas_covered]
        for st in gas_missing:
            name = _US_STATE_NAMES.get(st, st)
            gaps.append({
                "dimension": "gas",
                "key": st,
                "label": "No gas pipeline data: " + name + " (" + st + ")",
                "why": ("zero natural-gas pipeline segments tracked in "
                        + name + " — no gas-infrastructure coverage"),
                # A state with no pipeline rows also blocks its DCGI score,
                # so this is the upstream, high-priority fill.
                "priority": "high",
                "suggested_action": ("register gas pipeline + storage data for "
                                     + name + " (extend the gas_pipelines "
                                     "ingest to cover " + st + ")"),
            })
    elif gas_err == "no_rollup" and "gas" not in errors:
        errors["gas"] = "gas coverage unknown (gas-state rollup unavailable)"

    # ---- ISO / utility-BA live-feed coverage ----
    iso_feeds, iso_err = _iso_feed_freshness()
    if iso_err:
        errors["iso"] = iso_err
    iso_fresh = 0
    iso_total = len(_REGISTERED_ISOS) + len(_REGISTERED_UTILITY_BAS)
    # Only treat freshness as authoritative when the scan succeeded — a DB
    # error must not mark every feed as a gap.
    if not iso_err:
        for lbl in (_REGISTERED_ISOS + _REGISTERED_UTILITY_BAS):
            info = iso_feeds.get(lbl, {})
            if info.get("fresh"):
                iso_fresh += 1
                continue
            is_iso = lbl in _REGISTERED_ISOS
            age = info.get("age_hours")
            rows = int(info.get("rows") or 0)
            if rows == 0:
                why = ("registered grid feed '" + lbl + "' has produced ZERO "
                       "rows in grid_data — feed is dead/unwired")
                prio = "high"
            else:
                why = ("registered grid feed '" + lbl + "' is STALE — newest "
                       "row " + (str(age) + "h" if age is not None else "unknown")
                       + " old, exceeds " + str(_ISO_FRESH_HOURS) + "h freshness "
                       "window (" + str(rows) + " total rows)")
                # A big ISO going stale is worse than a small co-op BA.
                prio = "high" if is_iso else "med"
            gaps.append({
                "dimension": "iso",
                "key": lbl,
                "label": ("Grid feed " + ("dead" if rows == 0 else "stale")
                          + ": " + lbl),
                "why": why,
                "priority": prio,
                "suggested_action": (
                    ("register live feed for " + lbl
                     + " (no rows in grid_data — wire its extractor into "
                       "iso_orchestrator)") if rows == 0 else
                    ("restore the " + lbl + " feed — its grid_data rows are "
                     "stale (>" + str(_ISO_FRESH_HOURS) + "h); check the "
                     "extractor / cron")),
            })
    else:
        iso_fresh = 0  # unknown; reported via errors, coverage shown as 0/total

    # ---- DCPI market coverage (count gap, not per-market) ----
    dcpi_covered, dcpi_total, dcpi_err = _dcpi_coverage()
    if dcpi_err:
        errors["dcpi"] = dcpi_err
    if not dcpi_err:
        dcpi_short = max(0, dcpi_total - dcpi_covered)
        if dcpi_short > 0:
            # One aggregate gap (we don't have a canonical 286-name registry
            # here to enumerate WHICH markets are missing).
            pct = _pct(dcpi_covered, dcpi_total)
            gaps.append({
                "dimension": "dcpi",
                "key": "markets",
                "label": ("DCPI market shortfall: " + str(dcpi_covered) + "/"
                          + str(dcpi_total) + " scored (" + str(pct) + "%)"),
                "why": (str(dcpi_short) + " of the canonical " + str(dcpi_total)
                        + " markets have no DCPI power score "
                        + "(market_power_scores)"),
                "priority": "high" if pct < 70 else ("med" if pct < 90 else "low"),
                "suggested_action": (
                    "expand DCPI scoring toward the canonical " + str(dcpi_total)
                    + " markets — " + str(dcpi_short) + " still unscored (grow "
                    "_load_markets_dynamic / market_power_scores coverage)"),
            })

    # ---- Rank: high > med > low, then by dimension for stable output ----
    _prio_rank = {"high": 0, "med": 1, "low": 2}
    _dim_rank = {"iso": 0, "gas": 1, "dcgi": 2, "dcpi": 3}
    gaps.sort(key=lambda g: (_prio_rank.get(g.get("priority"), 9),
                             _dim_rank.get(g.get("dimension"), 9),
                             str(g.get("key"))))

    coverage = {
        "dcgi": {"covered": len(dcgi_covered), "total": dcgi_total,
                 "pct": _pct(len(dcgi_covered), dcgi_total)},
        "dcpi": {"covered": dcpi_covered, "total": dcpi_total,
                 "pct": _pct(dcpi_covered, dcpi_total)},
        "iso": {"covered": iso_fresh, "total": iso_total,
                "pct": _pct(iso_fresh, iso_total)},
        "gas": {"covered": len(gas_covered), "total": gas_total,
                "pct": _pct(len(gas_covered), gas_total)},
    }

    return {
        "coverage": coverage,
        "gaps": gaps,
        "gap_count": len(gaps),
        "errors": errors,
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


# ── /heal/findings actionable_backend_issues shaping ───────────────────────

# url namespace so the brain's source-mapper + cron/config filters route these
# correctly (they are EXPANSION proposals, not HTML body substitutions). The
# scheme deliberately mirrors the dchub://… abstract-finding urls Layer 5
# already maps (e.g. dchub://cron/dcpi_recompute → source file).
_COVERAGE_URL = {
    "dcgi": "dchub://coverage/dcgi",
    "dcpi": "dchub://coverage/dcpi",
    "iso":  "dchub://coverage/iso",
    "gas":  "dchub://coverage/gas",
}
# Per-dimension issue-label prefix. Starts with "coverage_" so it's excluded
# from FIX_MAP HTML substitutions (no FIX_MAP key starts with "coverage_") and
# routes to the expansion/human path — same convention as funnel_/data_/api_.
_COVERAGE_ISSUE_PREFIX = "coverage_gap_"

# Cap how many per-dimension findings we emit so a transient wide gap (e.g. a
# DB blip making all ISO feeds look stale) can't flood actionable_backend_issues.
_MAX_FINDINGS_PER_DIM = int(os.environ.get("DCHUB_COVERAGE_MAX_FINDINGS_PER_DIM", "8") or 8)


def coverage_findings():
    """Coverage gaps shaped EXACTLY like /heal/findings
    actionable_backend_issues entries: {url, issue, count, detail}.

    Appending the return of this into that aggregation lets the brain's
    learn-backend-issues loop (brain_v2_layer5) see coverage holes and emit
    expansion proposals for them — the detection→proposal half of the loop.

    Per-dimension capped (_MAX_FINDINGS_PER_DIM) and fully guarded: any error
    returns [] so it can never break /heal/findings. The `count` is the
    number of gaps rolled into that finding (high-signal dimensions get one
    finding PER gap up to the cap; the long tail collapses into a single
    rollup finding so the list stays bounded)."""
    try:
        result = compute_coverage_gaps()
    except Exception:
        return []
    gaps = result.get("gaps") or []
    if not gaps:
        return []

    by_dim = {}
    for g in gaps:
        by_dim.setdefault(g.get("dimension"), []).append(g)

    out = []
    for dim, dim_gaps in by_dim.items():
        url = _COVERAGE_URL.get(dim, "dchub://coverage/" + str(dim))
        # Emit one finding per gap up to the cap (so each is individually
        # proposable), then a single rollup for the remainder.
        head = dim_gaps[:_MAX_FINDINGS_PER_DIM]
        tail = dim_gaps[_MAX_FINDINGS_PER_DIM:]
        for g in head:
            out.append({
                "url": url,
                "issue": _COVERAGE_ISSUE_PREFIX + dim + ":" + str(g.get("key")),
                "count": 1,
                "detail": (g.get("suggested_action") or g.get("why") or
                           g.get("label") or "")[:300],
            })
        if tail:
            keys = ", ".join(str(t.get("key")) for t in tail[:25])
            out.append({
                "url": url,
                "issue": _COVERAGE_ISSUE_PREFIX + dim + ":_rollup",
                "count": len(tail),
                "detail": ("plus " + str(len(tail)) + " more " + dim
                           + " coverage gaps: " + keys)[:300],
            })
    return out


# ── DC Hub Media: recently-closed gaps as broadcast WINS ───────────────────

def coverage_wins(days=_DEFAULT_WIN_DAYS):
    """Recently-closed coverage gaps as agent-broadcast items
    (kind "coverage_win", weight 78).

    A "win" = a registered grid feed (ISO / utility BA) that produced its
    FIRST fresh rows within the last `days` (newest row is fresh AND the feed
    only just crossed the freshness line). This is the cheapest, most
    defensible signal of a closed gap we can compute from existing data:
    grid_data carries per-row timestamps, so "a feed that was dead/stale and
    is now fresh" is directly observable. DCGI/DCPI/gas closures need a
    coverage-history table we don't keep yet (v1 scope), so they're omitted
    rather than guessed — better silent than a fabricated win.

    Returns a list of broadcast-item dicts (same shape as agent_broadcast's
    _fetch_* helpers: kind/ts/title/summary/url/weight/tags). Fully guarded —
    returns [] on any error so it can never break the broadcast build."""
    try:
        days = max(1, min(int(days), 30))
    except Exception:
        days = _DEFAULT_WIN_DAYS
    wins = []
    labels = _REGISTERED_ISOS + _REGISTERED_UTILITY_BAS
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SET statement_timeout = 8000")
            placeholders = ",".join(["%s"] * len(labels))
            # A feed is a fresh WIN if its EARLIEST row is within the window
            # (brand-new coverage) OR it was dormant and only resumed inside
            # the window: we approximate "newly closed" as MIN(timestamp)
            # inside the window AND a fresh recent row. This avoids flagging
            # long-established feeds every day.
            cur.execute(
                "SELECT iso, MIN(timestamp) AS first_ts, MAX(timestamp) AS last_ts, "
                "       COUNT(*) AS total "
                "  FROM grid_data "
                " WHERE iso IN (" + placeholders + ") "
                " GROUP BY iso",
                labels,
            )
            now = datetime.now(timezone.utc)
            for iso, first_ts, last_ts, total in cur.fetchall() or []:
                if first_ts is None or last_ts is None:
                    continue
                ft = first_ts.replace(tzinfo=timezone.utc) if getattr(
                    first_ts, "tzinfo", None) is None else first_ts
                lt = last_ts.replace(tzinfo=timezone.utc) if getattr(
                    last_ts, "tzinfo", None) is None else last_ts
                first_age_h = (now - ft).total_seconds() / 3600.0
                last_age_h = (now - lt).total_seconds() / 3600.0
                # New coverage: first row within `days` AND currently fresh.
                if first_age_h <= days * 24 and last_age_h <= _ISO_FRESH_HOURS:
                    is_iso = iso in _REGISTERED_ISOS
                    kind_label = "grid operator" if is_iso else "utility balancing authority"
                    wins.append({
                        "kind": "coverage_win",
                        "ts": lt.isoformat(),
                        "title": ("DC Hub coverage expanded: live "
                                  + kind_label + " " + iso
                                  + " now tracked on the grid"),
                        "summary": ("DC Hub now ingests live grid data for "
                                    + iso + " — a newly-covered " + kind_label
                                    + " added in the last " + str(days)
                                    + " days, deepening the data-center power "
                                    "map. See /api/v1/brain/coverage-gaps."),
                        "url": "https://dchub.cloud/grid-intelligence",
                        "weight": 78,
                        "tags": ["coverage_win", "grid", "iso" if is_iso
                                 else "utility-ba", iso.lower()],
                    })
    except Exception:
        return []
    # Highest (newest) first; bound the list.
    wins.sort(key=lambda w: w.get("ts") or "", reverse=True)
    return wins[:25]


# ── Public-safe endpoint ───────────────────────────────────────────────────

def _cors(resp, max_age=300):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Cache-Control"] = ("public, max-age=%d, s-maxage=%d, "
                                     "stale-while-revalidate=600" % (max_age, max_age))
    return resp


@brain_coverage_radar_bp.route("/api/v1/brain/coverage-gaps",
                               methods=["GET", "OPTIONS"])
def coverage_gaps_endpoint():
    """Public-safe coverage radar: gap COUNTS + coverage %% per dimension and
    the (non-proprietary) list of WHAT'S MISSING — never any DCGI/DCPI score
    values. Self-aware transparency: "here's where DC Hub's map has holes."

    CORS-enabled, cached 300s, and NEVER 500 — any internal failure degrades
    to an ok:true summary with whatever dimensions succeeded."""
    if request.method == "OPTIONS":
        return _cors(jsonify({}), max_age=300), 204

    try:
        result = compute_coverage_gaps()
    except Exception as e:
        # Absolute last-resort guard — compute_coverage_gaps is already
        # internally guarded, but never let this endpoint 500.
        return _cors(jsonify({
            "ok": True,
            "coverage": {}, "gaps": [], "gap_count": 0,
            "degraded": True, "note": "coverage radar warming up",
            "_err": str(e)[:120],
            "as_of": datetime.now(timezone.utc).isoformat(),
        })), 200

    coverage = result.get("coverage") or {}
    gaps = result.get("gaps") or []

    # PUBLIC-SAFE projection: expose only dimension/key/label/why/priority/
    # suggested_action — i.e. WHAT is missing, never the proprietary score
    # numbers (this endpoint reads no scores, so there's nothing to mask, but
    # we project explicitly so a future gap dict can't leak a value field).
    public_gaps = [{
        "dimension": g.get("dimension"),
        "key": g.get("key"),
        "label": g.get("label"),
        "why": g.get("why"),
        "priority": g.get("priority"),
        "suggested_action": g.get("suggested_action"),
    } for g in gaps]

    by_priority = {"high": 0, "med": 0, "low": 0}
    by_dimension = {"dcgi": 0, "dcpi": 0, "iso": 0, "gas": 0}
    for g in gaps:
        p = g.get("priority")
        if p in by_priority:
            by_priority[p] += 1
        d = g.get("dimension")
        if d in by_dimension:
            by_dimension[d] += 1

    payload = {
        "ok": True,
        "index": "DC Hub Coverage Radar",
        "note": ("Self-aware coverage map: where DC Hub's DCPI / DCGI / ISO / "
                 "gas data has holes. Gap counts + coverage %% are public; "
                 "the proprietary scores themselves are not exposed here."),
        "coverage": coverage,
        "gap_count": result.get("gap_count", len(gaps)),
        "gaps_by_priority": by_priority,
        "gaps_by_dimension": by_dimension,
        "gaps": public_gaps,
        "degraded": bool(result.get("errors")),
        "endpoints": {
            "freshness_radar": "/api/v1/freshness/radar",
            "dcgi_scores": "/api/v1/dcgi/scores",
            "dcpi_scores": "/api/v1/dcpi/scores",
            "iso_health": "/api/v1/iso/all/health",
        },
        "as_of": result.get("as_of"),
        "license": "CC BY 4.0",
    }
    return _cors(jsonify(payload), max_age=300), 200
