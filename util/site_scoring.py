"""Point-level sub-scores for /api/site-score (analyze_site), composite-v2.4.

WHY (live screen 2026-09-25, NoVA + north NJ): every anchor scored power 100,
gas 95, market 60, risk 65, so the ranks differed only on fiber. Measured
causes in main.api_site_score:

  * power = min(100, 40 + 2*substations_50km + 1.5*plants_80km): a dense metro
    passes ~30 features and saturates at the cap;
  * gas = a step on the pipeline-segment count within ~50km: >=20 -> 95;
  * risk = STATE_RISK.get(state, 65), and find_sites' handoff sends no state,
    so every anchor took the fallback 65 — an unscored factor dressed as a
    measurement;
  * every lookup was `except: pass`, so a failed query read as "0 nearby".

This module holds the scoring rules, pure and testable. The handler supplies
the measurements and reports, per factor, what the score rests on
(score_basis), where it came from (source, as_of) and whether it was scored at
all. An unscored factor is null and leaves the composite, which is
renormalised over what was scored and says so.
"""
from __future__ import annotations

import math

METHODOLOGY_VERSION = "composite-v2.4"

# composite weights (unchanged from v2.3)
WEIGHTS = {"power_infrastructure": 0.25, "gas_pipeline_access": 0.10,
           "fiber_connectivity": 0.15, "market_conditions": 0.15,
           "risk_resilience": 0.35}

HV_KV = 230          # "high voltage" for a data-center interconnect
SEARCH_DEG = 1.0     # bounding box for the nearest-feature lookups (~110 km)


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def density_power(substations, plants):
    """The v2.3 rule: (score, saturated)."""
    raw = 40 + (substations or 0) * 2 + (plants or 0) * 1.5
    return min(100.0, raw), raw >= 100


def power_score(nearest_hv_km, nearest_hv_kv, substations, plants):
    """(score, basis). The nearest >=230 kV substation is what separates two
    metro sites; the density count only adds context (30%)."""
    dens, saturated = density_power(substations, plants)
    if nearest_hv_km is None:
        return dens, ("density_count_saturated" if saturated else "density_count")
    dist = max(20.0, 100.0 - 4.0 * nearest_hv_km)       # 0 km 100 · 10 km 60 · >=20 km 20
    kv = nearest_hv_kv or 0
    dist = min(100.0, dist + (5 if kv >= 500 else 3 if kv >= 345 else 0))
    return round(0.7 * dist + 0.3 * dens, 1), "measured_point:nearest_hv_substation"


def gas_score(nearest_km, segments_50km):
    """(score, basis): distance to the nearest active pipeline segment, else
    the v2.3 count band."""
    if nearest_km is not None:
        return round(max(20.0, 100.0 - 3.0 * nearest_km), 1), "measured_point:nearest_pipeline"
    n = segments_50km or 0
    band = 95 if n >= 20 else 85 if n >= 10 else 70 if n >= 3 else 55 if n >= 1 else 30
    return float(band), "count_band"


def market_score(facilities_100km):
    """(score, basis). Unchanged v2.3 band, now labelled: it is a facility
    density band, and it deliberately reads the densest markets (>=50) like
    empty ones (competition). It is not a demand or price measure."""
    n = facilities_100km or 0
    s = 60 if n < 5 else 85 if n < 20 else 75 if n < 50 else 60
    return float(s), "facility_density_band"


def composite(scores):
    """(overall, basis): weighted mean over the factors that were scored."""
    got = {k: v for k, v in scores.items() if v is not None and k in WEIGHTS}
    if not got:
        return None, "nothing_scored"
    w = sum(WEIGHTS[k] for k in got)
    overall = round(sum(v * WEIGHTS[k] for k, v in got.items()) / w, 1)
    missing = sorted(set(WEIGHTS) - set(got))
    return overall, ("all_factors" if not missing
                     else "renormalised_without:" + ",".join(missing))


_AS_OF = {"at": 0.0, "value": {}}


