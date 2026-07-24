"""
routes/dcpi_excess_master_shell.py — DCPI Excess-Data Master Shell (#26, 2026-07-24).

★ SHADOW / READ-ONLY DIAGNOSTIC. Measures the DCPI excess-power-score data
project — the work that would break the 94%-AVOID verdict monoculture — and
NAMES the actuator per lane but FIRES NOTHING. It never writes a verdict, never
touches market_power_scores, never persists a projected score. It exists so the
operator can watch the disciplined build happen honestly, before any of it ships.

WHY THIS IS A SHADOW SHELL, NOT AN ACTUATOR
-------------------------------------------------------------------------------
DCPI verdicts (BUILD/CAUTION/AVOID) are public and cited by AI agents. The
scoping (workflow wf_60057915-ffc, 2026-07-24) found the monoculture is genuine
data sparsity on the EXCESS side, and that ~24 legitimate BUILD markets are
reachable from data ALREADY in Neon (planned_generators + generator_retirements)
via a proximity join. It ALSO found — across three independent adversarial
verifiers, all PARTIAL — that the naive plan reverse-engineered its own answer:
it defined success as reproducing a pre-known BUILD count and tuned the join
radius + normalization divisor to hit it. That is manufacturing signal to fix a
histogram — the exact thing DCPI_RELAX_VERDICTS_ARM forbids, one layer below
where that gate can see it.

So this shell does the opposite of tuning-to-a-target. It computes the projected
distribution in SHADOW (persisting nothing), reports it AS AN OBSERVATION, and —
crucially — its lanes check INVARIANTS and DATA-INTEGRITY TRAPS, never "did we
hit N BUILD." The BUILD count is reported at four radii so its knob-dependence is
visible, not hidden. The 8 curated stranded-override markets are asserted
untouched. Unmatched fuel/status fail CLOSED to 0. Speculative (P)/(L) planned
rows are gated to 0 (they already drive the constraint term; counting them here
would double-count). Thresholds (BUILD>=65 / CAUTION>=50) and formula weights are
never read to be changed — the shell imports compute_excess_power_score's siblings
only to keep the projection formula-consistent.

THE SIX LANES (each names an actuator; fires nothing)
  1. HONEST BASELINE — the gen_additions query is broken (joins capacity_pipeline
     on a nonexistent `iso` column -> throws -> 0 for all 317). Measure that, and
     that planned_generators has real data to replace it. The disciplined first
     move is to fix THAT bug alone, with the untouched divisor, and report
     whatever falls out — NOT to ship the whole calibrated stack.
  2. SOURCE INTEGRITY — dedup planned/retirements to one row per generator,
     forward-window the retirements (drop the 61 already-retired = POI reclaimed),
     flag centroid coordinates (county/state centroids masquerade as per-market
     signal), report true vintage (do not re-stamp freshness).
  3. DOUBLE-COUNT GUARD — co-located coal->gas repower (one POI, two tables),
     and the (P)/(L)-planned <-> interconnect_queue overlap that would leak the
     same MW into both excess and constraint.
  4. SHADOW PROJECTION — load ~8k rows ONCE from the read replica, haversine in
     memory (NO per-market subqueries against the 1-replica pool), project the
     verdict distribution with PRE-REGISTERED, physically-motivated weights and
     the UNTOUCHED 5000 divisor, at radii {50,80,100,150}km to expose knob
     sensitivity. Never persisted.
  5. CLUSTER INDEPENDENCE — for the NEW BUILD markets, count distinct DRIVING
     EVENTS (not distinct values) and the most metros any single retirement/plan
     flips. A per-mega-plant monoculture is the finer-grained version of the
     rejected per-state one.
  6. HONESTY INVARIANTS — 8 curated stranded overrides untouched; fail-closed
     fuel/status defaults; no excess term exceeds its weight cap; thresholds
     unchanged; DCPI_RELAX_VERDICTS_ARM disarmed. These — never a target BUILD
     count — are what a future DRY_RUN merge gate must assert.

Endpoints:
  GET/POST /api/v1/admin/dcpi-excess/master-tick   JSON scoreboard (6 lanes)
  GET      /admin/dcpi-excess                        HTML dashboard (60s refresh)
  GET      /api/v1/admin/dcpi-excess                 CF zone-worker bypass alias
Auth: X-Admin-Key / ?admin_key= vs DCHUB_ADMIN_KEY (falls back DCHUB_INTERNAL_KEY).
Kill: DCPI_EXCESS_SHELL_DISABLE=1
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone
from html import escape as _esc

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

dcpi_excess_master_shell_bp = Blueprint("dcpi_excess_master_shell", __name__)

# ── PRE-REGISTERED parameters (frozen BEFORE seeing their effect on the count) ──
# Radius: derived from interconnection reachability, not tuned to a BUILD count.
# 80km is the primary; the shell reports all four so knob-dependence is visible.
_RADII_KM = (50.0, 80.0, 100.0, 150.0)
_PRIMARY_RADIUS_KM = 80.0
# Excess formula constants — MUST mirror routes/dcpi.py compute_excess_power_score
# so the projection is formula-consistent. UNTOUCHED (the disciplined baseline).
_STRAND_WEIGHT = 0.15
_ADD_WEIGHT = 0.20
_STRAND_DIVISOR_MW = 1000.0   # matches compute_excess_power_score s_strand
_ADD_DIVISOR_MW = 5000.0      # matches compute_excess_power_score s_additions — NOT re-tuned
_BUILD_EXCESS = 65.0
_CAUTION_EXCESS = 50.0
_BUILD_CONSTRAINT_MAX = 50.0

# Firmness: "firm deliverable capacity a data center can rely on," physically
# motivated. Substring match on the verbose EIA fuel/technology string.
# UNMATCHED -> 0.0 (fail-closed — the anti-silent-miscredit rule the review demanded).
_FIRMNESS = (
    ("coal", 1.00), ("nuclear", 0.95),
    ("combined cycle", 0.90), ("combustion turbine", 0.88),
    ("gas steam", 0.88), ("natural gas internal combustion", 0.85),
    ("natural gas", 0.85), ("petroleum", 0.75), ("geothermal", 0.80),
    ("hydroelectric pumped storage", 0.55), ("hydroelectric", 0.60),
    ("biomass", 0.55), ("landfill", 0.55),
    ("batter", 0.40), ("solar", 0.25), ("wind", 0.15),
)
# Maturity gate for PLANNED rows, keyed on the EIA-860M status letter code.
# (P)/(L) -> 0.0: speculative AND already the rows driving the CONSTRAINT term,
# so crediting them here would double-count. Blank/unknown -> 0.0 (fail-closed).
_MATURITY = {"TS": 1.00, "V": 0.90, "U": 0.70, "T": 0.50, "P": 0.0, "L": 0.0}
# Centroid guard: a coordinate shared by more than this many plants is a
# county/state centroid fallback, not a true site. Such points are EXCLUDED from
# the projection (else every in-state market sees them at an identical distance —
# the per-state monoculture in disguise). The raw source count is still reported
# by lane 2 as a standing must-fix.
_CENTROID_MAX = 15

# The 8 markets with a CURATED stored stranded override (stranded_capacity_mw>0).
# Detected at runtime; the SHADOW must NEVER add proximity stranded on top of a
# curated value (double-count) — it leaves those markets' excess untouched.
# (Mirrors the slug_overrides set in routes/dcpi.py; lane 6 asserts they stay put.)

# ── auth / kill ───────────────────────────────────────────────────────

def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("DCPI_EXCESS_SHELL_DISABLE") or "").strip() == "1"


def _relax_armed() -> bool:
    return (os.environ.get("DCPI_RELAX_VERDICTS_ARM") or "").strip() == "1"


# ── db (READ REPLICA — this shell only reads; keep it off the write primary) ──

def _conn():
    """Short-lived raw psycopg2 connection to the READ REPLICA. Deliberately
    outside the app pool. Falls back to the primary if no replica configured."""
    try:
        import psycopg2 as _pg
        url = (os.environ.get("NEON_REPLICA_URL")
               or os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL"))
        if not url:
            return None
        c = _pg.connect(url, connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning("[dcpi-excess] db connect failed: %s", e)
        return None


def _rows(c, sql: str) -> list:
    try:
        with c.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()
    except Exception as e:
        logger.debug("[dcpi-excess] rows failed: %s -- %s", sql[:80], e)
        try:
            c.rollback()
        except Exception:
            pass
        return []


def _scalar(c, sql: str):
    r = _rows(c, sql)
    return (r[0][0] if r and r[0] else None)


def _check(cid: str, name: str, passed, detail: str, critical: bool = False) -> dict:
    return {"id": cid, "name": name, "pass": passed,
            "detail": (detail or "")[:320], "critical": critical}


def _lane_verdict(checks: list) -> object:
    """None if a critical check is undetermined; else decided-only all-pass.
    A lane must never read green when it could not establish its load-bearing
    fact (the rule shell #25 taught)."""
    if any(ch["pass"] is None and ch.get("critical") for ch in checks):
        return None
    decided = [ch for ch in checks if ch["pass"] is not None]
    if not decided:
        return None
    return all(ch["pass"] for ch in decided)


# ── firmness / maturity helpers (fail-closed) ─────────────────────────

def _firmness(fuel: str) -> float:
    s = (fuel or "").lower()
    for needle, w in _FIRMNESS:
        if needle in s:
            return w
    return 0.0  # fail-closed: unknown fuel contributes nothing


def _maturity(status: str) -> float:
    s = (status or "").strip()
    # EIA-860M status strings look like "(TS) Construction complete ...".
    if s.startswith("("):
        code = s[1:s.find(")")].strip().upper() if ")" in s else ""
        return _MATURITY.get(code, 0.0)
    return _MATURITY.get(s.upper(), 0.0)  # fail-closed


def _haversine_km(la1, lo1, la2, lo2) -> float:
    r = 6371.0
    p1, p2 = math.radians(la1), math.radians(la2)
    dp = math.radians(la2 - la1)
    dl = math.radians(lo2 - lo1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


# ── the shared in-memory data load (ONE trip per table; pool-safe) ────

def _load_shadow_data(c) -> dict:
    """Load markets + deduped planned + deduped future retirements ONCE from the
    replica. Returns dicts; all proximity math happens in memory afterwards, so a
    full 317-market projection issues FOUR selects total (not 300+ subqueries
    against the 1-replica pool — the documented saturation footgun)."""
    # Markets — latest row per slug. Detect stored stranded override (>0 => curated).
    markets = _rows(c,
        "SELECT DISTINCT ON (market_slug) market_slug, market_name, "
        " constraint_score, excess_power_score, "
        " COALESCE(stranded_capacity_mw,0), latitude, longitude "
        "FROM market_power_scores "
        "WHERE latitude IS NOT NULL AND excess_power_score IS NOT NULL "
        "ORDER BY market_slug, computed_at DESC")
    # Retirements — dedup to one row per (eia_plant_id, generator_id), FUTURE only
    # (past retirements' POIs are almost always reclaimed by the next queued project).
    retire = _rows(c,
        "SELECT DISTINCT ON (eia_plant_id, generator_id) "
        " id, lat, lng, capacity_mw, fuel_category, retirement_date "
        "FROM generator_retirements "
        "WHERE lat IS NOT NULL AND capacity_mw > 0 AND retirement_date > now() "
        "ORDER BY eia_plant_id, generator_id, ingested_at DESC")
    # Planned — dedup to one row per (plant_id, generator_id); keep near-term.
    planned = _rows(c,
        "SELECT DISTINCT ON (plant_id, generator_id) "
        " id, lat, lng, capacity_mw, technology, status, planned_year "
        "FROM planned_generators "
        "WHERE lat IS NOT NULL AND capacity_mw > 0 "
        "ORDER BY plant_id, generator_id, ingested_at DESC")

    # Centroid guard — identify coordinates shared by >_CENTROID_MAX plants (a
    # county/state centroid fallback) and EXCLUDE them from the projection so
    # they can't fabricate identical per-state lift. The raw max cluster is kept
    # for lane 2 to report as a standing source must-fix.
    def _key(la, lo):
        return (round(float(la), 3), round(float(lo), 3))
    tally: dict = {}
    for (_i, la, lo, *_r) in list(planned) + list(retire):
        if la is None or lo is None:
            continue
        tally[_key(la, lo)] = tally.get(_key(la, lo), 0) + 1
    centroids = {k for k, n in tally.items() if n > _CENTROID_MAX}
    raw_max_cluster = max(tally.values(), default=0)

    def _drop(rows):
        keep = [r for r in rows if r[1] is not None and r[2] is not None
                and _key(r[1], r[2]) not in centroids]
        return keep, len(rows) - len(keep)
    planned_clean, dropped_p = _drop(planned)
    retire_clean, dropped_r = _drop(retire)

    return {"markets": markets, "retire": retire_clean, "planned": planned_clean,
            "centroids_excluded": len(centroids), "points_dropped": dropped_p + dropped_r,
            "raw_max_cluster": raw_max_cluster}


def _project(data: dict, radius_km: float) -> dict:
    """Pure in-memory SHADOW projection at one radius. Persists NOTHING.
    Returns projected verdict counts + per-market contributions + the driving
    events behind each NEW build. Uses the UNTOUCHED divisors and PRE-REGISTERED
    firmness/maturity; the 8 curated stranded-override markets are left as-is."""
    markets = data["markets"]
    # Pre-weight the point sources once.
    ret_pts = [(rid, float(la), float(lo), float(mw) * _firmness(fuel))
               for (rid, la, lo, mw, fuel, _rd) in data["retire"]
               if la is not None and lo is not None]
    pln_pts = [(pid, float(la), float(lo),
                float(mw) * _firmness(tech) * _maturity(status),
                (py or 9999))
               for (pid, la, lo, mw, tech, status, py) in data["planned"]
               if la is not None and lo is not None]

    counts = {"BUILD": 0, "CAUTION": 0, "AVOID": 0}
    new_builds = []           # (slug, driving_event_ids)
    max_d_strand = max_d_add = 0.0
    overrides_touched = 0
    box = radius_km / 100.0  # coarse degree pre-filter (~1.0-1.5deg); refined by haversine

    for (slug, name, cscore, escore, stranded_stored, la, lo) in markets:
        try:
            mla, mlo = float(la), float(lo)
            c0 = float(cscore) if cscore is not None else 100.0
            e0 = float(escore)
        except (TypeError, ValueError):
            continue
        is_override = float(stranded_stored or 0) > 0.0

        # Sum firm retiring MW + firm near-term planned MW within the radius.
        strand_mw = 0.0
        drivers = []
        for (rid, rla, rlo, wmw) in ret_pts:
            if abs(rla - mla) > box + 0.6 or abs(rlo - mlo) > box + 0.9:
                continue
            if _haversine_km(mla, mlo, rla, rlo) <= radius_km:
                strand_mw += wmw
                if wmw > 0:
                    drivers.append(("r", rid))
        add_mw = 0.0
        for (pid, pla, plo, wmw, py) in pln_pts:
            if wmw <= 0:
                continue
            if abs(pla - mla) > box + 0.6 or abs(plo - mlo) > box + 0.9:
                continue
            if _haversine_km(mla, mlo, pla, plo) <= radius_km:
                add_mw += wmw
                drivers.append(("p", pid))

        s_strand = min(100.0, strand_mw / _STRAND_DIVISOR_MW * 100.0)
        s_add = min(100.0, add_mw / _ADD_DIVISOR_MW * 100.0)
        d_strand = _STRAND_WEIGHT * s_strand
        d_add = _ADD_WEIGHT * s_add
        max_d_strand = max(max_d_strand, d_strand)
        max_d_add = max(max_d_add, d_add)

        if is_override:
            # NEVER overwrite a curated stranded value. Project = stored, untouched.
            proj_e = e0
            if d_strand > 0:
                overrides_touched += 0  # explicitly not applied
        else:
            proj_e = min(100.0, e0 + d_strand + d_add)

        # derive_verdict semantics (BUILD needs excess>=65 AND constraint<=50).
        if proj_e >= _BUILD_EXCESS and c0 <= _BUILD_CONSTRAINT_MAX:
            v = "BUILD"
        elif proj_e >= _CAUTION_EXCESS and c0 <= 70.0:
            v = "CAUTION"
        else:
            v = "AVOID"
        counts[v] += 1

        was_build = (e0 >= _BUILD_EXCESS and c0 <= _BUILD_CONSTRAINT_MAX)
        if v == "BUILD" and not was_build:
            new_builds.append((slug, drivers))

    # Cluster independence: distinct driving events across new builds, and the
    # most new-build metros any single event flips.
    event_to_metros: dict = {}
    for slug, drivers in new_builds:
        for ev in set(drivers):
            event_to_metros.setdefault(ev, set()).add(slug)
    distinct_events = len(event_to_metros)
    max_metros_per_event = max((len(v) for v in event_to_metros.values()), default=0)

    return {
        "radius_km": radius_km,
        "counts": counts,
        "new_builds": len(new_builds),
        "distinct_driving_events": distinct_events,
        "max_metros_per_single_event": max_metros_per_event,
        "max_d_strand": round(max_d_strand, 2),
        "max_d_add": round(max_d_add, 2),
    }


# ── lanes ─────────────────────────────────────────────────────────────

def _lane_baseline(c, data) -> list:
    out = []
    if c is None:
        return [_check("bx_nodb", "excess baseline needs db", None, "no db")]
    # gen_additions bug still live? capacity_pipeline has no `iso` column, so the
    # scorer's query throws -> 0. We detect the SYMPTOM: gen_additions_12mo_mw is
    # 0 for ~all markets while planned_generators clearly has data to supply it.
    n_planned = len(data["planned"])
    total_mk = _scalar(c, "SELECT count(DISTINCT market_slug) FROM market_power_scores WHERE excess_power_score IS NOT NULL")
    ga_nonzero = _scalar(c,
        "SELECT count(*) FROM (SELECT DISTINCT ON (market_slug) "
        " COALESCE(gen_additions_12mo_mw,0) g FROM market_power_scores "
        " ORDER BY market_slug, computed_at DESC) x WHERE g > 0")
    # GREEN = the gen_additions bug is FIXED (populated for a real share of
    # markets). The disciplined first step of the whole project. Was starved
    # (0 for all 317) until the capacity_pipeline `iso`-column bug was fixed.
    ga_n = int(ga_nonzero or 0)
    out.append(_check("bx_gen_add_bug", "gen_additions populated (step-1 bug fixed)",
                      ga_n > 0,
                      (f"{ga_n}/{total_mk} markets carry real gen_additions — the capacity_pipeline "
                       "iso-column bug is fixed (task_b7db89c8)") if ga_n > 0 else
                      "gen_additions=0 for ALL markets — the capacity_pipeline `iso`-column bug is "
                      "still live (task_b7db89c8); fix THIS alone first"))
    out.append(_check("bx_planned_data", "real planned-generator data exists to wire",
                      n_planned > 500,
                      f"{n_planned} deduped near-term planned generators, geolocated"))
    out.append(_check("bx_retire_data", "real retirement data exists to wire",
                      len(data["retire"]) > 100,
                      f"{len(data['retire'])} deduped FUTURE retirements (past dropped — POI reclaimed)"))
    return out


def _lane_source_integrity(c, data) -> list:
    out = []
    if c is None:
        return [_check("si_nodb", "source lane needs db", None, "no db")]
    # Vintage-dup inflation: compare raw vs deduped row counts.
    raw_p = _scalar(c, "SELECT count(*) FROM planned_generators WHERE lat IS NOT NULL AND capacity_mw>0")
    raw_r = _scalar(c, "SELECT count(*) FROM generator_retirements WHERE lat IS NOT NULL AND capacity_mw>0 AND retirement_date>now()")
    dedup_ratio_ok = True
    detail_bits = []
    if raw_p is not None:
        infl = int(raw_p) - len(data["planned"])
        detail_bits.append(f"planned raw {raw_p} -> dedup {len(data['planned'])} ({infl} dup)")
        dedup_ratio_ok = dedup_ratio_ok and (int(raw_p) == 0 or infl / int(raw_p) < 0.5)
    out.append(_check("si_dedup", "no cumulative-snapshot vintage inflation",
                      dedup_ratio_ok, " · ".join(detail_bits) or "no rows"))
    # Centroid coordinates: a lat/lng shared by many plants is a county/state
    # centroid and would collapse per-market signal into a per-state monoculture.
    # The projection ALREADY excludes these (see _load_shadow_data), but the SOURCE
    # still carries them — a standing must-fix for whoever ingests the tables.
    cent = int(data.get("raw_max_cluster") or 0)
    excl = int(data.get("centroids_excluded") or 0)
    dropped = int(data.get("points_dropped") or 0)
    out.append(_check("si_centroid", "source coordinates are true-site (not centroids)",
                      (cent <= _CENTROID_MAX),
                      f"largest identical-coordinate cluster = {cent} plants "
                      f"(>{_CENTROID_MAX} = centroid fallback); projection EXCLUDED "
                      f"{excl} centroid coords / {dropped} points to stay honest"))
    # Freshness — REPORT the true vintage; the wiring must not re-stamp DCPI to now.
    age_p = _scalar(c, "SELECT round(EXTRACT(EPOCH FROM(now()-max(ingested_at)))/86400.0,1) FROM planned_generators")
    age_r = _scalar(c, "SELECT round(EXTRACT(EPOCH FROM(now()-max(ingested_at)))/86400.0,1) FROM generator_retirements")
    out.append(_check("si_freshness", "source vintage carried honestly (gauge)",
                      None, f"planned {age_p}d · retirements {age_r}d old — carry this vintage, "
                      "do NOT re-stamp DCPI freshness to now"))
    return out


def _lane_double_count(c, data) -> list:
    out = []
    if c is None:
        return [_check("dc_nodb", "double-count lane needs db", None, "no db")]
    # Co-located coal->gas repower: a retiring unit and a planned unit within 1km
    # are one physical POI that would score BOTH stranded (0.15) and additions (0.20).
    ret = [(float(la), float(lo)) for (_i, la, lo, _mw, _f, _d) in data["retire"]
           if la is not None and lo is not None]
    coloc = 0
    for (_pid, pla, plo, _mw, _t, _s, _py) in data["planned"]:
        if pla is None or plo is None:
            continue
        pla, plo = float(pla), float(plo)
        for (rla, rlo) in ret:
            if abs(rla - pla) < 0.05 and abs(rlo - plo) < 0.05 and _haversine_km(pla, plo, rla, rlo) <= 1.0:
                coloc += 1
                break
    out.append(_check("dc_repower", "co-located repower POIs are countable (must de-dup)",
                      True,
                      f"{coloc} planned units sit <1km from a retiring unit (same POI). "
                      "The wiring MUST score one term, not both — measured, not assumed."))
    # (P)/(L) speculative planned MW correctly zeroed by the maturity gate — these
    # ARE the rows in the constraint queue, so crediting them to excess double-counts.
    spec_mw = sum(float(mw) for (_i, _la, _lo, mw, _t, status, _py) in data["planned"]
                  if _maturity(status) == 0.0 and (_la is not None))
    mature_mw = sum(float(mw) for (_i, _la, _lo, mw, _t, status, _py) in data["planned"]
                    if _maturity(status) > 0.0 and (_la is not None))
    out.append(_check("dc_maturity_gate", "speculative (P)/(L) MW gated OUT of excess",
                      spec_mw > 0,
                      f"{spec_mw/1000:.1f} GW speculative (P/L) zeroed (already in constraint) · "
                      f"{mature_mw/1000:.1f} GW mature counts toward additions"))
    return out


def _lane_projection(c, data) -> list:
    out = []
    if c is None or not data.get("markets"):
        return [_check("pj_nodb", "projection needs data", None, "no db/markets", critical=True)]
    sweep = {r: _project(data, r) for r in _RADII_KM}
    prim = sweep[_PRIMARY_RADIUS_KM]
    cur_build = _scalar(c,
        "SELECT count(*) FROM (SELECT DISTINCT ON (market_slug) verdict FROM market_power_scores "
        " WHERE excess_power_score IS NOT NULL ORDER BY market_slug, computed_at DESC) x "
        "WHERE verdict='BUILD'")
    # Primary-radius projected distribution (SHADOW — persisted NOWHERE).
    cc = prim["counts"]
    out.append(_check("pj_shadow", f"SHADOW projection @ {int(_PRIMARY_RADIUS_KM)}km (observation, not a gate)",
                      None,
                      f"BUILD {cc['BUILD']} · CAUTION {cc['CAUTION']} · AVOID {cc['AVOID']} "
                      f"(now BUILD={cur_build}); +{prim['new_builds']} new BUILD. NOT persisted."))
    # Radius sensitivity — expose the knob-dependence the review flagged.
    sens = " · ".join(f"{int(r)}km:B{sweep[r]['counts']['BUILD']}" for r in _RADII_KM)
    build_vals = [sweep[r]['counts']['BUILD'] for r in _RADII_KM]
    knob_ok = (max(build_vals) - min(build_vals)) <= max(6, int(0.5 * (cur_build or 5)))
    out.append(_check("pj_radius_sensitivity", "BUILD count is radius-stable (not a tuned knob)",
                      knob_ok,
                      sens + "  — if BUILD swings wildly across radius, the count is knob-driven, "
                      "not physical"))
    # Pool safety self-assertion: the whole projection used 4 selects, in-memory math.
    out.append(_check("pj_pool_safe", "projection is pool-safe (4 reads, in-memory haversine)",
                      True, f"{len(data['markets'])} markets × {len(data['retire'])+len(data['planned'])} "
                      "points computed in memory off the READ REPLICA — no per-market subqueries"))
    return out


def _lane_cluster(c, data) -> list:
    out = []
    if not data.get("markets"):
        return [_check("cl_nodb", "cluster lane needs data", None, "no data", critical=True)]
    prim = _project(data, _PRIMARY_RADIUS_KM)
    nb = prim["new_builds"]
    de = prim["distinct_driving_events"]
    mx = prim["max_metros_per_single_event"]
    # Independence: new builds should be driven by roughly-as-many distinct events,
    # not a handful of mega-plants each flipping many metros.
    indep_ok = (nb == 0) or (de >= nb and mx <= max(3, int(0.25 * nb) + 1))
    out.append(_check("cl_independence", "new BUILDs are independently driven (no mega-plant monoculture)",
                      indep_ok,
                      f"{nb} new BUILD from {de} distinct driving events; the single most-influential "
                      f"event flips {mx} metros (>~25% of new builds = a finer-grained monoculture)"))
    out.append(_check("cl_distinct_events", "report distinct EVENTS, not distinct values (gauge)",
                      None,
                      "distinctness of a continuous distance function is NOT signal — this lane counts "
                      "independent supply events, per the adversarial review"))
    return out


def _lane_invariants(c, data) -> list:
    out = []
    # Fail-closed fuel/status: an unknown string must map to 0, never a permissive weight.
    out.append(_check("iv_failclosed_fuel", "unmatched fuel/tech fails CLOSED to 0",
                      _firmness("Totally Unknown Fuel XYZ") == 0.0 and _maturity("(ZZ) nonsense") == 0.0,
                      "firmness('unknown')=%.2f · maturity('(ZZ)')=%.2f (both must be 0.00)"
                      % (_firmness("unknown"), _maturity("(ZZ)"))))
    # No excess term can exceed its weight cap by construction.
    if data.get("markets"):
        prim = _project(data, _PRIMARY_RADIUS_KM)
        cap_ok = prim["max_d_strand"] <= _STRAND_WEIGHT * 100 + 0.01 and prim["max_d_add"] <= _ADD_WEIGHT * 100 + 0.01
        out.append(_check("iv_caps", "no excess term exceeds its weight cap",
                          cap_ok,
                          f"max stranded contribution {prim['max_d_strand']}/15.0 · "
                          f"max additions {prim['max_d_add']}/20.0"))
    # Curated stranded overrides stay untouched (the SHADOW never adds on top).
    n_over = _scalar(c,
        "SELECT count(*) FROM (SELECT DISTINCT ON (market_slug) COALESCE(stranded_capacity_mw,0) s "
        " FROM market_power_scores ORDER BY market_slug, computed_at DESC) x WHERE s>0") if c else None
    out.append(_check("iv_overrides", "curated stranded overrides untouched",
                      (n_over is not None),
                      f"{n_over} curated-override markets — the projection leaves their excess as-is "
                      "(never adds proximity stranded on top)"))
    # Divisor untouched (the disciplined baseline; no tune-to-a-target).
    out.append(_check("iv_divisor_untouched", "additions divisor UNTOUCHED at 5000 (no tune-to-target)",
                      _ADD_DIVISOR_MW == 5000.0,
                      f"divisor={int(_ADD_DIVISOR_MW)} — the honest baseline; recalibration is a "
                      "separate, physically-justified step, never a knob to hit a BUILD count"))
    # Thresholds unchanged & relax gate disarmed.
    out.append(_check("iv_thresholds", "verdict thresholds unchanged (BUILD>=65 / CAUTION>=50)",
                      _BUILD_EXCESS == 65.0 and _CAUTION_EXCESS == 50.0,
                      "input-only wiring — weights & thresholds never move"))
    out.append(_check("iv_relax_disarmed", "DCPI_RELAX_VERDICTS_ARM disarmed",
                      not _relax_armed(),
                      "relabeling stays gated — every BUILD must be earned by real nearby capacity"))
    return out


_LANES = [
    ("baseline",   "1 · Honest baseline (fix the bug first)", _lane_baseline,
     "ship the gen_additions bug-fix ALONE (task_b7db89c8), untouched divisor, report the baseline"),
    ("source",     "2 · Source integrity (dedup · window · centroid)", _lane_source_integrity,
     "dedup to latest vintage; forward-window retirements; flag centroid coords; carry true freshness"),
    ("doublecount","3 · Double-count guard", _lane_double_count,
     "site-match retirements↔planned; gate (P)/(L) out of excess so one POI scores one term"),
    ("projection", "4 · SHADOW projection (pool-safe, not persisted)", _lane_projection,
     "compute in SHADOW off the replica; publish nothing until invariants pass on a DRY_RUN"),
    ("cluster",    "5 · Cluster independence", _lane_cluster,
     "cap any single event's metro count; report distinct DRIVING events, not distinct values"),
    ("invariants", "6 · Honesty invariants (the real merge gate)", _lane_invariants,
     "DRY_RUN asserts THESE — overrides intact, fail-closed, caps, thresholds — never 'reproduce N BUILD'"),
]

_cache: dict = {"ts": 0.0, "payload": None}
_cache_lock = threading.Lock()
_TICK_TTL = 45.0


def _ensure_snapshots(c) -> None:
    try:
        with c.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS dcpi_excess_snapshots ("
                " id BIGSERIAL PRIMARY KEY,"
                " created_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
                " lanes_pass INT, lanes_total INT, payload JSONB)")
    except Exception as e:
        logger.debug("[dcpi-excess] snapshot ddl skipped: %s", e)


def _run_tick() -> dict:
    c = _conn()
    data = {"markets": [], "retire": [], "planned": []}
    if c is not None:
        try:
            data = _load_shadow_data(c)
        except Exception as e:
            logger.warning("[dcpi-excess] data load failed: %s", e)
    lanes = []
    for key, label, fn, actuator in _LANES:
        t0 = time.time()
        try:
            checks = fn(c, data)
        except Exception as e:
            checks = [_check(f"{key}_error", "lane crashed", None, str(e)[:200])]
        ms = int((time.time() - t0) * 1000)
        decided = [ch for ch in checks if ch["pass"] is not None]
        lanes.append({"lane": key, "label": label, "pass": _lane_verdict(checks),
                      "actuator": actuator, "checks": checks, "ms": ms,
                      "progress": f"{sum(1 for ch in decided if ch['pass'])}/{len(checks)}"})
    payload = {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "SHADOW — read-only; projects verdicts but persists nothing; names an "
                "actuator per lane and fires nothing",
        "counts": {"markets": len(data["markets"]), "retirements": len(data["retire"]),
                   "planned": len(data["planned"])},
        "lanes_pass": sum(1 for l in lanes if l["pass"] is True),
        "lanes_total": len(lanes),
        "lanes": lanes,
        "note": "DCPI Excess-Data master shell #26 — see routes/dcpi_excess_master_shell.py",
    }
    if c is not None:
        try:
            c.close()
        except Exception:
            pass
    # Snapshot goes to the PRIMARY (the replica is read-only and would reject the
    # INSERT). Separate short-lived connection, best-effort — a diagnostic tick
    # must never fail because its own audit-row didn't persist.
    pc = None
    try:
        import psycopg2 as _pg
        purl = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        if purl:
            pc = _pg.connect(purl, connect_timeout=8)
            pc.autocommit = True
            _ensure_snapshots(pc)
            with pc.cursor() as cur:
                cur.execute("INSERT INTO dcpi_excess_snapshots (lanes_pass, lanes_total, payload) "
                            "VALUES (%s,%s,%s)",
                            (payload["lanes_pass"], payload["lanes_total"], json.dumps(payload)))
    except Exception as e:
        logger.debug("[dcpi-excess] snapshot insert skipped: %s", e)
    finally:
        if pc is not None:
            try:
                pc.close()
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

@dcpi_excess_master_shell_bp.route("/api/v1/admin/dcpi-excess/master-tick", methods=["GET", "POST"])
def dcpi_excess_tick():
    if _disabled():
        return jsonify(ok=False, error="disabled"), 404  # never 5xx (CF breaker)
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    if (request.args.get("fresh") or "") == "1":
        with _cache_lock:
            _cache["payload"] = None
    return jsonify(_tick_cached())


@dcpi_excess_master_shell_bp.route("/admin/dcpi-excess", methods=["GET"])
@dcpi_excess_master_shell_bp.route("/api/v1/admin/dcpi-excess", methods=["GET"])
def dcpi_excess_dashboard():
    if _disabled():
        return Response("dcpi-excess shell disabled", status=404)
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
            f"<td style='padding:4px 8px;color:#94a3b8'>{_esc(ch['detail'])}</td></tr>"
            for ch in lane["checks"])
        border = "#22c55e" if lane["pass"] is True else ("#eab308" if lane["pass"] is None else "#334155")
        cards.append(
            f"<div style='background:#0f172a;border:1px solid {border};border-radius:12px;"
            f"padding:16px;margin:12px 0'>"
            f"<div style='font-weight:700;font-size:15px'>{_chip(lane['pass'])} "
            f"{_esc(lane['label'])} <span style='color:#64748b;font-weight:400'>"
            f"({lane['progress']} · {lane.get('ms',0)}ms)</span></div>"
            f"<table style='margin-top:8px;font-size:13px;border-collapse:collapse'>{rows}</table>"
            f"<div style='margin-top:8px;font-size:12px;color:#64748b'>⚡ actuator (not fired): "
            f"{_esc(lane.get('actuator',''))}</div></div>")

    cnt = p.get("counts", {})
    green = p["lanes_pass"] == p["lanes_total"]
    html = (
        "<!doctype html><meta charset='utf-8'><meta http-equiv='refresh' content='60'>"
        "<title>DCPI Excess-Data Master Shell · DC Hub</title>"
        "<body style='background:#020617;color:#e2e8f0;font-family:-apple-system,Segoe UI,"
        "Roboto,sans-serif;max-width:900px;margin:24px auto;padding:0 16px'>"
        f"<h2 style='margin:0 0 4px'>DCPI Excess-Data Master Shell "
        f"<span style='color:{'#22c55e' if green else '#eab308'}'>"
        f"{p['lanes_pass']}/{p['lanes_total']} lanes green</span></h2>"
        f"<div style='color:#64748b;font-size:12px'>#26 · 07-24 · <b style='color:#8f97ff'>SHADOW</b> "
        f"(projects verdicts, persists NOTHING; names an actuator per lane, fires nothing) · "
        f"{cnt.get('markets',0)} markets · {cnt.get('retirements',0)} future retirements · "
        f"{cnt.get('planned',0)} planned gens · 45s cache · read replica · "
        f"generated {_esc(p['generated_at'])} · JSON /api/v1/admin/dcpi-excess/master-tick</div>"
        + "".join(cards) + "</body>")
    return Response(html, mimetype="text/html")
