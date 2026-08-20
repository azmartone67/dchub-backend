"""
iso_snapshot.py — Phase GG: comprehensive per-ISO snapshot.

The user's ask: "can we make our ISO pull more comprehensive?"

Today the platform tracks 11 ISOs (ERCOT, CAISO, NYISO, MISO, PJM, SPP,
ISO-NE, IESO, AESO, TVA, BPA) via the heartbeat surfaces table — but
the heartbeat only stores `last_updated, status`, not the actual data
points each ISO publishes.

This module is a bundled READ over everything we already have for each
ISO: heartbeat freshness, grid_intelligence (if cached), market_power_
scores filtered to the ISO's footprint, capacity_pipeline rollup, and a
peer-comparison rollup across all 11.

Two endpoints:
    GET /api/v1/iso/<iso_code>/snapshot     — per-ISO full picture
    GET /api/v1/iso/comparison              — head-to-head, all 11

All reads, no writes. The richer per-ISO ingestion (LMPs, fuel mix,
queue capacity) belongs to a separate extractor PR — this exposes
everything we already have through one clean tool-callable endpoint.
"""
import os
import re
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from util.capacity_pipeline import CP_OK

iso_snapshot_bp = Blueprint("iso_snapshot", __name__)

# The 11 ISOs the platform tracks (matches routes/heartbeat.py).
# 'AESO' removed 2026-05-30: its US-realtime extractor (iso_aeso.py) persisted
# 0 rows, so it surfaced here as a misleading markets_scored=0 row AND
# duplicated the working baseline AESO appended from _INTL_ISOS below. AESO now
# appears exactly ONCE in /iso/comparison — the baseline-model entry.
_KNOWN_ISOS = ['ERCOT', 'CAISO', 'NYISO', 'MISO', 'PJM', 'SPP',
               'ISONE', 'IESO', 'TVA', 'BPA']

# Phase ZZZZZ-round54 (2026-05-29) — international ISOs that ship via
# baseline_model_v1 from sibling iso_*_intl modules. Comparison rollup
# now includes these so the user-facing map can render all 14 ISOs in
# one table. They don't have DCPI markets in market_power_scores yet,
# so we surface their LMP + carbon intensity + renewable_pct + demand
# from their snapshot endpoints instead.
_INTL_ISOS = [
    {"code": "HYDROQUEBEC", "module": "routes.iso_hydroquebec",
     "label": "Hydro-Québec", "region": "Canada (QC)"},
    {"code": "AESO",        "module": "routes.iso_aeso_intl",
     "label": "AESO", "region": "Canada (AB)"},
    {"code": "NORDPOOL",    "module": "routes.iso_nordpool_intl",
     "label": "Nord Pool",   "region": "Nordics + Baltics"},
]


