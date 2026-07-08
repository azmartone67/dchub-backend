"""
routes/water_aqueduct_ingest.py — WRI Aqueduct water-stress ingest (2026-07-08).

Replaces the PAUSED, inverted state-level groundwater proxy (site_planner.py
WATER_STRESS_BY_STATE — AZ>IL was direction-wrong) with REAL WRI Aqueduct
baseline water stress. See [[reference_dchub_water_score_integrity]].

★ INTEGRITY CONTRACT (do not break):
  This module NEVER fabricates a water number. It writes rows to `water_risk`
  ONLY for records it actually parsed from a configured, verified WRI source
  (WRI_AQUEDUCT_URL). With no source set — or on any fetch/parse failure — it is
  an honest NO-OP (writes nothing, returns skipped=...). Until real WRI rows tag
  `source='wri_aqueduct'`, the rank_sites water objectives stay "unavailable"
  (routes/interconnection_queues._wri_water_available). So the water lever
  auto-enables the moment verified data lands and NEVER a moment before.

CRAWL-FIRST rollout (deliberate):
  1. Point WRI_AQUEDUCT_URL at a verified WRI Aqueduct subnational file
     (GeoJSON FeatureCollection or JSON/CSV array with a US-state key +
     baseline-water-stress score/category).
  2. POST /api/v1/admin/water/aqueduct-ingest?dry=1  → parse + preview, write 0.
  3. Eyeball the preview (AZ should read MORE stressed than IL — the exact
     inversion the old proxy got wrong).
  4. POST .../aqueduct-ingest (dry=0) → upsert real rows. rank_sites water
     objectives light up automatically.
  5. Only then wire a daily cron (crawler_scheduler) — not before a verified run.

Expected source shape (flexible parser):
  · GeoJSON: features[].properties with a state field (name_1 / state / NAME /
    st_abbr / iso_3166_2) + bws score (bws_score / bws_raw / score) and/or
    category (bws_cat / bws_label / category).
  · JSON/CSV array: rows of {state, bws_score|score, bws_cat|category}.
  bws_score is WRI's 0–5 baseline-water-stress raw; we normalize to
  water_stress_score 0–100 (100 = most stressed) and keep the raw in
  baseline_water_stress. US states only (mapped to 2-letter).

Endpoints (admin-gated: X-Admin-Key vs DCHUB_ADMIN_KEY / DCHUB_INTERNAL_KEY):
  GET  /api/v1/admin/water/aqueduct-ingest/status  — source config + row counts
  POST /api/v1/admin/water/aqueduct-ingest          — run (?dry=1 previews)
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import urllib.request

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

water_aqueduct_ingest_bp = Blueprint("water_aqueduct_ingest", __name__)

_SOURCE_TAG = "wri_aqueduct"

# US state name → 2-letter (for normalizing WRI subnational rows).
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


def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key") or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _source_url() -> str:
    return (os.environ.get("WRI_AQUEDUCT_URL") or "").strip()


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
        logger.debug("[water-ingest] conn failed: %s", e)
        return None


def _ensure_schema(c) -> None:
    with c.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS water_risk ("
            " id BIGSERIAL PRIMARY KEY,"
            " state TEXT,"
            " water_stress_score DOUBLE PRECISION,"   # 0-100, 100 = most stressed
            " baseline_water_stress DOUBLE PRECISION,"  # WRI raw bws_score (0-5)
            " bws_category TEXT,"
            " source TEXT,"
            " computed_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        # additive columns if an older water_risk table pre-exists
        for col, typ in (("baseline_water_stress", "DOUBLE PRECISION"),
                         ("bws_category", "TEXT"), ("source", "TEXT"),
                         ("computed_at", "TIMESTAMPTZ")):
            try:
                cur.execute(f"ALTER TABLE water_risk ADD COLUMN IF NOT EXISTS {col} {typ}")
            except Exception:
                pass


def _norm_state(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    if s.upper() in _ABBRS:
        return s.upper()
    # ISO 3166-2 like "US-AZ"
    if "-" in s and s.split("-")[-1].upper() in _ABBRS:
        return s.split("-")[-1].upper()
    return _STATE_ABBR.get(s.lower())


def _num(v):
    try:
        f = float(v)
        return f
    except Exception:
        return None


# WRI Aqueduct 4.0 encoding (verified vs the data dictionary, 2026-07-08):
#   bws_cat   -1..4  — the PUBLISHED category bucket (the stable signal):
#                      -1 Arid & Low Water Use · 0 Low(<10%) · 1 Low-Medium(10-20%)
#                      · 2 Medium-High(20-40%) · 3 High(40-80%) · 4 Extremely High(>80%)
#   bws_raw          — the raw withdrawal/available-supply RATIO (long-tailed; NOT 0-5).
#   bws_label        — human category label.
# We normalize from the CATEGORY (stable) and fall back to the ratio; NEVER assume
# a 0-5 scale (the old bug). 100 = most stressed.
def _cat_to_100(cat):
    c = _num(cat)
    if c is None:
        return None
    if c < 0:
        return 100.0   # Arid & Low Water Use → extreme scarcity for a water-hungry DC
    c = max(0.0, min(4.0, c))
    return round(c / 4.0 * 100.0, 1)


def _ratio_to_100(raw):
    r = _num(raw)
    if r is None:
        return None
    r = max(0.0, min(1.0, r))   # WRI caps Extremely High at >0.8; clamp the tail
    return round(r * 100.0, 1)


# Direction-integrity references: the 2026-07-07 pause was caused by an INVERTED
# proxy (AZ read less-stressed than IL). Before ANY write we assert the opposite —
# known arid states must clearly out-score known wet states, or we refuse.
_HI_STRESS_REF = ("AZ", "NV", "NM", "CA", "UT")
_LO_STRESS_REF = ("IL", "OH", "MI", "WI", "MN", "PA")


def _direction_sane(by_state: dict):
    """True/False if checkable, else None. Requires a clear (>10pt) margin so a
    flat/degenerate parse can't sneak through."""
    hi = [by_state[s]["score100"] for s in _HI_STRESS_REF
          if s in by_state and by_state[s].get("score100") is not None]
    lo = [by_state[s]["score100"] for s in _LO_STRESS_REF
          if s in by_state and by_state[s].get("score100") is not None]
    if len(hi) < 2 or len(lo) < 2:
        return None
    return (sum(hi) / len(hi)) > (sum(lo) / len(lo)) + 10.0


