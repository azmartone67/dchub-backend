#!/usr/bin/env python3
"""
scripts/build_wri_aqueduct_states.py — build a validated US-state baseline
water-stress CSV from WRI Aqueduct 4.0, for the water_aqueduct_ingest pipeline.

WHY THIS SCRIPT EXISTS
  The runtime ingest (routes/water_aqueduct_ingest.py) fetches ONE URL and parses
  it — great when the source is already province/state level. But Aqueduct 4.0's
  primary distribution is BASIN level (HydroSHEDS-6 / GDB / GeoParquet), which
  needs a spatial roll-up to US states before it can feed the ingest. This
  one-time offline aggregator does exactly that, produces a small CSV the ingest
  can consume via WRI_AQUEDUCT_URL, and applies the SAME integrity guard the
  ingest does so nothing inverted or fabricated ever ships.

  This is deliberately a standalone dev/ops script (run where the big source file
  is reachable), NOT a runtime import — it may need geopandas for the basin path.

INPUT MODES (auto-detected)
  A. PROVINCE/STATE file (has a name_1 / state field): pure-python, no deps.
     e.g. an Aqueduct province ranking GeoJSON/CSV.
  B. BASIN file (aq30/pfaf polygons, no state field): spatial-joins to US state
     polygons and takes the MAX (most-stressed) basin per state. Needs geopandas.

WRI Aqueduct 4.0 fields (verified vs data_dictionary_water-risk-atlas.md):
  bws_cat   -1..4  category (-1 Arid&LowUse · 0 Low · 1 Low-Med · 2 Med-High
                   · 3 High · 4 Extremely High) — the STABLE published signal.
  bws_raw          withdrawal/available RATIO (long-tailed; NOT 0-5).
  bws_label        human label. name_1 = province/state. name_0 = country.

USAGE
  # A — province file straight through:
  python scripts/build_wri_aqueduct_states.py \
      --src aqueduct40_province.geojson --out data/wri_aqueduct_us_states.csv

  # B — basin file + US state polygons (geopandas):
  python scripts/build_wri_aqueduct_states.py \
      --src Aq40_baseline_annual.gpkg --states cb_2022_us_state_500k.shp \
      --out data/wri_aqueduct_us_states.csv

  Then host the CSV (repo raw / R2) and set WRI_AQUEDUCT_URL to it, POST the
  admin ingest ?dry=1, confirm direction_sane=true, then ?dry=0.

INTEGRITY: refuses to write the CSV if arid states (AZ/NV/NM/CA/UT) don't clearly
out-score wet states (IL/OH/MI/WI/MN/PA). Same guard as the ingest — no inversion,
no fabrication.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys

_STATE_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
}
_ABBRS = set(_STATE_ABBR.values())
_HI_REF = ("AZ", "NV", "NM", "CA", "UT")
_LO_REF = ("IL", "OH", "MI", "WI", "MN", "PA")


def _num(v):
    try:
        return float(v)
    except Exception:
        return None


def _norm_state(v):
    if v is None:
        return None
    s = str(v).strip()
    if s.upper() in _ABBRS:
        return s.upper()
    if "-" in s and s.split("-")[-1].upper() in _ABBRS:
        return s.split("-")[-1].upper()
    return _STATE_ABBR.get(s.lower())


def _cat_to_100(cat):
    c = _num(cat)
    if c is None:
        return None
    if c < 0:
        return 100.0  # Arid & Low Water Use → extreme scarcity
    return round(max(0.0, min(4.0, c)) / 4.0 * 100.0, 1)


def _ratio_to_100(raw):
    r = _num(raw)
    if r is None:
        return None
    return round(max(0.0, min(1.0, r)) * 100.0, 1)


def _row_from_props(p: dict):
    st = _norm_state(p.get("name_1") or p.get("state") or p.get("NAME")
                     or p.get("st_abbr") or p.get("iso_3166_2") or p.get("region"))
    if not st:
        return None
    cat, raw = p.get("bws_cat"), p.get("bws_raw", p.get("bws_score", p.get("score")))
    label = p.get("bws_label") or p.get("bws_cat_label") or p.get("category")
    s100 = _cat_to_100(cat)
    if s100 is None:
        s100 = _ratio_to_100(raw)
    if s100 is None and label is None:
        return None
    return {"state": st, "water_stress_score": s100, "baseline_water_stress": _num(raw),
            "bws_category": (str(label)[:64] if label is not None else None)}


def _from_province_file(path: str) -> list[dict]:
    """Mode A — file already carries a name_1/state field. Pure python."""
    txt = open(path, "r", encoding="utf-8", errors="replace").read()
    recs = []
    try:
        data = json.loads(txt)
        feats = data.get("features") if isinstance(data, dict) else (data if isinstance(data, list) else [])
        for f in (feats or []):
            props = f.get("properties", f) if isinstance(f, dict) else {}
            r = _row_from_props(props)
            if r:
                recs.append(r)
    except json.JSONDecodeError:
        for row in csv.DictReader(txt.splitlines()):
            r = _row_from_props(row)
            if r:
                recs.append(r)
    return recs


def _from_basin_file(src: str, states_shp: str) -> list[dict]:
    """Mode B — basin polygons → spatial-join to US states, MAX per state.
    Requires geopandas (installed only in the offline env that runs this)."""
    try:
        import geopandas as gpd  # noqa
    except Exception:
        sys.exit("Mode B needs geopandas: pip install geopandas  (or pass a province-level --src)")
    basins = gpd.read_file(src)
    states = gpd.read_file(states_shp).to_crs(basins.crs)
    # centroid of each basin → the state it falls in (cheap, robust enough for a state roll-up)
    b = basins.copy()
    b["geometry"] = b.geometry.representative_point()
    joined = gpd.sjoin(b, states, predicate="within", how="inner")
    name_col = next((c for c in ("NAME", "STATE_NAME", "name") if c in joined.columns), None)
    recs = []
    for _, row in joined.iterrows():
        p = dict(row)
        if name_col:
            p["name_1"] = row[name_col]
        r = _row_from_props(p)
        if r:
            recs.append(r)
    return recs


def _dedup_max(recs: list[dict]) -> dict:
    best: dict[str, dict] = {}
    for r in recs:
        cur = best.get(r["state"])
        if cur is None or ((r["water_stress_score"] or -1) > (cur["water_stress_score"] or -1)):
            best[r["state"]] = r
    return best


def _direction_sane(by_state: dict):
    hi = [by_state[s]["water_stress_score"] for s in _HI_REF
          if s in by_state and by_state[s].get("water_stress_score") is not None]
    lo = [by_state[s]["water_stress_score"] for s in _LO_REF
          if s in by_state and by_state[s].get("water_stress_score") is not None]
    if len(hi) < 2 or len(lo) < 2:
        return None
    return (sum(hi) / len(hi)) > (sum(lo) / len(lo)) + 10.0


def main():
    ap = argparse.ArgumentParser(description="Build validated US-state Aqueduct water-stress CSV")
    ap.add_argument("--src", required=True, help="Aqueduct province OR basin file (GeoJSON/CSV/GPKG)")
    ap.add_argument("--states", help="US state polygons (shp/gpkg) — required only for basin sources")
    ap.add_argument("--out", default="data/wri_aqueduct_us_states.csv")
    args = ap.parse_args()

    recs = _from_province_file(args.src) if not args.states else _from_basin_file(args.src, args.states)
    if not recs:
        sys.exit("parsed 0 US states — check the source shape / field names (need name_1 + bws_cat/bws_raw).")
    by_state = _dedup_max(recs)
    sane = _direction_sane(by_state)
    ref = {s: by_state[s]["water_stress_score"] for s in (_HI_REF + _LO_REF) if s in by_state}
    print(f"parsed {len(by_state)} US states · direction_sane={sane} · refs={ref}")
    if sane is False:
        sys.exit("✗ direction_check_failed — arid states did NOT out-score wet states. "
                 "Source/mapping is inverted or wrong; refusing to write (integrity guard).")
    if sane is None:
        print("⚠ could not verify direction (missing reference states) — inspect the CSV before ingesting.")

    import os
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["state", "water_stress_score", "baseline_water_stress", "bws_category", "source"])
        for st in sorted(by_state):
            r = by_state[st]
            w.writerow([st, r["water_stress_score"], r["baseline_water_stress"],
                        r["bws_category"], "wri_aqueduct_40"])
    print(f"✓ wrote {len(by_state)} rows → {args.out}  (source tag: wri_aqueduct_40)")
    print("  next: host this CSV, set WRI_AQUEDUCT_URL=<raw url>, POST /api/v1/admin/water/aqueduct-ingest?dry=1")


if __name__ == "__main__":
    main()