def _intl_snapshot_row(iso_def):
    """Build a comparison-row dict from an international ISO's baseline
    snapshot. Best-effort import + best-effort field extraction so a
    broken intl module never poisons the comparison endpoint.
    Returns None on hard failure (skipped from rollup)."""
    try:
        mod = __import__(iso_def["module"], fromlist=["_live_snapshot",
                                                       "_baseline_snapshot",
                                                       "GENERATION_MIX",
                                                       "INSTALLED_CAPACITY_MW",
                                                       "RENEWABLE_PCT"])
    except Exception:
        return None
    # shell #41 WS2 (2026-07-28): prefer the module's LIVE snapshot. Each one
    # caches internally for 5 min, so /comparison shares the orchestrator's
    # fetch instead of adding one per request. Falls back to the model.
    snap = None
    try:
        _live = getattr(mod, "_live_snapshot", None)
        if callable(_live):
            snap = _live() or None
    except Exception:
        snap = None
    if snap is None:
        try:
            snap = mod._baseline_snapshot() or {}
        except Exception:
            snap = {}

    def _mv(key):
        v = snap.get(key)
        if isinstance(v, dict):
            return v.get("value")
        return v

    def _pct(key):
        """Percent 0-100 from a metric that may declare unit 'ratio' (the
        modeled modules emit 0.997) or 'pct' (the live ones emit 95.7).
        Converted off the DECLARED UNIT, never off magnitude — 0.9 is a
        plausible value under both readings."""
        v = snap.get(key)
        if isinstance(v, dict):
            val, unit = v.get("value"), (v.get("unit") or "").strip().lower()
        else:
            val, unit = v, ""
        if val is None:
            return None
        try:
            val = float(val)
        except (TypeError, ValueError):
            return None
        return round(val * 100.0, 1) if unit == "ratio" else round(val, 1)

    # Carbon intensity + renewable share are the headline metrics
    # international markets compete on. LMP (spot price) maps to the
    # same column as US LMPs so the frontend can render one table.
    spot_usd = (_mv("spot_price_usd_per_mwh")
                or _mv("avg_lmp_usd_per_mwh")
                or _mv("day_ahead_price_usd_per_mwh"))
    # data_method used to be the literal "baseline_model_v1" for every intl
    # row — now a lie for the modules that went live. Read it off the payload.
    method = _mv("method") or "baseline_model_v1"
    is_live = str(method).startswith("live")
    # ★ BUG FIX: this was `_as_float(snap.get("renewable_pct") or _mv(...) or
    # getattr(...))`. snap.get() returns the {"value", "unit"} DICT, which is
    # truthy, so the `or` chain never advanced and _as_float(dict) raised
    # TypeError → None. renewable_pct was therefore None on EVERY intl row of
    # /api/v1/iso/comparison, for every one of these operators, always.
    renewable_pct = _pct("renewable_pct")
    if renewable_pct is None:
        # Module constant, 0-1 ratio by convention.
        try:
            _const = getattr(mod, "RENEWABLE_PCT", None)
            renewable_pct = (round(float(_const) * 100.0, 1)
                             if _const is not None else None)
        except (TypeError, ValueError):
            renewable_pct = None
    return {
        "iso": iso_def["code"],
        "iso_label": iso_def["label"],
        "region": iso_def["region"],
        "data_method": method,
        "markets_scored": 0,
        "build_count": 0,
        "caution_count": 0,
        "avoid_count": 0,
        "avg_excess_power_score": None,
        "avg_constraint_score": None,
        "avg_time_to_power_months": None,
        # ★ 2026-08-20: these were the literals `0`, and they were the last
        # unflagged zeros on this endpoint. The intl modules are baseline
        # power-market models — they carry LMP, carbon intensity and renewable
        # share, and no facility or pipeline inventory whatsoever. Publishing 0
        # asserted Hydro-Québec and the Nordics have no data centres, in the
        # same table where the US rows publish real counts. Nothing was
        # measured here, so nothing numeric is claimed.
        "pipeline_projects": None,
        "pipeline_total_mw": None,
        "pipeline_measured": False,
        "pipeline_unavailable": None,
        "pipeline_basis": "not_tracked_for_intl_operators",
        "facility_count": None,
        "total_facility_mw": None,
        "facilities_measured": False,
        "facilities_unavailable": None,
        "facilities_basis": "not_tracked_for_intl_operators",
        "heartbeat_status": "live" if is_live else "baseline",
        # A live row's vintage is the FEED's stamp, carried in as_of. Age 0
        # was honest for a model computed on the spot; on a feed that runs
        # hours behind wall clock it would be a fabricated freshness.
        "heartbeat_age_hours": None if is_live else 0,
        "as_of": _mv("as_of"),
        # Intl-specific metrics — frontend uses these when DCPI is missing
        "lmp_usd_per_mwh": _as_float(spot_usd),
        "carbon_intensity_g_kwh": _as_float(_mv("carbon_intensity")),
        "renewable_pct": renewable_pct,
        "renewable_pct_units": "percent",
        "renewable_pct_basis": _mv("renewable_pct_basis"),
        "demand_mw": _as_float(_mv("demand_mw")),
        "installed_capacity_mw": getattr(mod, "INSTALLED_CAPACITY_MW", None),
    }