def _extract_rows(payload: str) -> list[dict]:
    """Parse the configured source into [{state, score100, raw, category}].
    Accepts GeoJSON FeatureCollection, JSON array, or CSV. Only US states are
    kept. Returns [] on anything unparseable — never guesses."""
    rows: list[dict] = []
    data = None
    try:
        data = json.loads(payload)
    except Exception:
        data = None

    def _consume(props: dict):
        st = _norm_state(props.get("name_1") or props.get("state") or props.get("NAME")
                         or props.get("st_abbr") or props.get("iso_3166_2")
                         or props.get("gid_1") or props.get("region"))
        if not st:
            return
        cat = props.get("bws_cat")
        raw = (props.get("bws_raw") if props.get("bws_raw") is not None
               else props.get("bws_score") if props.get("bws_score") is not None
               else props.get("score"))
        label = (props.get("bws_label") or props.get("bws_cat_label")
                 or props.get("category") or props.get("label"))
        # Prefer the WRI category bucket; fall back to the raw ratio.
        s100 = _cat_to_100(cat)
        if s100 is None:
            s100 = _ratio_to_100(raw)
        if s100 is None and label is None:
            return
        rows.append({"state": st, "score100": s100, "raw": _num(raw),
                     "category": (str(label)[:64] if label is not None else None)})

    if isinstance(data, dict) and (data.get("type") == "FeatureCollection"):
        for feat in (data.get("features") or []):
            if isinstance(feat, dict):
                _consume(feat.get("properties") or {})
    elif isinstance(data, list):
        for r in data:
            if isinstance(r, dict):
                _consume(r)
    else:
        # CSV fallback
        try:
            rdr = csv.DictReader(io.StringIO(payload))
            for r in rdr:
                _consume(r)
        except Exception:
            return []
    # de-dup by state, keep the strongest (max) score — subnational files may
    # carry multiple basins per state; the most-stressed is the siting-relevant one.
    best: dict[str, dict] = {}
    for r in rows:
        cur = best.get(r["state"])
        if cur is None or ((r["score100"] or -1) > (cur["score100"] or -1)):
            best[r["state"]] = r
    return list(best.values())


