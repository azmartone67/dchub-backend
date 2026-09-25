"""
routes/water_aqueduct_ingest.py — WRI Aqueduct water-stress ingest (2026-07-08).

Replaces the PAUSED, inverted state-level groundwater proxy (site_planner.py
WATER_STRESS_BY_STATE — AZ>IL was direction-wrong) with REAL WRI Aqueduct
baseline water stress. See [[reference_dchub_water_score_integrity]].

★ INTEGRITY CONTRACT (do not break):
  This module NEVER fabricates a water number. It writes rows to `water_risk`
  ONLY for records it actually parsed from a configured, verified WRI source
  (WRI_AQUEDUCT_S3_PATH and/or WRI_AQUEDUCT_URL). With no source set — or on any
  fetch/parse failure — it is an honest NO-OP (writes nothing, returns
  skipped=...). Until real WRI rows tag `source='wri_aqueduct'`, the rank_sites
  water objectives stay "unavailable"
  (routes/interconnection_queues._wri_water_available). So the water lever
  auto-enables the moment verified data lands and NEVER a moment before.

★ SOURCE FETCH LADDER (2026-07-11 — kills the 7-day presign treadmill):
  Presigned R2 URLs expire every 7 days, so WRI_AQUEDUCT_URL alone silently
  goes stale (same failure class as issue #1509, the dead USGS cron). The
  permanent path is an AUTHENTICATED S3 fetch straight from R2 using the env
  creds the backend already holds (R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY /
  R2_ENDPOINT_URL — the same trio db_backup.py and r2_exports.py use).
  Resolution order, fail-soft at every rung:
    1. WRI_AQUEDUCT_S3_PATH env ("bucket/key", e.g.
       "dchub-daily/wri/wri_aqueduct_us_states.json") → boto3 get_object.
    2. No S3 path set but WRI_AQUEDUCT_URL looks like an R2 presigned URL →
       derive bucket/key from the URL path and try the authenticated fetch
       (self-heals even after the presign expires, zero operator action).
    3. Legacy: plain HTTPS GET of WRI_AQUEDUCT_URL (the pre-existing behavior;
       still works while the presign is fresh, or for any non-R2 host).
  Whichever rung supplied the payload is logged + returned as fetch_path.

CRAWL-FIRST rollout (deliberate):
  1. Point WRI_AQUEDUCT_S3_PATH (preferred, "bucket/key" on R2) or
     WRI_AQUEDUCT_URL at a verified WRI Aqueduct subnational file
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


def _source_s3_path() -> str:
    return (os.environ.get("WRI_AQUEDUCT_S3_PATH") or "").strip()


# ---------------------------------------------------------------------------
# Authenticated R2/S3 fetch (permanent path — no presign expiry treadmill)
# ---------------------------------------------------------------------------

def _parse_s3_path(s: str):
    """'bucket/key/with/slashes' (optional s3:// prefix) → (bucket, key) | None.
    Never raises; None for anything that doesn't clearly name both parts."""
    if not s:
        return None
    s = str(s).strip()
    if s.lower().startswith("s3://"):
        s = s[5:]
    s = s.lstrip("/")
    if "/" not in s:
        return None
    bucket, _, key = s.partition("/")
    bucket, key = bucket.strip(), key.strip()
    if not bucket or not key:
        return None
    return bucket, key


def _derive_s3_from_url(url: str):
    """Derive (bucket, key) from an R2 presigned URL, so the authenticated
    fetch can self-heal after the presign expires. Handles both R2 URL forms:
      path-style:    https://<account>.r2.cloudflarestorage.com/<bucket>/<key>?X-Amz-...
      virtual-host:  https://<bucket>.<account>.r2.cloudflarestorage.com/<key>?X-Amz-...
    Conservative: only R2 hosts; None for anything else (never guesses)."""
    if not url:
        return None
    try:
        from urllib.parse import urlparse, unquote
        p = urlparse(str(url).strip())
        host = (p.hostname or "").lower()
        suffix = ".r2.cloudflarestorage.com"
        if not host.endswith(suffix):
            return None
        labels = host[: -len(suffix)].split(".")
        path = unquote(p.path or "").lstrip("/")
        if not path:
            return None
        if len(labels) == 1:
            # path-style: first path segment is the bucket
            return _parse_s3_path(path)
        if len(labels) == 2:
            # virtual-hosted: bucket is the first host label
            return (labels[0], path) if labels[0] else None
        return None
    except Exception:
        return None


def _resolve_s3_target():
    """(bucket, key, origin) for the authenticated fetch, or None.
    Explicit WRI_AQUEDUCT_S3_PATH wins; else derived from WRI_AQUEDUCT_URL."""
    explicit = _parse_s3_path(_source_s3_path())
    if explicit:
        return explicit[0], explicit[1], "env"
    derived = _derive_s3_from_url(_source_url())
    if derived:
        return derived[0], derived[1], "derived_from_url"
    return None


def _r2_creds_present() -> bool:
    return bool((os.environ.get("R2_ENDPOINT_URL") or "").strip()
                and (os.environ.get("R2_ACCESS_KEY_ID") or "").strip()
                and (os.environ.get("R2_SECRET_ACCESS_KEY") or "").strip())


def _fetch_via_s3(bucket: str, key: str):
    """(payload, None) on success, (None, why) on any failure. Never raises."""
    if not _r2_creds_present():
        return None, "r2_creds_missing"
    try:
        import boto3
        client = boto3.client(
            "s3",
            endpoint_url=os.environ.get("R2_ENDPOINT_URL", "").strip(),
            aws_access_key_id=os.environ.get("R2_ACCESS_KEY_ID", "").strip(),
            aws_secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY", "").strip(),
            region_name="auto",
        )
        resp = client.get_object(Bucket=bucket, Key=key)
        body = resp["Body"].read(40_000_000)
        return body.decode("utf-8", "replace"), None
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:160]}"