def _conn():
    import psycopg2
    return psycopg2.connect(os.environ.get("DATABASE_URL"), connect_timeout=8)


def _as_float(v):
    try:
        return round(float(v), 2) if v is not None else None
    except (TypeError, ValueError):
        return None


def _norm_iso(s):
    return re.sub(r"[^A-Z]+", "", (s or "").upper())


def _heartbeat_for_iso(cur, iso):
    """Pull the iso_<iso> heartbeat surface row if present."""
    surface = f"iso_{iso.lower()}"
    try:
        cur.execute(
            """SELECT last_updated, stale_after_hours, status,
                      last_refresh_attempt, last_refresh_ok, last_refresh_info
                 FROM freshness_checks WHERE surface = %s""",
            (surface,))
        row = cur.fetchone()
    except Exception:
        return None
    if not row:
        return None
    last_updated = row[0]
    age_hours = None
    if last_updated:
        age_hours = (datetime.now(timezone.utc)
                     - (last_updated if last_updated.tzinfo
                        else last_updated.replace(tzinfo=timezone.utc))).total_seconds() / 3600
    return {
        "surface": surface,
        "last_updated": last_updated.isoformat() if last_updated else None,
        "stale_after_hours": row[1],
        "status": row[2],
        "age_hours": round(age_hours, 2) if age_hours is not None else None,
        "last_refresh_attempt": row[3].isoformat() if row[3] else None,
        "last_refresh_ok": row[4],
        "last_refresh_info": (row[5] or '')[:200],
    }


def _dcpi_aggregate(iso, conn=None):
    """Lazy handle on THE ISO-level DCPI rollup. Imported inside the call so
    this module keeps no import-time dependency on routes.dcpi (which pulls in
    the scoring stack)."""
    from routes.dcpi import _aggregate_iso_stats
    return _aggregate_iso_stats(iso, conn=conn)


def _round1(v):
    try:
        return round(float(v), 1)
    except (TypeError, ValueError):
        return None


def dcpi_row_to_snapshot(row):
    """PURE. Map ONE routes.dcpi._aggregate_iso_stats row to this endpoint's
    dcpi block. Split out so the mapping is testable without a database."""
    if not row:
        return None
    by_verdict = {}
    for key, verdict in (("build_count", "BUILD"), ("caution_count", "CAUTION"),
                         ("avoid_count", "AVOID"), ("low_signal_count", "LOW_SIGNAL")):
        n = row.get(key)
        if n:
            by_verdict[verdict] = int(n)
    return {
        "markets_scored": int(row.get("market_count") or 0),
        "by_verdict": by_verdict,
        "avg_excess_power_score": _round1(row.get("avg_excess")),
        "avg_constraint_score": _round1(row.get("avg_constraint")),
        "avg_time_to_power_months": _round1(row.get("avg_time_to_power_months")),
        "basis": ("routes.dcpi._aggregate_iso_stats — THE ISO-level DCPI "
                  "rollup, shared with /api/v1/dcpi/iso/<ISO>, "
                  "/api/v1/dcpi/iso-comparison and the MCP grid tools, over "
                  "the latest PUBLISHED row per market"),
    }