def as_of(c):
    """{table: latest updated_at ISO date} for the tables the site score
    reads, cached per process for 6h (MAX over 133k substations is not a
    per-request cost). A table that cannot be read is absent, never guessed."""
    import time as _t
    if _AS_OF["value"] and _t.time() - _AS_OF["at"] < 21600:
        return _AS_OF["value"]
    out = {}
    for table in ('substations', 'gas_pipelines'):
        try:
            c.execute(f"SELECT MAX(updated_at) FROM {table}")
            v = (c.fetchone() or [None])[0]
            if v is not None:
                out[table] = str(v)[:10]
        except Exception:
            try:
                c.connection.rollback()
            except Exception:
                pass
    _AS_OF.update(at=_t.time(), value=out)
    return out


def coverage(state, state_basis, hv, gas_km, as_of, errors, *,
             power_basis, gas_basis, fiber_basis, market_basis, risk_basis):
    """composite-v2.4 per-factor coverage for /api/site-score."""
    def _f(basis, source, as_of_key=None, err_keys=()):
        errs = {k: errors[k] for k in err_keys if k in errors}
        d = {'scored': not str(basis).startswith('risk_not_scored'),
             'basis': basis, 'source': source,
             'as_of': (as_of or {}).get(as_of_key) if as_of_key else None}
        if errs:
            d['lookup_errors'] = errs
        return d
    return {
        'state': {'value': state or None, 'basis': state_basis},
        'power_infrastructure': _f(
            power_basis, 'HIFLD substations (nearest >=230 kV) + substation/plant counts',
            'substations', ('substations', 'infrastructure_layers_substations',
                            'infrastructure_layers_plants', 'discovered_power_plants',
                            'nearest_hv_substation')),
        'gas_pipeline_access': _f(gas_basis, 'gas_pipelines (active segments)',
                                  'gas_pipelines', ('gas_pipelines', 'nearest_gas_pipeline')),
        'fiber_connectivity': _f(fiber_basis, 'PeeringDB carrier presence + FCC fiber coverage',
                                 None, ('fiber_parcel',)),
        'market_conditions': _f(market_basis, 'discovered_facilities within ~100 km'),
        'risk_resilience': _f(risk_basis, 'DC Hub state risk table (not county-level)'),
    }


FACTORS = tuple(WEIGHTS)


def scored_factors(scores):
    """'n/5': how many of the five composite factors carry a score."""
    scores = scores or {}
    return "%d/%d" % (sum(1 for k in FACTORS if scores.get(k) is not None), len(FACTORS))


def row_completeness(row):
    """For a rank_sites candidate: (scored_factors 'n/m' or None, risk_missing).

    A row is recognised as a site-score row by what it carries: a
    scored_factors string, a site-score `scores` block, or the factor fields
    themselves (all five, flattened). Anything else says nothing: (None, False).
    risk_missing is True when the row carries risk_resilience as null or its
    composite was renormalised without it."""
    if not isinstance(row, dict):
        return None, False
    sf = row.get("scored_factors")
    scores = row.get("scores") if isinstance(row.get("scores"), dict) else None
    flat = {k: row[k] for k in FACTORS if k in row}
    src = scores if scores is not None else (flat or None)
    risk_missing = False
    if src is not None and "risk_resilience" in src and src.get("risk_resilience") is None:
        risk_missing = True
    if "risk_resilience" in str(row.get("overall_basis") or ""):
        risk_missing = True
    if isinstance(sf, str) and "/" in sf:
        return sf, risk_missing
    if scores is not None:
        return scored_factors(scores), risk_missing
    if len(flat) == len(FACTORS):          # all five flattened: a site-score row
        got = sum(1 for v in flat.values() if v is not None)
        return "%d/%d" % (got, len(FACTORS)), risk_missing
    # A caller who passed only some factor fields chose its objectives; that
    # is not an incomplete site-score row (rank_sites declares any missing
    # objective itself, in missing_objectives).
    return None, risk_missing


def is_complete(sf):
    try:
        n, m = (int(x) for x in str(sf).split("/"))
        return n == m
    except Exception:
        return True