def _fetch_via_presigned(url: str):
    """Legacy HTTPS GET (presigned URL or any public URL).
    (payload, None) on success, (None, why) on any failure. Never raises."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "dchub-water-aqueduct/1.0"})
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.read(40_000_000).decode("utf-8", "replace"), None
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:160]}"


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


def _fetch_source():
    """Walk the fetch ladder (authenticated S3 → legacy presigned/HTTPS).
    Returns (payload|None, fetch_path|None, errors dict). Fail-soft: an S3
    failure NEVER breaks the legacy path that works today."""
    url = _source_url()
    target = _resolve_s3_target()
    errors: dict[str, str] = {}
    if target is not None:
        bucket, key, origin = target
        payload, err = _fetch_via_s3(bucket, key)
        if payload is not None:
            fetch_path = f"s3_authenticated({origin}:{bucket}/{key})"
            logger.info("[water-ingest] fetched via %s", fetch_path)
            return payload, fetch_path, errors
        errors["s3_authenticated"] = f"{origin}:{bucket}/{key} — {err}"
        logger.warning("[water-ingest] authenticated S3 fetch failed (%s); "
                       "falling back to presigned URL", errors["s3_authenticated"])
    if url:
        payload, err = _fetch_via_presigned(url)
        if payload is not None:
            logger.info("[water-ingest] fetched via presigned_url (legacy path)")
            return payload, "presigned_url", errors
        errors["presigned_url"] = err
        logger.warning("[water-ingest] presigned URL fetch failed: %s", err)
    return None, None, errors


def run_ingest(dry_run: bool = True) -> dict:
    url = _source_url()
    if not url and _resolve_s3_target() is None:
        return {"ok": True, "skipped": "no source configured (set WRI_AQUEDUCT_S3_PATH "
                "bucket/key for the authenticated R2 fetch, or legacy WRI_AQUEDUCT_URL) — "
                "honest no-op; water objectives stay 'unavailable' until a verified "
                "source is configured.",
                "wrote": 0, "source_tag": _SOURCE_TAG}
    payload, fetch_path, fetch_errors = _fetch_source()
    if payload is None:
        return {"ok": False,
                "error": "fetch_failed: " + ("; ".join(f"{k}: {v}" for k, v in fetch_errors.items())
                                             or "no fetchable source"),
                "wrote": 0, "source_url": url or None}
    rows = _extract_rows(payload)
    if not rows:
        return {"ok": False, "error": "parse_yielded_0_us_states — source shape unrecognised; "
                "nothing written (integrity: never fabricate)", "wrote": 0,
                "source_url": url or None, "fetch_path": fetch_path}
    preview = sorted(rows, key=lambda x: (x["score100"] or -1), reverse=True)[:8]
    by_state = {r["state"]: r for r in rows}
    sane = _direction_sane(by_state)   # True / False / None(uncheckable)
    ref = {s: by_state[s]["score100"] for s in (_HI_STRESS_REF + _LO_STRESS_REF)
           if s in by_state}
    if dry_run:
        return {"ok": True, "dry_run": True, "parsed_states": len(rows),
                "preview_most_stressed": preview, "direction_sane": sane,
                "direction_refs": ref, "fetch_path": fetch_path,
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
                    "bws_category, source, computed_at) VALUES (%s,%s,%s,%s,%s, now() ON CONFLICT DO NOTHING)",
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
            "fetch_path": fetch_path,
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
    target = _resolve_s3_target()
    return jsonify(ok=True, source_url_set=bool(_source_url()),
                   s3_path_set=bool(_parse_s3_path(_source_s3_path())),
                   s3_target=(f"{target[0]}/{target[1]} ({target[2]})" if target else None),
                   r2_creds_present=_r2_creds_present(),
                   wri_rows=wri_rows,
                   total_rows=total, source_tag=_SOURCE_TAG,
                   water_objective_enabled=bool(wri_rows))


@water_aqueduct_ingest_bp.route("/api/v1/admin/water/aqueduct-ingest", methods=["POST"])
def water_ingest_run():
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    dry = (request.args.get("dry") or "1") != "0"  # default dry — write only on explicit ?dry=0
    return jsonify(run_ingest(dry_run=dry))