def _unavailable(e, cur=None):
    """Shape the `error` half of a fail-soft ``(block, error)`` return, and
    un-poison the transaction so the NEXT read still has a chance.

    `kind` separates the two failure modes the 07-31 note conflated. A
    psycopg2.Error means the server answered (schema/data problem); anything
    else means we never got that far — a bug on THIS side of the socket, which
    is what the literal percent was.

    One implementation on purpose. Two fail-soft rollups in this file now
    publish `kind`, and a hand-copied `isinstance(e, psycopg2.Error)` in each
    is exactly how the two would come to disagree about what "database" means
    while both kept reporting confidently.

    ★★ ROLLBACK — measured in production 2026-08-20, minutes after the reason
    channels above went live, and the reason they were worth adding. In
    Postgres one failed statement aborts the WHOLE transaction: every later
    statement on that connection raises InFailedSqlTransaction until someone
    rolls back. `/api/v1/iso/comparison` runs all 13 ISOs over a SINGLE
    connection, so the served payload looked like this:

        ERCOT  pipeline    UndefinedColumn: column "iso" does not exist  <- real
        ERCOT  facilities  InFailedSqlTransaction                        <- collateral
        CAISO  both        InFailedSqlTransaction                        <- collateral
        ...   (all 13 ISOs, both blocks)

    ONE broken query, TWENTY-FIVE casualties — and before the reason channels
    every one of those 26 rendered as a confident `0`, so a head-to-head table
    asserted that every tracked ISO had zero facilities and zero pipeline
    projects over a database holding 18,500+ facilities. Rolling back here
    contains the damage to the query that actually failed: the facilities half
    of the table is not broken at all, it was only ever downstream of a
    poisoned transaction. Same class as the all-zero /agent/index — see the
    psycopg2 `with <conn>` autocommit note; autocommit does NOT exempt an
    explicit transaction from this.

    The rollback is best-effort on purpose: if it fails there is nothing useful
    left to do, and callers still need the ORIGINAL error rather than a
    secondary one about cleanup.
    """
    if cur is not None:
        try:
            cur.connection.rollback()
        except Exception:
            pass
    try:
        import psycopg2
        _kind = ("database" if isinstance(e, psycopg2.Error)
                 else "client_side")
    except Exception:
        _kind = "unknown"
    return {
        "reason": f"{type(e).__name__}: {str(e)[:160]}",
        "kind": _kind,
        "at": datetime.now(timezone.utc).isoformat(),
    }


def _no_row(what):
    """A COUNT(*) that returns no row at all is not a zero of anything."""
    return {"reason": f"{what} returned no row", "kind": "database",
            "at": datetime.now(timezone.utc).isoformat()}


def _dcpi_for_iso(cur, iso):
    """Roll up market_power_scores to the ISO level.

    r-one-ttp (2026-08-08): delegates to routes.dcpi._aggregate_iso_stats
    instead of running its own query. This function used to be a SECOND
    implementation of the same rollup over a DIFFERENT population — no
    `published = true` filter and a case-sensitive `iso = %s` — and it computed
    avg_time_to_power_months from time_to_power_months while the MCP shaper
    filled a field of the IDENTICAL name from avg_queue_wait_months. ERCOT
    published 55.3 here and 71.5 there, same name, same instant. One rollup
    now, so the two cannot drift again.
    """
    try:
        rows = _dcpi_aggregate(iso, conn=cur.connection)
    except Exception:
        return None
    return dcpi_row_to_snapshot(rows[0]) if rows else None