def run_ingest(dry_run: bool = True) -> dict:
    url = _source_url()
    if not url:
        return {"ok": True, "skipped": "WRI_AQUEDUCT_URL not set — honest no-op; "
                "water objectives stay 'unavailable' until a verified source is configured.",
                "wrote": 0, "source_tag": _SOURCE_TAG}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "dchub-water-aqueduct/1.0"})
        with urllib.request.urlopen(req, timeout=45) as r:
            payload = r.read(40_000_000).decode("utf-8", "replace")
    except Exception as e:
        return {"ok": False, "error": f"fetch_failed: {type(e).__name__}: {str(e)[:160]}",
                "wrote": 0, "source_url": url}
    rows = _extract_rows(payload)
    if not rows:
        return {"ok": False, "error": "parse_yielded_0_us_states — source shape unrecognised; "
                "nothing written (integrity: never fabricate)", "wrote": 0, "source_url": url}
    preview = sorted(rows, key=lambda x: (x["score100"] or -1), reverse=True)[:8]
    by_state = {r["state"]: r for r in rows}
    sane = _direction_sane(by_state)   # True / False / None(uncheckable)
    ref = {s: by_state[s]["score100"] for s in (_HI_STRESS_REF + _LO_STRESS_REF)
           if s in by_state}
    if dry_run:
        return {"ok": True, "dry_run": True, "parsed_states": len(rows),
                "preview_most_stressed": preview, "direction_sane": sane,
                "direction_refs": ref,
                "note": ("dry run — 0 rows written. direction_sane must be true "
                         "(arid AZ/NV/NM > wet IL/OH/MI by >10pt) before ?dry=0 will write."),
                "wrote": 0}
    # ★ INTEGRITY GATE: never re-ship the 2026-07-07 inversion. A False check
    # (high-stress states NOT clearly above low-stress) HARD-REFUSES the write.
    if sane is False:
        return {"ok": False, "error": "direction_check_failed — arid states did NOT out-score wet "
                "states; refusing to write (this is exactly the inverted proxy that paused water). "
                "Fix the source/mapping and retry.", "direction_refs": ref, "wrote": 0}
    c = _conn()
    if c is None:
        return {"ok": False, "error": "no_db", "wrote": 0}
    wrote = 0
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            # replace any prior WRI-sourced rows atomically (autocommit; small set)
            cur.execute("DELETE FROM water_risk WHERE LOWER(COALESCE(source,'')) LIKE 'wri%%'")
            for r in rows:
                cur.execute(
                    "INSERT INTO water_risk (state, water_stress_score, baseline_water_stress, "
                    "bws_category, source, computed_at) VALUES (%s,%s,%s,%s,%s, now())",
                    (r["state"], r["score100"], r["raw"], r["category"], _SOURCE_TAG))
                wrote += 1
    except Exception as e:
        return {"ok": False, "error": f"write_failed: {type(e).__name__}: {str(e)[:160]}",
                "wrote": wrote}
    finally:
        try: c.close()
        except Exception: pass
    return {"ok": True, "wrote": wrote, "parsed_states": len(rows),
            "preview_most_stressed": preview, "source_tag": _SOURCE_TAG,
            "note": "rank_sites water objectives now auto-enable (data-gated)."}


@water_aqueduct_ingest_bp.route("/api/v1/admin/water/aqueduct-ingest/status", methods=["GET"])
def water_ingest_status():
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    c = _conn()
    wri_rows = total = None
    if c is not None:
        try:
            with c.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM water_risk WHERE LOWER(COALESCE(source,'')) LIKE 'wri%%'")
                wri_rows = int((cur.fetchone() or [0])[0])
                cur.execute("SELECT COUNT(*) FROM water_risk")
                total = int((cur.fetchone() or [0])[0])
        except Exception as e:
            logger.debug("[water-ingest] status probe: %s", e)
        finally:
            try: c.close()
            except Exception: pass
    return jsonify(ok=True, source_url_set=bool(_source_url()), wri_rows=wri_rows,
                   total_rows=total, source_tag=_SOURCE_TAG,
                   water_objective_enabled=bool(wri_rows))


@water_aqueduct_ingest_bp.route("/api/v1/admin/water/aqueduct-ingest", methods=["POST"])
def water_ingest_run():
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    dry = (request.args.get("dry") or "1") != "0"  # default dry — write only on explicit ?dry=0
    return jsonify(run_ingest(dry_run=dry))