def _pipeline_for_iso(cur, iso):
    """Construction pipeline rollup for the ISO.

    Returns ``(block, error)``. `block` is None whenever the rollup could not
    be produced, and `error` then carries WHY — a two-value return rather than
    a bare None because both callers below rendered that None as the number
    zero, and a zero is a claim.

    ★ 2026-07-31 audit — DEAD READ, always has been.

    ★★ 2026-08-19 RE-MEASURED, and the 07-31 diagnosis above it was WRONG.
    It read: "`capacity_pipeline` has no `iso` column ... this raises
    UndefinedColumn". The schema half is right; the exception was not, and
    nobody could ever have observed it. Until #2958 the query below carried a
    literal ``LIKE '%construct%'`` while passing an args tuple, so psycopg2's
    client-side parameter interpolation raised FIRST and the statement was
    never sent at all:

        before #2958   IndexError: tuple index out of range   (never sent)
        since  #2958   psycopg2.errors.UndefinedColumn: column "iso" does not
                       exist                                  (server replies)

    (measured 2026-08-19 against a local PG 18.4 with capacity_pipeline built
    to the live column set; the control with the iso predicate swapped for
    `market` returns rows, so the doubled percent is not itself the fault.)
    The distinction matters for whoever picks this up: for three weeks the
    failure was OURS, in this process, and no amount of reading Postgres logs
    would have shown it — which is exactly how it sat mislabelled. #2958
    doubled the percent, so the docstring's UndefinedColumn is only NOW the
    real cause. The `kind` field on the error records which of the two a live
    failure was, so this cannot go stale silently a second time.

    There is no mechanical repair available for the schema half: the only
    location columns are market (free-text city/state), region and country,
    and `region` is unusable as an ISO proxy — measured 2026-07-31 it is NULL
    on 1,087 of 1,973 rows and literally 'Unknown' on another 590, i.e. 85%
    unusable, with the remainder a mix of 'US' / 'North America' / 'APAC' /
    'EMEA' that are continents, not ISOs. Deriving ISO here needs either a
    real `iso` column on the table or a market→ISO mapping applied at write
    time (util/iso_taxonomy.STATE_ISO is the existing one) — a data-
    modelling decision, not a bug fix.

    The data_flag guard is applied now so that whoever adds the ISO
    predicate inherits it instead of reintroducing the unfiltered sum.
    """
    try:
        cur.execute(
            f"""SELECT COUNT(*) AS n, COALESCE(SUM(capacity_mw), 0) AS total_mw,
                      COUNT(*) FILTER (WHERE
                          LOWER(COALESCE(phase, status, '')) LIKE '%%construct%%')
                          AS construction_count,
                      COALESCE(SUM(capacity_mw) FILTER (WHERE
                          LOWER(COALESCE(phase, status, '')) LIKE '%%construct%%'), 0)
                          AS construction_mw
                 FROM capacity_pipeline
                WHERE UPPER(COALESCE(iso, '')) = %s
                  AND {CP_OK}""",
            (iso,))
        row = cur.fetchone()
    except Exception as e:
        # See _unavailable: `kind` records which side of the socket failed.
        return None, _unavailable(e, cur)
    if not row:
        # A COUNT(*) that returns no row at all is not "zero projects" either.
        return None, _no_row("query")
    return {
        "project_count": int(row[0] or 0),
        "total_mw": _as_float(row[1]),
        "under_construction_count": int(row[2] or 0),
        "under_construction_mw": _as_float(row[3]),
    }, None


def _facilities_for_iso(cur, iso):
    """Facility count + total MW in the ISO footprint. ISO mapping is
    loose (we use market_power_scores.iso -> state set, then filter
    facilities by state). Best-effort.

    Returns ``(block, error)`` for the same reason `_pipeline_for_iso` above
    does: `block` is None whenever the footprint could not be produced, and
    `error` then carries WHY. Both callers below rendered a bare None as the
    number zero — /api/v1/iso/comparison via
    ``facilities.get("facility_count", 0)``, which publishes a confident
    "0 facilities" for an ISO in a head-to-head table when the truth is that a
    query broke. See `_unavailable` for what `kind` records and
    `_pipeline_for_iso` for why that distinction is load-bearing.

    ★ THREE outcomes, not two. `basis` on the block names which one, and it is
    published at the TOP LEVEL of both callers — see the BPA lesson below for
    why burying it inside the block was not enough:

      raised          the market_power_scores lookup or the facilities
                      COUNT/SUM threw. Nothing was measured.
                      -> (None, error)
      no mapping      the state lookup SUCCEEDED and returned nothing. Nothing
                      BROKE, but there is also no state set to sum over, so
                      there is nothing to have measured.
                      -> ({facility_count: None, basis: no_iso_state_mapping},
                          None)
      measured        states resolved and the COUNT/SUM ran. `facility_count`
                      is a real number, and 0 here genuinely means zero.
                      -> ({facility_count: int, basis: iso_state_footprint},
                          None)

    The original code reached `if not states: return None` from the first two —
    a bare ``except: states = []`` swallowed a raising query into the
    no-mapping case, and downstream both became the number 0.

    ★★ BPA, measured live 2026-08-20 — and the correction to this function's
    own first attempt. #2962 called the no-mapping case a "measured empty" and
    returned `facility_count: 0`, reasoning that `states: []` was self-evident
    enough to keep the zero honest. Production disagreed. BPA is absent from
    `market_power_scores` entirely (`/api/v1/dcpi/iso/BPA` -> `iso_not_found`),
    so it took that path and /iso/comparison published

        BPA   facility_count: 0   facilities_measured: true

    over territory (WA/OR/ID) containing Quincy, Hillsboro and Umatilla. That
    is *more* confident than the bare 0 it replaced — the flag asserted we had
    looked. And the `states: []` evidence never reached the reader: it lives
    inside this block, which routes/tier_gate.py strips for anonymous callers,
    while the flag passed through. **Evidence that a gate can remove cannot be
    what makes a number honest.** Hence `facility_count: None` here, and
    `facilities_basis` published beside the count rather than inside it.
    """
    try:
        cur.execute(
            "SELECT DISTINCT state FROM market_power_scores WHERE iso = %s",
            (iso,))
        states = [r[0] for r in cur.fetchall() if r[0]]
    except Exception as e:
        return None, _unavailable(e, cur)
    if not states:
        # Nothing broke — so this is not an `error` — but nothing was measured
        # either. A count of None cannot be misread as a count of zero.
        return {"facility_count": None, "total_facility_mw": None,
                "states": [], "basis": "no_iso_state_mapping"}, None
    try:
        cur.execute(
            """SELECT COUNT(*),
                      COALESCE(SUM(power_mw), 0)
                 FROM facilities
                WHERE UPPER(state) = ANY(%s)
                  AND country IN ('US', 'USA', 'United States', 'Canada', 'CA')""",
            ([s.upper() for s in states],))
        row = cur.fetchone()
    except Exception as e:
        return None, _unavailable(e, cur)
    if not row:
        return None, _no_row("facility count")
    return {
        "facility_count": int(row[0] or 0),
        "total_facility_mw": _as_float(row[1]),
        "states": states,
        # A 0 carrying this basis IS a real count of zero over a real state set.
        "basis": "iso_state_footprint",
    }, None


@iso_snapshot_bp.route("/api/v1/iso/<iso_code>/snapshot", methods=["GET"])
def iso_snapshot(iso_code):
    """Full per-ISO snapshot: heartbeat freshness, DCPI rollup,
    pipeline, facilities. Single connection, best-effort per section."""
    iso = _norm_iso(iso_code)
    if iso not in _KNOWN_ISOS:
        # ★ CASE-SENSITIVE ROUTING, verified live: /api/v1/iso/aeso/snapshot
        # (lowercase) is matched by the static iso_aeso_intl blueprint and
        # returns 200, while /api/v1/iso/AESO/snapshot falls through to this
        # converter and 404s. _norm_iso uppercases, so adding these codes to
        # _KNOWN_ISOS would only change the uppercase path and leave TWO
        # DIFFERENT PAYLOADS on two casings of the same URL. Point at the
        # owning route instead of forking the response.
        _INTL_ROUTES = {d["code"]: f"/api/v1/iso/{d['code'].lower()}/snapshot"
                        for d in _INTL_ISOS}
        _served_by = _INTL_ROUTES.get(iso)
        return jsonify(ok=False, error="unknown_iso",
                       known=_KNOWN_ISOS,
                       intl_operators=sorted(_INTL_ROUTES),
                       served_by=_served_by,
                       hint=(f"{iso} is an international operator served by its "
                             f"own blueprint at {_served_by} (lowercase path)"
                             if _served_by else
                             "not a tracked ISO; see /api/v1/iso/comparison")), 404
    try:
        with _conn() as c, c.cursor() as cur:
            heartbeat = _heartbeat_for_iso(cur, iso)
            dcpi = _dcpi_for_iso(cur, iso)
            pipeline, pipeline_err = _pipeline_for_iso(cur, iso)
            facilities, facilities_err = _facilities_for_iso(cur, iso)
        from routes.tier_gate import jsonify_gated_snapshot
        _payload = {
            "ok": True,
            "iso": iso,
            "heartbeat": heartbeat,
            "dcpi": dcpi,
            "pipeline": pipeline,
            # ★ 2026-08-19: `pipeline: null` alone is unreadable — it could
            # mean "this ISO has no tracked pipeline" or "the rollup broke".
            # It has meant the second one for the whole life of this route.
            # Say which, next to the block it explains.
            "pipeline_measured": pipeline_err is None,
            "pipeline_unavailable": pipeline_err,
            "facilities": facilities,
            # Same treatment, same reason: this route used to omit the
            # facilities block entirely with no statement that it could not be
            # produced, which reads exactly like an ISO we hold nothing for.
            # ★ `measured` requires a real count, not merely the absence of an
            # error — the no-mapping case raises nothing and measures nothing.
            "facilities_measured": (facilities_err is None
                                    and (facilities or {}).get(
                                        "facility_count") is not None),
            "facilities_unavailable": facilities_err,
            # ★ Top level ON PURPOSE. tier_gate strips the `facilities` block
            # for anonymous callers, so a basis carried only inside it never
            # reaches the reader who sees the number (the BPA case).
            "facilities_basis": (facilities or {}).get("basis"),
            "drill_deeper": {
                "live_grid": f"/api/v1/grid/{iso}",
                "dcpi_markets_in_iso": f"/api/v1/dcpi/iso/{iso}",
                "comparison_with_other_isos": "/api/v1/iso/comparison",
            },
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        # provenance-v1 (2026-07-11): collection-level block. as_of = the ISO
        # heartbeat's last real refresh when known (the data vintage), else
        # generation time. Fail-soft — never breaks the snapshot.
        try:
            from routes.provenance import attach_provenance
            attach_provenance(
                _payload,
                source=f"{iso} public ISO/RTO feed + DC Hub DCPI scoring",
                method=("realtime per-ISO ingestion (heartbeat-tracked) + "
                        "DCPI market/pipeline/facility rollup"),
                as_of=((heartbeat or {}).get("last_updated")
                       or _payload["generated_at"]),
                # v1: the snapshot mixes published feed data with DC Hub
                # DCPI scoring (modeled) — conservative baseline is inferred.
                default_v="inferred",
            )
        except Exception:
            pass
        return jsonify_gated_snapshot(_payload, 200)
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:300]), 200


@iso_snapshot_bp.route("/api/v1/iso/comparison", methods=["GET"])
def iso_comparison():
    """Head-to-head: every tracked ISO with its DCPI rollup + pipeline +
    facility footprint, ranked by avg excess-power score.

    Phase ZZZZZ-round54: now includes 3 international ISOs (HYDROQUEBEC,
    AESO-intl, NORDPOOL) from sibling baseline-model modules. These show
    up with `data_method: 'baseline_model_v1'` and carry intl-specific
    metrics (lmp_usd_per_mwh, carbon_intensity_g_kwh, renewable_pct).
    """
    out = []
    try:
        with _conn() as c, c.cursor() as cur:
            for iso in _KNOWN_ISOS:
                dcpi = _dcpi_for_iso(cur, iso) or {}
                _pipe, _pipe_err = _pipeline_for_iso(cur, iso)
                pipeline = _pipe or {}
                _fac, _fac_err = _facilities_for_iso(cur, iso)
                facilities = _fac or {}
                heartbeat = _heartbeat_for_iso(cur, iso) or {}
                out.append({
                    "iso": iso,
                    "data_method": "realtime",
                    "markets_scored": dcpi.get("markets_scored", 0),
                    "build_count": (dcpi.get("by_verdict") or {}).get("BUILD", 0),
                    "caution_count": (dcpi.get("by_verdict") or {}).get("CAUTION", 0),
                    "avoid_count": (dcpi.get("by_verdict") or {}).get("AVOID", 0),
                    "avg_excess_power_score": dcpi.get("avg_excess_power_score"),
                    "avg_constraint_score": dcpi.get("avg_constraint_score"),
                    "avg_time_to_power_months": dcpi.get("avg_time_to_power_months"),
                    # ★ 2026-08-19: this `, 0` default was the loudest form of
                    # the same lie — a head-to-head table publishing
                    # "0 projects" for all ten ISOs because the rollup threw
                    # client-side before it ever reached Postgres. None means
                    # unknown; 0 would mean measured-and-empty.
                    "pipeline_projects": (pipeline.get("project_count", 0)
                                          if _pipe_err is None else None),
                    "pipeline_total_mw": pipeline.get("total_mw"),
                    "pipeline_measured": _pipe_err is None,
                    "pipeline_unavailable": _pipe_err,
                    # ★ 2026-08-19: the sibling of the `, 0` above, and the
                    # more expensive of the two — a head-to-head table
                    # publishing "0 facilities" for an ISO reads as a market
                    # with nothing built in it. None means unknown; 0 now only
                    # ever means measured-and-empty.
                    "facility_count": (facilities.get("facility_count")
                                       if _fac_err is None else None),
                    "total_facility_mw": facilities.get("total_facility_mw"),
                    # ★ 2026-08-20: was `_fac_err is None`, which called BPA
                    # measured because nothing had raised. Nothing raised AND
                    # nothing was measured — BPA has no row in
                    # market_power_scores at all. Require the count itself.
                    "facilities_measured": (_fac_err is None
                                            and facilities.get("facility_count")
                                            is not None),
                    "facilities_unavailable": _fac_err,
                    "facilities_basis": facilities.get("basis"),
                    "heartbeat_status": heartbeat.get("status"),
                    "heartbeat_age_hours": heartbeat.get("age_hours"),
                })
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:300]), 200

    # Append intl ISOs after US/CA real-time set. Best-effort — a
    # broken module is skipped (does NOT 500 the endpoint).
    for iso_def in _INTL_ISOS:
        row = _intl_snapshot_row(iso_def)
        if row:
            out.append(row)

    # Rank by avg excess-power (best opportunity first); push None to end.
    out.sort(key=lambda r: (r["avg_excess_power_score"] is None,
                             -(r["avg_excess_power_score"] or 0)))
    _cmp_payload = {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "isos": out,
        "ranking_by": "avg_excess_power_score",
        "iso_count": len(out),
        "coverage": {
            "realtime_us_ca": _KNOWN_ISOS,
            "baseline_intl": [d["code"] for d in _INTL_ISOS],
        },
    }
    # provenance-v1 (2026-07-11): collection block. Per-record confidence is
    # the EXISTING data_method field (realtime | baseline_model_v1) — the
    # block just names the convention instead of adding a duplicate flag.
    try:
        from routes.provenance import attach_provenance
        attach_provenance(
            _cmp_payload,
            source=("US/CA ISO public realtime feeds + international "
                    "baseline models (per-record data_method)"),
            method=("realtime per-ISO ingestion + DCPI rollup; rows with "
                    "data_method=baseline_model_v1 are modeled baselines, "
                    "not live telemetry"),
            as_of=_cmp_payload["generated_at"],
            # v1: baseline_model_v1 (modeled) rows + DCPI rollups can appear
            # alongside realtime rows — conservative baseline is inferred.
            default_v="inferred",
        )
    except Exception:
        pass
    return jsonify(_cmp_payload), 200
